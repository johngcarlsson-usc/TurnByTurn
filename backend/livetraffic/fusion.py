"""Learned fusion: every logged source -> current speed at each freeway detector (LightGBM).

The label is what Caltrans loop detectors measured (PeMS station_5min, posted the
next day; see detectors.py), so the model learns from public ground truth and
never from Google. One row per detector per 5 minutes; features are what was
known live at the end of those 5 minutes (each source's latest logged cycle,
if fresh enough):
- the time-of-week prior (PeMS, as Valhalla uses it), free flow, hour of week;
- open: message-sign corridor ratio on the detector's segment and that
  corridor's free-flow minutes; the nearest camera on the same freeway
  (both direction groups' speed against the camera's own fast speed, density,
  stopped vehicles, distance); the nearest express-lane toll on the same
  freeway and direction (price, and price above that sign's lowest);
  incident effects (CHP, 511, Caltrans closures) on the segment;
- full adds TomTom's ratio and Google's color on the segment.
Scored against the prior alone on held-out data: whole days left out once
there are two or more days, else hour blocks of the one day (optimistic).

    venv/bin/python -m livetraffic.fusion 2026-10-30 2026-10-31 [--features open|full]
"""

import argparse
import gzip
import json
import math
import os
import re
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from . import caltrans_cms, detectors
from .chp import _local_m
from .run import HISTORY_DIR

PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_AGE_S = {"tomtom": 1200, "google": 600, "cms": 360, "cctv": 600, "toll": 1800, "chp": 360, "sf511": 600,
             "lcs": 600}
NEAR_M = 3000                  # cameras and toll signs this close on the same freeway count
CAMERA_FAST_PCT = 85           # a camera group's "free flow": this percentile of its logged speeds
OPEN = ["prior", "freeflow", "hourOfWeek", "weekend", "cms", "cmsMin", "camLow", "camHigh", "camDensity",
        "camStopped", "camDist", "toll", "tollAbove", "tollDist", "incident"]
FULL = OPEN + ["tomtom", "google"]


def _records(source, day):
    """[(unix time, record)] logged for this local day (history files are named by UTC date)."""
    out = []
    d = datetime.fromisoformat(day)
    for utc_day in (day, (d + timedelta(days=1)).strftime("%Y-%m-%d")):
        path = os.path.join(HISTORY_DIR, f"{source}-{utc_day}.jsonl.gz")
        if os.path.exists(path):
            with gzip.open(path, "rt") as h:
                for line in h:
                    r = json.loads(line)
                    t = datetime.fromisoformat(r["at"])
                    if t.astimezone(PACIFIC).strftime("%Y-%m-%d") == day:
                        out.append((t.timestamp(), r))
    return sorted(out, key=lambda x: x[0])


class Timeline:
    """The latest value at or before a time, if no older than max_age."""

    def __init__(self, items, max_age):
        self.t = [t for t, _ in items]
        self.v = [v for _, v in items]
        self.max_age = max_age

    def at(self, t):
        i = bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 and t - self.t[i] <= self.max_age else None


def _fwy_dir(text):
    """"I-680 SB" -> ("680", "S"); "SM-101 NB" -> ("101", "N")."""
    m = re.search(r"(\d+)\s*([NSEW])B", text)
    return (m.group(1), m.group(2)) if m else (None, None)


def edge_sources(day, edge_ids):
    """{source: Timeline of {edge id: value}} for the per-edge sources, kept to edge_ids."""
    out = {}
    for source in ("tomtom", "google", "chp", "sf511", "lcs"):
        items = []
        for t, r in _records(source, day):
            values = r.get("values") or r.get("levels") or {}
            items.append((t, {int(k): v for k, v in values.items() if int(k) in edge_ids}))
        out[source] = Timeline(items, MAX_AGE_S[source])
    return out


def cms_source(day, edge_ids):
    """Timeline of {edge id: (ratio, corridor free-flow minutes)}, caltrans_cms.poll's rule."""
    refs = caltrans_cms.references(HISTORY_DIR)
    with open(caltrans_cms.CORRIDORS) as h:
        corridors = json.load(h)
    items = []
    for t, r in _records("cms", day):
        shown = {k: (v["min"] if isinstance(v, dict) else v) for k, v in r["values"].items()}
        edges = {}
        for k, minutes in sorted(shown.items(), key=lambda kv: refs.get(kv[0], 1e9)):
            if k in refs and corridors.get(k):
                ratio = 1.0 if minutes - refs[k] <= 1 else max(0.05, min(1.0, refs[k] / minutes))
                for e in corridors[k]:
                    if e in edge_ids:
                        edges.setdefault(e, (ratio, refs[k]))
        items.append((t, edges))
    return Timeline(items, MAX_AGE_S["cms"])


