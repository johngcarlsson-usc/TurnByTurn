"""Live flow from TomTom vector traffic tiles (free tier, FT_TOMTOM_API_KEY).

Each line in a `flow/relative` tile carries `traffic_level`: current speed as a
fraction of free flow (0-1), plus `road_closure`. Tiles are drawn into a
fine raster (z11 tiles at 8x = about 10 m per pixel) and sampled along each catalog
edge like the Google colors.

Budget: the free tier allows about 200k tile requests a month. 52 z11 tiles cover
the Bay Area catalog, so one sweep every 15 min uses about 150k.

Free-flow speeds (run.py applies the ratio to these, not to Valhalla's typical
speed): `python -m livetraffic.tomtom freeflow` fetches the relative tiles and
the `absolute` ones (traffic_level = current km/h) once, 104 requests, and keeps
absolute / relative per edge where traffic is moving at half of free flow or better.
"""

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import mapbox_vector_tile
import requests
from PIL import Image, ImageDraw

from .google_tiles import sample_points

URL = "https://api.tomtom.com/traffic/map/4/tile/flow/{kind}/{z}/{x}/{y}.pbf"
FREEFLOW = os.environ.get("TOMTOM_FREEFLOW", os.path.expanduser("~/geo/livetraffic/tomtom_freeflow.json"))
ZOOM = 11
SCALE = 8                  # raster pixels per 256-px tile pixel (z11 -> z14 resolution)
LINE_WIDTH = 3
CLOSED = 2000              # raster value for a closed road
SEARCH_RADIUS = 2
MIN_COVERAGE = 0.3


def _raster(content):
    """A 2048x2048 'I' image: 0 = no data, 1..1001 = traffic_level*1000+1, CLOSED."""
    size = 256 * SCALE
    image = Image.new("I", (size, size), 0)
    draw = ImageDraw.Draw(image)
    layers = mapbox_vector_tile.decode(content, default_options={"y_coord_down": True})
    for layer in layers.values():
        extent = layer.get("extent", 4096)
        k = size / extent
        for feature in layer["features"]:
            props, geom = feature["properties"], feature["geometry"]
            value = CLOSED if props.get("road_closure") else int(round(float(props.get("traffic_level", 1)) * 1000)) + 1
            lines = [geom["coordinates"]] if geom["type"] == "LineString" else (
                geom["coordinates"] if geom["type"] == "MultiLineString" else [])
            for line in lines:
                if len(line) >= 2:
                    draw.line([(x * k, y * k) for x, y in line], fill=value, width=LINE_WIDTH)
    return image


def fetch(tiles, key, workers=4, kind="relative"):
    session = requests.Session()

    def get(tile):
        x, y = tile
        try:
            response = session.get(URL.format(kind=kind, z=ZOOM, x=x, y=y), params={"key": key}, timeout=20)
            if response.status_code == 200:
                return tile, _raster(response.content)
        except (requests.RequestException, ValueError):
            pass
        return tile, None

    with ThreadPoolExecutor(workers) as pool:
        images = dict(pool.map(get, sorted(tiles)))
    ok = {t: im for t, im in images.items() if im is not None}
    if tiles and len(ok) < 0.5 * len(tiles):
        raise RuntimeError(f"only {len(ok)}/{len(tiles)} TomTom tiles came back (key or quota?)")
    return ok


def edge_ratios(edges, images):
    """{edge id: ratio 0-1 or "closed"}; the 25th-percentile sample (a jam on part of an edge counts)."""
    out = {}
    fine_zoom = ZOOM + int(math.log2(SCALE))
    for edge in edges:
        values, total = [], 0
        for x, y in sample_points(edge["shape"], fine_zoom):
            total += 1
            image = images.get((int(x // (256 * SCALE)), int(y // (256 * SCALE))))
            if image is None:
                continue
            px, py = int(x % (256 * SCALE)), int(y % (256 * SCALE))
            found = 0
            for r in range(SEARCH_RADIUS + 1):
                for ox in range(-r, r + 1):
                    for oy in range(-r, r + 1):
                        if not found and 0 <= px + ox < 256 * SCALE and 0 <= py + oy < 256 * SCALE:
                            found = image.getpixel((px + ox, py + oy))
                if found:
                    break
            if found:
                values.append(found)
        if total and len(values) >= MIN_COVERAGE * total:
            if values.count(CLOSED) > len(values) / 2:
                out[edge["id"]] = "closed"
                continue
            levels = sorted((v - 1) / 1000 for v in values if v != CLOSED)
            if levels:
                out[edge["id"]] = levels[int(0.25 * (len(levels) - 1))]
    return out


def poll(edges):
    key = os.environ.get("FT_TOMTOM_API_KEY")
    if not key:
        raise RuntimeError("FT_TOMTOM_API_KEY is not set")
    started = time.time()
    from .google_tiles import tiles_for
    tiles = tiles_for(edges, ZOOM)
    images = fetch(tiles, key)
    ratios = edge_ratios(edges, images)
    return ratios, {"tiles": len(tiles), "fetched": len(images), "edges": len(ratios),
                    "closed": sum(1 for v in ratios.values() if v == "closed"),
                    "slow": sum(1 for v in ratios.values() if v != "closed" and v < 0.6),
                    "seconds": round(time.time() - started, 1)}


def freeflow(edges):
    """{edge id: free-flow km/h} from one relative and one absolute sweep; written to FREEFLOW."""
    from . import _load_local_env
    from .google_tiles import tiles_for
    _load_local_env()
    key, tiles = os.environ["FT_TOMTOM_API_KEY"], tiles_for(edges, ZOOM)
    rel = edge_ratios(edges, fetch(tiles, key))
    kph = edge_ratios(edges, fetch(tiles, key, kind="absolute"))  # same raster code: value / 1000 = km/h
    out = {str(e): kph[e] / rel[e] for e in rel
           if isinstance(rel[e], float) and rel[e] >= 0.5 and isinstance(kph.get(e), float) and kph[e] > 0}
    with open(FREEFLOW + ".tmp", "w") as handle:
        json.dump(out, handle)
    os.replace(FREEFLOW + ".tmp", FREEFLOW)
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["freeflow"]:
        from . import catalog
        print(len(freeflow(catalog.load())), "edges with a TomTom free-flow speed in", FREEFLOW)
