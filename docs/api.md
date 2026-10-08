# API

`visible_zmanim.api.handle(params)` takes a dict of parameters and returns `(http_status, body)`. Over HTTP the same parameters work as a query string or as a JSON body.

## Parameters

| Name | Default | Meaning |
|---|---|---|
| `lat`, `lon` | | Location in decimal degrees (WGS84). |
| `address` | | Used when `lat`/`lon` are missing. Street addresses are most precise; a bare city name resolves to its center. |
| `date` | today at the location | `YYYY-MM-DD`. |
| `start`, `end` | | A date range instead of `date`, up to 366 days. |
| `height` | `1.7` | Eye height above the ground in meters. Use it for an upper floor or a roof. |
| `ground` | from terrain data | Ground elevation in meters above sea level, if you know it better than the elevation data. |
| `min_distance` | `100` | Ignore terrain closer than this many meters. Very close ground (a slope across the street) usually shouldn't count. Set `0` to include everything. |
| `variants` | all | Comma list of `sea_level`, `elevation`, `visible`. Leaving out `visible` skips the horizon calculation. `sea_level` alone skips terrain data entirely. |
| `fields` | all | Comma list of zman names to keep (for example `sunrise,sunset,sof_zman_shma_gra`). Applies to every section. In a JSON body it can also be a list. |
| `tz` | from the location | IANA time zone for the output times. |
| `refraction` | `34` | Horizontal refraction in arcminutes. |
| `k` | `0.13` | Terrestrial refraction coefficient. |
| `wait` | `15` | Seconds to wait for a new horizon before answering 202 (max 20). |
| `include` | | `horizon` adds the skyline and the sun's path around visible sunrise and sunset (single date only), for drawing. |

## Status codes

| Code | Meaning |
|---|---|
| 200 | Done. |
| 202 | The terrain horizon for this location is being computed (the first request in a new area downloads terrain data). `sea_level`, `elevation` and `fixed` are already filled in; `visible` is `{"status": "computing"}`. Retry the same request after `retry_after_s` seconds (also sent as a `Retry-After` header). |
| 400 | Bad parameters. `error` says which. |
| 404 | Address not found. |
| 500 | Horizon computation failed. It is retried after 10 minutes. |
| 503 | A data source (address lookup or terrain) is temporarily unreachable. Retry in a minute. |

## Response

```json
{
  "location": {
    "lat": 40.094099, "lon": -74.214989, "timezone": "America/New_York",
    "ground_m": 20.81, "ground_source": "usgs_3dep_1m",
    "height_m": 1.7, "eye_elevation_m": 22.51,
    "address": {"query": "...", "matched": "...", "source": "nominatim", "precision": "house"}
  },
  "settings": {"variants": ["sea_level", "elevation", "visible"], "min_distance_m": 100, "refraction_arcmin": 34, "terrestrial_k": 0.13},
  "days": [
    {
      "date": "2026-10-07",
      "fixed": {"chatzos": "...", "alos_16_1": "...", "...": "..."},
      "sea_level": {"sunrise": "2026-10-07T06:59:15-04:00", "sunset": "...", "...": "..."},
      "elevation": {"...": "..."},
      "visible": {
        "sunrise": "...", "sunset": "...", "...": "...",
        "sunrise_details": {
          "blocking": {"lat": 40.087578, "lon": -74.141336, "distance_m": 6323, "height_m": 18.4, "angle_deg": -0.062, "source": "usgs_3dep_13"},
          "vs_sea_level_s": -22
        },
        "sunset_details": {"...": "..."}
      }
    }
  ],
  "sources": {"...": "attribution for the data used"},
  "version": "0.1.0"
}
```

`address` only appears when an address was given. `ground_m` and `ground_source` are `null` when only `sea_level` was requested, since no terrain is looked up. Times are ISO 8601 in the location's time zone, rounded to the second. A zman that doesn't happen on that day (the sun never sets, or never gets 16.1° below the horizon) is `null`.

### `fixed`

Times that don't depend on the horizon, so they are the same in every version.

| Field | Definition |
|---|---|
| `chatzos` | Solar transit: the sun crosses the local meridian. |
| `alos_16_1` | Sun's center 16.1° below the horizon in the morning. |
| `tzais_16_1` | Same in the evening. |
| `tzais_8_5` | Sun's center 8.5° below the horizon in the evening. |
| `sof_zman_shma_mga_16_1` | 3 hours of a day running from `alos_16_1` to `tzais_16_1`. |
| `sof_zman_tfila_mga_16_1` | 4 hours of that day. |

### `sea_level`, `elevation`, `visible`

The same fields in each, computed from that version's sunrise and sunset. A GRA hour is (sunset − sunrise) / 12; an MGA hour is the same from `alos_72` to `tzais_72`.

| Field | Definition |
|---|---|
| `sunrise`, `sunset` | First and last moment the sun's upper edge is visible. |
| `alos_72`, `tzais_72` | 72 minutes before sunrise and after sunset. |
| `sof_zman_shma_gra`, `sof_zman_tfila_gra` | 3 and 4 GRA hours after sunrise. |
| `sof_zman_shma_mga_72`, `sof_zman_tfila_mga_72` | 3 and 4 MGA hours after `alos_72`. |
| `mincha_gedola_gra`, `mincha_ketana_gra` | 6.5 and 9.5 GRA hours after sunrise. |
| `plag_hamincha_gra` | 10.75 GRA hours after sunrise. |
| `candle_lighting` | 18 minutes before sunset. |
| `shaah_zmanis_gra_s`, `shaah_zmanis_mga_72_s` | Length of the hours in seconds. |
| `sunrise_azimuth`, `sunset_azimuth` | Compass direction of the sun at those moments, degrees from true north. |

`visible` also has `sunrise_details` and `sunset_details`. `blocking` is the terrain point the sun clears in that direction: its position, distance, height above sea level, the angle it appears at from the observer, and which dataset the height came from. The source is one of `usgs_3dep_1m`, `usgs_3dep_13`, `ea_lidar_1m`, `gedtm30`, or `sea_level` when nothing in range is higher than the sea horizon and the sea horizon itself is reported. `vs_sea_level_s` is the difference from the `sea_level` time in seconds.

### `horizon`

Only with `include=horizon` and a single date. For each of `sunrise` and `sunset`:

| Field | Meaning |
|---|---|
| `azimuth_start`, `azimuth_step` | The skyline covers 24° centered on the sun's direction at the event, in 0.1° steps. |
| `terrain_deg` | Apparent angle of the skyline above the flat horizon for each step (negative when looking down). |
| `sun_path` | `time`, `azimuth` and `altitude_deg` of the sun's center every 30 s for 40 minutes either side of the event, as it appears through the same refraction the calculation uses. |
| `sun_radius_deg` | The sun's apparent radius. |

At the event time the sun's top edge (`altitude_deg + sun_radius_deg`) is exactly on the skyline.

## Differences from KosherJava

* Candle lighting follows each version's own sunset. KosherJava always uses sea-level sunset.
* The `elevation` dip includes terrestrial refraction, so it is about 7% larger than KosherJava's `acos(R / (R + h))`, about 20 s at 800 m.
* Near the start and end of the midnight sun, KosherJava can report a sunset after solar midnight because it uses the sun's declination at noon for the whole day. This package follows the sun continuously and reports no sunset on those days.