def camera_fast():
    """{camera: fast speed in vehicle lengths/s}, from all logged camera history."""
    speeds = defaultdict(list)
    for path in sorted(os.listdir(HISTORY_DIR)):
        if path.startswith("cctv-"):
            with gzip.open(os.path.join(HISTORY_DIR, path), "rt") as h:
                for line in h:
                    for cam, v in json.loads(line)["values"].items():
                        speeds[cam] += [g["vlps"] for g in v["groups"].values() if g["n"] >= 3]
    return {cam: float(np.percentile(v, CAMERA_FAST_PCT)) for cam, v in speeds.items() if len(v) >= 20}


def features(day, mainline, profs, freeflow, edge_of, cameras, tolls):
    """{(station, minute of day): feature dict} for every 5 minutes some live source covers."""
    edge_ids = set(edge_of.values())
    per_edge = edge_sources(day, edge_ids)
    cms = cms_source(day, edge_ids)
    cctv = Timeline(_records("cctv", day), MAX_AGE_S["cctv"])
    toll = Timeline(_records("toll", day), MAX_AGE_S["toll"])
    fast = camera_fast()
    toll_low = defaultdict(lambda: math.inf)
    for _, r in _records("toll", day):
        for k, v in r["values"].items():
            toll_low[k] = min(toll_low[k], v[0])
    near_cam, near_toll = {}, {}  # station -> (camera id, metres) / (toll key, metres)
    for sid in edge_of:
        st = mainline[sid]
        cands = [(math.hypot(*_local_m(st["lat"], st["lon"], c["lat"], c["lon"])), cid) for cid, c in cameras.items()
                 if re.sub(r"\D", "", c["route"]) == st["fwy"] and cid in fast]
        cands = [c for c in cands if c[0] <= NEAR_M]
        if cands:
            near_cam[sid] = min(cands)[::-1]
        cands = [(math.hypot(*_local_m(st["lat"], st["lon"], v["lat"], v["lon"])), k) for k, v in tolls.items()
                 if v["fwy"] == st["fwy"] and v["dir"] == st["dir"]]
        cands = [c for c in cands if c[0] <= NEAR_M]
        if cands:
            near_toll[sid] = min(cands)[::-1]
    out = {}
    start = datetime.fromisoformat(day).replace(tzinfo=PACIFIC)
    for minute in range(0, 1440, 5):
        local = start + timedelta(minutes=minute)
        q = (local + timedelta(minutes=5)).timestamp()
        now = {s: per_edge[s].at(q) for s in per_edge}
        c, cam, tl = cms.at(q), cctv.at(q), toll.at(q)
        if not any(v for v in now.values()) and not c and not cam and not tl:
            continue
        how = ((local.weekday() + 1) % 7) * 24 + local.hour
        for sid, e in edge_of.items():
            f = {k: np.nan for k in FULL}
            f.update(prior=profs[sid][how] / freeflow[sid], freeflow=freeflow[sid], hourOfWeek=how,
                     weekend=float(local.weekday() >= 5))
            if now["tomtom"] and isinstance(now["tomtom"].get(e), float):
                f["tomtom"] = now["tomtom"][e]
            if now["google"] and e in now["google"]:
                f["google"] = detectors.GOOGLE_RATIO.get(now["google"][e], np.nan)
            effects = [now[s].get(e) for s in ("chp", "sf511", "lcs") if now[s] and e in now[s]]
            if effects:
                f["incident"] = min(0.0 if v == "closed" else float(v) for v in effects)
            if c and e in c:
                f["cms"], f["cmsMin"] = c[e]
            if cam and sid in near_cam and near_cam[sid][0] in cam["values"]:
                cid, dist = near_cam[sid]
                v = cam["values"][cid]
                ratios = [g["vlps"] / fast[cid] for g in v["groups"].values() if g["n"] >= 2]
                if ratios:
                    f.update(camLow=min(ratios), camHigh=max(ratios))
                f.update(camDensity=v["perFrame"], camStopped=v["stopped"] / max(v["vehicles"], 1), camDist=dist)
            if tl and sid in near_toll and near_toll[sid][0] in tl["values"]:
                k, dist = near_toll[sid]
                price = tl["values"][k][0]
                f.update(toll=price, tollAbove=price - toll_low[k], tollDist=dist)
            out[(sid, minute)] = f
    return out


