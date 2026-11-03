"""Daily catch-up, run by a timer: everything that needs a finished day.

1. Bus observations (bus_probes.build) for every logged UTC day before today that has none.
2. For every local day with logged history before today whose PeMS station_5min file is
   posted (the next day): score the sources against the detectors (detectors.main), once.
3. If a new day was scored: retrain and score the fusion model on all scored days,
   full and open features (held-out whole days once there are two).
Output goes to stdout (the timer appends it to ~/geo/livetraffic/nightly.log); each
scored day leaves a marker in DONE_DIR so it is not redone.

    venv/bin/python -m livetraffic.nightly
"""

import contextlib
import glob
import io
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import bus_probes, detectors, fusion
from .run import HISTORY_DIR

PACIFIC = ZoneInfo("America/Los_Angeles")
DONE_DIR = os.path.expanduser("~/geo/livetraffic/daily")


def _days(prefix):
    return sorted({re.search(r"(\d{4}-\d\d-\d\d)", p).group(1)
                   for p in glob.glob(os.path.join(HISTORY_DIR, f"{prefix}-2*.jsonl.gz"))})


def main():
    print(f"=== nightly {datetime.now(PACIFIC):%Y-%m-%d %H:%M %Z}", flush=True)
    utc_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for day in _days("transit"):
        if day < utc_today and not os.path.exists(os.path.join(bus_probes.OUT_DIR, f"obs-{day}.jsonl.gz")):
            bus_probes.build(day)
    os.makedirs(DONE_DIR, exist_ok=True)
    local_today = datetime.now(PACIFIC).strftime("%Y-%m-%d")
    # local days with history: a UTC-named file holds that local day (its first 7-8 hours are the evening before,
    # which only matters for the very first file)
    days = sorted(set(_days("tomtom") + _days("cms") + _days("cctv")))
    new = False
    for day in days:
        marker = os.path.join(DONE_DIR, f"detectors-{day}.txt")
        if day >= local_today or os.path.exists(marker):
            continue
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                detectors.main(day)
        except FileNotFoundError:  # PeMS has not posted that day yet
            print(f"{day}: PeMS file not posted yet", flush=True)
            continue
        with open(marker, "w") as h:
            h.write(out.getvalue())
        print(out.getvalue(), flush=True)
        new = True
    scored = sorted(re.search(r"(\d{4}-\d\d-\d\d)", p).group(1) for p in glob.glob(os.path.join(DONE_DIR, "detectors-*.txt")))
    if new and scored:
        for name, features in (("full", fusion.FULL), ("open", fusion.OPEN)):
            print(f"--- fusion, {name} features, days {scored[0]}..{scored[-1]} ({len(scored)})", flush=True)
            fusion.main(scored, features)


if __name__ == "__main__":
    main()
