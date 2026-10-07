from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("VISIBLE_ZMANIM_DATA")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "visible-zmanim"


def cache_limit_bytes() -> int:
    return int(float(os.environ.get("VISIBLE_ZMANIM_CACHE_GB", "50")) * 1024**3)


@dataclass(frozen=True)
class Physics:
    horizon_refraction_arcmin: float = 34.0
    terrestrial_k: float = 0.13
    solar_semidiameter_1au_arcsec: float = 959.63
    density_scaling: bool = True

    def with_(self, **kw) -> "Physics":
        return replace(self, **kw)


@dataclass(frozen=True)
class HorizonSettings:
    azimuth_step_deg: float = 0.05
    # radial step = max(min_step_m, distance * relative_step)
    min_step_m: float = 2.0
    relative_step: float = 0.002
    min_distance_m: float = 0.0
    max_distance_m: float = 200_000.0
    geodesic_node_spacing_m: float = 5_000.0

    def with_(self, **kw) -> "HorizonSettings":
        return replace(self, **kw)


DEFAULT_PHYSICS = Physics()
DEFAULT_HORIZON = HorizonSettings()
