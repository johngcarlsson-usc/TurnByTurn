"""Forecast Caltrans message-sign travel times 15, 30 and 60 minutes ahead (LightGBM).

Training data is the sign history run.py logs (history/cms-*.jsonl.gz): every
reading of every (sign, destination) with its corridor's inputs that cycle.
The label is the same sign's own reading h minutes later, so the signs label
themselves and no outside data is needed (open version). Features per reading:
- the time now, 10 and 30 minutes ago, and the trend, all relative to the
  sign's free-flow time (its lowest reading);
- the usual time for this hour of the week (median over the training days),
  and how far now is from it;
- hour of the week; corridor closures, incident factor, TomTom and Google ratios.
Scored only on the last days (never trained on), against the baselines a
forecaster must beat: "the future equals now" (persistence) and the usual
time for that hour of the week.

    venv/bin/python -m livetraffic.forecast            # train and score
    venv/bin/python -m livetraffic.forecast --test-days 2
"""

import argparse
import glob
import gzip
import json
import os
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from .run import HISTORY_DIR

PACIFIC = ZoneInfo("America/Los_Angeles")
HORIZONS = (15, 30, 60)    # minutes ahead
LAGS = (10, 30)
TOLERANCE_S = 180          # a lag or target reading must be this close to its time
MIN_TRAIN_DAYS = 7         # fewer: the run only checks the pipeline
FEATURES = ["now", "lag10", "lag30", "trend10", "trend30", "usual", "fromUsual", "hourOfWeek", "freeflowMin",
            "closed", "incident", "tomtom", "google"]


def load(history_dir=HISTORY_DIR):
    """{key: [(unix time, minutes, inputs dict)]} from the full loop's sign logs, time-sorted."""
    series = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(history_dir, "cms-2*.jsonl.gz"))):  # not cms-open-*: same readings
        with gzip.open(path, "rt") as handle:
            for line in handle:
                record = json.loads(line)
                t = datetime.fromisoformat(record["at"]).timestamp()
                for key, value in record["values"].items():
                    if isinstance(value, dict):
                        series[key].append((t, value["min"], value))
    return {k: sorted(v, key=lambda r: r[0]) for k, v in series.items()}


def _at(times, readings, t):
    """The reading closest to t within TOLERANCE_S, or None."""
    i = bisect_left(times, t)
    best = min((j for j in (i - 1, i) if 0 <= j < len(times)), key=lambda j: abs(times[j] - t), default=None)
    return readings[best][1] if best is not None and abs(times[best] - t) <= TOLERANCE_S else None


def hour_of_week(t):
    local = datetime.fromtimestamp(t, PACIFIC)
    return ((local.weekday() + 1) % 7) * 24 + local.hour  # Sunday 00:00 = 0, as Valhalla


def dataset(series, train_until):
    """Rows (key, t, features, {h: label}); free flow and usual times use only readings before train_until."""
    rows = []
    for key, readings in series.items():
        times = [r[0] for r in readings]
        past = [r for r in readings if r[0] < train_until]
        if not past:
            continue
        freeflow = min(r[1] for r in past)
        by_hour = defaultdict(list)
        for t, minutes, _ in past:
            by_hour[hour_of_week(t)].append(minutes)
        usual = {h: float(np.median(v)) for h, v in by_hour.items()}
        for t, minutes, inputs in readings:
            lags = {lag: _at(times, readings, t - lag * 60) for lag in LAGS}
            labels = {h: _at(times, readings, t + h * 60) for h in HORIZONS}
            if None in lags.values() or all(v is None for v in labels.values()):
                continue
            how = hour_of_week(t)
            u = usual.get(how)
            f = {"now": minutes / freeflow, "lag10": lags[10] / freeflow, "lag30": lags[30] / freeflow,
                 "trend10": (minutes - lags[10]) / freeflow, "trend30": (minutes - lags[30]) / freeflow,
                 "usual": u / freeflow if u else np.nan, "fromUsual": minutes / u if u else np.nan,
                 "hourOfWeek": how, "freeflowMin": freeflow, "closed": inputs.get("closed") or 0,
                 "incident": inputs["incident"] if inputs.get("incident") is not None else np.nan,
                 "tomtom": inputs["tomtom"] if inputs.get("tomtom") is not None else np.nan,
                 "google": inputs["google"] if inputs.get("google") is not None else np.nan}
            usual_at = {h: usual.get(hour_of_week(t + h * 60)) for h in HORIZONS}
            rows.append((key, t, minutes, f, labels, usual_at))
    return rows


