"""The road catalog: which Valhalla edges live traffic covers, and their shapes.

Built once per Valhalla tile build (GraphIds are not stable across rebuilds):
major OSM ways in the region are read with pyosmium, and each way (both
directions unless one-way) is map-matched with Valhalla's /trace_attributes,
which returns the edge ids, free-flow speeds and matched geometry. Live
sources then look up colors / incidents along each edge's shape.

    venv/bin/python -m livetraffic.catalog <california.osm.pbf>
"""

import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import osmium
import polyline
import requests

VALHALLA = os.environ.get("VALHALLA_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
CATALOG = os.environ.get("LIVETRAFFIC_CATALOG", os.path.expanduser("~/geo/livetraffic/edges.jsonl.gz"))
BBOX = (37.15, -122.65, 38.15, -121.55)  # Bay Area: south, west, north, east
CLASSES = {"motorway", "trunk", "primary", "secondary", "motorway_link", "trunk_link"}
ATTRIBUTES = ["edge.id", "edge.speed", "edge.names", "edge.road_class", "edge.use", "edge.begin_shape_index",
              "edge.end_shape_index", "shape"]


def read_ways(pbf):
    """[(way id, [(lat, lon), ...] in travel order)] for each direction of travel."""
    out = []
    fp = (osmium.FileProcessor(pbf).with_locations("flex_mem")
          .with_filter(osmium.filter.KeyFilter("highway")))
    for way in fp:
        if not way.is_way() or way.tags.get("highway") not in CLASSES:
            continue
        coords = [(n.location.lat, n.location.lon) for n in way.nodes if n.location.valid()]
        if len(coords) < 2:
            continue
        lat = sum(c[0] for c in coords) / len(coords)
        lon = sum(c[1] for c in coords) / len(coords)
        if not (BBOX[0] < lat < BBOX[2] and BBOX[1] < lon < BBOX[3]):
            continue
        implied = way.tags["highway"].startswith("motorway") or way.tags.get("junction") == "roundabout"
        oneway = way.tags.get("oneway", "yes" if implied else "no")
        if oneway != "-1":
            out.append((way.id, coords))
        if oneway in {"no", "-1"}:
            out.append((way.id, coords[::-1]))
    return out


def match(session, coords):
    """Valhalla edges along one way's geometry: [{id, speed, names, road_class, use, shape}].

    `use` is Valhalla's edge use ("road", "ramp", "turn_channel", ...): road_class alone
    cannot tell a ramp from the freeway beside it."""
    response = session.post(f"{VALHALLA}/trace_attributes", timeout=30, json={
        "shape": [{"lat": lat, "lon": lon} for lat, lon in coords],
        "costing": "auto", "shape_match": "map_snap",
        "filters": {"attributes": ATTRIBUTES, "action": "include"},
    })
    if response.status_code != 200:
        return []
    data = response.json()
    shape = polyline.decode(data.get("shape", ""), 6)
    edges = []
    for edge in data.get("edges", []):
        part = shape[edge["begin_shape_index"]:edge["end_shape_index"] + 1]
        if len(part) >= 2:
            edges.append({"id": edge["id"], "speed": edge.get("speed"), "names": edge.get("names", []),
                          "road_class": edge.get("road_class"), "use": edge.get("use"), "shape": [[round(a, 6), round(b, 6)] for a, b in part]})
    return edges


def build(pbf, workers=8):
    started = time.time()
    ways = read_ways(pbf)
    print(f"{len(ways)} directed ways in {time.time() - started:.0f}s", flush=True)
    catalog, done = {}, 0
    with ThreadPoolExecutor(workers) as pool, requests.Session() as session:
        for edges in pool.map(lambda w: match(session, w[1]), ways, chunksize=64):
            for edge in edges:
                catalog.setdefault(edge["id"], edge)
            done += 1
            if done % 5000 == 0:
                print(f"{done}/{len(ways)} matched, {len(catalog)} edges, {time.time() - started:.0f}s", flush=True)
    os.makedirs(os.path.dirname(CATALOG), exist_ok=True)
    with gzip.open(CATALOG, "wt") as handle:
        for edge in catalog.values():
            handle.write(json.dumps(edge) + "\n")
    print(f"wrote {len(catalog)} edges to {CATALOG} in {time.time() - started:.0f}s", flush=True)


def load():
    with gzip.open(CATALOG, "rt") as handle:
        return [json.loads(line) for line in handle]


if __name__ == "__main__":
    build(os.path.expanduser(sys.argv[1]))
