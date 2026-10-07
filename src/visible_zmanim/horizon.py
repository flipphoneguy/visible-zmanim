from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from pyproj import Geod

from .config import DEFAULT_HORIZON, DEFAULT_PHYSICS, HorizonSettings, Physics, data_dir
from .dem.terrain import Terrain
from .refraction import sea_horizon_threshold_deg, threshold_deg

WGS84_A = 6_378_137.0
WGS84_E2 = 6.69437999014e-3
OBLIQUITY = 23.44
ARC_MARGIN_DEG = 25.0
RAY_BATCH = 200
PROFILE_THREADS = min(4, os.cpu_count() or 1)
PROFILE_VERSION = 1
_GEOD = Geod(ellps="WGS84")


def radius_along(lat, az):
    """Earth's radius of curvature at lat in the direction az (Euler)."""
    s2 = math.sin(math.radians(lat)) ** 2
    m = WGS84_A * (1 - WGS84_E2) / (1 - WGS84_E2 * s2) ** 1.5
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * s2)
    a = np.radians(az)
    return 1.0 / (np.cos(a) ** 2 / m + np.sin(a) ** 2 / n)


def sun_azimuth_mask(lat, az):
    """True for azimuths the rising or setting sun can be in during the year, with a margin for high horizons."""
    c = math.cos(math.radians(lat))
    x = math.sin(math.radians(OBLIQUITY)) / c if c > 1e-6 else 2.0
    if x >= 1 or abs(lat) >= 60:
        return np.ones_like(az, dtype=bool)
    lo = math.degrees(math.acos(x)) - ARC_MARGIN_DEG
    hi = math.degrees(math.acos(-x)) + ARC_MARGIN_DEG
    return ((az >= lo) & (az <= hi)) | ((az >= 360 - hi) & (az <= 360 - lo))


def sample_distances(s: HorizonSettings):
    d = [max(s.min_distance_m, 1.0)]
    while d[-1] < s.max_distance_m:
        d.append(d[-1] + max(s.min_step_m, d[-1] * s.relative_step))
    return np.array(d[:-1] + [s.max_distance_m])


@dataclass
class Profile:
    lat: float
    lon: float
    ground_m: float
    eye_height_m: float
    step: float
    threshold: np.ndarray  # per azimuth bin, deg; -90 where not computed
    block_d: np.ndarray
    block_h: np.ndarray
    block_e: np.ndarray
    block_lat: np.ndarray
    block_lon: np.ndarray
    computed: np.ndarray
    block_src: np.ndarray
    ground_source: str = ""

    @property
    def eye_m(self):
        return self.ground_m + self.eye_height_m

    def bin(self, az):
        return np.rint(np.asarray(az) / self.step).astype(int) % len(self.threshold)

    def margin(self, alt, az, sd):
        """How far (deg) the highest visible part of the sun's disk is above the horizon; positive means some of it is visible."""
        alt, az, sd = (np.asarray(a, dtype=float) for a in (alt, az, sd))
        n = len(self.threshold)
        reach = int(math.ceil(float(np.max(sd)) / self.step / 0.5)) + 1
        offsets = np.arange(-reach, reach + 1)
        bins = (np.rint(az / self.step).astype(int)[..., None] + offsets) % n
        dx = (bins * self.step - az[..., None] + 180.0) % 360.0 - 180.0
        x = dx * np.cos(np.radians(alt))[..., None]
        inside = np.abs(x) < sd[..., None]
        limb = alt[..., None] + np.sqrt(np.maximum(sd[..., None] ** 2 - x**2, 0.0))
        m = np.where(inside, limb - self.threshold[bins], -np.inf)
        return m.max(axis=-1)

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, **{k: v for k, v in asdict(self).items()})
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Profile":
        with np.load(path) as z:
            kw = {k: z[k] for k in z.files}
        for k in ("lat", "lon", "ground_m", "eye_height_m", "step"):
            kw[k] = float(kw[k])
        kw["ground_source"] = str(kw["ground_source"])
        return cls(**kw)


