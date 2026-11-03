"""Caltrans District 4 changeable message signs (cwwp2.dot.ca.gov, public, no key).

About 40 Bay Area freeway signs show travel times to three destinations each
("SF DWNTWN 22 MIN"), refreshed every minute. Per the 511 FAQ these times come
from probe data 511 buys from HERE and are updated every 2 minutes; Caltrans
publishes them under its Conditions of Use. They are the only public live
freeway signal we have found, so they are the open version's live measurement.

Each (sign, destination) is a corridor:
- where it ends: the destination text with its abbreviations spelled out,
  geocoded with our Photon near the sign; a junction ("HWY 1", "S880/280") ends
  at the nearest catalog freeway edge of that route ahead of the sign;
- which edges it covers: the freeway mainline edges of Valhalla's route from
  the sign to that end (cached in CORRIDORS);
- how slow it is: its own lowest time seen in the logged history (at least one
  night, 1-5 AM, so it is free flow) divided by the time shown now. Comparing a
  sign with itself avoids depending on where exactly Caltrans puts the end point.
An edge on several corridors takes the shortest one's ratio (the most local).
"""

import glob
import gzip
import json
import math
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .chp import _bearing, _local_m

URL = "https://cwwp2.dot.ca.gov/data/d4/cms/cmsStatusD04.json"
VALHALLA = os.environ.get("VALHALLA_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
PHOTON = os.environ.get("PHOTON_URL", "http://127.0.0.1:2322").rstrip("/")
CORRIDORS = os.path.expanduser("~/geo/livetraffic/cms_corridors.json")
HISTORY_GLOB = "cms-*.jsonl.gz"
PACIFIC = ZoneInfo("America/Los_Angeles")
MIN_REFERENCE_MIN = 8      # shorter corridors: whole-minute rounding alone moves the ratio 10%+
NIGHT_HOURS = range(1, 5)  # the reference needs a reading from these local hours
TIME = re.compile(r"^(.+?)\s+(\d+)(?:\s*-\s*(\d+))?\s*MIN$")
JUNCTION = re.compile(r"^(?:HWY|RTE|JCT|SR)?\s*[NSEW]?\s*\d+(?:\s*/\s*[NSEW]?\s*\d+)*$")
# words on D4 signs -> spelled out for the geocoder
WORDS = {"DWNTWN": "Downtown", "ARPT": "Airport", "BRDG": "Bridge", "VY": "Valley", "CRZ": "Cruz",
         "CR": "Creek", "CK": "Creek", "STA": "Santa", "PK": "Park", "PRK": "Park", "PT": "Point",
         "HEALDSBRG": "Healdsburg", "SACRAMNTO": "Sacramento", "MORG": "Morgan", "ROHNRT": "Rohnert",
         "CARQ": "Carquinez", "SJ": "San Jose", "SJC": "San Jose", "SF": "San Francisco",
         "SM": "San Mateo", "OAK": "Oakland", "HALF": "Half"}


def fetch():
    """[{sign, route, direction, lat, lon, label, minutes}] for every travel time shown now."""
    out = []
    for item in requests.get(URL, timeout=30).json()["data"]:
        cms = item["cms"]
        if cms.get("inService") != "true":
            continue
        loc, msg = cms["location"], cms["message"]
        for phase in ("phase1", "phase2"):
            for i in (1, 2, 3):
                m = TIME.match(msg.get(phase, {}).get(f"{phase}Line{i}", "").strip())
                if m:
                    low, high = int(m.group(2)), int(m.group(3) or m.group(2))
                    out.append({"sign": loc["locationName"].split()[0].rstrip("-"), "route": loc["route"],
                                "direction": loc["direction"], "lat": float(loc["latitude"]),
                                "lon": float(loc["longitude"]), "label": " ".join(m.group(1).split()),
                                "minutes": (low + high) / 2})
    return out


def key(t):
    return f'{t["sign"]}|{t["label"]}'


# ------------------------------------------------------------------ corridors
def _route_numbers(text):
    return [n for n in re.findall(r"\d+", text)]


def _junction_end(t, edges):
    """The nearest catalog freeway edge of the named route ahead of the sign."""
    own = _route_numbers(t["route"])
    wanted = [n for n in _route_numbers(t["label"]) if n not in own][:1]
    if not wanted:
        return None
    heading = {"North": 0, "East": 90, "South": 180, "West": 270}.get(t["direction"])
    best = None
    for edge in edges:
        if not any(re.match(rf"(?i)(I|US|CA|SR)[ -]?{wanted[0]}\b", n) for n in edge["names"]):
            continue
        lat, lon = edge["shape"][len(edge["shape"]) // 2]
        x, y = _local_m(t["lat"], t["lon"], lat, lon)
        d = math.hypot(x, y)
        if heading is not None and d > 500:
            b = _bearing((0, 0), (x, y))
            if min(abs(b - heading), 360 - abs(b - heading)) > 90:
                continue  # behind the sign
        if best is None or d < best[0]:
            best = (d, lat, lon)
    return best[1:] if best else None


def _place_end(t):
    words = [WORDS.get(w, w.title()) for w in t["label"].split()]
    reply = requests.get(f"{PHOTON}/api", timeout=10,
                         params={"q": " ".join(words), "limit": 1, "lat": t["lat"], "lon": t["lon"]}).json()
    if not reply.get("features"):
        return None
    lon, lat = reply["features"][0]["geometry"]["coordinates"]
    return lat, lon


def corridor(t, edges, mainline_ids):
    """Freeway mainline edge ids from the sign to the destination, or None."""
    end = _junction_end(t, edges) if JUNCTION.match(t["label"]) else _place_end(t)
    if not end:
        return None
    heading = {"North": 0, "East": 90, "South": 180, "West": 270}.get(t["direction"])
    start = {"lat": t["lat"], "lon": t["lon"]}
    if heading is not None:
        start.update(heading=heading, heading_tolerance=60)  # the sign's carriageway
    trip = requests.post(f"{VALHALLA}/route", timeout=30, json={
        "locations": [start, {"lat": end[0], "lon": end[1]}], "costing": "auto"}).json().get("trip")
    if not trip:
        return None
    walked = requests.post(f"{VALHALLA}/trace_attributes", timeout=30, json={
        "encoded_polyline": trip["legs"][0]["shape"], "costing": "auto", "shape_match": "edge_walk",
        "filters": {"attributes": ["edge.id"], "action": "include"}}).json().get("edges") or []
    ids = [e["id"] for e in walked if e["id"] in mainline_ids]
    return ids or None


def load_corridors(times, edges):
    """{key: [edge ids]}, built once per (sign, destination) and cached in CORRIDORS."""
    cache = {}
    if os.path.exists(CORRIDORS):
        with open(CORRIDORS) as handle:
            cache = json.load(handle)
    missing = [t for t in times if key(t) not in cache]
    if missing:
        freeways = [e for e in edges if e["road_class"] in {"motorway", "trunk"} and e.get("use") == "road"]
        ids = {e["id"] for e in freeways}
        for t in missing:
            cache[key(t)] = corridor(t, freeways, ids)
        with open(CORRIDORS + ".tmp", "w") as handle:
            json.dump(cache, handle)
        os.replace(CORRIDORS + ".tmp", CORRIDORS)
    return cache


# ------------------------------------------------------------------ reference
def references(history_dir):
    """{key: lowest minutes logged}, only for keys with a night-time (1-5 AM) reading.
    A logged value is the minutes, or a record {"min": minutes, ...corridor inputs} (run.poll_cms)."""
    low, night = {}, set()
    for path in glob.glob(os.path.join(history_dir, HISTORY_GLOB)):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                record = json.loads(line)
                hour = datetime.fromisoformat(record["at"]).astimezone(PACIFIC).hour
                for k, value in record["values"].items():
                    minutes = value["min"] if isinstance(value, dict) else value
                    low[k] = min(low.get(k, minutes), minutes)
                    if hour in NIGHT_HOURS:
                        night.add(k)
    return {k: v for k, v in low.items() if k in night and v >= MIN_REFERENCE_MIN}


_REFS = {}  # {"hour", "refs"}: references, re-read once an hour


def poll(edges, history_dir):
    """({edge id: ratio}, info, {key: minutes shown}, {key: [corridor edge ids]})."""
    times = fetch()
    shown = {key(t): t["minutes"] for t in times}
    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    if _REFS.get("hour") != hour:
        _REFS.update(hour=hour, refs=references(history_dir))
    refs, corridors = _REFS["refs"], load_corridors(times, edges)
    ratios, used = {}, 0
    for k, minutes in sorted(shown.items(), key=lambda kv: refs.get(kv[0], 1e9)):  # shortest first
        ids = corridors.get(k)
        if k not in refs or not ids:
            continue
        used += 1
        ratio = 1.0 if minutes - refs[k] <= 1 else max(0.05, min(1.0, refs[k] / minutes))  # 1 min = rounding
        for edge_id in ids:
            ratios.setdefault(edge_id, ratio)
    return ratios, {"times": len(shown), "withReference": sum(k in refs for k in shown),
                    "used": used, "edges": len(ratios),
                    "corridorsMissing": sum(1 for k in shown if not corridors.get(k))}, shown, corridors
