"""Route-level ETA model: predict Google Maps' time from our own route's features.

In the spirit of Boeing & Zhou, "Travel Time Prediction from Sparse Open Data"
(IJGIS 2026): a tree model corrects a naive route time with what the route
crosses (signals, intersections, turns) and where its time is spent (road
classes). Here the naive time is ours (Valhalla, "live" or "open"), the target
is log(google / ours), and the prediction is ours x exp(model).

Scored on held-out batches (one benchmark seed = one batch = one time of day),
so the model is always tested on trips and a time it never trained on; a random
5-fold is shown alongside, with the uncorrected time and benchmark.py's ridge
correction under the same splits.

    venv/bin/python -m livetraffic.route_model ~/geo/livetraffic/bench/*.jsonl
    venv/bin/python -m livetraffic.route_model --mode open ~/geo/livetraffic/bench/*.jsonl
"""

import argparse
import json
import math
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np

from . import benchmark
from .benchmark import BANDS, CLASSES

TZ = ZoneInfo("America/Los_Angeles")
CONTROL = ("signals", "intersections", "transitionSeconds", "left", "right", "slightLeft", "slightRight", "uTurn")
FEATURES = ("ours", "typical", "predicted", "liveDelay", *(f"sec_{c}" for c in CLASSES),
            *(f"share_{c}" for c in CLASSES), *CONTROL, "km", "miles", "maneuvers", "hourOfWeek", "weekend")
# conservative for a few thousand rows: small shallow trees, slow learning, strong leaf minimum
PARAMS = {"objective": "regression", "learning_rate": 0.03, "num_leaves": 15, "max_depth": 5,
          "min_data_in_leaf": 25, "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
          "lambda_l2": 5.0, "verbose": -1, "seed": 0, "num_threads": 4}
ROUNDS = 300
FOLDS = 5
TOP = 15


def usable(row, mode):
    """Has Google's time, ours for the mode with its class breakdown, and typical."""
    return ("minutes" in row.get("google", {}) and row["google"]["minutes"] > 0
            and (row.get(mode) or {}).get("classSeconds") and "minutes" in row.get("typical", {}))


def features(row, mode):
    """One row's feature vector (FEATURES order); NaN where a field is absent (older rows)."""
    ours = row[mode]
    sec = ours["classSeconds"]
    total = sum(sec.values()) or float("nan")
    control = ours.get("control") or {}
    local = datetime.fromisoformat(row["at"]).astimezone(TZ)
    pick = lambda d, k: float(d[k]) if k in d else float("nan")
    return [ours["minutes"], row["typical"]["minutes"], pick(row.get("predicted", {}), "minutes"),
            ours["minutes"] - row["typical"]["minutes"], *(sec[c] for c in CLASSES),
            *(sec[c] / total for c in CLASSES), *(pick(control, k) for k in CONTROL),
            row["km"], ours["miles"], pick(ours, "maneuvers"), local.weekday() * 24 + local.hour,
            float(local.weekday() >= 5)]


def train(rows, mode):
    x = np.array([features(r, mode) for r in rows])
    y = np.array([math.log(r["google"]["minutes"] / r[mode]["minutes"]) for r in rows])
    return lgb.train(PARAMS, lgb.Dataset(x, y, feature_name=list(FEATURES)), num_boost_round=ROUNDS)


def predict(model, rows, mode):
    x = np.array([features(r, mode) for r in rows])
    return [r[mode]["minutes"] * math.exp(p) for r, p in zip(rows, model.predict(x))]


def splits(rows, how):
    """[(train indices, test indices)]: by batch (seed), or random FOLDS."""
    if how == "batch":
        seeds = sorted({r.get("seed") for r in rows}, key=str)
        return [([i for i, r in enumerate(rows) if r.get("seed") != s], [i for i, r in enumerate(rows) if r.get("seed") == s])
                for s in seeds]
    order = list(range(len(rows)))
    random.Random(0).shuffle(order)
    tests = [order[k::FOLDS] for k in range(FOLDS)]
    return [([i for i in order if i not in set(test)], test) for test in tests]


def cross_validate(rows, mode, how):
    """{method: [held-out minutes per row]} for uncorrected, ridge and the route model."""
    as_live = [{**r, "live": r[mode]} for r in rows]  # benchmark's ridge reads row["live"]
    out = {name: [None] * len(rows) for name in (mode, "ridge", "route model")}
    for fit_idx, test_idx in splits(rows, how):
        ridge = benchmark.solve([as_live[i] for i in fit_idx])
        model = train([rows[i] for i in fit_idx], mode)
        for i, p in zip(test_idx, predict(model, [rows[i] for i in test_idx], mode)):
            out[mode][i] = rows[i][mode]["minutes"]
            out["ridge"][i] = benchmark.apply(ridge, as_live[i])
            out["route model"][i] = p
    return out


def report(rows, held):
    for band in [*BANDS, "all"]:
        sel = [i for i, r in enumerate(rows) if band in (r["band"], "all")]
        if not sel:
            continue
        for name, pred in held.items():
            err = [pred[i] - rows[i]["google"]["minutes"] for i in sel]
            pct = sorted(abs(e) / max(rows[i]["google"]["minutes"], 1) * 100 for e, i in zip(err, sel))
            print(f"  {band:>6} {name:>12}: mean error {sum(err) / len(err):+5.1f} min, median |error| "
                  f"{pct[len(pct) // 2]:4.1f}%, within 10%: {sum(p <= 10 for p in pct):>4}/{len(sel)}, "
                  f"within 20%: {sum(p <= 20 for p in pct):>4}/{len(sel)}")


def main(rows, mode):
    rows = [r for r in rows if usable(r, mode)]
    seeds = sorted({r.get("seed") for r in rows}, key=str)
    with_control = sum(bool(r[mode].get("control")) for r in rows)
    print(f"{len(rows)} trips with mode {mode!r}, batches (seeds) {seeds}, {with_control} with control counts")
    if len(rows) < 2 * FOLDS:
        raise SystemExit("too few trips to cross-validate")
    if len(seeds) >= 2:
        print(f"\nheld-out batches (leave one seed out, {len(seeds)} folds):")
        report(rows, cross_validate(rows, mode, "batch"))
    else:
        print("\nWARNING: only one batch, so no batch can be held out; random folds below share the batch's "
              "time of day and conditions with training and will overstate how well this generalizes")
    print(f"\nrandom {FOLDS}-fold (for comparison):")
    report(rows, cross_validate(rows, mode, "random"))
    model = train(rows, mode)
    gain = sorted(zip(FEATURES, model.feature_importance("gain")), key=lambda kv: -kv[1])
    total = sum(g for _, g in gain) or 1
    print(f"\ntop features by gain (model fitted on all {len(rows)} trips):")
    for name, g in gain[:TOP]:
        print(f"  {name:>18} {g / total * 100:5.1f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", nargs="+")
    parser.add_argument("--mode", choices=("live", "open"), default="live")
    parser.add_argument("--seed", type=int, nargs="*", help="only these benchmark seeds")
    args = parser.parse_args()
    rows = [json.loads(line) for path in args.jsonl for line in open(os.path.expanduser(path))]
    main([r for r in rows if not args.seed or r.get("seed") in args.seed], args.mode)