def compute_profile(lat, lon, eye_height_m, *, terrain: Terrain, physics: Physics = DEFAULT_PHYSICS, settings: HorizonSettings = DEFAULT_HORIZON, ground_m: float | None = None) -> Profile:
    ground_source = "given"
    if ground_m is None:
        ground_m, ground_source = terrain.ground(lat, lon)
    eye = ground_m + eye_height_m
    n_bins = int(round(360.0 / settings.azimuth_step_deg))
    all_az = np.arange(n_bins) * settings.azimuth_step_deg
    computed = sun_azimuth_mask(lat, all_az)
    ray_az = all_az[computed]

    dist = sample_distances(settings)
    node_d = np.arange(0.0, settings.max_distance_m + settings.geodesic_node_spacing_m, settings.geodesic_node_spacing_m)
    seg = np.minimum((dist // settings.geodesic_node_spacing_m).astype(int), len(node_d) - 2)
    w = (dist - node_d[seg]) / settings.geodesic_node_spacing_m

    thr = np.full(n_bins, -90.0)
    out = {k: np.full(n_bins, np.nan) for k in ("d", "h", "e", "lat", "lon")}
    block_src = np.zeros(n_bins, dtype=np.int8)
    bins_idx = np.flatnonzero(computed)
    k = physics.terrestrial_k
    sea_floor = sea_horizon_threshold_deg(eye, physics)

    def batch(start):
        az = ray_az[start:start + RAY_BATCH]
        n = len(az)
        lon_n, lat_n, _ = _GEOD.fwd(np.full((n, len(node_d)), lon), np.full((n, len(node_d)), lat), np.repeat(az[:, None], len(node_d), 1), np.broadcast_to(node_d, (n, len(node_d))))
        lon_n = np.degrees(np.unwrap(np.radians(lon_n), axis=1))
        s_lat = lat_n[:, seg] * (1 - w) + lat_n[:, seg + 1] * w
        s_lon = lon_n[:, seg] * (1 - w) + lon_n[:, seg + 1] * w
        s_lon = (s_lon + 180.0) % 360.0 - 180.0
        d2 = np.broadcast_to(dist, s_lat.shape)
        az2 = np.broadcast_to(az[:, None], s_lat.shape)
        h, src = terrain.heights(lat, lon, s_lat.ravel(), s_lon.ravel(), d2.ravel(), az2.ravel(), relative_step=settings.relative_step, min_res_m=1.0, with_source=True)
        h, src = h.reshape(s_lat.shape), src.reshape(s_lat.shape)
        r = radius_along(lat, az)[:, None]
        drop = d2**2 * (1 - k) / (2 * r)
        e = np.degrees(np.arctan2(h - eye - drop, d2))
        gamma = np.degrees(d2 / r)
        t = threshold_deg(e, gamma, h, physics, r)
        j = t.argmax(axis=1)
        rows = np.arange(n)
        b = bins_idx[start:start + n]
        best = t[rows, j]
        bd, bh, be, bla, blo, bs = dist[j], h[rows, j], e[rows, j], s_lat[rows, j], s_lon[rows, j], src[rows, j]
        # Nothing beyond the sampled range is assumed lower than sea level, so the sea horizon is a floor.
        sea = best < sea_floor
        if sea.any():
            r_sea = radius_along(lat, az[sea])
            d_sea = np.sqrt(2 * max(eye, 0.0) * r_sea / (1 - k))
            best[sea], bd[sea], bh[sea], bs[sea] = sea_floor, d_sea, 0.0, 0
            be[sea] = -np.degrees(np.sqrt(2 * max(eye, 0.0) * (1 - k) / r_sea))
            blo[sea], bla[sea], _ = _GEOD.fwd(np.full(sea.sum(), lon), np.full(sea.sum(), lat), az[sea], d_sea)
        thr[b] = best
        out["d"][b], out["h"][b], out["e"][b], out["lat"][b], out["lon"][b] = bd, bh, be, bla, blo
        block_src[b] = bs

    with ThreadPoolExecutor(PROFILE_THREADS) as pool:
        list(pool.map(batch, range(0, len(ray_az), RAY_BATCH)))

    return Profile(lat=lat, lon=lon, ground_m=ground_m, eye_height_m=eye_height_m, step=settings.azimuth_step_deg, threshold=thr, block_d=out["d"], block_h=out["h"], block_e=out["e"], block_lat=out["lat"], block_lon=out["lon"], computed=computed, block_src=block_src, ground_source=ground_source)


def profile_key(lat, lon, eye_height_m, ground_m, physics: Physics, settings: HorizonSettings, terrain: Terrain) -> str:
    key = {"v": PROFILE_VERSION, "lat": round(lat, 6), "lon": round(lon, 6), "eye": round(eye_height_m, 2), "ground": None if ground_m is None else round(ground_m, 2), "physics": asdict(physics), "settings": asdict(settings), "usgs": terrain.use_usgs}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()


def get_profile(lat, lon, eye_height_m, *, terrain: Terrain, physics: Physics = DEFAULT_PHYSICS, settings: HorizonSettings = DEFAULT_HORIZON, ground_m: float | None = None, cache_dir: Path | None = None) -> Profile:
    folder = Path(cache_dir or data_dir() / "dem" / "cache" / "horizon")
    path = folder / f"{profile_key(lat, lon, eye_height_m, ground_m, physics, settings, terrain)}.npz"
    if path.exists():
        return Profile.load(path)
    p = compute_profile(lat, lon, eye_height_m, terrain=terrain, physics=physics, settings=settings, ground_m=ground_m)
    p.save(path)
    return p


_running: dict[str, threading.Event] = {}
_running_lock = threading.Lock()
ERROR_TTL_S = 600


class ProfileError(Exception):
    pass


def get_profile_async(lat, lon, eye_height_m, *, terrain: Terrain, physics: Physics = DEFAULT_PHYSICS, settings: HorizonSettings = DEFAULT_HORIZON, ground_m: float | None = None, wait_s: float = 20.0, cache_dir: Path | None = None) -> Profile | None:
    """Like get_profile, but gives up after wait_s and returns None while the computation continues in the background. Only one process computes a given profile at a time."""
    folder = Path(cache_dir or data_dir() / "dem" / "cache" / "horizon")
    folder.mkdir(parents=True, exist_ok=True)
    key = profile_key(lat, lon, eye_height_m, ground_m, physics, settings, terrain)
    path, err = folder / f"{key}.npz", folder / f"{key}.error"
    if path.exists():
        return Profile.load(path)
    if err.exists():
        if time.time() - err.stat().st_mtime < ERROR_TTL_S:
            raise ProfileError(err.read_text())
        err.unlink(missing_ok=True)
    with _running_lock:
        event = _running.get(key)
        if event is None:
            lock = open(folder / f"{key}.lock", "w")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                return None
            if path.exists():
                lock.close()
                return Profile.load(path)
            event = _running[key] = threading.Event()

            def run():
                try:
                    compute_profile(lat, lon, eye_height_m, terrain=terrain, physics=physics, settings=settings, ground_m=ground_m).save(path)
                except Exception as e:
                    err.write_text(f"{type(e).__name__}: {e}")
                finally:
                    lock.close()
                    with _running_lock:
                        _running.pop(key, None)
                    event.set()

            threading.Thread(target=run, daemon=True).start()
    event.wait(wait_s)
    if path.exists():
        return Profile.load(path)
    if err.exists():
        raise ProfileError(err.read_text())
    return None
