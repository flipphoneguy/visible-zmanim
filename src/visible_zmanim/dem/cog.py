from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import rasterio
from rasterio.errors import RasterioIOError
from rasterio.windows import Window

from .store import TileStore

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
}
FETCH_THREADS = 8
MOSAIC_MAX_PIXELS = 64_000_000


class MissingSource(Exception):
    pass


@dataclass(frozen=True)
class Level:
    transform: tuple  # GDAL order: x0, dx, 0, y0, 0, dy
    width: int
    height: int
    block_w: int
    block_h: int

    @property
    def res(self) -> float:
        return abs(self.transform[1])


class RemoteCog:
    _local = threading.local()

    def __init__(self, url: str, store: TileStore, key: str | None = None):
        self.url = url
        self.store = store
        self.key = key or hashlib.sha1(url.encode()).hexdigest()[:16]
        self._meta = None
        self._meta_lock = threading.Lock()

    def _open(self, level: int):
        handles = getattr(self._local, "handles", None)
        if handles is None:
            handles = self._local.handles = {}
        ds = handles.get((self.url, level))
        if ds is None:
            with rasterio.Env(**GDAL_ENV):
                kw = {"overview_level": level - 1} if level > 0 else {}
                ds = rasterio.open(self.url, **kw)
            handles[(self.url, level)] = ds
        return ds

    @property
    def meta(self) -> dict:
        with self._meta_lock:
            if self._meta is None:
                meta = self.store.load_json(f"{self.key}/meta.json")
                if meta is None:
                    meta = self._read_meta()
                    self.store.save_json(f"{self.key}/meta.json", meta)
                if meta.get("missing"):
                    raise MissingSource(self.url)
                meta["levels"] = [Level(**lv) for lv in meta["levels"]]
                self._meta = meta
            return self._meta

    def _read_meta(self) -> dict:
        try:
            with rasterio.Env(**GDAL_ENV), rasterio.open(self.url) as ds:
                factors = ds.overviews(1)
                crs = ds.crs.to_wkt()
                nodata = ds.nodata
        except RasterioIOError:
            return {"missing": True}
        levels = []
        for lv in range(len(factors) + 1):
            ds = self._open(lv)
            t = ds.transform
            bh, bw = ds.block_shapes[0]
            levels.append({"transform": (t.c, t.a, 0.0, t.f, 0.0, t.e), "width": ds.width, "height": ds.height, "block_w": bw, "block_h": bh})
        return {"url": self.url, "crs": crs, "nodata": nodata, "levels": levels}

    def level_for(self, resolution) -> np.ndarray:
        """Coarsest level whose pixel is no bigger than ``resolution`` (same units as the CRS)."""
        res0 = self.meta["levels"][0].res
        lv = np.floor(np.log2(np.maximum(np.asarray(resolution, dtype=float), res0) / res0)).astype(int)
        return np.clip(lv, 0, len(self.meta["levels"]) - 1)

    def _rel(self, level, r, c):
        return f"{self.key}/L{level}/{r}_{c}.npz"

    def _fetch(self, level, r, c, pinned=False):
        lv = self.meta["levels"][level]
        x0, y0 = c * lv.block_w, r * lv.block_h
        w, h = min(lv.block_w, lv.width - x0), min(lv.block_h, lv.height - y0)
        data = self._open(level).read(1, window=Window(x0, y0, w, h)).astype(np.float32)
        nodata = self.meta["nodata"]
        bad = ~np.isfinite(data) | (np.abs(data) > 1e5)
        if nodata is not None:
            bad |= data == np.float32(nodata)
        data[bad] = np.nan
        self.store.save_chunk(self._rel(level, r, c), data, pinned=pinned)
        return data

    def chunk(self, level, r, c):
        arr = self.store.load_chunk(self._rel(level, r, c))
        return arr if arr is not None else self._fetch(level, r, c)

    def ensure(self, level, blocks, pinned=False):
        """Download any of the (row, col) blocks not on disk yet. With ``pinned`` they go to the pinned store."""
        if pinned:
            todo = []
            for b in blocks:
                rel = self._rel(level, *b)
                if (self.store.pinned / rel).exists():
                    continue
                if not self.store.promote(rel):
                    todo.append(b)
        else:
            todo = [b for b in blocks if self.store.find(self._rel(level, *b)) is None]
        if not todo:
            return
        with ThreadPoolExecutor(FETCH_THREADS) as pool:
            list(pool.map(lambda b: self._fetch(level, b[0], b[1], pinned), todo))

    def blocks_for_bbox(self, level, x_min, y_min, x_max, y_max):
        lv = self.meta["levels"][level]
        x0, dx, _, y0, _, dy = lv.transform
        cols = sorted({int(np.clip((x - x0) / dx, 0, lv.width - 1)) // lv.block_w for x in (x_min, x_max)})
        rows = sorted({int(np.clip((y - y0) / dy, 0, lv.height - 1)) // lv.block_h for y in (y_min, y_max)})
        return [(r, c) for r in range(rows[0], rows[-1] + 1) for c in range(cols[0], cols[-1] + 1)]

    def sample(self, x, y, level: int) -> np.ndarray:
        """Bilinear heights at CRS coordinates x, y from one level. NaN outside the raster or over nodata."""
        lv = self.meta["levels"][level]
        x0, dx, _, y0, _, dy = lv.transform
        px = (np.asarray(x, dtype=float) - x0) / dx - 0.5
        py = (np.asarray(y, dtype=float) - y0) / dy - 0.5
        ix, iy = np.floor(px).astype(np.int64), np.floor(py).astype(np.int64)
        fx, fy = px - ix, py - iy
        blocks = self._prefetch(level, lv, ix, iy)
        if not blocks:
            return np.full(px.shape, np.nan)
        r0, r1 = min(b[0] for b in blocks), max(b[0] for b in blocks)
        c0, c1 = min(b[1] for b in blocks), max(b[1] for b in blocks)
        h, w = (r1 - r0 + 1) * lv.block_h, (c1 - c0 + 1) * lv.block_w
        if h * w <= MOSAIC_MAX_PIXELS:
            # Stitch the needed blocks into one array and index it directly; blocks with no samples stay NaN.
            mosaic = np.full((h, w), np.nan, dtype=np.float32)
            for r, c in blocks:
                a = self.chunk(level, r, c)
                y_, x_ = (r - r0) * lv.block_h, (c - c0) * lv.block_w
                mosaic[y_:y_ + a.shape[0], x_:x_ + a.shape[1]] = a
            jx, jy = ix - c0 * lv.block_w, iy - r0 * lv.block_h
            ok = (jx >= 0) & (jx + 1 < w) & (jy >= 0) & (jy + 1 < h)
            out = np.full(px.shape, np.nan)
            jx, jy, fx, fy = jx[ok], jy[ok], fx[ok], fy[ok]
            out[ok] = (mosaic[jy, jx] * (1 - fx) + mosaic[jy, jx + 1] * fx) * (1 - fy) + (mosaic[jy + 1, jx] * (1 - fx) + mosaic[jy + 1, jx + 1] * fx) * fy
            return out
        v00 = self._gather(level, lv, iy, ix)
        v01 = self._gather(level, lv, iy, ix + 1)
        v10 = self._gather(level, lv, iy + 1, ix)
        v11 = self._gather(level, lv, iy + 1, ix + 1)
        return (v00 * (1 - fx) + v01 * fx) * (1 - fy) + (v10 * (1 - fx) + v11 * fx) * fy

    def _prefetch(self, level, lv, ix, iy):
        inside = (ix >= -1) & (ix < lv.width) & (iy >= -1) & (iy < lv.height)
        if not inside.any():
            return []
        ix, iy = ix[inside], iy[inside]
        keys = []
        for dy, dx in ((0, 0), (0, 1), (1, 0), (1, 1)):
            cols = np.clip(ix + dx, 0, lv.width - 1) // lv.block_w
            rows = np.clip(iy + dy, 0, lv.height - 1) // lv.block_h
            keys.append(np.unique(rows * 1_000_000 + cols))
        blocks = sorted((int(k // 1_000_000), int(k % 1_000_000)) for k in np.unique(np.concatenate(keys)))
        self.ensure(level, blocks)
        return blocks

    def _gather(self, level, lv, iy, ix):
        out = np.full(iy.shape, np.nan, dtype=np.float64)
        ok = (ix >= 0) & (ix < lv.width) & (iy >= 0) & (iy < lv.height)
        if not ok.any():
            return out
        r, c = iy[ok] // lv.block_h, ix[ok] // lv.block_w
        keys = r * 1_000_000 + c
        uniq, inv = np.unique(keys, return_inverse=True)
        vals = np.empty(keys.shape, dtype=np.float64)
        yy, xx = iy[ok] % lv.block_h, ix[ok] % lv.block_w
        for k, key in enumerate(uniq):
            sel = inv == k
            vals[sel] = self.chunk(level, int(key // 1_000_000), int(key % 1_000_000))[yy[sel], xx[sel]]
        out[ok] = vals
        return out
