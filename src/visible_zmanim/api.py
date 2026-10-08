"""Framework-independent request handling: ``handle(params)`` returns ``(http_status, json_body)``."""
from __future__ import annotations

import datetime as dt
import math
import threading
from importlib.metadata import PackageNotFoundError, version
from zoneinfo import ZoneInfo

import numpy as np
import requests
from rasterio.errors import RasterioIOError

from .config import DEFAULT_HORIZON, DEFAULT_PHYSICS
from .dem.cog import MissingSource
from .dem.terrain import SOURCES, Terrain, TerrainUnavailable
from .geocode import AddressNotFound, GeocoderUnavailable, geocode
from .horizon import ProfileError, get_profile_async
from .sun import Sun
from .zmanim import compute

MAX_DAYS = 366
DEFAULT_EYE_HEIGHT_M = 1.7
DEFAULT_WAIT_S = 15.0
MAX_WAIT_S = 20.0
VIEW_HALF_WIDTH_DEG = 12.0
VIEW_STEP_DEG = 0.1
VIEW_PATH_S = 40 * 60
VIEW_PATH_STEP_S = 30
VARIANTS = ("sea_level", "elevation", "visible")
NUMERIC_KEYS = {"shaah_zmanis_gra_s", "shaah_zmanis_mga_72_s", "sunrise_azimuth", "sunset_azimuth"}
ATTRIBUTION = {
    "sun": "Skyfield with NASA JPL DE440s ephemeris",
    "terrain": {
        "usgs_3dep_1m": "USGS 3D Elevation Program 1 m lidar DEM (public domain)",
        "usgs_3dep_13": "USGS 3D Elevation Program 1/3 arc-second DEM (public domain)",
        "gedtm30": "GEDTM30 v1.2, OpenGeoHub, CC BY 4.0 (doi:10.5281/zenodo.14900181)",
    },
    "geocoding": "Nominatim, data (c) OpenStreetMap contributors, ODbL; US Census Geocoder",
}

try:
    VERSION = version("visible-zmanim")
except PackageNotFoundError:
    VERSION = "unknown"

_lock = threading.Lock()
_terrain: Terrain | None = None
_tzf = None


class BadRequest(Exception):
    pass


def _shared_terrain() -> Terrain:
    global _terrain
    with _lock:
        if _terrain is None:
            _terrain = Terrain()
        return _terrain


def _timezone_at(lat, lon) -> str:
    global _tzf
    with _lock:
        if _tzf is None:
            from timezonefinder import TimezoneFinder
            _tzf = TimezoneFinder()
    return _tzf.timezone_at(lng=lon, lat=lat) or "UTC"


def _float(params, name, default=None, lo=-math.inf, hi=math.inf):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number")
    if not (lo <= v <= hi):
        raise BadRequest(f"{name} must be between {lo} and {hi}")
    return v


def _list(params, name):
    raw = params.get(name)
    if raw in (None, "", []):
        return None
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def _text(params, name):
    raw = params.get(name)
    if raw is not None and not isinstance(raw, str):
        raise BadRequest(f"{name} must be a string")
    return raw


def _dates(params, tz):
    for name in ("date", "start", "end"):
        _text(params, name)
    try:
        if params.get("start") or params.get("end"):
            start = dt.date.fromisoformat(params.get("start") or params.get("end"))
            end = dt.date.fromisoformat(params.get("end") or params.get("start"))
        else:
            start = end = dt.date.fromisoformat(params["date"]) if params.get("date") else dt.datetime.now(tz).date()
    except ValueError:
        raise BadRequest("dates must be YYYY-MM-DD")
    if end < start:
        raise BadRequest("end is before start")
    n = (end - start).days + 1
    if n > MAX_DAYS:
        raise BadRequest(f"at most {MAX_DAYS} days per request")
    return [start + dt.timedelta(days=i) for i in range(n)]


def _iso(t, tz):
    if t is None or not np.isfinite(t):
        return None
    return dt.datetime.fromtimestamp(round(float(t)), tz).isoformat()


def _num(v, digits=1):
    return None if v is None or not np.isfinite(v) else round(float(v), digits)


