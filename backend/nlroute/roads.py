"""Named roads as points, for "take 280" / "use Skyline Boulevard".

The routing graph can't be searched by name, but Valhalla's /locate returns
the nearest edge of a given road class to any point, with its names and its
direction of travel. Sampling a grid over the trip and keeping the edges that
carry the road gives points on it, offline. The planner then routes THROUGH
one of them, facing the way the driver is going: the cheapest way to make
Valhalla use a road it would otherwise skip, with no extra stop.
"""

import math

from .geo import haversine_m
from .rules import road_name_matches
from . import valhalla

GRID = 12
# Freeways first; streets and state routes need the wider classes.
CLASS_PASSES = ["motorway", "primary", "tertiary"]


def road_points(name, bbox, grid=GRID):
    """Points on the named road within bbox (south, west, north, east):
    [{"lat", "lng", "heading", "roadClass", "names"}]."""
    south, west, north, east = bbox
    samples = [(south + (north - south) * (i + 0.5) / grid, west + (east - west) * (j + 0.5) / grid)
               for i in range(grid) for j in range(grid)]
    found, seen = [], set()
    for road_class in CLASS_PASSES:
        locations = [{"lat": lat, "lon": lon,
                      "search_filter": {"min_road_class": road_class, "exclude_ramp": True}}
                     for lat, lon in samples]
        for loc in valhalla._post("locate", {"verbose": True, "costing": "auto", "locations": locations}):
            for edge in loc.get("edges") or []:
                names = (edge.get("edge_info") or {}).get("names") or []
                key = (edge.get("edge_id") or {}).get("value")
                if key in seen or not road_name_matches(name, names):
                    continue
                seen.add(key)
                found.append({
                    "lat": edge["correlated_lat"], "lng": edge["correlated_lon"],
                    "heading": round(edge.get("heading") or 0),
                    "roadClass": ((edge.get("edge") or {}).get("classification") or {}).get("classification"),
                    "names": names,
                })
        if found:
            break
    return found


def _bearing(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _angle(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def via_candidates(points, origin, destination, limit=24):
    """Points on the road whose direction of travel heads toward the
    destination, best first by straight-line detour."""
    o, d = (origin["lat"], origin["lng"]), (destination["lat"], destination["lng"])
    direct = haversine_m(*o, *d)
    out = []
    for p in points:
        here = (p["lat"], p["lng"])
        if haversine_m(*here, *d) > 300 and _angle(p["heading"], _bearing(here, d)) > 90:
            continue  # the other carriageway, or driving away
        detour = haversine_m(*o, *here) + haversine_m(*here, *d) - direct
        # How far along the trip it sits (0 = start, 1 = end), by distance.
        along = haversine_m(*o, *here) / max(1.0, haversine_m(*o, *here) + haversine_m(*here, *d))
        out.append(dict(p, detour=detour, along=round(along, 3)))
    # "Take 280" means the main stretch: a point just past the start would
    # satisfy it on paper and then leave the road. Prefer the middle band.
    middle = [c for c in out if 0.3 <= c["along"] <= 0.7]
    pool = middle or out
    pool.sort(key=lambda c: c["detour"])
    return pool[:limit]


def bbox_for(points, pad_frac=0.3, min_pad=0.05):
    lats = [p["lat"] for p in points]
    lngs = [p["lng"] for p in points]
    pad_lat = max(min_pad, (max(lats) - min(lats)) * pad_frac)
    pad_lng = max(min_pad, (max(lngs) - min(lngs)) * pad_frac)
    return (min(lats) - pad_lat, min(lngs) - pad_lng, max(lats) + pad_lat, max(lngs) + pad_lng)


def insert_vias(points, vias):
    """Put each through-point between the two consecutive points where it adds
    the least straight-line distance."""
    points = list(points)
    for via in vias:
        best = min(range(1, len(points)), key=lambda i: (
            haversine_m(points[i - 1]["lat"], points[i - 1]["lng"], via["lat"], via["lng"])
            + haversine_m(via["lat"], via["lng"], points[i]["lat"], points[i]["lng"])
            - haversine_m(points[i - 1]["lat"], points[i - 1]["lng"], points[i]["lat"], points[i]["lng"])))
        points.insert(best, via)
    return points
