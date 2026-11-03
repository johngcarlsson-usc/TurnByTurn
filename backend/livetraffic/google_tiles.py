"""Live congestion from Google Maps traffic-tile colors (research use).

Method from the World Bank googletraffic package and arXiv 2105.12235: fetch
the traffic tiles, classify road pixels into Google's four colors, and sample
them along each catalog edge. Colors are taken from live tiles:
#16e098 free flow, #ffd042 slow, #f34d42 congested, #a82626 stop-and-go.
Lines for each direction of travel are drawn on the right of the road
(US), so samples sit slightly right of the edge's direction of travel.
"""

import io
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

TILE_URL = os.environ.get("GOOGLE_TRAFFIC_TILE_URL",
                          "https://mts{s}.google.com/vt/lyrs=h,traffic&x={x}&y={y}&z={z}&style=3")
ZOOM = 14
PALETTE = {1: (0x16, 0xE0, 0x98), 2: (0xFF, 0xD0, 0x42), 3: (0xF3, 0x4D, 0x42), 4: (0xA8, 0x26, 0x26)}
MAX_COLOR_DISTANCE = 60     # RGB distance to count a pixel as a traffic color
SAMPLE_STEP_PX = 2          # sample every 2 px along an edge
RIGHT_OFFSET_PX = 1.5       # direction-of-travel line sits right of the centerline
SEARCH_RADIUS_PX = 2
MIN_COVERAGE = 0.3          # share of samples that must be colored to trust an edge
HEADERS = {"User-Agent": "Mozilla/5.0"}


def to_pixel(lat, lon, z=ZOOM):
    """Global pixel coordinates (256-px tiles) at zoom z."""
    scale = 256 * 2 ** z
    x = (lon + 180) / 360 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def classify(pixel):
    r, g, b, a = pixel
    if a < 150:
        return None
    level, best = None, MAX_COLOR_DISTANCE ** 2
    for lvl, (pr, pg, pb) in PALETTE.items():
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best:
            level, best = lvl, d
    return level


def sample_points(shape, z=ZOOM):
    """Pixel points along an edge, shifted right of travel."""
    pts = [to_pixel(lat, lon, z) for lat, lon in shape]
    out = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        # right-hand normal in screen coordinates (y grows downward)
        nx, ny = -dy / length, dx / length
        steps = max(1, int(length / SAMPLE_STEP_PX))
        for i in range(steps):
            t = (i + 0.5) / steps
            out.append((x0 + dx * t + nx * RIGHT_OFFSET_PX, y0 + dy * t + ny * RIGHT_OFFSET_PX))
    return out


def tiles_for(edges, z=ZOOM):
    tiles = set()
    for edge in edges:
        for x, y in sample_points(edge["shape"], z):
            tiles.add((int(x // 256), int(y // 256)))
    return tiles


def fetch_tiles(tiles, z=ZOOM, workers=4):
    """{(x, y): RGBA image} for the tiles that came back; raises if most fail (blocked)."""
    session = requests.Session()
    session.headers.update(HEADERS)

    def get(tile):
        x, y = tile
        url = TILE_URL.format(s=(x + y) % 4, x=x, y=y, z=z)
        try:
            response = session.get(url, timeout=15)
            if response.status_code == 200 and response.headers.get("content-type", "").startswith("image"):
                return tile, Image.open(io.BytesIO(response.content)).convert("RGBA")
        except requests.RequestException:
            pass
        return tile, None

    with ThreadPoolExecutor(workers) as pool:
        images = dict(pool.map(get, sorted(tiles)))
    ok = {t: im for t, im in images.items() if im is not None}
    if tiles and len(ok) < 0.5 * len(tiles):
        raise RuntimeError(f"only {len(ok)}/{len(tiles)} traffic tiles came back (blocked?)")
    return ok


def edge_levels(edges, images, z=ZOOM):
    """{edge id: congestion level 1-4} for edges with enough colored samples.

    The level is the 75th-percentile sample, so a jam on part of a long edge counts.
    """
    out = {}
    for edge in edges:
        levels, total = [], 0
        for x, y in sample_points(edge["shape"], z):
            total += 1
            image = images.get((int(x // 256), int(y // 256)))
            if image is None:
                continue
            px, py = int(x % 256), int(y % 256)
            found = None
            for r in range(SEARCH_RADIUS_PX + 1):
                for ox in range(-r, r + 1):
                    for oy in range(-r, r + 1):
                        qx, qy = px + ox, py + oy
                        if 0 <= qx < 256 and 0 <= qy < 256:
                            found = classify(image.getpixel((qx, qy)))
                            if found:
                                break
                    if found:
                        break
                if found:
                    break
            if found:
                levels.append(found)
        if total and len(levels) >= MIN_COVERAGE * total:
            levels.sort()
            out[edge["id"]] = levels[min(len(levels) - 1, int(0.75 * len(levels)))]
    return out


def poll(edges, z=ZOOM):
    started = time.time()
    tiles = tiles_for(edges, z)
    images = fetch_tiles(tiles, z)
    levels = edge_levels(edges, images, z)
    return levels, {"tiles": len(tiles), "fetched": len(images), "edges": len(levels),
                    "seconds": round(time.time() - started, 1)}
