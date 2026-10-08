from __future__ import annotations

import time

import numpy as np
import rasterio
import requests
from pyproj import CRS
from rasterio.errors import RasterioIOError

from .cog import RETRIES, RemoteCog
from .store import TileStore

EA_WCS = "https://environment.data.gov.uk/spatialdata/lidar-composite-digital-terrain-model-dtm-1m/wcs"
EA_COVERAGE = "13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Elevation_DTM_1m"
# Coverage extent in British National Grid metres.
EA_BOUNDS = (80_000.0, 4_000.0, 656_000.0, 665_000.0)
BLOCK = 512
LEVELS = 3  # 1, 2 and 4 m


class EnglandLidar(RemoteCog):
    """Environment Agency LIDAR Composite DTM (1 m, England), read block by block over WCS with the same caching as the COG sources."""

    def __init__(self, store: TileStore):
        super().__init__(EA_WCS, store, "ea_lidar_dtm1m")

    def _read_meta(self) -> dict:
        x0, y0, x1, y1 = EA_BOUNDS
        levels = []
        for lv in range(LEVELS):
            res = 2.0**lv
            levels.append({"transform": (x0, res, 0.0, y1, 0.0, -res), "width": int((x1 - x0) / res), "height": int((y1 - y0) / res), "block_w": BLOCK, "block_h": BLOCK})
        return {"url": EA_WCS, "crs": CRS.from_epsg(27700).to_wkt(), "nodata": None, "levels": levels}

    def covers(self, x, y) -> bool:
        x0, y0, x1, y1 = EA_BOUNDS
        return x0 <= x <= x1 and y0 <= y <= y1

    def _fetch(self, level, r, c, pinned=False):
        lv = self.meta["levels"][level]
        res = lv.res
        left = EA_BOUNDS[0] + c * BLOCK * res
        top = EA_BOUNDS[3] - r * BLOCK * res
        params = [
            ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"), ("coverageId", EA_COVERAGE), ("format", "image/tiff"),
            ("subset", f"E({left},{left + BLOCK * res})"), ("subset", f"N({top - BLOCK * res},{top})"),
        ]
        if level:
            params.append(("scalefactor", str(1 / res)))
        for attempt in range(RETRIES):
            try:
                resp = requests.get(EA_WCS, params=params, timeout=90)
                resp.raise_for_status()
                with rasterio.MemoryFile(resp.content) as mem, mem.open() as ds:
                    data = ds.read(1).astype(np.float32)
                break
            except (requests.RequestException, RasterioIOError):
                if attempt == RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        data[~np.isfinite(data) | (np.abs(data) > 1e5)] = np.nan
        self.store.save_chunk(self._rel(level, r, c), data, pinned=pinned)
        return data
