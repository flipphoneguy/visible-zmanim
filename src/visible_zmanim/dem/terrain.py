from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import requests
from pyproj import CRS, Proj, Transformer

from .cog import MissingSource, RemoteCog
from .store import TileStore

GEDTM_URL = "https://s3.opengeohub.org/global/dtm/v1.2/gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif"
USGS13_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{name}/USGS_13_{name}.tif"
TNM_API = "https://tnmaccess.nationalmap.gov/api/v1/products"
USGS1M_DATASET = "Digital Elevation Model (DEM) 1 meter"
M_PER_DEG = 111_320.0
INDEX_CELL = 0.1
SOURCES = ("sea_level", "usgs_3dep_1m", "usgs_3dep_13", "gedtm30")
CONTEXT_CACHE = 64


class TerrainUnavailable(Exception):
    """A data source couldn't be reached. Results computed without it would be silently worse, so nothing is computed."""


@dataclass(frozen=True)
class Bands:
    lidar_max_m: float = 3_000.0
    usgs13_max_m: float = 50_000.0


class _ProjectedFrame:
    """Maps (distance, azimuth) from the observer to coordinates of a projected CRS with a local plane (accurate to cm within a few km)."""

    def __init__(self, crs_wkt, lat, lon):
        crs = CRS.from_wkt(crs_wkt)
        self.x0, self.y0 = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
        f = Proj(crs).get_factors(lon, lat)
        self.convergence = f.meridian_convergence
        self.scale = f.meridional_scale

    def xy(self, d, az):
        a = np.radians(np.asarray(az) - self.convergence)
        s = np.asarray(d) * self.scale
        return self.x0 + s * np.sin(a), self.y0 + s * np.cos(a)


