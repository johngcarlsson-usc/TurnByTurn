"""Bay Area express-lane toll prices from 511 SF Bay's toll API (key: FT_SF511_API_KEY).

Express-lane tolls are set from how full the lanes are, so the price posted at
each toll sign is a live congestion signal on I-680, I-880, I-80 and US-101
(San Mateo): about 130 signs and 180 sign-to-destination prices. The feed's
"travel-time" field is 0 everywhere (checked 2026-10-31), and tolls run on
weekdays only (prices then stay at their last value; "last-updated" says when).
Logged only for now (run.HISTORY_DIR, "toll-YYYY-MM-DD.jsonl.gz"), as a
feature for the learned models.

511 allows 60 requests an hour per key; with transit (30) and two loops'
events (24) this polls every 15 minutes (4 an hour).
"""

import os

import requests

from . import _load_local_env

URL = "https://api.511.org/toll/programs"


def poll():
    """{"sign|destination": [price, lat, lon, last-updated, program]}."""
    _load_local_env()
    response = requests.get(URL, params={"api_key": os.environ["FT_SF511_API_KEY"], "format": "json"}, timeout=60)
    response.raise_for_status()
    response.encoding = "utf-8-sig"  # 511 starts its JSON with a byte-order mark
    out = {}
    for program in response.json()["toll-programs"]:
        name = program["toll-authority-info"]["toll-program-name"]
        for sign in program.get("toll-signs") or []:
            lon, lat = sign["geography"]["coordinates"]
            for dest in sign.get("toll-destinations") or []:
                rates = dest.get("toll-rates") or []
                if rates:
                    out[f'{sign["id"]}|{dest["id"]}'] = [rates[0]["toll-price"], lat, lon,
                                                        rates[0]["last-updated"], name]
    return out, {"prices": len(out)}