def _blocking(profile, az):
    if profile is None or az is None or not np.isfinite(az):
        return None
    b = profile.bin(az)
    if not profile.computed[b]:
        return None
    return {
        "lat": _num(profile.block_lat[b], 6),
        "lon": _num(profile.block_lon[b], 6),
        "distance_m": _num(profile.block_d[b], 0),
        "height_m": _num(profile.block_h[b], 1),
        "angle_deg": _num(profile.block_e[b], 3),
        "source": SOURCES[int(profile.block_src[b])],
    }


def handle(params) -> tuple[int, dict]:
    try:
        return _handle(params)
    except BadRequest as e:
        return 400, {"error": str(e)}
    except AddressNotFound as e:
        return 404, {"error": str(e)}
    except GeocoderUnavailable as e:
        return 503, {"error": str(e)}
    except (TerrainUnavailable, RasterioIOError, requests.RequestException) as e:
        return 503, {"error": f"terrain data is unavailable right now, try again shortly ({e})"}
    except ProfileError as e:
        if str(e).startswith("TerrainUnavailable"):
            return 503, {"error": f"terrain data is unavailable right now, try again shortly ({e})"}
        return 500, {"error": f"horizon computation failed: {e}"}


def _handle(params) -> tuple[int, dict]:
    location = {}
    lat, lon = _float(params, "lat", lo=-90, hi=90), _float(params, "lon", lo=-180, hi=180)
    if lat is None or lon is None:
        address = _text(params, "address")
        if not address:
            raise BadRequest("give lat and lon, or address")
        g = geocode(address)
        lat, lon = g["lat"], g["lon"]
        location["address"] = {"query": address, "matched": g["matched"], "source": g["source"], "precision": g.get("precision")}

    tz_name = _text(params, "tz") or _timezone_at(lat, lon)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        raise BadRequest(f"unknown time zone {tz_name!r}")
    dates = _dates(params, tz)

    variants = _list(params, "variants") or list(VARIANTS)
    unknown = set(variants) - set(VARIANTS)
    if unknown:
        raise BadRequest(f"unknown variants: {', '.join(sorted(unknown))}")
    fields = _list(params, "fields")
    include = _list(params, "include") or []

    height = _float(params, "height", DEFAULT_EYE_HEIGHT_M, lo=0, hi=1000)
    ground_given = _float(params, "ground", lo=-500, hi=9000)
    min_distance = _float(params, "min_distance", DEFAULT_HORIZON.min_distance_m, lo=0, hi=50_000)
    physics = DEFAULT_PHYSICS
    refraction = _float(params, "refraction", lo=0, hi=120)
    if refraction is not None:
        physics = physics.with_(horizon_refraction_arcmin=refraction)
    k = _float(params, "k", lo=-1, hi=0.9)
    if k is not None:
        physics = physics.with_(terrestrial_k=k)
    settings = DEFAULT_HORIZON.with_(min_distance_m=min_distance)
    wait = _float(params, "wait", DEFAULT_WAIT_S, lo=0, hi=MAX_WAIT_S)

    terrain = _shared_terrain()
    ground, ground_source = ground_given, "given" if ground_given is not None else None
    if ground_given is None and ("elevation" in variants or "visible" in variants):
        try:
            ground, ground_source = terrain.ground(lat, lon)
        except MissingSource as e:
            raise TerrainUnavailable(str(e)) from e
    # Sea-level times alone only use the observer's height for parallax, well under a tenth of a second.
    eye = (ground or 0.0) + height

    profile, pending = None, False
    if "visible" in variants:
        known = (ground, ground_source) if ground_given is None else None
        profile = get_profile_async(lat, lon, height, terrain=terrain, physics=physics, settings=settings, ground_m=ground_given, known_ground=known, wait_s=wait)
        pending = profile is None

    res = compute(lat, lon, eye, dates, physics=physics, profile=profile, variants=variants, sun=Sun.shared(), tz=tz)

    days = []
    for i, d in enumerate(dates):
        day = {"date": d.isoformat(), "fixed": _section(res["fixed"], i, tz, fields)}
        for v in variants:
            if v == "visible" and pending:
                day[v] = {"status": "computing"}
                continue
            data = res["variants"][v]
            sec = _section(data, i, tz, fields)
            if v == "visible":
                sea = res["variants"].get("sea_level")
                for event in ("sunrise", "sunset"):
                    if fields and event not in fields:
                        continue
                    az = data[f"{event}_azimuth"][i]
                    detail = {"blocking": _blocking(profile, az)}
                    if sea is not None:
                        detail["vs_sea_level_s"] = _num(data[event][i] - sea[event][i], 0)
                    sec[f"{event}_details"] = detail
            day[v] = sec
        days.append(day)

    location.update({
        "lat": round(lat, 6), "lon": round(lon, 6), "timezone": tz_name,
        "ground_m": None if ground is None else round(ground, 2), "ground_source": ground_source,
        "height_m": height, "eye_elevation_m": round(eye, 2),
    })
    body = {
        "location": location,
        "settings": {"variants": variants, "min_distance_m": min_distance, "refraction_arcmin": physics.horizon_refraction_arcmin, "terrestrial_k": physics.terrestrial_k},
        "days": days,
        "sources": ATTRIBUTION,
        "version": VERSION,
    }
    if "horizon" in include and profile is not None and len(dates) == 1 and "visible" in res["variants"]:
        body["horizon"] = {e: _horizon_view(profile, res["variants"]["visible"], e, lat, lon, eye, physics, tz) for e in ("sunrise", "sunset")}
    if pending:
        body["status"] = "computing"
        body["retry_after_s"] = 15
        body["message"] = "The visible horizon for this location is being computed (terrain data is downloaded the first time an area is used). Retry the same request shortly."
        return 202, body
    return 200, body


