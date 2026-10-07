from __future__ import annotations

import datetime as dt

import numpy as np

from .config import Physics, DEFAULT_PHYSICS
from .refraction import sea_horizon_threshold_deg, sea_level_threshold_deg
from .sun import DAY, Sun, find_crossings

HALF_DAY = DAY / 2
CANDLE_LIGHTING_MIN = 18.0
DEGREE_ZMANIM = {"alos_16_1": (-16.1, True), "tzais_16_1": (-16.1, False), "tzais_8_5": (-8.5, False)}


def solar_noon_guess(dates, lon):
    midnight = np.array([dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() for d in dates])
    return midnight + HALF_DAY - lon / 15.0 * 3600.0


def _rise_set(noon, margin, step, iterations):
    rise = find_crossings(margin, noon - HALF_DAY, noon, step, rising=True, iterations=iterations)
    set_ = find_crossings(margin, noon, noon + HALF_DAY, step, rising=False, iterations=iterations)
    return rise, set_


def _limb_margin(track, physics, threshold):
    def margin(t):
        alt, _, dist = track.altaz(t)
        return alt + track.semidiameter(dist, physics) - threshold
    return margin


def _profile_margin(track, physics, profile):
    def margin(t):
        alt, az, dist = track.altaz(t)
        return profile.margin(alt, az, track.semidiameter(dist, physics))
    return margin


def _azimuth(track, t):
    az = np.full(t.shape, np.nan)
    ok = np.isfinite(t)
    if ok.any():
        az[ok] = track.altaz(t[ok])[1]
    return az


def derived(sunrise, sunset):
    shaah = (sunset - sunrise) / 12.0
    alos72 = sunrise - 72 * 60.0
    tzais72 = sunset + 72 * 60.0
    shaah_mga = (tzais72 - alos72) / 12.0
    return {
        "sunrise": sunrise,
        "sunset": sunset,
        "alos_72": alos72,
        "tzais_72": tzais72,
        "sof_zman_shma_gra": sunrise + 3 * shaah,
        "sof_zman_shma_mga_72": alos72 + 3 * shaah_mga,
        "sof_zman_tfila_gra": sunrise + 4 * shaah,
        "sof_zman_tfila_mga_72": alos72 + 4 * shaah_mga,
        "mincha_gedola_gra": sunrise + 6.5 * shaah,
        "mincha_ketana_gra": sunrise + 9.5 * shaah,
        "plag_hamincha_gra": sunrise + 10.75 * shaah,
        "candle_lighting": sunset - CANDLE_LIGHTING_MIN * 60.0,
        "shaah_zmanis_gra_s": shaah,
        "shaah_zmanis_mga_72_s": shaah_mga,
    }


def compute(lat, lon, height_m, dates, *, physics: Physics = DEFAULT_PHYSICS, profile=None, variants=("sea_level", "elevation", "visible"), sun: Sun | None = None):
    """Zmanim as unix seconds (NaN where the event doesn't happen) for each date. ``height_m`` is the eye height above sea level."""
    sun = sun or Sun.shared()
    noon_guess = solar_noon_guess(dates, lon)
    track = sun.track(lat, lon, height_m, noon_guess.min() - DAY, noon_guess.max() + DAY)
    noon = track.transits(noon_guess)

    fixed = {"chatzos": noon}
    for name, (alt, rising) in DEGREE_ZMANIM.items():
        def margin(t, alt=alt):
            return track.altaz(t)[0] - alt
        lo, hi = (noon - HALF_DAY, noon) if rising else (noon, noon + HALF_DAY)
        fixed[name] = find_crossings(margin, lo, hi, 1200.0, rising=rising, iterations=18)
    alos, tzais = fixed["alos_16_1"], fixed["tzais_16_1"]
    shaah_16 = (tzais - alos) / 12.0
    fixed["sof_zman_shma_mga_16_1"] = alos + 3 * shaah_16
    fixed["sof_zman_tfila_mga_16_1"] = alos + 4 * shaah_16

    out = {"fixed": fixed, "variants": {}}
    for variant in variants:
        if variant == "sea_level":
            margin, step, iters = _limb_margin(track, physics, sea_level_threshold_deg(physics)), 1200.0, 18
        elif variant == "elevation":
            margin, step, iters = _limb_margin(track, physics, sea_horizon_threshold_deg(height_m, physics)), 1200.0, 18
        elif variant == "visible":
            if profile is None:
                continue
            margin, step, iters = _profile_margin(track, physics, profile), 20.0, 12
        else:
            raise ValueError(f"unknown variant {variant!r}")
        rise, set_ = _rise_set(noon, margin, step, iters)
        out["variants"][variant] = derived(rise, set_)
        out["variants"][variant]["sunrise_azimuth"] = _azimuth(track, rise)
        out["variants"][variant]["sunset_azimuth"] = _azimuth(track, set_)
    return out
