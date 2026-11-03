"""Compare our drive times with Google Maps' (research use, outside data).

For each trip, at the same moment: Google Maps' driving options (read from
the web page with headless Chromium via Playwright), Valhalla with live
traffic (full and open versions), Valhalla with the time-of-week prior only (PeMS, "predicted"), and
Valhalla on typical speeds. Google's number is itself a
prediction, not a measured drive; it is the reference we have.

Trips are random but repeatable (fixed seed): both ends are points on
catalog roads in the Bay Area, split evenly into short (3-10 km straight
line), medium (10-25 km) and long (25-60 km) trips.

    venv/bin/python -m livetraffic.benchmark                # 240 trips, seed 1
    venv/bin/python -m livetraffic.benchmark --n 60 --seed 2 --shots
    venv/bin/python -m livetraffic.benchmark --fit ~/geo/livetraffic/bench/*.jsonl

Results are appended to ~/geo/livetraffic/bench/YYYY-MM-DD.jsonl.
Playwright is a test-only dependency: pip install playwright && playwright install chromium
"""

import argparse
import asyncio
import json
import math
import os
import random
import re
from datetime import datetime, timezone

import numpy as np
import requests

from . import catalog

VALHALLA = os.environ.get("VALHALLA_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
# the "open" version (run.py --profile open: government/open sources only) has its own Valhalla
VALHALLA_OPEN = os.environ.get("VALHALLA_OPEN_URL", "http://127.0.0.1:8003").rstrip("/")
MODES = ("live", "open", "predicted", "typical")
OUT_DIR = os.path.expanduser("~/geo/livetraffic/bench")
GOOGLE = "https://www.google.com/maps/dir/{a[0]},{a[1]}/{b[0]},{b[1]}/data=!4m2!4m1!3e0?hl=en"
BANDS = {"short": (3, 10), "medium": (10, 25), "long": (25, 60)}  # straight-line km
TABS = 4
CONTEXT_TRIPS = 40
# road classes for the time breakdown and the correction fit (fit())
CLASSES = ("freeway", "ramp", "primary", "secondary", "local")
CLASS_OF = {"motorway": "freeway", "trunk": "freeway", "primary": "primary", "secondary": "secondary"}

# Each route card's text: "31 min\n16.1 miles\nvia CA-84 W\nBest route now due to traffic conditions ..."
CARDS_JS = r"""() => {
  const out = [];
  for (const el of document.querySelectorAll('h1')) {
    if (!/^via /.test(el.textContent)) continue;
    let card = el;
    while (card && !(/\d+ min|\d+ hr/.test(card.innerText) && /(miles|ft)\b/.test(card.innerText))) card = card.parentElement;
    if (card) out.push(card.innerText);
  }
  return out;
}"""


def km(a, b):
    k = math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot((a[0] - b[0]) * 110.54, (a[1] - b[1]) * 111.32 * k)


def sample_trips(n, seed):
    """n trips, an equal share per distance band, both ends on catalog road points."""
    rng = random.Random(seed)
    points = [tuple(e["shape"][len(e["shape"]) // 2]) for e in catalog.load()
              if e.get("use") == "road" and e["road_class"] in {"motorway", "trunk", "primary", "secondary"}]
    trips, per_band = [], n // len(BANDS)
    for band, (lo, hi) in BANDS.items():
        found = 0
        while found < per_band:
            a, b = rng.choice(points), rng.choice(points)
            if lo <= km(a, b) <= hi:
                trips.append({"band": band, "from": a, "to": b, "km": round(km(a, b), 1)})
                found += 1
    return trips


def minutes(text):
    hours = re.search(r"(\d+) hr", text)
    mins = re.search(r"(\d+) min", text)
    return (int(hours.group(1)) * 60 if hours else 0) + (int(mins.group(1)) if mins else 0)


def parse_card(text):
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    time = next(line for line in lines if re.fullmatch(r"(\d+ hr ?)?(\d+ min)?", line) and line)
    dist = next(line for line in lines if re.fullmatch(r"[\d.,]+ (miles|ft)", line))
    via = next(line for line in lines if line.startswith("via "))[4:]
    value = float(re.search(r"[\d.,]+", dist).group().replace(",", ""))
    note = lines[lines.index("via " + via) + 1] if lines.index("via " + via) + 1 < len(lines) else ""
    return {"minutes": minutes(time), "miles": round(value / 5280 if dist.endswith("ft") else value, 1),
            "via": via, "note": note}


async def google(page, a, b, shot=None):
    await page.goto(GOOGLE.format(a=a, b=b), wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_function(r"() => /via .+\n/.test(document.body.innerText)", timeout=30000)
    await page.wait_for_timeout(1500)  # let the traffic-aware times settle
    if shot:
        await page.screenshot(path=shot)
    cards = [parse_card(text) for text in await page.evaluate(CARDS_JS)]
    if not cards:
        raise RuntimeError("no route cards on the page")
    fastest = min(cards, key=lambda c: c["minutes"])
    return {"minutes": fastest["minutes"], "miles": fastest["miles"], "via": fastest["via"],
            "note": fastest["note"], "best": cards[0], "cards": len(cards)}


def valhalla(a, b, mode, when=None):
    body = {"locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}], "costing": "auto",
            "directions_options": {"units": "miles"}}
    base = VALHALLA_OPEN if mode == "open" else VALHALLA
    if mode in {"live", "open"}:
        body["date_time"] = {"type": 0}
    elif mode == "predicted":  # time-of-week prior only (PeMS): any departure near now also takes
        # live speeds, so leave "current" out of the speed types explicitly
        body["date_time"] = {"type": 1, "value": (when or datetime.now()).strftime("%Y-%m-%dT%H:%M")}
        body["costing_options"] = {"auto": {"speed_types": ["freeflow", "constrained", "predicted"]}}
    reply = requests.post(f"{base}/route", json=body, timeout=60).json()
    if "trip" not in reply:
        return {"error": str(reply.get("error", reply))[:200]}
    trip = reply["trip"]
    roads = {}
    for maneuver in trip["legs"][0]["maneuvers"]:
        for name in maneuver.get("street_names", []):
            roads[name] = roads.get(name, 0) + maneuver["length"]
    out = {"minutes": round(trip["summary"]["time"] / 60, 1), "miles": round(trip["summary"]["length"], 1),
           "roads": {k: round(v, 2) for k, v in sorted(roads.items(), key=lambda kv: -kv[1])[:6]},
           "maneuvers": len(trip["legs"][0]["maneuvers"])}
    if mode in {"live", "open"}:
        out["classSeconds"], out["control"] = class_seconds(trip["legs"][0]["shape"], body.get("date_time"), base)
        out["control"].update(turns(trip["legs"][0]["maneuvers"]))
    return out


def road_class(edge):
    if edge.get("use") in {"ramp", "turn_channel"}:
        return "ramp"
    return CLASS_OF.get(edge.get("road_class"), "local")


# Valhalla maneuver types -> the turn classes of Boeing & Zhou, "Travel Time Prediction from
# Sparse Open Data" (IJGIS 2026), whose route-level features this records for the route model
TURNS = {9: "slightRight", 10: "right", 11: "right", 12: "uTurn", 13: "uTurn", 14: "left", 15: "left",
         16: "slightLeft"}


def turns(maneuvers):
    out = dict.fromkeys(sorted(set(TURNS.values())), 0)
    for m in maneuvers:
        if m["type"] in TURNS:
            out[TURNS[m["type"]]] += 1
    return out


def class_seconds(shape, date_time, base=VALHALLA):
    """(seconds of the route on each road class (CLASSES), control features): from Valhalla's own
    per-node elapsed times, which with the route's date_time add up to the route's time exactly
    (live included); signals and intersections crossed, and seconds of turn/intersection cost."""
    body = {"encoded_polyline": shape, "costing": "auto", "shape_match": "edge_walk",
            "filters": {"attributes": ["edge.road_class", "edge.use", "node.elapsed_time", "node.traffic_signal",
                                       "node.intersecting_edges", "node.transition_time"], "action": "include"}}
    if date_time:
        body["date_time"] = date_time
    edges = requests.post(f"{base}/trace_attributes", json=body, timeout=60).json().get("edges") or []
    out, before = dict.fromkeys(CLASSES, 0.0), 0.0
    control = {"signals": 0, "intersections": 0, "transitionSeconds": 0.0}
    for edge in edges:
        node = edge.get("end_node", {})
        elapsed = node.get("elapsed_time", before)
        out[road_class(edge)] += elapsed - before
        before = elapsed
        control["signals"] += bool(node.get("traffic_signal"))
        control["intersections"] += bool(node.get("intersecting_edges"))
        control["transitionSeconds"] += node.get("transition_time", 0.0)
    control["transitionSeconds"] = round(control["transitionSeconds"], 1)
    return ({k: round(v, 1) for k, v in out.items()}, control) if edges else (None, {})


def _norm(name):
    """"I-880 N", "I 880 North" -> "I880"; "CA-84 W", "CA 84" -> "SR84"; others: letters and digits."""
    m = re.match(r"(?i)(I|US|CA|SR)[- ]?(\d+)", name.strip())
    if m:
        return ("SR" if m.group(1).upper() in {"CA", "SR"} else m.group(1).upper()) + m.group(2)
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def same_road(via, roads, total):
    """Is Google's first named road at least 15% of our route's length?"""
    first = _norm(re.split(r" and |/", via)[0])
    share = sum(length for name, length in roads.items() if _norm(name) == first)
    return total > 0 and share / total >= 0.15


async def measure(context, trip, slot, shots):
    page = await context.new_page()
    try:
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **trip}
        shot = os.path.join(OUT_DIR, f"trip-{slot:03d}.png") if shots else None
        for attempt in range(2):
            try:
                row["google"] = await google(page, trip["from"], trip["to"], shot)
                break
            except Exception as error:
                row["google"] = {"error": f"{type(error).__name__}: {error}"[:200]}
        results = await asyncio.gather(*(asyncio.to_thread(valhalla, trip["from"], trip["to"], m) for m in MODES))
        row.update(zip(MODES, results))
        live = row["live"]
        if "minutes" in row["google"] and "minutes" in live:
            row["sameRoad"] = same_road(row["google"]["via"], live["roads"], live["miles"])
        return row
    finally:
        await page.close()


async def run(n, seed, shots):
    from playwright.async_api import async_playwright

    trips = sample_trips(n, seed)
    status = json.load(open(os.path.expanduser("~/geo/livetraffic/status.json")))
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{datetime.now():%Y-%m-%d}.jsonl")
    rows, gate = [], asyncio.Semaphore(TABS)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        contexts = {}

        async def context_for(slot):
            # a fresh session every CONTEXT_TRIPS trips (one long session degraded after ~190 pages)
            key = slot // CONTEXT_TRIPS
            if key not in contexts:
                contexts[key] = await browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
            return contexts[key]

        async def one(slot, trip):
            async with gate:
                row = await measure(await context_for(slot), trip, slot, shots)
            rows.append(row)
            with open(out_path, "a") as handle:  # saved as it goes
                handle.write(json.dumps({**row, "seed": seed, "liveWritten": status.get("written"),
                                         "calibration": status.get("googleCalibration", {}).get("ratio")}) + "\n")
            g = row["google"]
            print(f"{len(rows):>4}/{len(trips)} {row['band']:<6} {row['km']:>5} km  google "
                  f"{g.get('minutes', '?'):>4}  live {row['live'].get('minutes', '?'):>6}  predicted "
                  f"{row['predicted'].get('minutes', '?'):>6}  open {row['open'].get('minutes', '?'):>6}  typical "
                  f"{row['typical'].get('minutes', '?'):>6}  {g.get('via', g.get('error', ''))[:40]}"
                  f"{'  ours: ' + row['live']['error'][:60] if 'error' in row['live'] else ''}", flush=True)

        await asyncio.gather(*(one(i, t) for i, t in enumerate(trips)))
        await browser.close()
    summarize(rows)
    return rows


def summarize(rows):
    modes = [m for m in MODES if any(m in r for r in rows)]  # older rows have no "open"
    ok = [r for r in rows if "minutes" in r["google"] and all("minutes" in r.get(k, {}) for k in modes)]
    print(f"\n{len(ok)} of {len(rows)} trips with a time from Google and from us")
    for band in [*BANDS, "all"]:
        sel = [r for r in ok if band in (r["band"], "all")]
        if not sel:
            continue
        print(f"{band} ({len(sel)} trips, Google median {sorted(r['google']['minutes'] for r in sel)[len(sel) // 2]} min)")
        for name in modes:
            err = [r[name]["minutes"] - r["google"]["minutes"] for r in sel]
            pct = sorted(abs(e) / max(r["google"]["minutes"], 1) * 100 for e, r in zip(err, sel))
            print(f"  {name:>8}: mean error {sum(err) / len(err):+5.1f} min, median |error| {pct[len(pct) // 2]:4.0f}%, "
                  f"within 10%: {sum(p <= 10 for p in pct):>3}/{len(sel)}, within 20%: {sum(p <= 20 for p in pct):>3}/{len(sel)}")
        print(f"  same main road as Google: {sum(r.get('sameRoad', False) for r in sel)}/{len(sel)}")


# ------------------------------------------------------------------ correction fit
# The research doc's "ETA inversion" (MapReuse), in a simple form: Google's time
# ~ sum over road classes of (multiplier x our base time on that class) +
# (multiplier x the delay live traffic adds over typical speeds). Ridge on
# relative error, pulled toward "no correction" (all multipliers 1). Scored
# only on held-out trips. Tried and dropped: one multiplier per class on the
# live time (no delay term) and seconds per maneuver (fitted to 0).
RIDGE = 2.0
TERMS = (*CLASSES, "liveDelay")


def _features(row):
    """Base seconds per class (live delay taken off the freeway time, where live speeds apply) and the delay."""
    sec = dict(row["live"]["classSeconds"])
    delay = max(row["live"]["minutes"] - row["typical"]["minutes"], 0) * 60
    sec["freeway"] = max(sec["freeway"] - delay, 0)
    return [sec[c] for c in CLASSES] + [delay]


def solve(rows, ridge=RIDGE):
    """{term: multiplier}."""
    x = np.array([_features(r) for r in rows])
    g = np.array([r["google"]["minutes"] * 60.0 for r in rows])
    reg = math.sqrt(ridge) * np.eye(len(TERMS))
    w = np.linalg.lstsq(np.vstack([x / g[:, None], reg]), np.concatenate([np.ones(len(g)), reg @ np.ones(len(TERMS))]),
                        rcond=None)[0]
    return {t: round(float(v), 3) for t, v in zip(TERMS, w)}


def apply(fit, row):
    return float(np.array(_features(row)) @ np.array([fit[t] for t in TERMS])) / 60


def fit(rows, folds=5, seed=0):
    rows = [r for r in rows if "minutes" in r["google"] and (r.get("live") or {}).get("classSeconds")
            and "minutes" in r.get("typical", {})]
    order = list(range(len(rows)))
    random.Random(seed).shuffle(order)
    held = {}
    for k in range(folds):
        test = set(order[k::folds])
        model = solve([r for i, r in enumerate(rows) if i not in test])
        held.update({i: apply(model, rows[i]) for i in test})
    print(f"{len(rows)} trips; correction fitted on all: {solve(rows)}")
    for band in [*BANDS, "all"]:
        sel = [i for i, r in enumerate(rows) if band in (r["band"], "all")]
        if not sel:
            continue
        for name, pred in (("live", lambda i: rows[i]["live"]["minutes"]), ("live corrected", held.get)):
            err = [pred(i) - rows[i]["google"]["minutes"] for i in sel]
            pct = sorted(abs(e) / max(rows[i]["google"]["minutes"], 1) * 100 for e, i in zip(err, sel))
            print(f"  {band:>6} {name:>14}: mean error {sum(err) / len(err):+5.1f} min, median |error| "
                  f"{pct[len(pct) // 2]:4.1f}%, within 10%: {sum(p <= 10 for p in pct):>3}/{len(sel)}, "
                  f"within 20%: {sum(p <= 20 for p in pct):>3}/{len(sel)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=240)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--shots", action="store_true")
    parser.add_argument("--fit", nargs="+", metavar="JSONL", help="fit and cross-validate the per-class correction")
    args = parser.parse_args()
    if args.fit:
        fit([json.loads(line) for path in args.fit for line in open(os.path.expanduser(path))])
    else:
        asyncio.run(run(args.n, args.seed, args.shots))
