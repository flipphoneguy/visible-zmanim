# visible-zmanim

Visible sunrise and sunset (netz and shkia) over real terrain, plus the zmanim built on them.

Standard zmanim calculators, including KosherJava, assume a flat horizon. Where hills or mountains block the sun, the sun you actually see rises later and sets earlier, sometimes by many minutes. This package traces the horizon around any point using bare-earth elevation data (1 m lidar where available) and finds the moment the first sliver of the sun appears and the last sliver disappears.

**Try it:** [tools.flipphoneguy.duckdns.org/zmanim](https://tools.flipphoneguy.duckdns.org/zmanim) has an easy page with address search, a map, horizon drawings and monthly tables. The same calculations are available as a public API at `https://api.flipphoneguy.duckdns.org/zmanim`, for example [`?lat=40.0941&lon=-74.2150`](https://api.flipphoneguy.duckdns.org/zmanim?lat=40.0941&lon=-74.2150) (parameters in [docs/api.md](docs/api.md)).

For every date it returns three versions of sunrise and sunset, each with its own derived zmanim:

| Version | What it assumes |
|---|---|
| `sea_level` | Flat horizon at sea level. Matches KosherJava's default `getSeaLevelSunrise()` to about 0.1 s. |
| `elevation` | The observer is at their real height and can see down to a sea-level horizon (KosherJava's elevation-adjusted sunrise, with a better dip formula). |
| `visible` | The real terrain around the observer. |

## Quick start

```sh
pip install git+https://github.com/flipphoneguy/visible-zmanim
visible-zmanim zmanim lat=40.0941 lon=-74.2150 date=2026-10-07
```

The first call in a new area downloads terrain data (a minute or so). After that a new point in the same area takes about 2 seconds and a point that was already computed is instant.

From Python:

```python
from visible_zmanim.api import handle

status, body = handle({"address": "Lakewood, NJ", "date": "2026-10-07"})
print(body["days"][0]["visible"]["sunrise"])
```

`handle` takes the same parameters as the HTTP API and returns `(http_status, json_body)`, so it drops into any web framework. See [docs/api.md](docs/api.md) for all parameters and the response format, and [docs/method.md](docs/method.md) for how everything is calculated.

## Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `VISIBLE_ZMANIM_DATA` | `~/.local/share/visible-zmanim` | Where the ephemeris, terrain and horizon caches live |
| `VISIBLE_ZMANIM_CACHE_GB` | `50` | Size cap of the evictable cache (pinned regions don't count) |
| `VISIBLE_ZMANIM_MEMORY_MB` | `1024` | Decoded terrain kept in memory per process |

Terrain is stored in two places under the data folder. `dem/pinned/` holds regions downloaded ahead of time with `visible-zmanim prefetch` and is never deleted. `dem/cache/` holds everything fetched on demand plus computed horizons, and the least recently used files are removed when it grows past the cap.

## Command line

```sh
visible-zmanim zmanim lat=31.778 lon=35.235 start=2026-10-01 end=2026-10-31
visible-zmanim regions                 # list predefined regions
visible-zmanim prefetch --plan israel  # count the blocks a region needs
visible-zmanim prefetch rockland nyc   # download them into the pinned store
visible-zmanim cache                   # cache size, and evict down to the cap
```

## Accuracy

Sun positions come from NASA JPL's DE440s ephemeris through Skyfield. The sea-level times are tested against KosherJava's SPA calculator on 192 place and date combinations (including the Arctic and high altitude) and agree to about 0.1 s.

The visible times are only as good as the terrain data and the atmosphere. Bare-earth lidar (US and England) is accurate to centimeters; the global 30 m model used elsewhere is accurate to a few meters, which matters most for ridges within a few kilometers. Refraction near the horizon changes with the weather and can move a real sunrise by tens of seconds, occasionally more over cold water or snow. Expect the visible times to be within about half a minute on a normal day. Published research comparing calculated and observed sunrises in Jerusalem found about ±15 s for most of the year with a similar method (Keller and Hall, Computers & Geosciences 161, 2022).

## Data sources

| Data | Used for | License |
|---|---|---|
| [JPL DE440s](https://ssd.jpl.nasa.gov/planets/eph_export.html) via [Skyfield](https://rhodesmill.org/skyfield/) | Sun position | Public domain / MIT |
| [USGS 3DEP](https://www.usgs.gov/3d-elevation-program) 1 m lidar DEM | Terrain within 3 km, US | Public domain |
| USGS 3DEP 1/3 arc-second DEM | Terrain within 50 km, US and border areas | Public domain |
| [Environment Agency LIDAR Composite DTM](https://environment.data.gov.uk/dataset/13787b9a-26a4-4775-8523-806d13af58fc) 1 m | Terrain within 3 km, England | Open Government Licence v3.0 |
| [GEDTM30](https://doi.org/10.5281/zenodo.14900181) v1.2 (OpenGeoHub) | Terrain everywhere else | CC BY 4.0 |
| [Nominatim](https://nominatim.org/) | Address lookup | Data © OpenStreetMap contributors, ODbL |
| [US Census Geocoder](https://geocoding.geo.census.gov/) | US street addresses | Public domain |

Nothing is bundled; data is downloaded on first use. Nominatim allows one request per second and results are cached.

## Tests

```sh
pip install -e '.[test]'
pytest
```

`tests/data/kosherjava_reference.jsonl` is generated from a KosherJava checkout with `tools/kosherjava_reference/generate.sh`.

## License

GPL-3.0. See [LICENSE](LICENSE).
