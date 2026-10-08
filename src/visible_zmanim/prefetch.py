"""Download every terrain block an observer inside a region could need, into the pinned store."""
from __future__ import annotations

import math

import numpy as np
from pyproj import CRS, Transformer

from .config import DEFAULT_HORIZON
from .dem.cog import MissingSource
from .dem.terrain import M_PER_DEG, Terrain

# name: (lon_min, lat_min, lon_max, lat_max)
REGIONS = {
    "rockland": (-74.25, 41.00, -73.88, 41.37),
    "monroe_kiryas_joel": (-74.22, 41.28, -74.10, 41.36),
    "nyc": (-74.26, 40.49, -73.70, 40.92),
    "five_towns": (-73.77, 40.59, -73.68, 40.66),
    "lakewood_jackson": (-74.40, 40.00, -74.10, 40.16),
    "passaic_clifton": (-74.18, 40.83, -74.10, 40.88),
    "teaneck_bergen": (-74.15, 40.85, -73.94, 40.98),
    "elizabeth": (-74.25, 40.64, -74.17, 40.70),
    "baltimore": (-76.75, 39.30, -76.62, 39.42),
    "montreal": (-73.70, 45.45, -73.57, 45.53),
    "london": (-0.31, 51.54, -0.04, 51.63),
    "manchester": (-2.33, 53.48, -2.23, 53.57),
    "israel": (34.20, 29.40, 35.95, 33.35),
}


def _expand(bbox, meters):
    lon0, lat0, lon1, lat1 = bbox
    dlat = meters / M_PER_DEG
    dlon = dlat / max(math.cos(math.radians(max(abs(lat0), abs(lat1)))), 0.05)
    return lon0 - dlon, lat0 - dlat, lon1 + dlon, lat1 + dlat


def _level_reach(res0_m, n_levels, relative_step, max_d):
    """(level, max distance) pairs: level L is used out to res0 * 2^(L+1) / relative_step."""
    out = []
    for lv in range(n_levels):
        reach = min(res0_m * 2 ** (lv + 1) / relative_step, max_d)
        out.append((lv, reach))
        if reach >= max_d:
            break
    # Farther samples are clipped to the coarsest level, so it has to cover the rest of the range.
    out[-1] = (out[-1][0], max_d)
    return out


def plan(terrain: Terrain, bbox, *, relative_step=DEFAULT_HORIZON.relative_step, max_d=DEFAULT_HORIZON.max_distance_m):
    """(jobs, pins): the (cog, level, blocks) needed for observers inside bbox, and the small metadata files those lookups depend on."""
    jobs, pins = [], set()
    lon0, lat0, lon1, lat1 = bbox
    us = terrain.use_usgs and terrain._maybe_us((lat0 + lat1) / 2, (lon0 + lon1) / 2)

    if us:
        lidar_box = _expand(bbox, terrain.bands.lidar_max_m)
        cells = set()
        for la in np.arange(lidar_box[1], lidar_box[3] + 0.1, 0.1):
            for lo in np.arange(lidar_box[0], lidar_box[2] + 0.1, 0.1):
                cells.add((min(la, lidar_box[3]), min(lo, lidar_box[2])))
        items = {}
        for la, lo in cells:
            pins.add(terrain.index_rel(la, lo))
            for it in terrain._lidar_items(la, lo):
                items[it["url"]] = it
        for it in items.values():
            to_utm = Transformer.from_crs("EPSG:4326", CRS.from_user_input(f"EPSG:269{it['zone']:02d}"), always_xy=True)
            cog = terrain._lidar_cog(it["url"])
            pins.add(f"{cog.key}/meta.json")
            try:
                levels = cog.meta["levels"]
            except MissingSource:
                continue
            for lv, reach in _level_reach(1.0, len(levels), relative_step, terrain.bands.lidar_max_m):
                box = _expand(bbox, reach)
                xs, ys = to_utm.transform([box[0], box[2], box[0], box[2]], [box[1], box[1], box[3], box[3]])
                x_min, x_max = max(min(xs), it["x"]), min(max(xs), it["x"] + 10_000)
                y_min, y_max = max(min(ys), it["y"]), min(max(ys), it["y"] + 10_000)
                if x_min < x_max and y_min < y_max:
                    jobs.append((cog, lv, cog.blocks_for_bbox(lv, x_min, y_min, x_max, y_max)))

        box13 = _expand(bbox, terrain.bands.usgs13_max_m)
        for ilat in range(math.ceil(box13[1]), math.ceil(box13[3]) + 1):
            for ilon in range(math.floor(box13[0]), math.floor(box13[2]) + 1):
                name = terrain._usgs13_name(ilat, ilon)
                pins.add(f"usgs13_{name}/meta.json")
                cog = terrain._usgs13_tile(name)
                if cog is None:
                    continue
                res0 = cog.meta["levels"][0].res * M_PER_DEG
                for lv, reach in _level_reach(res0, len(cog.meta["levels"]), relative_step, terrain.bands.usgs13_max_m):
                    b = _expand(bbox, reach)
                    x_min, x_max = max(b[0], ilon), min(b[2], ilon + 1)
                    y_min, y_max = max(b[1], ilat - 1), min(b[3], ilat)
                    if x_min < x_max and y_min < y_max:
                        jobs.append((cog, lv, cog.blocks_for_bbox(lv, x_min, y_min, x_max, y_max)))

    if terrain._maybe_england((lat0 + lat1) / 2, (lon0 + lon1) / 2):
        eng = terrain.england
        pins.add(f"{eng.key}/meta.json")
        to_bng = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
        for lv, reach in _level_reach(1.0, len(eng.meta["levels"]), relative_step, terrain.bands.lidar_max_m):
            box = _expand(bbox, reach)
            xs, ys = to_bng.transform([box[0], box[2], box[0], box[2]], [box[1], box[1], box[3], box[3]])
            jobs.append((eng, lv, eng.blocks_for_bbox(lv, min(xs), min(ys), max(xs), max(ys))))

    # GEDTM also fills in wherever USGS has no data (open sea, across the border), so it is needed at every distance.
    g = terrain.gedtm
    pins.add(f"{g.key}/meta.json")
    res0 = g.meta["levels"][0].res * M_PER_DEG
    for lv, reach in _level_reach(res0, len(g.meta["levels"]), relative_step, max_d):
        b = _expand(bbox, reach)
        jobs.append((g, lv, g.blocks_for_bbox(lv, *b)))
    return jobs, sorted(pins)


def run(name: str, terrain: Terrain | None = None, log=print):
    terrain = terrain or Terrain()
    jobs, pins = plan(terrain, REGIONS[name])
    for rel in pins:
        terrain.store.promote(rel)
    total = sum(len(b) for _, _, b in jobs)
    log(f"{name}: {total} blocks in {len(jobs)} groups")
    done = 0
    for cog, lv, blocks in jobs:
        cog.ensure(lv, blocks, pinned=True)
        done += len(blocks)
        log(f"{name}: {done}/{total} ({cog.key} L{lv})")
