# How it works

This page explains every calculation in the package, from the sun's position to the terrain horizon. No astronomy background is assumed.

## 1. Where the sun is

The sun's position comes from [Skyfield](https://rhodesmill.org/skyfield/) using NASA JPL's DE440s ephemeris, the same data used for spacecraft navigation. For a place and a moment it gives the sun's **altitude** (degrees above a flat horizon, negative below it) and **azimuth** (compass direction, 0° north, 90° east). These are the true geometric positions; refraction is added separately (section 3).

Skyfield applies light travel time, aberration, nutation and the observer's exact position on the Earth, which makes each position slow to compute. Since the sun moves very smoothly, `sun.Track` asks Skyfield for exact positions once an hour and interpolates in between with a cubic spline. The interpolation error is below 0.00001°, far smaller than anything else here, and it makes positions about 200 times faster.

**Chatzos** is the moment the sun crosses the local meridian (its hour angle is zero). This is the "astronomical chatzos" KosherJava uses by default, not the midpoint between sunrise and sunset.

## 2. What sunrise means

Sunrise is the moment the **top edge** of the sun first becomes visible; sunset is the moment it disappears. Two things make this different from "the sun's center at altitude 0":

* **The sun's size.** Its radius in the sky is about 0.27°. It changes slightly through the year with the Earth-Sun distance (largest in January) and is computed from that distance, the way KosherJava's date-based table does.
* **Refraction.** The atmosphere bends light coming in at a low angle, lifting the sun's image. At the horizon the lift is about 0.57°, more than the sun's whole width.

So at sea-level sunrise the sun's center is really about 0.83° below the horizon. This is KosherJava's standard definition (34′ refraction plus the sun's radius), and the `sea_level` version reproduces it.

## 3. The threshold: one formula for all three versions

The package describes every horizon with one number per direction, the **threshold**: the true altitude the sun's top edge must reach for its light to clear the horizon and reach the observer. Sunrise is when the top edge's true altitude rises past the threshold.

For a flat sea-level horizon, the threshold is minus the horizontal refraction, -34′. That is the KosherJava definition.

For anything else, the light ray is followed backwards from the observer:

1. The ray leaves the observer at the apparent angle `e` of an obstacle (a mountain top, or the sea horizon).
2. Near the ground it bends slightly downward. Its curvature is `k / R`, where `R` is the Earth's radius and `k` is the terrestrial refraction coefficient (0.13, the usual surveying value). By the time it reaches the obstacle, at a central angle `γ` (distance divided by the Earth's radius), its angle above the obstacle's own horizontal is `e_p = e + γ(1 − k)`.
3. From the obstacle it climbs out of the atmosphere and is bent by ordinary astronomical refraction `R(e_p)`. That depends on the angle (less refraction at higher angles) and on the air density at the obstacle's height (thinner air bends less).
4. Turning back to the observer's frame gives:

```
threshold = e_p − R(e_p) − γ
```

If `e_p` is negative, the ray is still going down when it passes the obstacle, so it continues to the point where it is level (over lower ground or the sea) before rising. The formula is applied at that point.

This one formula covers all three versions:

* **Sea level:** the obstacle is the horizon right at the observer, so `e = 0`, `γ = 0`, and the threshold is `−R(0) = −34′`.
* **Elevation:** an observer at height `h` looking at a sea horizon. The tangent point is at `γ = √(2h / (R(1 − k)))`, which gives `threshold = −34′ − √(2h / (R(1 − k)))`. KosherJava uses `acos(R / (R + h))`, which leaves out terrestrial refraction. The difference is about 7%, or about 20 seconds of sunrise at 800 m.
* **Visible:** the real terrain, as described next.

Refraction uses Bennett's formula, scaled so that 0° at sea level gives exactly the configured horizontal refraction (34′), and multiplied by the standard-atmosphere air density at the height where the ray leaves the terrain.

## 4. The terrain horizon

`horizon.compute_profile` builds the threshold for every direction around the observer:

