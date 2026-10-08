import datetime as dt

from visible_zmanim.api import handle


def test_bad_types_are_400():
    for params in ({"lat": [1], "lon": "0"}, {"lat": "0", "lon": "0", "date": 20261007}, {"address": 5}, {"lat": "0", "lon": "0", "tz": 3}):
        status, body = handle(params)
        assert status == 400, (params, status, body)


def test_date_means_the_local_calendar_day():
    # Samoa and Kiritimati keep time zones about a day away from what their longitude suggests.
    for lat, lon in ((-13.83, -171.76), (1.87, -157.36), (52.9, 173.1)):
        status, body = handle({"lat": str(lat), "lon": str(lon), "date": "2026-10-07", "variants": "sea_level"})
        assert status == 200, body
        day = body["days"][0]
        for key in ("sunrise", "sunset"):
            assert day["sea_level"][key].startswith("2026-10-07"), (lat, lon, key, day["sea_level"][key])
        assert body["location"]["ground_m"] is None


def test_today_by_default():
    status, body = handle({"lat": "40.0941", "lon": "-74.2150", "variants": "sea_level"})
    assert status == 200 and dt.date.fromisoformat(body["days"][0]["date"])
