from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from skyfield.api import Loader, wgs84

from .config import Physics, data_dir

DAY = 86400.0
NODE_STEP = 3600.0


class Sun:
    _lock = threading.Lock()
    _shared: "Sun | None" = None

    def __init__(self, root: Path | None = None):
        folder = Path(root or data_dir()) / "ephemeris"
        folder.mkdir(parents=True, exist_ok=True)
        loader = Loader(str(folder), verbose=False)
        self.ts = loader.timescale(builtin=True)
        eph = loader("de440s.bsp")
        self._earth = eph["earth"]
        self._sun = eph["sun"]

    @classmethod
    def shared(cls) -> "Sun":
        with cls._lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    def utc(self, unix):
        u = np.atleast_1d(np.asarray(unix, dtype=float))
        whole = np.floor(u / DAY)
        return self.ts.utc(1970, 1, 1 + whole, 0, 0, u - whole * DAY)

    def track(self, lat: float, lon: float, height_m: float, start_unix: float, end_unix: float) -> "Track":
        return Track(self, lat, lon, height_m, start_unix, end_unix)


class Track:
    """Topocentric sun for one observer over a time span: exact Skyfield nodes every hour, cubic interpolation between."""

    def __init__(self, sun: Sun, lat, lon, height_m, start_unix, end_unix):
        self.lat = lat
        t0 = np.floor(start_unix / NODE_STEP) * NODE_STEP - 2 * NODE_STEP
        t1 = np.ceil(end_unix / NODE_STEP) * NODE_STEP + 2 * NODE_STEP
        self.nodes = np.arange(t0, t1 + NODE_STEP / 2, NODE_STEP)
        observer = sun._earth + wgs84.latlon(lat, lon, elevation_m=height_m)
        app = observer.at(sun.utc(self.nodes)).observe(sun._sun).apparent()
        ha, dec, dist = app.hadec()
        self._ha = np.unwrap(np.radians(ha.hours * 15.0))
        self._dec = dec.radians
        self._dist = dist.au
        self._sin_lat = np.sin(np.radians(lat))
        self._cos_lat = np.cos(np.radians(lat))

    def _interp(self, values, unix):
        x = (np.asarray(unix, dtype=float) - self.nodes[0]) / NODE_STEP
        i = np.clip(np.floor(x).astype(int), 1, len(self.nodes) - 3)
        f = x - i
        p0, p1, p2, p3 = values[i - 1], values[i], values[i + 1], values[i + 2]
        # Catmull-Rom
        return p1 + 0.5 * f * (p2 - p0 + f * (2 * p0 - 5 * p1 + 4 * p2 - p3 + f * (3 * (p1 - p2) + p3 - p0)))

    def hour_angle_deg(self, unix):
        return (np.degrees(self._interp(self._ha, unix)) + 180.0) % 360.0 - 180.0

    def altaz(self, unix):
        """True (unrefracted) altitude and azimuth in degrees and distance in AU."""
        h = self._interp(self._ha, unix)
        d = self._interp(self._dec, unix)
        sin_d, cos_d, cos_h = np.sin(d), np.cos(d), np.cos(h)
        alt = np.arcsin(np.clip(self._sin_lat * sin_d + self._cos_lat * cos_d * cos_h, -1, 1))
        az = np.arctan2(-cos_d * np.sin(h), self._cos_lat * sin_d - self._sin_lat * cos_d * cos_h)
        return np.degrees(alt), np.degrees(az) % 360.0, self._interp(self._dist, unix)

    def semidiameter(self, distance_au, physics: Physics):
        return physics.solar_semidiameter_1au_arcsec / 3600.0 / np.asarray(distance_au)

    def transits(self, approx_unix):
        t = np.atleast_1d(np.asarray(approx_unix, dtype=float))
        for _ in range(4):
            t = t - self.hour_angle_deg(t) / 15.0 * 3600.0
        return t


def find_crossings(margin, starts, ends, step, *, rising: bool, iterations: int = 14):
    """First rising (or last setting) zero crossing of margin(t) in each [start, end] window, NaN where none. Positive margin means visible."""
    starts = np.asarray(starts, dtype=float)
    ends = np.asarray(ends, dtype=float)
    n_steps = int(np.ceil(np.max(ends - starts) / step)) + 1
    grid = np.minimum(starts[:, None] + np.arange(n_steps)[None, :] * step, ends[:, None])
    visible = margin(grid.ravel()).reshape(grid.shape) > 0
    if rising:
        cross = ~visible[:, :-1] & visible[:, 1:]
        idx = cross.argmax(axis=1)
    else:
        cross = visible[:, :-1] & ~visible[:, 1:]
        idx = cross.shape[1] - 1 - cross[:, ::-1].argmax(axis=1)
    found = cross.any(axis=1)
    out = np.full(len(starts), np.nan)
    if not found.any():
        return out
    sel = np.flatnonzero(found)
    a, b = grid[sel, idx[sel]], grid[sel, idx[sel] + 1]
    for _ in range(iterations):
        mid = (a + b) / 2
        vis = margin(mid) > 0
        if rising:
            a, b = np.where(vis, a, mid), np.where(vis, mid, b)
        else:
            a, b = np.where(vis, mid, a), np.where(vis, b, mid)
    out[sel] = (a + b) / 2
    return out