* **Directions.** Rays every 0.05°, so about ten rays cross the sun's disk. Only directions where the sun can rise or set during the year are traced, plus a 25° margin for a sun climbing over high terrain. Above 60° latitude the whole circle is traced.
* **Distances.** Each ray is sampled from `min_distance` (default 100 m) out to 200 km. The step grows with distance: 2 m up close, then 0.2% of the distance. Far away, the lateral gap between rays is already bigger than that, and small details there don't change the angle.
* **Positions.** Points along each ray follow the true geodesic on the WGS84 ellipsoid. Exact points are computed every 5 km and the rest are interpolated, with under a meter of error.
* **Earth's curvature.** A point at distance `d` appears lower by `d² (1 − k) / (2R)`, where `R` is the Earth's radius of curvature in that direction (it differs slightly between north-south and east-west).
* **The result.** The threshold is computed for every sample and the highest one on each ray is kept, together with the point that caused it (position, distance, height, angle and dataset). Nothing beyond 200 km is assumed to be below sea level, so the sea horizon is a lower limit.

The profile is saved per point and settings, so the same address is instant the next time.

## 5. When the sun clears the horizon

For each day, the solver steps through the half day before chatzos (for sunrise) and after it (for sunset), 20 seconds at a time. At each step it checks whether any part of the sun's disk is above the threshold, testing every ray that crosses the disk. It then narrows down the crossing to about 0.01 s.

* **Sunrise** is the first moment any part of the disk is visible. If the sun appears in a gap between two peaks and then hides behind the next one, the first appearance counts.
* **Sunset** is the last moment any part of it is visible.

Because the threshold is looked up in the direction the sun actually is at each moment, the sun's sideways movement along the horizon is handled. That movement matters a lot: at 41° north the sun moves about 4° sideways while it climbs 5°.

## 6. Terrain data

All data is bare earth (a terrain model, not a surface model), so trees and buildings are not included.

| Distance | US and border areas | Everywhere else |
|---|---|---|
| up to 3 km | USGS 3DEP 1 m lidar | GEDTM30 |
| 3 to 50 km | USGS 3DEP 1/3 arc-second (~10 m) | GEDTM30 |
| beyond 50 km | GEDTM30 | GEDTM30 |

Where a dataset has no data (open sea, across a border), the next one fills in. Where none has data, the height is 0 (sea level).

The files are read straight from their public cloud copies, one block at a time, at the coarsest zoom level that is still fine enough for the distance (about 0.2% of the distance). Far terrain therefore costs little. Blocks are stored as compressed whole centimeters relative to each block's lowest point, which is accurate to 5 mm and about 4.5 times smaller than raw. Stored blocks are kept in two places: `pinned` (regions downloaded ahead of time, never deleted) and `cache` (everything else, oldest removed when the cache passes its size limit).

Accuracy of the data, roughly: lidar to about 10 cm, the 1/3 arc-second data to about 1 to 2 m, GEDTM30 to about 4 m. A height error matters most up close: 3 m of error at 2 km shifts sunrise by about 25 seconds at 41° north, and the same error at 20 km by about 3 seconds.

## 7. Zmanim

From each version's sunrise and sunset, the GRA hour is `(sunset − sunrise) / 12`. The MGA hour uses alos and tzais 72 minutes before sunrise and after sunset instead. Sof zman shma, tefila, mincha gedola, mincha ketana and plag are 3, 4, 6.5, 9.5 and 10.75 hours into the day, as in KosherJava's `getSofZmanShmaGRA()` and related methods. Degree-based times (alos 16.1°, tzais 8.5°, tzais 16.1°) use the sun's true geometric center with no refraction and no radius, as KosherJava does, and they don't depend on the horizon.

## 8. Limits

* **Weather.** Refraction near the horizon changes with temperature layers in the air. Cold air near the ground (common on clear winter mornings, and over water or snow) can delay a real sunset or advance a sunrise by tens of seconds. The model uses the standard atmosphere and can't know the day's weather.
* **Terrain data.** Outside the US the 30 m global model can smooth sharp ridges and miss a few meters of height.
* **Nearby ground.** Terrain closer than `min_distance` is ignored. Set it to 0 to include everything, keeping in mind that a sidewalk slope can then decide the result.
* **The observer's own height.** It comes from the terrain data at the given point. For an upper floor, add the height with `height`.
