from __future__ import annotations

import fcntl
import hashlib
import json
import time
from pathlib import Path

import requests

from .config import data_dir

NOMINATIM = "https://nominatim.openstreetmap.org/search"
CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
USER_AGENT = "visible-zmanim/0.1 (https://github.com/flipphoneguy/visible-zmanim)"


class GeocodeError(Exception):
    pass


class AddressNotFound(GeocodeError):
    pass


class GeocoderUnavailable(GeocodeError):
    pass


def _cache_path(query: str) -> Path:
    key = hashlib.sha1(" ".join(query.lower().split()).encode()).hexdigest()
    return data_dir() / "geocode" / f"{key}.json"


def _nominatim_slot():
    """Nominatim allows one request per second; enforce it across processes."""
    stamp = data_dir() / "geocode" / ".nominatim"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    with open(stamp, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        last = float(f.read() or 0)
        wait = last + 1.1 - time.time()
        if wait > 0:
            time.sleep(wait)
        f.seek(0)
        f.truncate()
        f.write(str(time.time()))


def _census(query: str):
    r = requests.get(CENSUS, params={"address": query, "benchmark": "Public_AR_Current", "format": "json"}, timeout=20)
    r.raise_for_status()
    matches = r.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    m = matches[0]
    return {"lat": m["coordinates"]["y"], "lon": m["coordinates"]["x"], "matched": m["matchedAddress"], "source": "us_census", "precision": "street_interpolated"}


def _nominatim(query: str):
    _nominatim_slot()
    r = requests.get(NOMINATIM, params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1}, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    res = r.json()
    if not res:
        return None
    p = res[0]
    return {"lat": float(p["lat"]), "lon": float(p["lon"]), "matched": p["display_name"], "source": "nominatim", "precision": p.get("addresstype") or p.get("type"), "country_code": p.get("address", {}).get("country_code")}


def geocode(query: str) -> dict:
    """Address to coordinates. Nominatim worldwide; for US street addresses the Census geocoder is preferred when it matches."""
    path = _cache_path(query)
    if path.exists():
        return json.loads(path.read_text())
    try:
        result = _nominatim(query)
        if (result is None or result.get("country_code") == "us") and any(ch.isdigit() for ch in query):
            census = _census(query)
            if census:
                result = census
    except requests.RequestException as e:
        raise GeocoderUnavailable(f"address lookup is unavailable right now: {e}") from e
    if result is None:
        raise AddressNotFound(f"no match for {query!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result))
    return result
