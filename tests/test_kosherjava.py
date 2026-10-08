import datetime as dt
import json
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from visible_zmanim.config import DEFAULT_PHYSICS
from visible_zmanim.zmanim import compute

REF = Path(__file__).parent / "data" / "kosherjava_reference.jsonl"
# KosherJava's elevation formula: straight rays, no density scaling.
KJ_PHYSICS = DEFAULT_PHYSICS.with_(terrestrial_k=0.0, density_scaling=False)

SEA_LEVEL_KEYS = {
    "sea_level_sunrise": "sunrise",
    "sea_level_sunset": "sunset",
    "alos_72": "alos_72",
    "tzais_72": "tzais_72",
    "sof_zman_shma_gra": "sof_zman_shma_gra",
    "sof_zman_shma_mga_72": "sof_zman_shma_mga_72",
    "sof_zman_tfila_gra": "sof_zman_tfila_gra",
    "sof_zman_tfila_mga_72": "sof_zman_tfila_mga_72",
    "mincha_gedola_gra": "mincha_gedola_gra",
    "mincha_ketana_gra": "mincha_ketana_gra",
    "plag_hamincha_gra": "plag_hamincha_gra",
    "candle_lighting": "candle_lighting",
}
FIXED_KEYS = ["chatzos", "alos_16_1", "tzais_16_1", "tzais_8_5", "sof_zman_shma_mga_16_1", "sof_zman_tfila_mga_16_1"]


def load(calculator):
    groups = defaultdict(list)
    for line in REF.read_text().splitlines():
        row = json.loads(line)
        if row["calculator"] == calculator:
            groups[row["place"]].append(row)
    return groups


def ts(value):
    return np.nan if value is None else dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def diffs(calculator):
    out = defaultdict(list)
    for place, rows in load(calculator).items():
        r0 = rows[0]
        dates = [dt.date.fromisoformat(r["date"]) for r in rows]
        res = compute(r0["lat"], r0["lon"], r0["elevation"], dates, physics=KJ_PHYSICS, variants=("sea_level", "elevation"), tz=ZoneInfo(r0["zone"]))
        sea, elev, fixed = res["variants"]["sea_level"], res["variants"]["elevation"], res["fixed"]
        for i, r in enumerate(rows):
            pairs = [(k, sea[v][i]) for k, v in SEA_LEVEL_KEYS.items()]
            pairs += [("elevation_sunrise", elev["sunrise"][i]), ("elevation_sunset", elev["sunset"][i])]
            pairs += [(k, fixed[k][i]) for k in FIXED_KEYS]
            for key, ours in pairs:
                theirs = ts(r[key])
                if np.isnan(ours) and not np.isnan(theirs) and abs(theirs - fixed["chatzos"][i]) > 12 * 3600:
                    # KosherJava's noon-declination shortcut can report an event past solar midnight at polar edges.
                    continue
                assert np.isnan(ours) == np.isnan(theirs), (place, r["date"], key, ours, theirs)
                if not np.isnan(ours):
                    out[key].append((abs(ours - theirs), place, r["date"]))
    return out


@pytest.mark.parametrize("calculator,limit", [("spa", 1.0), ("noaa", 10.0)])
def test_matches_kosherjava(calculator, limit):
    worst = {k: max(v) for k, v in diffs(calculator).items()}
    bad = {k: w for k, w in worst.items() if w[0] > limit}
    assert not bad, bad
