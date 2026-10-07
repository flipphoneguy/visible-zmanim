from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from ..config import cache_limit_bytes, data_dir

MEMORY_BYTES = int(float(os.environ.get("VISIBLE_ZMANIM_MEMORY_MB", "1024")) * 1024**2)


def encode(a: np.ndarray) -> dict:
    """Lossless to 1 cm (1 dm for chunks spanning more than 655 m of relief, which only happens far away)."""
    finite = np.isfinite(a)
    if not finite.any():
        return {"empty": np.array(a.shape)}
    lo, hi = float(np.nanmin(a)), float(np.nanmax(a))
    for scale, dtype in ((0.01, np.int16), (0.1, np.int16), (0.01, np.int32)):
        info = np.iinfo(dtype)
        if (hi - lo) / scale < info.max - 1:
            q = np.where(finite, np.rint((np.nan_to_num(a) - lo) / scale), info.min).astype(dtype)
            return {"q": q, "offset": np.array(lo), "scale": np.array(scale)}
    raise ValueError("height range too large")


def decode(z) -> np.ndarray:
    if "empty" in z:
        return np.full(tuple(z["empty"]), np.nan, dtype=np.float32)
    q = z["q"]
    out = q.astype(np.float32) * np.float32(z["scale"]) + np.float32(z["offset"])
    out[q == np.iinfo(q.dtype).min] = np.nan
    return out


class TileStore:
    """Terrain chunks on disk: ``pinned/`` is never evicted, ``cache/`` is kept under a size cap (least recently used goes first)."""

    def __init__(self, root: Path | None = None, cap_bytes: int | None = None):
        base = Path(root or data_dir()) / "dem"
        self.pinned = base / "pinned"
        self.cache = base / "cache"
        self.cap = cache_limit_bytes() if cap_bytes is None else cap_bytes
        self.pinned.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self._lock_path = base / ".evict.lock"
        self._written = 0
        self._mem: OrderedDict[str, np.ndarray] = OrderedDict()
        self._mem_bytes = 0
        self._mem_lock = threading.Lock()

    def find(self, rel: str) -> Path | None:
        p = self.pinned / rel
        if p.exists():
            return p
        p = self.cache / rel
        if p.exists():
            try:
                os.utime(p)
            except OSError:
                pass
            return p
        return None

    def load_chunk(self, rel: str) -> np.ndarray | None:
        with self._mem_lock:
            a = self._mem.get(rel)
            if a is not None:
                self._mem.move_to_end(rel)
                return a
        p = self.find(rel)
        if p is None:
            return None
        with np.load(p) as z:
            a = decode(z)
        self._remember(rel, a)
        return a

    def save_chunk(self, rel: str, array: np.ndarray, pinned: bool = False) -> None:
        self._write(rel, pinned, lambda f: np.savez_compressed(f, **encode(array)))
        self._remember(rel, array)

    def _remember(self, rel, a):
        with self._mem_lock:
            if rel in self._mem:
                return
            self._mem[rel] = a
            self._mem_bytes += a.nbytes
            while self._mem_bytes > MEMORY_BYTES and len(self._mem) > 1:
                _, old = self._mem.popitem(last=False)
                self._mem_bytes -= old.nbytes

    def load_json(self, rel: str):
        p = self.find(rel)
        return None if p is None else json.loads(p.read_text())

    def save_json(self, rel: str, obj, pinned: bool = False) -> None:
        self._write(rel, pinned, lambda f: f.write(json.dumps(obj).encode()))

    def _write(self, rel, pinned, writer):
        target = (self.pinned if pinned else self.cache) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            writer(f)
        os.replace(tmp, target)
        if not pinned:
            self._written += target.stat().st_size
            if self._written > self.cap // 50:
                self._written = 0
                self.evict()

    def cache_size(self) -> int:
        return sum(p.stat().st_size for p in self.cache.rglob("*") if p.is_file())

    def evict(self) -> int:
        """Delete least recently used cache files until the cache is under 90% of the cap. Returns bytes freed."""
        with open(self._lock_path, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            files = [(st.st_mtime, st.st_size, p) for p in self.cache.rglob("*") if p.is_file() and not p.name.endswith(".tmp") for st in [p.stat()]]
            total = sum(s for _, s, _ in files)
            if total <= self.cap:
                return 0
            freed = 0
            for _, size, path in sorted(files):
                if total - freed <= self.cap * 0.9:
                    break
                try:
                    path.unlink()
                    freed += size
                except FileNotFoundError:
                    pass
            return freed
