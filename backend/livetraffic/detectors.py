"""Score every logged live source against Caltrans loop detectors (PeMS), the day after.

PeMS posts each day's station_5min file the next day (checked 2026-10-31: Friday's
file was up Saturday afternoon). That makes the detectors free ground truth for
freeways, independent of Google: for every logged cycle of a source, each
mainline detector's own speed in the same 5 minutes is compared with the speed
the source implies on the detector's road segment (the source's ratio times the
detector's own free-flow speed, its 1-5 AM median). Baselines on the same
points: the PeMS time-of-week prior (what Valhalla uses with no live data)
and plain free flow (no traffic at all).

Detectors are matched to the nearest catalog mainline edge on the same freeway
and direction within MAX_MATCH_M. Message-sign ratios use the same rule as
caltrans_cms.poll (shortest corridor first; references from all logged history,
so slightly optimistic until there are many days of it).

    venv/bin/python -m livetraffic.detectors 2026-10-30        # downloads that day's file if needed
"""

import gzip
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from . import caltrans_cms, catalog, pems
from .chp import DIRECTIONS, _bearing, _local_m
from .run import HISTORY_DIR

PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_MATCH_M = 300
CONGESTED = 0.75            # detector speed under 75% of its free flow
GOOGLE_RATIO = {1: 1.0, 2: 0.755, 3: 0.446, 4: 0.2}  # run.py's calibration on 2026-10-30 (red-dark not calibrated)
NIGHT_HOURS = range(1, 5)
UNUSUAL_MPH = 10


def station_edges(edges, mainline):
    """{station: edge id}: the nearest mainline edge of the same freeway and direction (pems.assign's rule)."""
    by_fwy = defaultdict(list)
    for edge in edges:
        if edge["road_class"] in {"motorway", "trunk"} and edge.get("use") == "road":
            for fwy, signed in pems._fwy_signed(edge["names"]).items():
                by_fwy[fwy].append((edge, signed))
    out = {}
    for sid, st in mainline.items():
        best = None
        for edge, signed in by_fwy.get(st["fwy"], []):
            if signed and st["dir"] != signed:
                continue
            shape = edge["shape"]
            d = min(math.hypot(*_local_m(st["lat"], st["lon"], lat, lon)) for lat, lon in shape)
            if d > MAX_MATCH_M or (best and d >= best[0]):
                continue
            if not signed:
                travel = _bearing(_local_m(shape[0][0], shape[0][1], *shape[0]),
                                  _local_m(shape[0][0], shape[0][1], *shape[-1]))
                heading = DIRECTIONS[st["dir"]]
                if min(abs(travel - heading), 360 - abs(travel - heading)) >= 90:
                    continue
            best = (d, edge["id"])
        if best:
            out[sid] = best[1]
    return out


def truth(day, mainline):
    """{station: {minute of day (multiple of 5): mph}} from that day's station_5min file."""
    name = f"d{pems.DISTRICT:02d}_text_station_5min_{day.replace('-', '_')}.txt.gz"
    path = os.path.join(pems.DATA_DIR, name)
    if not os.path.exists(path):
        pems.download([day], kind="station_5min")
    out = defaultdict(dict)
    with gzip.open(path, "rt") as h:
        for line in h:
            f = line.split(",", 12)
            if f[1] in mainline and f[11] and f[8] and float(f[8]) >= pems.MIN_OBSERVED_PCT:
                out[f[1]][int(f[0][11:13]) * 60 + int(f[0][14:16])] = float(f[11])
    return out