def toll_signs(days):
    """{sign|destination: {lat, lon, fwy, dir}} from the toll history."""
    out = {}
    for day in days:
        for _, r in _records("toll", day):
            for k, v in r["values"].items():
                fwy, d = _fwy_dir(v[4])
                if fwy:
                    out[k] = {"lat": v[1], "lon": v[2], "fwy": fwy, "dir": d}
    return out


def camera_list(days):
    """{camera: {route, lat, lon}} from the live camera list (positions are not logged)."""
    from . import cctv
    try:
        return {c["id"]: c for c in cctv.cameras()}
    except Exception:  # offline: no camera features
        return {}


def main(days, names):
    import lightgbm as lgb

    mainline, profs, freeflow, edge_of = detectors.setup()
    cameras, tolls = camera_list(days), toll_signs(days)
    rows = []  # (day, minute, station, features, label ratio, prior mph, free flow mph)
    for day in days:
        speeds = detectors.truth(day, mainline)
        feats = features(day, mainline, profs, freeflow, edge_of, cameras, tolls)
        n = 0
        for (sid, minute), f in feats.items():
            t = speeds.get(sid, {}).get(minute)
            if t is not None and np.isfinite(f["prior"]):
                rows.append((day, minute, sid, f, t / freeflow[sid], profs[sid][f["hourOfWeek"]], freeflow[sid]))
                n += 1
        print(f"{day}: {n} detector-5-minute rows with live data")
    if not rows:
        return
    x = np.array([[r[3][k] for k in names] for r in rows], dtype=float)
    y = np.array([r[4] for r in rows])
    ff = np.array([r[6] for r in rows])
    prior = np.array([r[5] for r in rows])
    truth_mph = y * ff
    used_days = sorted({r[0] for r in rows})
    if len(used_days) >= 2:
        folds = np.array([used_days.index(r[0]) for r in rows])
        how = "whole days held out"
    else:
        folds = np.array([(r[1] // 60) % 5 for r in rows])
        how = "one day only: hour blocks held out (optimistic: same day's regime)"
    pred = np.full(len(rows), np.nan)
    params = {"objective": "l1", "learning_rate": 0.05, "num_leaves": 63, "min_data_in_leaf": 100,
              "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1}
    for k in sorted(set(folds)):
        tr, te = folds != k, folds == k
        model = lgb.train(params, lgb.Dataset(x[tr], y[tr], feature_name=names), num_boost_round=400)
        pred[te] = model.predict(x[te])
    est = pred * ff
    jam = y < detectors.CONGESTED
    odd = np.abs(truth_mph - prior) > detectors.UNUSUAL_MPH
    err = lambda e, sel: f"{np.mean(np.abs(e[sel] - truth_mph[sel])):5.2f}"
    print(f"{len(rows)} rows, features: {len(names)} ({how})")
    print(f"  mean abs error, mph    all ({len(rows)})   congested ({jam.sum()})   unusual for the hour ({odd.sum()})")
    every = np.ones(len(rows), bool)
    print(f"  prior              {err(prior, every)}        {err(prior, jam)}             {err(prior, odd)}")
    print(f"  fusion model       {err(est, every)}        {err(est, jam)}             {err(est, odd)}")
    for k in names:
        col = x[:, names.index(k)]
        if k not in ("prior", "freeflow", "hourOfWeek", "weekend"):
            print(f"  {k:>11}: present on {np.isfinite(col).mean():.0%} of rows")
    model = lgb.train(params, lgb.Dataset(x, y, feature_name=names), num_boost_round=400)
    top = sorted(zip(model.feature_importance("gain"), names), reverse=True)
    total = sum(g for g, _ in top) or 1
    print("  top features by gain: " + ", ".join(f"{n} {g / total:.0%}" for g, n in top[:8]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("days", nargs="+")
    parser.add_argument("--features", choices=("open", "full"), default="full")
    args = parser.parse_args()
    main(args.days, OPEN if args.features == "open" else FULL)