def _horizon_view(profile, visible, event, lat, lon, eye, physics, tz):
    """Skyline around the event direction and the sun's apparent path past it. At each moment the path is shifted by the refraction of the skyline point the sun's disk is closest to clearing, the same point the calculation uses, so the disk touches the skyline exactly at the computed time."""
    t_event, az_event = visible[event][0], visible[f"{event}_azimuth"][0]
    if not (np.isfinite(t_event) and np.isfinite(az_event)):
        return None
    az = az_event + np.arange(-VIEW_HALF_WIDTH_DEG, VIEW_HALF_WIDTH_DEG + VIEW_STEP_DEG / 2, VIEW_STEP_DEG)
    bins = profile.bin(az)
    ok = profile.computed[bins]
    terrain = np.where(ok, profile.block_e[bins], np.nan)

    times = t_event + np.arange(-VIEW_PATH_S, VIEW_PATH_S + 1, VIEW_PATH_STEP_S)
    track = Sun.shared().track(lat, lon, eye, times[0], times[-1])
    alt, saz, dist = track.altaz(times)
    _, sb = profile.margin(alt, saz, track.semidiameter(dist, physics), return_bin=True)
    lift = np.where(profile.computed[sb], profile.block_e[sb] - profile.threshold[sb], np.nan)
    inside = np.abs((saz - az_event + 180) % 360 - 180) <= VIEW_HALF_WIDTH_DEG
    def hms(t):
        return dt.datetime.fromtimestamp(round(float(t)), tz).strftime("%H:%M:%S")

    return {
        "azimuth_start": round(float(az[0]) % 360, 2),
        "azimuth_step": VIEW_STEP_DEG,
        "terrain_deg": [None if not np.isfinite(v) else round(float(v), 3) for v in terrain],
        "sun_radius_deg": round(float(track.semidiameter(dist[0], physics)), 4),
        "sun_path": {
            "time": [hms(t) for t in times[inside]],
            "azimuth": [round(float(a), 3) for a in saz[inside]],
            "altitude_deg": [None if not np.isfinite(v) else round(float(v), 3) for v in (alt + lift)[inside]],
        },
    }


def _section(data, i, tz, fields):
    out = {}
    for key, values in data.items():
        if fields and key not in fields:
            continue
        v = values[i]
        out[key] = _num(v, 2 if key.endswith("azimuth") else 1) if key in NUMERIC_KEYS else _iso(v, tz)
    return out
