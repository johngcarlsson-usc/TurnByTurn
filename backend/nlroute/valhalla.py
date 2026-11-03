import json
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import requests

from .geo import decode_shape


VALHALLA_BASE_URL = os.environ.get("VALHALLA_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
VALHALLA_TIMEOUT_SECONDS = float(os.environ.get("VALHALLA_TIMEOUT_SECONDS", "20"))


class ValhallaError(RuntimeError):
    def __init__(self, message, payload=None):
        super().__init__(message)
        self.payload = payload


# Live traffic (backend/livetraffic, outside data) writes Valhalla's traffic.tar
# and a status file. Valhalla only applies live speeds to requests that depart
# "now" (date_time type 0), so those are added here, and only while the live
# data is fresh; LIVE_TRAFFIC=0 keeps every route on typical speeds.
LIVE_TRAFFIC = os.environ.get("LIVE_TRAFFIC", "1").strip().lower() not in {"0", "false", "no", "off"}
LIVE_STATUS = os.environ.get("LIVETRAFFIC_STATUS", os.path.expanduser("~/geo/livetraffic/status.json"))
LIVE_MAX_AGE_S = 600


def live_traffic():
    """What live traffic routes use right now: {"on", "label", "sources", "ageSeconds"}."""
    if not LIVE_TRAFFIC:
        return {"on": False, "label": "Typical speeds (live traffic switched off)"}
    try:
        with open(LIVE_STATUS) as handle:
            status = json.load(handle)
        written = datetime.fromisoformat(status["written"])
    except (OSError, ValueError, KeyError):
        return {"on": False, "label": "Typical speeds (no live traffic running)"}
    age = (datetime.now(timezone.utc) - written).total_seconds()
    names = {"google": "Google traffic colors", "tomtom": "TomTom flow", "chp": "CHP incidents",
             "sf511": "511 SF Bay events", "lcs": "Caltrans lane closures"}
    # only sources that change speeds (bus positions are logged, not used yet)
    sources = [name for name, s in status.get("sources", {}).items() if s.get("ok") and name in names]
    if age > LIVE_MAX_AGE_S or not sources:
        return {"on": False, "label": "Typical speeds (live traffic is stale)", "ageSeconds": round(age)}
    return {"on": True, "ageSeconds": round(age), "sources": sources,
            "label": "Live traffic: " + " + ".join(names[s] for s in sources)}


def _with_live(action, body):
    if action in {"route", "sources_to_targets"} and "date_time" not in body and live_traffic()["on"]:
        return {**body, "date_time": {"type": 0}}
    return body


def _post(action, body, timeout=VALHALLA_TIMEOUT_SECONDS, live=True):
    if live:
        body = _with_live(action, body)
    response = requests.post(f"{VALHALLA_BASE_URL}/{action}", json=body, timeout=timeout)
    data = response.json()
    if response.status_code != 200:
        raise ValhallaError(data.get("error") or f"Valhalla {action} failed", data)
    return data


def location(point, kind="break"):
    loc = {"lat": point["lat"], "lon": point["lng"], "type": point.get("kind") or kind}
    if point.get("label"):
        loc["name"] = point["label"]
    if point.get("preferredSide"):
        loc["preferred_side"] = point["preferredSide"]
    if point.get("heading") is not None:
        # A through-point on a named road ("take 280"): face the direction of
        # travel and snap only to that road's class, never a frontage road.
        loc["heading"] = point["heading"]
        loc["heading_tolerance"] = 45
    if point.get("roadClass"):
        loc["search_filter"] = {"min_road_class": point["roadClass"], "exclude_ramp": True}
    return loc


def route(points, costing="auto", costing_options=None, exclude_locations=None,
          exclude_polygons=None, alternates=0):
    """Route through points in order. Returns a list of normalized trips
    (the first is the primary, the rest Valhalla alternates)."""
    body = {
        "locations": [location(p) for p in points],
        "costing": costing,
        "directions_options": {"units": "miles"},
    }
    if costing_options:
        body["costing_options"] = {costing: costing_options}
    if exclude_locations:
        body["exclude_locations"] = [{"lat": p["lat"], "lon": p["lng"]} for p in exclude_locations]
    if exclude_polygons:
        body["exclude_polygons"] = exclude_polygons
    if alternates and len(points) == 2:
        body["alternates"] = alternates
    data = _post("route", body)
    trips = [data["trip"], *[alt["trip"] for alt in data.get("alternates", [])]]
    return [normalize_trip(trip) for trip in trips]


def route_osrm_text(points, costing="auto", costing_options=None, exclude_locations=None,
                    exclude_polygons=None):
    """The same route in OSRM format with banner and voice instructions:
    the raw response text Ferrostar's RouteAdapter parses for navigation."""
    body = {
        "locations": [location(p) for p in points],
        "costing": costing,
        "format": "osrm",
        "banner_instructions": True,
        "voice_instructions": True,
        "directions_options": {"units": "miles"},
    }
    if costing_options:
        body["costing_options"] = {costing: costing_options}
    if exclude_locations:
        body["exclude_locations"] = [{"lat": p["lat"], "lon": p["lng"]} for p in exclude_locations]
    if exclude_polygons:
        body["exclude_polygons"] = exclude_polygons
    response = requests.post(f"{VALHALLA_BASE_URL}/route", json=_with_live("route", body),
                             timeout=VALHALLA_TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise ValhallaError(response.text[:300])
    return response.text


def normalize_trip(trip):
    coords = []
    steps = []
    leg_shapes = []
    leg_durations = []
    for leg in trip["legs"]:
        leg_coords = decode_shape(leg["shape"])
        offset = len(coords)
        # Consecutive legs share their joining vertex.
        if coords and leg_coords and coords[-1] == leg_coords[0]:
            leg_coords_to_add = leg_coords[1:]
            offset -= 1
        else:
            leg_coords_to_add = leg_coords
        coords.extend(leg_coords_to_add)
        leg_shapes.append(leg["shape"])
        leg_durations.append(leg["summary"]["time"])
        for maneuver in leg.get("maneuvers", []):
            steps.append({
                "instruction": maneuver.get("instruction"),
                "verbalPreTransition": maneuver.get("verbal_pre_transition_instruction"),
                "streetNames": maneuver.get("street_names") or [],
                "distanceMiles": maneuver.get("length", 0.0),
                "durationSeconds": maneuver.get("time", 0.0),
                "type": maneuver.get("type"),
                "bearingBefore": maneuver.get("bearing_before"),
                "bearingAfter": maneuver.get("bearing_after"),
                "beginShapeIndex": maneuver.get("begin_shape_index", 0) + offset,
            })
    summary = trip["summary"]
    return {
        "coordinates": [[lon, lat] for lat, lon in coords],
        "latlons": coords,
        "legShapes": leg_shapes,
        "legDurations": leg_durations,
        "sidesOfStreet": [loc.get("side_of_street") for loc in trip.get("locations", [])],
        "steps": steps,
        "distanceMiles": summary.get("length", 0.0),
        "durationSeconds": summary.get("time", 0.0),
        "hasToll": bool(summary.get("has_toll")),
        "hasHighway": bool(summary.get("has_highway")),
        "hasFerry": bool(summary.get("has_ferry")),
    }


EDGE_ATTRIBUTES = [
    "edge.names", "edge.road_class", "edge.use", "edge.toll", "edge.tunnel", "edge.bridge",
    "edge.begin_shape_index", "edge.end_shape_index", "edge.way_id", "edge.speed", "edge.length",
]


def edges_for_route(trip, costing="auto"):
    """Map-match a solved route back onto graph edges, one call per leg.

    This is the verifier's view of the route: it reads what roads the line
    actually drives on, independently of what the planner asked for.
    """
    edges = []
    for shape in trip["legShapes"]:
        data = _post("trace_attributes", {
            "encoded_polyline": shape,
            # edge_walk first, map-matching fallback: very short legs can defeat
            # an exact walk ("failed to find exact route match").
            "shape_match": "walk_or_snap",
            "costing": costing,
            "filters": {"attributes": EDGE_ATTRIBUTES, "action": "include"},
        })
        leg_points = decode_shape(shape)
        for edge in data.get("edges", []):
            begin = edge.get("begin_shape_index", 0)
            end = edge.get("end_shape_index", begin)
            edges.append({
                "names": edge.get("names") or [],
                "lengthMiles": (edge.get("length") or 0.0) * 0.621371,  # trace_attributes reports km
                "roadClass": edge.get("road_class"),
                "use": edge.get("use"),
                "toll": bool(edge.get("toll")),
                "tunnel": bool(edge.get("tunnel")),
                "bridge": bool(edge.get("bridge")),
                "wayId": edge.get("way_id"),
                "latlons": leg_points[begin:end + 1],
            })
    return edges


STREET_SNAP = {"min_road_class": "residential"}
# A live (time-dependent) matrix needs service_limits.max_timedep_distance_matrix
# > 0 in valhalla.json; with 0, Valhalla silently ignores date_time and returns
# typical-speed times. It is fast on reachable points (20x20 street points:
# 3.5 s live vs 0.7 s typical) but expands across the state when a pair cannot
# connect (random points incl. water: 155 s), so a live call that takes longer
# than this is retried on typical speeds.
LIVE_MATRIX_TIMEOUT_S = 8


def _matrix_call(sources, targets, costing, costing_options, exclude_locations, exclude_polygons,
                 resnap_sources=(), resnap_targets=()):
    def loc(point, resnap):
        out = {"lat": point["lat"], "lon": point["lng"]}
        if resnap:
            out["search_filter"] = STREET_SNAP
        return out

    body = {
        "sources": [loc(p, i in resnap_sources) for i, p in enumerate(sources)],
        "targets": [loc(p, j in resnap_targets) for j, p in enumerate(targets)],
        "costing": costing,
        "units": "miles",
    }
    if costing_options:
        body["costing_options"] = {costing: costing_options}
    if exclude_locations:
        body["exclude_locations"] = [{"lat": p["lat"], "lon": p["lng"]} for p in exclude_locations]
    if exclude_polygons:
        body["exclude_polygons"] = exclude_polygons
    with_live = _with_live("sources_to_targets", body)
    live = "date_time" in with_live
    try:
        rows = _post("sources_to_targets", with_live,
                     LIVE_MATRIX_TIMEOUT_S if live else VALHALLA_TIMEOUT_SECONDS, live=False)
    except requests.Timeout:
        if not live:
            raise
        live = False
        rows = _post("sources_to_targets", body, live=False)
    rows = rows["sources_to_targets"]
    return ([[cell.get("time") for cell in row] for row in rows],
            [[cell.get("distance") for cell in row] for row in rows], live)


def _mostly_null(cells):
    nulls = sum(cell is None for cell in cells)
    return nulls > 0 and nulls * 2 >= len(cells)


def matrix(sources, targets, costing="auto", costing_options=None, exclude_locations=None,
           exclude_polygons=None):
    """Durations (s) and distances (mi) between point lists.

    Matrix nulls are NOT proof of unreachability. Measured on 400 random Bay
    Area shops: 8 (2%) came back null in their whole row and column, every
    one snapped to a service road or parking aisle, and /route reached all 8.
    Valhalla's CostMatrix fails on some such edges (not dead ends, no access
    restrictions: the cause is inside costmatrix). Re-snapping just those
    points to the nearest street fixed all 8, so:

    1. points whose row or column is mostly null are re-snapped to the street
       and only their rows/columns recomputed;
    2. any null left after that is backfilled with a direct /route;
    3. only a pair /route also fails on stays None.
    """
    opts = (costing, costing_options, exclude_locations, exclude_polygons)
    durations, distances, live = _matrix_call(sources, targets, *opts)

    bad_sources = {i for i, row in enumerate(durations) if _mostly_null(row)}
    bad_targets = {j for j in range(len(targets)) if _mostly_null([row[j] for row in durations])}
    if bad_sources:
        rows = sorted(bad_sources)
        d, m, live_rows = _matrix_call([sources[i] for i in rows], targets, *opts,
                            resnap_sources=set(range(len(rows))), resnap_targets=bad_targets)
        live = live and live_rows
        for k, i in enumerate(rows):
            for j in range(len(targets)):
                if d[k][j] is not None:
                    durations[i][j], distances[i][j] = d[k][j], m[k][j]
    if bad_targets:
        cols = sorted(bad_targets)
        d, m, live_cols = _matrix_call(sources, [targets[j] for j in cols], *opts,
                            resnap_sources=bad_sources, resnap_targets=set(range(len(cols))))
        live = live and live_cols
        for i in range(len(sources)):
            for k, j in enumerate(cols):
                if d[i][k] is not None:
                    durations[i][j], distances[i][j] = d[i][k], m[i][k]

    missing = []
    for i, source in enumerate(sources):
        for j, target in enumerate(targets):
            if durations[i][j] is not None:
                continue
            if source["lat"] == target["lat"] and source["lng"] == target["lng"]:
                durations[i][j], distances[i][j] = 0.0, 0.0
            else:
                missing.append((i, j))

    def backfill(pair):
        i, j = pair
        try:
            return pair, route([sources[i], targets[j]], *opts)[0]
        except ValhallaError:
            return pair, None

    if missing:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for (i, j), trip in pool.map(backfill, missing):
                if trip is not None:
                    durations[i][j] = trip["durationSeconds"]
                    distances[i][j] = trip["distanceMiles"]
    matrix.last_stats = {"resnappedSources": len(bad_sources), "resnappedTargets": len(bad_targets),
                         "routeBackfills": len(missing), "liveTraffic": live}
    return durations, distances