class Terrain:
    """Bare-earth heights from USGS 3DEP (1 m lidar, 1/3 arc-second) where available, else GEDTM30, else sea level (0 m)."""

    def __init__(self, store: TileStore | None = None, bands: Bands = Bands(), use_usgs: bool = True):
        self.store = store or TileStore()
        self.bands = bands
        self.use_usgs = use_usgs
        self.gedtm = RemoteCog(GEDTM_URL, self.store, "gedtm30_v1.2")
        self._usgs13: dict[str, RemoteCog | None] = {}
        self._lidar: dict[str, RemoteCog] = {}
        self._lock = threading.Lock()
        self._contexts: OrderedDict[tuple, tuple] = OrderedDict()

    # USGS 1/3 arc-second, one file per 1x1 degree tile
    def _usgs13_tile(self, name):
        with self._lock:
            if name in self._usgs13:
                return self._usgs13[name]
        cog = RemoteCog(USGS13_URL.format(name=name), self.store, f"usgs13_{name}")
        try:
            cog.meta
        except MissingSource:
            cog = None
        with self._lock:
            return self._usgs13.setdefault(name, cog)

    @staticmethod
    def _usgs13_name(ilat: int, ilon: int):
        """Tile covering latitudes (ilat - 1, ilat] and longitudes [ilon, ilon + 1)."""
        return f"{'n' if ilat > 0 else 's'}{abs(ilat):02d}{'w' if ilon < 0 else 'e'}{abs(ilon):03d}"

    @staticmethod
    def _maybe_us(lat, lon):
        return -180 <= lon <= -60 and 15 <= lat <= 72

    # USGS 1 m lidar, 10 km UTM tiles from several survey projects
    @staticmethod
    def index_rel(lat, lon):
        return f"usgs1m_index/{math.floor(lat / INDEX_CELL)}_{math.floor(lon / INDEX_CELL)}.json"

    def _lidar_items(self, lat, lon):
        cell_lat, cell_lon = math.floor(lat / INDEX_CELL), math.floor(lon / INDEX_CELL)
        rel = self.index_rel(lat, lon)
        items = self.store.load_json(rel)
        if items is None:
            bbox = f"{cell_lon * INDEX_CELL},{cell_lat * INDEX_CELL},{(cell_lon + 1) * INDEX_CELL},{(cell_lat + 1) * INDEX_CELL}"
            for attempt in range(3):
                try:
                    r = requests.get(TNM_API, params={"datasets": USGS1M_DATASET, "bbox": bbox, "max": 200, "outputFormat": "JSON"}, timeout=60)
                    r.raise_for_status()
                    break
                except requests.RequestException as e:
                    if attempt == 2:
                        raise TerrainUnavailable(f"USGS lidar index unavailable: {e}") from e
                    time.sleep(2 ** attempt)
            items = []
            for it in r.json().get("items", []):
                m = re.search(r"_(\d+)_x(\d+)y(\d+)_", it["downloadURL"])
                if m and it["downloadURL"].endswith(".tif"):
                    zone, x, y = map(int, m.groups())
                    items.append({"url": it["downloadURL"], "date": it.get("publicationDate") or "", "zone": zone, "x": x * 10_000, "y": (y - 1) * 10_000})
            items.sort(key=lambda i: i["date"], reverse=True)
            self.store.save_json(rel, items)
        return items

    def _lidar_cog(self, url):
        with self._lock:
            if url not in self._lidar:
                self._lidar[url] = RemoteCog(url, self.store, "usgs1m_" + url.rsplit("/", 1)[1][:-4])
            return self._lidar[url]

    def lidar_tiles_near(self, lat, lon, radius_m):
        dlat = radius_m / M_PER_DEG
        dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
        seen, out = set(), []
        for la in np.arange(lat - dlat, lat + dlat + INDEX_CELL, INDEX_CELL):
            for lo in np.arange(lon - dlon, lon + dlon + INDEX_CELL, INDEX_CELL):
                for it in self._lidar_items(min(la, lat + dlat), min(lo, lon + dlon)):
                    if it["url"] not in seen:
                        seen.add(it["url"])
                        out.append(it)
        out.sort(key=lambda i: i["date"], reverse=True)
        return out

    def _lidar_context(self, lat0, lon0):
        """Lidar tiles around the observer, newest survey first, and a local plane per UTM zone. Built once per observer."""
        key = (round(lat0, 7), round(lon0, 7))
        with self._lock:
            ctx = self._contexts.get(key)
            if ctx is not None:
                self._contexts.move_to_end(key)
                return ctx
        tiles = self.lidar_tiles_near(lat0, lon0, self.bands.lidar_max_m)
        frames = {}
        for it in tiles:
            crs = f"EPSG:269{it['zone']:02d}"
            if crs not in frames:
                frames[crs] = _ProjectedFrame(CRS.from_user_input(crs).to_wkt(), lat0, lon0)
        with self._lock:
            self._contexts[key] = (tiles, frames)
            while len(self._contexts) > CONTEXT_CACHE:
                self._contexts.popitem(last=False)
        return tiles, frames

    def heights(self, lat0, lon0, lat, lon, d, az, *, relative_step=0.002, min_res_m=1.0, with_source=False):
        """Heights for sample points given both as lat/lon and as distance/azimuth from the observer at (lat0, lon0)."""
        lat, lon, d, az = (np.asarray(a, dtype=float) for a in (lat, lon, d, az))
        res_m = np.maximum(d * relative_step, min_res_m)
        h = np.full(lat.shape, np.nan)
        src = np.zeros(lat.shape, dtype=np.int8)
        us = self.use_usgs and self._maybe_us(lat0, lon0)

        if us:
            near = d <= self.bands.lidar_max_m
            if near.any():
                tiles, frames = self._lidar_context(lat0, lon0)
                near_idx = np.flatnonzero(near)
                coords = {}
                for it in tiles:
                    crs = f"EPSG:269{it['zone']:02d}"
                    if crs not in coords:
                        coords[crs] = frames[crs].xy(d[near_idx], az[near_idx])
                    x, y = coords[crs]
                    inside = (x >= it["x"]) & (x < it["x"] + 10_000) & (y >= it["y"]) & (y < it["y"] + 10_000) & np.isnan(h[near_idx])
                    if not inside.any():
                        continue
                    cog = self._lidar_cog(it["url"])
                    try:
                        levels = cog.level_for(res_m[near_idx[inside]])
                    except MissingSource:
                        continue
                    idx, xs, ys = near_idx[inside], x[inside], y[inside]
                    for lv in np.unique(levels):
                        m = levels == lv
                        h[idx[m]] = cog.sample(xs[m], ys[m], int(lv))
                    src[idx[~np.isnan(h[idx])]] = 1

            mid = (d <= self.bands.usgs13_max_m) & np.isnan(h)
            if mid.any():
                idx_mid = np.flatnonzero(mid)
                keys = (np.ceil(lat[mid]).astype(np.int64) + 90) * 1000 + np.floor(lon[mid]).astype(np.int64) + 180
                for key in np.unique(keys):
                    cog = self._usgs13_tile(self._usgs13_name(int(key // 1000 - 90), int(key % 1000 - 180)))
                    if cog is None:
                        continue
                    idx = idx_mid[keys == key]
                    levels = cog.level_for(res_m[idx] / M_PER_DEG)
                    for lv in np.unique(levels):
                        sel = idx[levels == lv]
                        h[sel] = cog.sample(lon[sel], lat[sel], int(lv))
                    src[idx[~np.isnan(h[idx])]] = 2

        rest = np.isnan(h)
        if rest.any():
            idx = np.flatnonzero(rest)
            levels = self.gedtm.level_for(res_m[idx] / M_PER_DEG)
            for lv in np.unique(levels):
                sel = idx[levels == lv]
                h[sel] = self.gedtm.sample(lon[sel], lat[sel], int(lv))
            src[idx[~np.isnan(h[idx])]] = 3
        h = np.where(np.isnan(h), 0.0, h)
        return (h, src) if with_source else h

    def ground(self, lat, lon):
        h, src = self.heights(lat, lon, [lat], [lon], [0.0], [0.0], with_source=True)
        return float(h[0]), SOURCES[src[0]]
