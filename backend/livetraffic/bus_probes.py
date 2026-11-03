"""Bus positions -> street travel speeds (the research doc's GTFS-RT probe idea).

transit.py logs every bus in 511's regional GTFS-realtime feed every 2 minutes.
Here two consecutive positions of the same bus on the same trip become one
observation: Valhalla map-matches the pair with bus costing (/trace_attributes,
map_snap), giving the street edges driven and the distance along them, and the
speed is that distance over the time between the two positions. Buses stop for
passengers, so this speed is not a car speed; the usable signal is a stretch's
speed now compared with buses' usual speed on that stretch at that hour.

Only buses count (GTFS route_type 3 in 511's static regional feed): light rail
and cable cars run in their own right of way. A pair is skipped when the bus
did not move (dwell, or no new fix), when the fixes are under 30 s or over 300 s
apart, or when the matched path is over twice the straight line (a bad match).

    venv/bin/python -m livetraffic.bus_probes build 2026-10-31   # -> ~/geo/livetraffic/bus/obs-2026-10-31.jsonl.gz
    venv/bin/python -m livetraffic.bus_probes check 2026-10-31   # agreement with TomTom on the same streets
"""

import csv
import glob
import gzip
import json
import math
import os
import sys
import threading
from bisect import bisect_left
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from statistics import median

import numpy as np
import requests

from .run import HISTORY_DIR

VALHALLA = os.environ.get("VALHALLA_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
GTFS_ROUTES = os.path.expanduser("~/geo/gtfs/rg/routes.txt")  # 511 datafeeds, operator_id=RG
OUT_DIR = os.path.expanduser("~/geo/livetraffic/bus")
BUS = {"3"}
MIN_DT_S, MAX_DT_S = 30, 300
MIN_MOVE_KM = 0.03
MAX_DETOUR = 2.0
WORKERS = 8
_local = threading.local()  # one kept-alive connection per thread: 100k+ fresh connections ran out of local ports


def _session():
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


def bus_routes():
    with open(GTFS_ROUTES, encoding="utf-8-sig") as handle:
        return {r["route_id"] for r in csv.DictReader(handle) if r["route_type"] in BUS}


def _km(a, b):
    return math.hypot((a[0] - b[0]) * 110.574, (a[1] - b[1]) * 111.32 * math.cos(math.radians(a[0])))


def pairs(day):
    """(vehicle, route, trip, first fix, second fix) for consecutive fixes of a moving bus on one trip."""
    routes = bus_routes()
    snaps = []
    for path in sorted(glob.glob(os.path.join(HISTORY_DIR, f"transit-{day}.jsonl.gz"))):
        with gzip.open(path, "rt") as handle:
            snaps += [json.loads(line)["values"] for line in handle]
    out = []
    for before, after in zip(snaps, snaps[1:]):
        for vehicle, b in after.items():
            a = before.get(vehicle)
            if (a and b[3] in routes and b[4] and a[4] == b[4] and MIN_DT_S <= b[2] - a[2] <= MAX_DT_S
                    and _km(a, b) >= MIN_MOVE_KM):
                out.append((vehicle, b[3], b[4], a, b))
    return out


def match(pair):
    """One observation {t, route, edges, km, seconds, kph} or None."""
    _, route, trip, a, b = pair
    body = {"shape": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}], "costing": "bus",
            "shape_match": "map_snap", "trace_options": {"search_radius": 40},
            "filters": {"attributes": ["edge.id", "edge.length", "matched.distance_along_edge"], "action": "include"}}
    try:
        reply = _session().post(f"{VALHALLA}/trace_attributes", json=body, timeout=30).json()
    except (requests.RequestException, ValueError):
        return None
    edges, points = reply.get("edges"), reply.get("matched_points")
    if not edges or not points or len(points) != 2:
        return None
    length = sum(e["length"] for e in edges)  # km
    length -= edges[0]["length"] * points[0].get("distance_along_edge", 0.0)
    length -= edges[-1]["length"] * (1 - points[-1].get("distance_along_edge", 1.0))
    seconds = b[2] - a[2]
    if length <= 0 or length > MAX_DETOUR * max(_km(a, b), MIN_MOVE_KM):
        return None
    return {"t": (a[2] + b[2]) / 2, "route": route, "trip": trip, "edges": [e["id"] for e in edges],
            "km": round(length, 4), "seconds": seconds, "kph": round(length / seconds * 3600, 2)}


def build(day):
    todo = pairs(day)
    with ThreadPoolExecutor(WORKERS) as pool:
        obs = [o for o in pool.map(match, todo, chunksize=64) if o]
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"obs-{day}.jsonl.gz")
    with gzip.open(path + ".tmp", "wt") as handle:
        handle.writelines(json.dumps(o) + "\n" for o in obs)
    os.replace(path + ".tmp", path)
    print(f"{len(todo)} moving bus pairs, {len(obs)} matched -> {path}")


def check(day):
    """Do buses slow down where and when TomTom says streets are slow? Each observation's speed
    relative to its stretch's median speed that day, against TomTom's mean ratio on the same
    catalog edges at the nearest TomTom sweep (within 8 minutes)."""
    from datetime import datetime
    with gzip.open(os.path.join(OUT_DIR, f"obs-{day}.jsonl.gz"), "rt") as handle:
        obs = [json.loads(line) for line in handle]
    sweeps = []
    for path in sorted(glob.glob(os.path.join(HISTORY_DIR, f"tomtom-{day}.jsonl.gz"))):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                record = json.loads(line)
                sweeps.append((datetime.fromisoformat(record["at"]).timestamp(), record.get("values") or {}))
    times = [s[0] for s in sweeps]
    by_edge = defaultdict(list)
    for o in obs:
        for e in o["edges"]:
            by_edge[e].append(o["kph"])
    usual_edge = {e: median(v) for e, v in by_edge.items() if len(v) >= 8}
    bus, tom = [], []
    for o in obs:
        known = [usual_edge[e] for e in o["edges"] if e in usual_edge]
        u = median(known) if known else None  # the usual bus speed on these edges that day
        i = bisect_left(times, o["t"])
        near = [j for j in (i - 1, i) if 0 <= j < len(times) and abs(times[j] - o["t"]) <= 480]
        if not u or not near:
            continue
        values = sweeps[near[0]][1]
        ratios = [values[str(e)] for e in o["edges"] if isinstance(values.get(str(e)), float)]
        if ratios:
            bus.append(o["kph"] / u)
            tom.append(sum(ratios) / len(ratios))
    if len(bus) < 50:
        print(f"only {len(bus)} observations overlap TomTom")
        return
    rank = lambda v: np.argsort(np.argsort(v))
    rho = np.corrcoef(rank(np.array(bus)), rank(np.array(tom)))[0, 1]
    print(f"{len(obs)} observations, {len(bus)} on TomTom-covered streets with a usual speed; "
          f"Spearman rho(bus speed / usual, TomTom ratio) = {rho:.3f}")
    tom, bus = np.array(tom), np.array(bus)
    for lo, hi in ((0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01)):
        sel = (tom >= lo) & (tom < hi)
        if sel.any():
            print(f"  TomTom ratio {lo:.2f}-{min(hi, 1):.2f}: {sel.sum():6d} obs, bus speed / usual median {np.median(bus[sel]):.2f}")


if __name__ == "__main__":
    {"build": build, "check": check}[sys.argv[1]](sys.argv[2])
