import datetime as dt

import numpy as np

from visible_zmanim.config import DEFAULT_HORIZON, DEFAULT_PHYSICS
from visible_zmanim.horizon import compute_profile
from visible_zmanim.refraction import sea_horizon_threshold_deg
from visible_zmanim.zmanim import compute

FAST = DEFAULT_HORIZON.with_(azimuth_step_deg=0.25, max_distance_m=60_000)


class FlatSea:
    use_usgs = False

    def heights(self, lat0, lon0, lat, lon, d, az, with_source=False, **kw):
        h = self._h(np.asarray(d), np.asarray(az)) if hasattr(self, "_h") else np.zeros(np.shape(lat))
        return (h, np.zeros(h.shape, dtype=np.int8)) if with_source else h

    def ground(self, lat, lon):
        return 0.0, "test"


class Wall(FlatSea):
    """A 200 m ridge 2 km to the east (azimuth 60-120)."""

    def _h(self, d, az):
        return np.where((np.abs(d - 2000) < 50) & (az > 60) & (az < 120), 200.0, 0.0)


def test_flat_sea_matches_analytic_dip():
    for eye in (0.0, 2.0, 150.0, 800.0):
        p = compute_profile(31.78, 35.23, eye, terrain=FlatSea(), settings=FAST)
        expected = sea_horizon_threshold_deg(eye, DEFAULT_PHYSICS)
        got = p.threshold[p.computed]
        assert np.allclose(got, expected, atol=1.5e-3), (eye, got.min(), got.max(), expected)


def test_visible_equals_elevation_over_sea():
    dates = [dt.date(2026, 3, 20), dt.date(2026, 6, 21), dt.date(2026, 12, 21)]
    p = compute_profile(31.78, 35.23, 150.0, terrain=FlatSea(), settings=FAST)
    res = compute(31.78, 35.23, 150.0, dates, profile=p, variants=("elevation", "visible"))
    for key in ("sunrise", "sunset"):
        assert np.allclose(res["variants"]["visible"][key], res["variants"]["elevation"][key], atol=1.0)


def test_ridge_delays_sunrise_only():
    dates = [dt.date(2026, 3, 20)]
    p = compute_profile(31.78, 35.23, 0.0, terrain=Wall(), settings=FAST)
    res = compute(31.78, 35.23, 0.0, dates, profile=p, variants=("sea_level", "visible"))
    sea, vis = res["variants"]["sea_level"], res["variants"]["visible"]
    # 200 m at 2 km is ~5.7 deg; at 31.8N the sun climbs ~0.21 deg/min, so about 25-30 minutes.
    delay = (vis["sunrise"][0] - sea["sunrise"][0]) / 60
    assert 22 < delay < 32, delay
    assert abs(vis["sunset"][0] - sea["sunset"][0]) < 1.0


class Slope(FlatSea):
    """A ridge 2 km away whose height rises with azimuth, so the sun's disk first clears it off-center."""

    def _h(self, d, az):
        return np.where((np.abs(d - 2000) < 50) & (az > 60) & (az < 120), 100.0 + 20.0 * (az - 60.0), 0.0)


def test_drawn_sun_touches_skyline_at_event():
    from zoneinfo import ZoneInfo
    from visible_zmanim.api import _horizon_view
    lat, lon, tz = 31.78, 35.23, ZoneInfo("Asia/Jerusalem")
    p = compute_profile(lat, lon, 0.0, terrain=Slope(), settings=DEFAULT_HORIZON.with_(azimuth_step_deg=0.05, max_distance_m=10_000))
    res = compute(lat, lon, 0.0, [dt.date(2026, 3, 20)], profile=p, variants=("visible",), tz=tz)
    view = _horizon_view(p, res["variants"]["visible"], "sunrise", lat, lon, 0.0, DEFAULT_PHYSICS, tz)
    path = view["sun_path"]
    event = dt.datetime.fromtimestamp(round(res["variants"]["visible"]["sunrise"][0]), tz).strftime("%H:%M:%S")
    i = path["time"].index(event)
    r, az_c, alt_c = view["sun_radius_deg"], path["azimuth"][i], path["altitude_deg"][i]
    az0, step, terrain = view["azimuth_start"], view["azimuth_step"], np.array(view["terrain_deg"], dtype=float)
    az = az0 + step * np.arange(len(terrain))
    x = (az - az_c) * np.cos(np.radians(alt_c))
    inside = np.abs(x) < r
    gap = (alt_c + np.sqrt(r**2 - x[inside] ** 2)) - terrain[inside]
    assert abs(gap.max()) < 0.01, gap.max()