def cycles(day):
    """[(source, local datetime, {edge id: ratio})] for every logged cycle that falls on this local day."""
    out = []
    utc_days = {day, (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d")}
    refs = caltrans_cms.references(HISTORY_DIR)
    corridors = {}
    if os.path.exists(caltrans_cms.CORRIDORS):
        with open(caltrans_cms.CORRIDORS) as h:
            corridors = json.load(h)
    for source in ("tomtom", "google", "cms"):
        for utc_day in sorted(utc_days):
            path = os.path.join(HISTORY_DIR, f"{source}-{utc_day}.jsonl.gz")
            if not os.path.exists(path):
                continue
            with gzip.open(path, "rt") as h:
                for line in h:
                    record = json.loads(line)
                    local = datetime.fromisoformat(record["at"]).astimezone(PACIFIC)
                    if local.strftime("%Y-%m-%d") != day:
                        continue
                    if source == "tomtom":
                        ratios = {int(k): v for k, v in record["values"].items() if isinstance(v, float)}
                    elif source == "google":
                        levels = record.get("levels") or record.get("values") or {}  # older lines say "levels"
                        ratios = {int(k): GOOGLE_RATIO[v] for k, v in levels.items() if v in GOOGLE_RATIO}
                    else:
                        ratios = {}
                        shown = {k: (v["min"] if isinstance(v, dict) else v) for k, v in record["values"].items()}
                        for k, minutes in sorted(shown.items(), key=lambda kv: refs.get(kv[0], 1e9)):
                            if k in refs and corridors.get(k):
                                r = 1.0 if minutes - refs[k] <= 1 else max(0.05, min(1.0, refs[k] / minutes))
                                for e in corridors[k]:
                                    ratios.setdefault(e, r)
                    out.append((source, local, ratios))
    return out


def report(name, rows):
    """rows: (truth mph, estimate mph, prior mph, free flow mph)."""
    if not rows:
        print(f"{name:>8}: no overlapping points")
        return
    a = np.array(rows)
    t = a[:, 0]
    jam = t < CONGESTED * a[:, 3]
    line = lambda est, sel: f"{np.mean(np.abs(est[sel] - t[sel])):5.1f} mph" if sel.any() else "   -"
    print(f"{name:>8}: {len(a):7d} points ({jam.sum():6d} congested) | mean abs error all / congested: "
          f"source {line(a[:, 1], ~np.zeros(len(a), bool))} / {line(a[:, 1], jam)}, "
          f"prior {line(a[:, 2], ~np.zeros(len(a), bool))} / {line(a[:, 2], jam)}, "
          f"free flow {line(a[:, 3], ~np.zeros(len(a), bool))} / {line(a[:, 3], jam)}")
    if jam.any():
        caught = (a[jam, 1] < CONGESTED * a[jam, 3]).mean()
        false = (a[~jam, 1] < CONGESTED * a[~jam, 3]).mean() if (~jam).any() else float("nan")
        print(f"{'':>8}  congestion caught {caught:.0%}, false alarms on free-flowing points {false:.0%}")
    odd = np.abs(t - a[:, 2]) > UNUSUAL_MPH  # the prior is off: where live data has to earn its keep
    if odd.any():
        blend = (a[:, 1] + a[:, 2]) / 2
        print(f"{'':>8}  unusual for the hour (prior off by {UNUSUAL_MPH}+ mph, {odd.sum()} points): source "
              f"{line(a[:, 1], odd)}, prior {line(a[:, 2], odd)}, mean of the two {line(blend, odd)}; "
              f"mean of the two on all points {line(blend, ~np.zeros(len(a), bool))}")


SETUP = os.path.join(pems.DATA_DIR, "detector-setup.json")


def setup():
    """(mainline, {station: 168 prior mph}, {station: free-flow mph}, {station: edge id}) for every mainline
    detector with a full-week profile and a matched edge. Cached in SETUP (delete it after a new PeMS download
    or a tile rebuild)."""
    mainline = pems.stations()
    if os.path.exists(SETUP):
        with open(SETUP) as h:
            c = json.load(h)
        return mainline, {k: np.array(v, dtype=float) for k, v in c["profs"].items()}, c["freeflow"], \
            {k: int(v) for k, v in c["edge_of"].items()}
    profs = pems.profiles(mainline)
    freeflow = {}
    for sid, prof in profs.items():
        night = [prof[d * 24 + h] for d in range(7) for h in NIGHT_HOURS if np.isfinite(prof[d * 24 + h])]
        if night:
            freeflow[sid] = float(np.median(night))
    edge_of = station_edges(catalog.load(), {s: mainline[s] for s in freeflow})
    profs = {s: profs[s] for s in edge_of}
    freeflow = {s: freeflow[s] for s in edge_of}
    with open(SETUP + ".tmp", "w") as h:
        json.dump({"profs": {k: [None if not np.isfinite(x) else float(x) for x in v] for k, v in profs.items()},
                   "freeflow": freeflow, "edge_of": edge_of}, h)
    os.replace(SETUP + ".tmp", SETUP)
    return mainline, profs, freeflow, edge_of


def main(day):
    mainline, profs, freeflow, edge_of = setup()
    speeds = truth(day, mainline)
    by_edge = defaultdict(list)
    for sid, e in edge_of.items():
        if sid in speeds:
            by_edge[e].append(sid)
    print(f"{day}: {len(speeds)} detectors with data, {sum(map(len, by_edge.values()))} matched to a catalog edge "
          f"with a profile")
    rows = defaultdict(list)
    for source, local, ratios in cycles(day):
        minute = local.hour * 60 + local.minute // 5 * 5
        how = ((local.weekday() + 1) % 7) * 24 + local.hour
        for e, r in ratios.items():
            for sid in by_edge.get(e, ()):
                t = speeds[sid].get(minute)
                prior = profs[sid][how]
                if t is not None and np.isfinite(prior):
                    rows[source].append((t, r * freeflow[sid], prior, freeflow[sid]))
    for source in ("tomtom", "google", "cms"):
        report(source, rows[source])


if __name__ == "__main__":
    main(sys.argv[1])
