"""Refraction and horizon thresholds. A threshold is the true altitude the sun's upper limb must pass to be seen; see docs/method.md."""
from __future__ import annotations

import numpy as np

from .config import Physics

EARTH_RADIUS_M = 6_371_008.8


def bennett_arcmin(apparent_alt_deg):
    h = np.asarray(apparent_alt_deg, dtype=float)
    return 1.0 / np.tan(np.radians(h + 7.31 / (h + 4.4)))


_BENNETT_0 = float(bennett_arcmin(0.0))


def density_factor(height_m):
    """Standard-atmosphere air density relative to sea level."""
    h = np.clip(np.asarray(height_m, dtype=float), -500.0, 11_000.0)
    t = 288.15 - 0.0065 * h
    return (t / 288.15) ** 5.25588 * 288.15 / t


def refraction_deg(apparent_alt_deg, height_m, physics: Physics):
    # Bennett scaled so that 0 deg at sea level equals the configured horizontal refraction.
    r = bennett_arcmin(apparent_alt_deg) * (physics.horizon_refraction_arcmin / _BENNETT_0) / 60.0
    if physics.density_scaling:
        r = r * density_factor(height_m)
    return r


def threshold_deg(e_deg, gamma_deg, obstacle_height_m, physics: Physics, earth_radius_m=EARTH_RADIUS_M):
    """Threshold for an obstacle at apparent elevation e and central angle gamma."""
    k = physics.terrestrial_k
    e = np.asarray(e_deg, dtype=float)
    gamma = np.asarray(gamma_deg, dtype=float)
    height = np.asarray(obstacle_height_m, dtype=float)
    e_p = e + gamma * (1.0 - k)
    # A ray still descending past the obstacle continues to where it is level.
    descending = e_p < 0
    extra = np.where(descending, -e_p / (1.0 - k), 0.0)
    drop = np.radians(np.where(descending, e_p, 0.0)) ** 2 * earth_radius_m / (2.0 * (1.0 - k))
    height = np.where(descending, np.maximum(0.0, height - drop), height)
    e_p = np.maximum(e_p, 0.0)
    return e_p - refraction_deg(e_p, height, physics) - (gamma + extra)


def sea_level_threshold_deg(physics: Physics):
    return -physics.horizon_refraction_arcmin / 60.0


def sea_horizon_threshold_deg(eye_height_m, physics: Physics, earth_radius_m=EARTH_RADIUS_M):
    """Sea horizon seen from eye_height_m (the "elevation" variant)."""
    h = max(float(eye_height_m), 0.0)
    gamma = np.degrees(np.sqrt(2.0 * h / (earth_radius_m * (1.0 - physics.terrestrial_k))))
    return -physics.horizon_refraction_arcmin / 60.0 - gamma
