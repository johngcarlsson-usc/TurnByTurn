"""Caltrans Lane Closure System (cwwp2.dot.ca.gov, public, no key).

Every planned closure on the state highways with its begin and end point,
route, direction, lanes closed and crew status. A closure counts only while it
is in place: code 1097 (closure set up) without 1098 (picked up) or 1022
(cancelled). Unlike the point incidents from CHP and 511, the closure is
matched only along the stretch from its begin to its end point.

Impacts reuse the 511 assumptions (sf511.STATE_IMPACT) so there is one set of
tunable numbers. A planned closure is overruled in run.py where both TomTom and Google
measure moving traffic on the edge (seen 2026-10-30: a "Full, 1 of 1 lanes"
record on the 6-lane Dumbarton Bridge while traffic was flowing).

Ramp and connector closures apply to ramp edges only, and only when the
record has a real extent: most give the same point for begin and end, and a
point cannot say which ramp of an interchange is meant (a 150 m circle at
US-101/I-80 caught 18 ramp edges). Mainline closures never touch ramps.
"""

import math
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from .chp import DIRECTIONS, NOT_MAINLINE, _bearing, _local_m
from .sf511 import STATE_IMPACT, road_name

URL = "https://cwwp2.dot.ca.gov/data/d{d}/lcs/lcsStatusD{d:02d}.json"
DISTRICTS = (4, 5)         # Bay Area and Santa Cruz; the catalog region
REGION = (37.0, 38.3, -122.8, -121.4)
CORRIDOR_M = 60            # an edge point this close to the begin-end line is on the closure
POINT_RADIUS_M = 150       # closures whose begin and end are the same point
RAMP_FACILITIES = {"On Ramp", "Off Ramp", "Connector"}
RAMP_CORRIDOR_M = 30       # a ramp edge must run along the begin-end line ...
RAMP_HEADING_DEG = 45      # ... in the same direction
POINT_M = 50               # begin and end closer than this: a point, not a stretch


def _in_place(closure):
    return (closure["code1097"]["isCode1097"] == "true" and closure["code1098"]["isCode1098"] != "true"
            and closure["code1022"]["isCode1022"] != "true")


def _lanes(text):
    """Travel lanes named in "1", "Left HOV", "4, 5, 6, RShoulder"; shoulders and bike lanes do not count."""
    parts = [p.strip().lower() for p in (text or "").split(",")]
    return sum(1 for p in parts if p and "shoulder" not in p and "bike" not in p)


def impact(closure):
    kind = closure["typeOfClosure"]
    if kind == "Full":
        return STATE_IMPACT["CLOSED"]
    if kind in {"One-Way Traffic", "Alternating Lanes"}:
        return STATE_IMPACT["SINGLE_LANE_ALTERNATING"]
    if kind == "Lane":
        total = int(closure["totalExistingLanes"]) if closure["totalExistingLanes"].isdigit() else 0
        closed = _lanes(closure["lanesClosed"])
        if closure["lanesClosed"].strip().lower() == "all" or (total and closed >= total):
            return STATE_IMPACT["CLOSED"]
        return STATE_IMPACT["SOME_LANES_CLOSED"] if closed else None
    return None  # moving closures, shoulder work


def fetch():
    def get(district):
        response = requests.get(URL.format(d=district), timeout=60)
        response.raise_for_status()
        return [item["lcs"] for item in response.json()["data"]]

    with ThreadPoolExecutor(len(DISTRICTS)) as pool:
        return [lcs for batch in pool.map(get, DISTRICTS) for lcs in batch]


def heading(direction):
    """"South" -> 180; "East / West", "N/A" -> None (both ways)."""
    return DIRECTIONS.get(direction[:1].upper()) if direction in {"North", "South", "East", "West"} else None


def _segment_distance(px, py, bx, by):
    """Distance from (px, py) to the segment (0, 0)-(bx, by), in metres."""
    length2 = bx * bx + by * by
    t = 0.0 if not length2 else max(0.0, min(1.0, (px * bx + py * by) / length2))
    return math.hypot(px - t * bx, py - t * by)


def corridor_edges(begin, end, road, travel_heading, edges):
    """Catalog edges on `road` and heading that run along begin -> end."""
    bx, by = _local_m(begin[0], begin[1], *end)
    radius = POINT_RADIUS_M if math.hypot(bx, by) < POINT_M else CORRIDOR_M
    hits = []
    for edge in edges:
        if road and not any(n.replace("-", " ").upper().startswith(road.upper()) for n in edge["names"]):
            continue
        pts = [_local_m(begin[0], begin[1], lat, lon) for lat, lon in edge["shape"]]
        if min(_segment_distance(x, y, bx, by) for x, y in pts) > radius:
            continue
        travel = _bearing(pts[0], pts[-1])
        if travel_heading is not None and min(abs(travel - travel_heading), 360 - abs(travel - travel_heading)) > 60:
            continue  # the other carriageway
        hits.append(edge["id"])
    return hits


def ramp_edges(begin, end, ramps):
    """Ramp edges running along begin -> end; [] when the record is a single point."""
    bx, by = _local_m(begin[0], begin[1], *end)
    if math.hypot(bx, by) < POINT_M:
        return []
    want, hits = _bearing((0, 0), (bx, by)), []
    for edge in ramps:
        pts = [_local_m(begin[0], begin[1], lat, lon) for lat, lon in edge["shape"]]
        if min(_segment_distance(x, y, bx, by) for x, y in pts) > RAMP_CORRIDOR_M:
            continue
        travel = _bearing(pts[0], pts[-1])
        if min(abs(travel - want), 360 - abs(travel - want)) <= RAMP_HEADING_DEG:
            hits.append(edge["id"])
    return hits


def poll(edges):
    """{edge id: factor or "closed"} for every Caltrans closure in place in the region."""
    effects, used = {}, []
    region = [e for e in edges if e["road_class"] in {"motorway", "trunk", "primary", "secondary"}
              and e.get("use") not in NOT_MAINLINE]
    ramps = [e for e in edges if e.get("use") == "ramp"]
    south, north, west, east = REGION
    for lcs in fetch():
        closure, location = lcs["closure"], lcs["location"]
        if not _in_place(closure):
            continue
        effect = impact(closure)
        if effect is None:
            continue
        b, e = location["begin"], location["end"]
        try:
            begin = (float(b["beginLatitude"]), float(b["beginLongitude"]))
            end = (float(e["endLatitude"]), float(e["endLongitude"]))
        except ValueError:
            continue
        if not (south < begin[0] < north and west < begin[1] < east):
            continue
        if closure["facility"] in RAMP_FACILITIES:
            hit = ramp_edges(begin, end, ramps)
        else:
            hit = corridor_edges(begin, end, road_name(b["beginRoute"]), heading(location["travelFlowDirection"]), region)
        if not hit:
            continue
        used.append({"id": lcs["index"], "route": b["beginRoute"], "direction": location["travelFlowDirection"],
                     "from": b["beginLocationName"], "to": e["endLocationName"], "facility": closure["facility"], "type": closure["typeOfClosure"],
                     "lanes": f'{closure["lanesClosed"]}/{closure["totalExistingLanes"]}',
                     "work": closure["typeOfWork"], "effect": effect, "edges": len(hit)})
        for edge_id in hit:
            prev = effects.get(edge_id)
            effects[edge_id] = "closed" if "closed" in (prev, effect) else min(prev or 1.0, effect)
    return effects, used