def score(name, pred, true):
    err = np.abs(np.array(pred) - np.array(true)) / np.array(true) * 100
    return f"{name:>12}: median |error| {np.median(err):5.1f}%, mean {np.mean(err):5.1f}%, 90th pct {np.percentile(err, 90):5.1f}%"


def main(test_days):
    import lightgbm as lgb

    series = load()
    stamps = sorted(t for readings in series.values() for t, _, _ in readings)
    if not stamps:
        print("no sign history yet")
        return
    days = sorted({datetime.fromtimestamp(t, PACIFIC).date() for t in stamps})
    print(f"{len(series)} sign corridors, {len(stamps)} readings over {len(days)} days ({days[0]} to {days[-1]})")
    if len(days) <= test_days:
        print("not enough days to hold any out yet")
        return
    split = datetime.combine(days[-test_days], datetime.min.time(), PACIFIC).timestamp()
    if len(days) - test_days < MIN_TRAIN_DAYS:
        print(f"only {len(days) - test_days} training days (want {MIN_TRAIN_DAYS}+): pipeline check, not a result")
    rows = dataset(series, split)
    train = [r for r in rows if r[1] < split]
    test = [r for r in rows if r[1] >= split]
    for h in HORIZONS:
        tr = [r for r in train if r[4][h] is not None and r[1] + h * 60 < split]  # labels inside the training days
        te = [r for r in test if r[4][h] is not None]
        if len(tr) < 200 or not te:
            print(f"+{h} min: too few rows (train {len(tr)}, test {len(te)})")
            continue
        x = lambda rs: np.array([[r[3][f] for f in FEATURES] for r in rs], dtype=float)
        y = np.log([r[4][h] / r[2] for r in tr])  # log of future / now
        model = lgb.train({"objective": "regression", "learning_rate": 0.03, "num_leaves": 31,
                           "min_data_in_leaf": 50, "bagging_fraction": 0.8, "bagging_freq": 1,
                           "feature_fraction": 0.8, "verbose": -1},
                          lgb.Dataset(x(tr), y, feature_name=FEATURES), num_boost_round=400)
        pred = np.array([r[2] for r in te]) * np.exp(model.predict(x(te)))
        true = [r[4][h] for r in te]
        print(f"+{h} min ahead: train {len(tr)}, test {len(te)} readings")
        print("  " + score("persistence", [r[2] for r in te], true))
        usual = [(r[5][h], t) for r, t in zip(te, true) if r[5][h]]
        if usual:
            print("  " + score("usual", [u for u, _ in usual], [t for _, t in usual]) + f"  (n={len(usual)})")
        print("  " + score("model", pred, true))
        moving = [(p, r[2], t) for p, r, t in zip(pred, te, true) if abs(t - r[2]) >= 2]
        if moving:  # where traffic changed: the cases persistence gets wrong
            print(f"  where the time changed by 2+ min (n={len(moving)}):")
            print("  " + score("persistence", [m[1] for m in moving], [m[2] for m in moving]))
            print("  " + score("model", [m[0] for m in moving], [m[2] for m in moving]))
        top = sorted(zip(model.feature_importance("gain").round().astype(int), FEATURES), reverse=True)[:5]
        print("  top features: " + ", ".join(f"{n} {v}" for v, n in top))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-days", type=int, default=1)
    main(parser.parse_args().test_days)
