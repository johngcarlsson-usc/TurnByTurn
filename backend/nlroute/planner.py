"""Constraint IR -> verified route, recording every step for the UI.

Each stage appends to `trace`, which the app shows as the "show your work"
panel: what was resolved, what was searched, what the solver chose, which
edges the exclusion loop removed, and what the verifier concluded.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import custom, geocode, places, roads, team, valhalla
from .constraints import dwell_minutes
from .geo import circle_lonlat, haversine_m
from shapely.geometry import LineString

from .rules import AvoidArea, AvoidBridge, AvoidRoad, road_name_matches
from .solver import route_seconds, solve_any_order, solve_ordered
from .verify import AFTER_WINDOW_FRACTION, AFTER_WINDOW_MIN, arrival_side, verify


LOCAL_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/Los_Angeles"))
SAVED_PLACES_PATH = os.environ.get(
    "SAVED_PLACES_JSON", os.path.join(os.path.dirname(os.path.dirname(__file__)), "saved_places.json"),
)
BAY_AREA_CENTER = {"lat": 37.70, "lng": -122.25}
MAX_EXCLUSION_ROUNDS = 8
CANDIDATES_PER_ERRAND = 6

# Named districts drawn as circles. A gazetteer is honest about what "downtown"
# means; anything not listed falls back to an Overture point + default radius.
AREA_GAZETTEER = {
    "downtown san francisco": (37.7890, -122.4010, 1.1),
    "downtown sf": (37.7890, -122.4010, 1.1),
    "downtown": (37.7890, -122.4010, 1.1),
    "financial district": (37.7946, -122.3999, 0.5),
    "soma": (37.7785, -122.4056, 0.8),
    "south of market": (37.7785, -122.4056, 0.8),
    "the mission": (37.7599, -122.4148, 0.8),
    "mission district": (37.7599, -122.4148, 0.8),
    "tenderloin": (37.7847, -122.4141, 0.35),
    "union square": (37.7880, -122.4075, 0.3),
    "chinatown": (37.7941, -122.4078, 0.3),
    "downtown oakland": (37.8044, -122.2711, 0.8),
    "downtown san jose": (37.3337, -121.8907, 0.8),
    "downtown berkeley": (37.8700, -122.2680, 0.5),
    "downtown palo alto": (37.4443, -122.1630, 0.5),
}

# Bay Area bridges as [lon, lat] centerlines, buffered into footprints.
BRIDGES = {
    "Bay Bridge": [(-122.3885, 37.7885), (-122.3700, 37.8070), (-122.3600, 37.8140), (-122.3000, 37.8255)],
    "Golden Gate Bridge": [(-122.4755, 37.8065), (-122.4786, 37.8324)],
    "San Mateo Bridge": [(-122.2560, 37.5810), (-122.1950, 37.5985), (-122.1300, 37.6170)],
    "Dumbarton Bridge": [(-122.1520, 37.4900), (-122.1100, 37.5080)],
    "Richmond-San Rafael Bridge": [(-122.4860, 37.9440), (-122.3920, 37.9290)],
    "Carquinez Bridge": [(-122.2270, 38.0660), (-122.2240, 38.0540)],
    "Benicia-Martinez Bridge": [(-122.1250, 38.0430), (-122.1260, 38.0260)],
}
BRIDGE_ALIASES = {
    "bay bridge": "Bay Bridge", "san francisco oakland bay bridge": "Bay Bridge",
    "golden gate": "Golden Gate Bridge", "golden gate bridge": "Golden Gate Bridge",
    "san mateo bridge": "San Mateo Bridge", "san mateo hayward bridge": "San Mateo Bridge",
    "dumbarton": "Dumbarton Bridge", "dumbarton bridge": "Dumbarton Bridge",
    "richmond bridge": "Richmond-San Rafael Bridge", "richmond san rafael bridge": "Richmond-San Rafael Bridge",
    "carquinez bridge": "Carquinez Bridge", "benicia bridge": "Benicia-Martinez Bridge",
    "benicia martinez bridge": "Benicia-Martinez Bridge",
}


def bridge_rule(name):
    key = " ".join(name.lower().replace("-", " ").replace("the ", " ").split())
    canonical = BRIDGE_ALIASES.get(key)
    if not canonical:
        return None
    footprint = LineString(BRIDGES[canonical]).buffer(0.004)
    return AvoidBridge(canonical, footprint)


RIGHT_SIDE_MAX_OFFSET_M = 80

POSITION_RANGES = {
    "anywhere": (0.0, 1.0), "early": (0.0, 0.4), "middle": (0.3, 0.7), "late": (0.6, 1.0),
}


class PlanningError(ValueError):
    """A request the planner cannot turn into a route; the message is for the driver."""


def load_saved_places():
    try:
        with open(SAVED_PLACES_PATH) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _point(lat, lng, label, **extra):
    return {"lat": float(lat), "lng": float(lng), "label": label, **extra}


class Planner:
    def __init__(self, ir, current_location=None, now=None, storm_rings=None, shared_locations=None,
                 code_writer=None, picked=None):
        self.ir = ir
        # Suggestions the driver picked in the app ({text: {lat, lng, label}}): exact, no search.
        self.picked = {str(k).strip().lower(): v for k, v in (picked or {}).items()
                       if isinstance(v, dict) and "lat" in v and "lng" in v}
        # code_writer(feedback) -> {"summary", "code"}: the model writing custom route code.
        self.code_writer = code_writer
        self.custom_code = None
        self.custom_failure = None
        self.custom_data = None
        self.shared_locations = shared_locations or {}
        self.current_location = current_location
        self.now = now or datetime.now(LOCAL_TZ)
        self.storm_rings = storm_rings or []
        self.trace = []
        self.saved = load_saved_places()
        self.costing = ir.get("profile") or "auto"
        self.costing_options = {}
        avoid = ir["avoid"]
        if avoid["highways"]:
            self.costing_options["use_highways"] = 0.0
        if avoid["tolls"]:
            self.costing_options["use_tolls"] = 0.0
        if avoid["ferries"]:
            self.costing_options["use_ferry"] = 0.0
        self.exclude_locations = []
        self.exclude_polygons = []
        self.rules = []
        self.notes = []
        self.rejected = set()
        self.via_points = []  # through-points that put the route on a road asked for ("take 280")

    # ------------------------------------------------------------------ trace
    def step(self, stage, title, **data):
        self.trace.append({"stage": stage, "title": title, **data})

    # -------------------------------------------------------------- resolving
    def near_hint(self):
        return self.current_location or BAY_AREA_CENTER

    def resolve(self, text, role):
        key = text.strip().lower()
        if key in self.picked:
            spot = self.picked[key]
            return _point(spot["lat"], spot["lng"], spot.get("label") or text, source="picked")
        for name, saved in self.saved.items():
            if key in {name.lower(), f"my {name.lower()}"}:
                return _point(saved["lat"], saved["lng"], saved.get("label", name.title()), source="saved")
        # Towns and lakes: Nominatim (a bare “Tahoe” is the lake, “Davis” the town).
        area = geocode.lookup_area(text)
        if area and area["kind"] == "water":
            raise PlanningError(
                f"\u201c{text}\u201d is a lake, not somewhere to drive to. Which town: "
                + ("Tahoe City, South Lake Tahoe, or Truckee?" if "tahoe" in text.lower() else "which shore?"))
        if area:
            self.step("resolve", f"{role}: \u201c{text}\u201d",
                      match={"label": area["label"], "category": "town (OpenStreetMap)", "locality": None},
                      alternatives=[], ambiguous=False)
            return _point(area["lat"], area["lng"], area["label"], source="OpenStreetMap")
        # Named places, streets and addresses: Photon, which forgives typos and extra words.
        here = self.near_hint()
        hits = geocode.photon_search(text, near=here, limit=5)
        if hits:
            top = hits[0]
            # A far exact-name match over nearby ones ("Sinnott": a preserve 60 km
            # off vs Sinnott Lane and John Sinnott Elementary here): ask, don't guess.
            def km(h):
                return haversine_m(here["lat"], here["lng"], h["lat"], h["lng"]) / 1000
            near_hits = [h for h in hits[1:] if km(h) < 10]
            if km(top) > 25 and near_hits:
                options = [top] + near_hits[:2]
                raise PlanningError(
                    f"Which \u201c{text}\u201d? "
                    + ", ".join(f"{h['label']} ({h['detail'] or h['category']})" for h in options[:-1])
                    + f", or {options[-1]['label']} ({options[-1]['detail'] or options[-1]['category']})?")
            self.step("resolve", f"{role}: \u201c{text}\u201d",
                      match={"label": top["label"], "category": f"{top['category']} (Photon / OpenStreetMap)",
                             "locality": top["detail"] or None},
                      alternatives=[{"label": h["label"], "locality": h["detail"]} for h in hits[1:]],
                      ambiguous=False)
            return _point(top["lat"], top["lng"], top["label"], address=top["detail"] or None,
                          category=top["category"], source="Photon")
        # Photon down: Overture.
        candidates = places.resolve_place(text, near=self.near_hint(), radius_m=400000)
        if not candidates:
            raise PlanningError(f"I couldn't find “{text}” in the open places data.")
        top = candidates[0]
        ambiguous = len(candidates) > 1 and candidates[1]["score"] > top["score"] - 0.05 and \
            haversine_m(top["lat"], top["lng"], candidates[1]["lat"], candidates[1]["lng"]) > 2000
        self.step(
            "resolve", f"{role}: “{text}”",
            match=top, alternatives=candidates[1:3], ambiguous=ambiguous,
        )
        return _point(top["lat"], top["lng"], top["label"], address=top.get("address"),
                      category=top.get("category"), source="Overture", id=top["id"])

    def resolve_area(self, text, radius_miles=None):
        key = text.strip().lower()
        if key.startswith("the ") and key not in AREA_GAZETTEER:
            key = key[4:]
        if key in AREA_GAZETTEER:
            lat, lng, default_radius = AREA_GAZETTEER[key]
            source = "gazetteer"
        else:
            found = places.resolve_place(text, near=self.near_hint())
            if not found:
                raise PlanningError(f"I don't know where “{text}” is.")
            lat, lng, default_radius = found[0]["lat"], found[0]["lng"], 0.75
            source = f"Overture point ({found[0]['label']})"
        radius = radius_miles or default_radius
        return circle_lonlat(lat, lng, radius * 1609.34), {"center": [lng, lat], "radiusMiles": radius,
                                                          "source": source}

    # --------------------------------------------------------------- routing
    def route_with_exclusions(self, points, label):
        """Route, map-match, exclude violating edges, repeat until clean.

        Blocking one freeway usually just moves the route to the next one, so
        a single exclusion pass is not enough; this loop is what turns "skip
        the downtown freeway" into a route with no downtown freeway at all.
        """
        points = roads.insert_vias(points, self.via_points)
        rounds = []
        trip = edges = None
        for attempt in range(MAX_EXCLUSION_ROUNDS):
            started = time.perf_counter()
            trip = valhalla.route(points, self.costing, self.costing_options or None,
                                  self.exclude_locations, self.exclude_polygons)[0]
            edges = valhalla.edges_for_route(trip, self.costing) if self.rules else []
            bad = [edge for edge in edges if any(rule.violates(edge) for rule in self.rules)]
            rounds.append({
                "attempt": attempt + 1,
                "minutes": round(trip["durationSeconds"] / 60, 1),
                "violations": len(bad),
                "roads": sorted({name for edge in bad for name in edge["names"]})[:5],
                "ms": round((time.perf_counter() - started) * 1000),
                "coordinates": trip["coordinates"],
            })
            if not bad:
                break
            new_points = []
            for edge in bad:
                lat, lng = edge["latlons"][len(edge["latlons"]) // 2]
                if not any(abs(lat - p["lat"]) < 1e-6 and abs(lng - p["lng"]) < 1e-6
                           for p in self.exclude_locations + new_points):
                    new_points.append({"lat": lat, "lng": lng})
            if not new_points:
                break
            self.exclude_locations.extend(new_points)
        if self.rules:
            self.step("exclude", f"{label}: route → check → exclude loop", rounds=rounds,
                      excludedPoints=len(self.exclude_locations))
        return trip

    # ---------------------------------------------------------------- errands
    @staticmethod
    def stretch_at(trip, minutes, window_minutes):
        """The part of the trip's line driven within window_minutes of minutes, from its maneuver times
        (each maneuver's time spread over its vertices by distance). None if the trip is shorter."""
        latlons, steps = trip["latlons"], trip["steps"]
        if not steps or trip["durationSeconds"] < minutes * 60 * 0.8:
            return None
        cum_m = [0.0]
        for a, b in zip(latlons, latlons[1:]):
            cum_m.append(cum_m[-1] + haversine_m(a[0], a[1], b[0], b[1]))
        clock = [0.0] * len(latlons)
        starts = [st["beginShapeIndex"] for st in steps] + [len(latlons) - 1]
        elapsed = 0.0
        for st, i0, i1 in zip(steps, starts, starts[1:]):
            span = cum_m[i1] - cum_m[i0]
            for i in range(i0, i1 + 1):
                clock[i] = elapsed + (st["durationSeconds"] * (cum_m[i] - cum_m[i0]) / span if span > 0 else 0)
            elapsed += st["durationSeconds"]
        idx = [i for i, t in enumerate(clock) if abs(t - minutes * 60) <= window_minutes * 60]
        if len(idx) < 2:  # a window narrower than the vertex spacing: the segment driven at that time
            at = next((i for i, t in enumerate(clock) if t >= minutes * 60), len(latlons) - 1)
            idx = [max(at - 1, 0), max(at, 1)]
        return latlons[idx[0]:idx[-1] + 1]

    def errand_candidates(self, stop, skeleton_latlons):
        lo, hi = POSITION_RANGES[stop["position"]]
        side_only = stop.get("side") == "right"
        if stop.get("afterMinutes") and getattr(self, "skeleton", None):
            # the stretch driven around that time; if nothing's there, widen (2x, 3x) and say so
            target = stop["afterMinutes"]
            base = max(AFTER_WINDOW_MIN, AFTER_WINDOW_FRACTION * target)
            for widen in (1, 2, 3):
                stretch = self.stretch_at(self.skeleton, target, base * widen)
                if stretch is None:
                    raise PlanningError(f"This trip is shorter than {target:g} minutes, so there's no point "
                                        f"{target:g} minutes in to stop for {stop['what']}.")
                found = places.search_along_route(stretch, stop["what"], stop.get("brands"),
                                                  max_offset_m=RIGHT_SIDE_MAX_OFFSET_M if side_only else 2000,
                                                  limit=200 if side_only else 60)
                if found:
                    break
            stop["afterWindowMinutes"] = base * widen
            note = (f"No {stop['what']} within {base:g} min of {target:g} min in, so the search "
                    f"widened to \u00b1{base * widen:g} min.")
            if widen > 1 and note not in self.notes:  # this runs again when a stop is re-picked
                self.notes.append(note)
            lo, hi = 0.0, 1.0
        else:
            found = places.search_along_route(skeleton_latlons, stop["what"], stop.get("brands"),
                                              max_offset_m=RIGHT_SIDE_MAX_OFFSET_M if side_only else 2000,
                                              limit=200 if side_only else 60)
        found = [c for c in found if lo <= c["alongRouteFraction"] <= hi]
        if side_only:
            # "On the right" only means something for a place fronting the road
            # you're driving, so require it to be close as well as on that side.
            # Side relative to the plain route is only a hint (the final route
            # may arrive another way); stations right on the road pass too, and
            # the arrival check in plan() makes the final call.
            found = [c for c in found if c["side"] == "right" or c["offsetMeters"] <= 30][:60]
        found = [c for c in found if c["id"] not in self.rejected]
        return found

    def probe_extent(self):
        """Lat/lons of the plain trip, so live weather can be sampled over it."""
        try:
            if self.ir.get("origin"):
                origin = self.resolve(self.ir["origin"], "Start")
            elif self.current_location:
                origin = _point(self.current_location["lat"], self.current_location["lng"], "Current location")
            else:
                return None
            if not self.ir.get("destination"):
                return [(origin["lat"], origin["lng"])]
            destination = self.resolve(self.ir["destination"], "Destination")
            return valhalla.route([origin, destination], self.costing)[0]["latlons"]
        except (PlanningError, valhalla.ValhallaError):
            return None
        finally:
            self.trace = []

    def plan(self):
        ir = self.ir
        started = time.perf_counter()
        self.step("interpret", "Constraints from the request", ir=ir)
        if ir.get("clarification"):
            raise PlanningError(ir["clarification"])

        # 1. Endpoints and named stops.
        if ir.get("origin"):
            origin = self.resolve(ir["origin"], "Start")
        elif self.current_location:
            origin = _point(self.current_location["lat"], self.current_location["lng"], "Current location")
        else:
            raise PlanningError("Where are you starting from? I don't have your location.")
        if ir.get("destination"):
            destination = self.resolve(ir["destination"], "Destination")
        elif ir.get("returnToOrigin"):
            destination = dict(origin, label=f"Back to {origin['label']}")
        else:
            destination = None
        stops = []
        for stop in ir["stops"]:
            resolved = dict(stop)
            if stop["kind"] == "place":
                resolved["point"] = self.resolve(stop["text"], "Stop")
            stops.append(resolved)
        team_tasks = (ir.get("team") or {}).get("tasks")
        if destination is None and not team_tasks:
            if not stops:
                raise PlanningError("Where would you like to go?")
            if stops[-1]["kind"] == "place":
                destination = stops.pop()["point"]

        # 2. Avoidance rules -> geometry.
        avoid = ir["avoid"]
        area_shapes = []
        for area in avoid["areas"]:
            ring, info = self.resolve_area(area["text"], area["radiusMiles"])
            rule = AvoidArea(area["text"], ring, area["roads"])
            self.rules.append(rule)
            if area["roads"] == "all":
                self.exclude_polygons.append(ring)
            area_shapes.append({"label": area["text"], "roads": area["roads"], "ring": ring, **info})
        for road in avoid["roads"]:
            bridge = bridge_rule(road["name"])
            if bridge:
                self.rules.append(bridge)
                continue
            within = within_label = None
            if road.get("near"):
                within, _ = self.resolve_area(road["near"])
                within_label = road["near"]
            self.rules.append(AvoidRoad(road["name"], within, within_label))
        if self.storm_rings:
            self.exclude_polygons.extend(self.storm_rings)
        if area_shapes or avoid["roads"] or self.storm_rings or self.costing_options:
            self.step("geometry", "Constraints → geometry", areas=area_shapes,
                      roads=[rule.describe() for rule in self.rules if isinstance(rule, AvoidRoad)],
                      stormCells=len(self.storm_rings), costingOptions=self.costing_options)

        if ir.get("meetup", {}).get("friendFrom"):
            return self.plan_meetup(origin, destination, started)
        if team_tasks:
            return team.plan_team(self, origin, started)
        if ir.get("useRoads") and destination is not None:
            for name in ir["useRoads"]:
                self.via_points.append(self.via_for_road(name, origin, destination))
        if ir.get("custom"):
            if destination is None:
                raise PlanningError("Where are you headed? I need a destination for that.")
            for point in self.apply_custom(origin, destination,
                                           [s["point"] for s in stops if s["kind"] == "place"]):
                stops.append({"kind": "place", "point": point, "dwellMinutes": point.pop("dwellMinutes", None)})

        # 3. Baseline: what a toggle-and-proximity app would give.
        fixed_points = [s["point"] for s in stops if s["kind"] == "place"]
        errands = [s for s in stops if s["kind"] == "errand"]
        if destination is None:
            # "Find me gas": end at the best errand stop near here.
            destination = None
            baseline = None
        else:
            baseline = valhalla.route([origin, *fixed_points, destination], self.costing)[0]
            self.step("baseline", "Unconstrained route", minutes=round(baseline["durationSeconds"] / 60, 1),
                      miles=round(baseline["distanceMiles"], 1), coordinates=baseline["coordinates"])
            road_rules = [rule for rule in self.rules if not isinstance(rule, AvoidArea)]
            if road_rules:
                baseline_edges = valhalla.edges_for_route(baseline, self.costing)
                for rule in road_rules:
                    if not any(rule.violates(edge) for edge in baseline_edges):
                        # A rule that matches nothing is either unnecessary or
                        # mis-defined; say which we can't tell, never "avoided".
                        self.notes.append(
                            f"The unconstrained route doesn't use anything matching \u201c{rule.describe()}\u201d, "
                            "so there was nothing to avoid (or the name isn't in the map data).")

        # 4. Skeleton honoring the avoidance rules, used to search for errands.
        self.skeleton = None
        if destination is not None:
            skeleton = self.route_with_exclusions([origin, *fixed_points, destination], "Corridor")
            self.skeleton = skeleton
            skeleton_latlons = skeleton["latlons"]
        else:
            skeleton = None
            skeleton_latlons = [(origin["lat"], origin["lng"]), (origin["lat"] + 1e-4, origin["lng"])]

        # 5-6. Choose stops and an order, route through them, and re-pick any
        # "on the right" stop the car would actually reach on the left.
        open_ended = destination is None
        for _ in range(4):
            visit = self.choose_stops(origin, destination, stops, errands, skeleton_latlons)
            end = visit.pop() if open_ended else destination
            ordered_stops = visit
            if errands or (ir["stopOrder"] == "any" and len(fixed_points) > 1):
                final = self.route_with_exclusions([origin, *ordered_stops, end], "Final route")
                final = self.fix_arrival_sides(origin, ordered_stops, end, final)
            else:
                final = skeleton
            wrong = [s for i, s in enumerate(ordered_stops)
                     if s.get("side") == "right" and arrival_side(final, i, s) != "right"]
            if not wrong:
                break
            self.rejected.update(s["id"] for s in wrong if s.get("id"))
            self.step("side", "You'd arrive with it on the left: reject and re-pick",
                      rejected=[s["label"] for s in wrong])
        destination = end

        # 7. Independent verification on a fresh map-match.
        edges = valhalla.edges_for_route(final, self.costing)
        depart_clock = self.now.hour * 3600 + self.now.minute * 60 + self.now.second
        deadline_clock = self.clock_seconds(ir.get("arriveBy"))
        road_checks = []
        for name in ir.get("useRoads") or []:
            on = [e for e in edges if road_name_matches(name, e.get("names"))]
            miles = sum(e.get("lengthMiles") or 0 for e in on)
            road_checks.append({"name": f"Takes {name}", "passed": bool(on),
                                "detail": (f"{miles:.1f} mi on it" if miles else f"{len(on)} segment(s)") if on
                                else "route never uses it"})
        if self.custom_code:
            road_checks.append(self.custom_check(final))
        elif self.custom_failure:
            road_checks.append({"name": f"Custom: {ir['custom']}", "passed": False,
                                "detail": f"no working code: {self.custom_failure}"})
        checks = road_checks + verify(final, edges, ir, ordered_stops, self.rules, self.storm_rings,
                        depart_clock if (deadline_clock is not None or any(
                            s.get("closesAtClock") is not None for s in ordered_stops)) else None,
                        deadline_clock)
        self.step("verify", "Independent check of the final route", checks=checks)

        dwell_total = sum(s.get("dwellSeconds", 0) for s in ordered_stops)
        nav_points = [origin, *ordered_stops, destination] if (errands or (
            ir["stopOrder"] == "any" and len(fixed_points) > 1)) else [origin, *fixed_points, destination]
        return {
            "navRequest": self.nav_request(nav_points),
            "origin": origin,
            "destination": destination,
            "stops": ordered_stops,
            "route": public_trip(final),
            "baseline": public_trip(baseline) if baseline else None,
            "dwellSeconds": dwell_total,
            "areas": area_shapes,
            "excludedPoints": self.exclude_locations,
            "stormRings": self.storm_rings,
            "checks": checks,
            "allChecksPassed": all(check["passed"] for check in checks),
            "notes": self.notes,
            "trace": self.trace,
            "plannerMs": round((time.perf_counter() - started) * 1000),
        }

    def clock_seconds(self, hhmm):
        if not hhmm:
            return None
        hours, minutes = (int(part) for part in hhmm.split(":"))
        return hours * 3600 + minutes * 60

    def choose_stops(self, origin, destination, stops, errands, skeleton_latlons):
        """Return the chosen stop points in visiting order (plus the trip end
        appended when the request had no destination)."""
        ir = self.ir
        fixed = [s for s in stops if s["kind"] == "place"]
        if not errands and not (ir["stopOrder"] == "any" and len(fixed) > 1):
            return [dict(s["point"], dwellSeconds=dwell_minutes(s) * 60) for s in stops]

        open_ended = destination is None
        t0 = time.perf_counter()
        # Stage 1: screen each errand's candidates by real detour minutes, using
        # two thin matrices (start -> candidates, candidates -> end). Cheap,
        # and it keeps the full matrix in stage 2 small.
        opts = (self.costing, self.costing_options or None, self.exclude_locations, self.exclude_polygons)
        searches = {}
        for index, stop in enumerate(stops):
            if stop["kind"] != "errand":
                continue
            candidates = self.errand_candidates(stop, skeleton_latlons)
            label = f"Search: {stop['what']}" + (f" ({' or '.join(stop['brands'])})" if stop.get("brands") else "") \
                + (", right side only" if stop.get("side") == "right" else "") \
                + (f", about {stop['afterMinutes']:g} min in" if stop.get("afterMinutes") else "")
            if not candidates:
                self.step("search", label, found=0, position=stop["position"], points=[])
                where = (f"right on this route (within {RIGHT_SIDE_MAX_OFFSET_M} m, so not along freeways)"
                         if stop.get("side") == "right" else "along this route")
                raise PlanningError(f"No {stop['what']} found {where} in the open places data.")
            points = [_point(c["lat"], c["lng"], c["label"], address=c.get("address"), category=c["category"],
                             locality=c.get("locality"), source="Overture", id=c["id"]) for c in candidates]
            searches[index] = (label, candidates, points)

        def screen(points):
            to_cand = valhalla.matrix([origin], points, *opts)[0][0]
            if open_ended:
                return to_cand
            from_cand = [row[0] for row in valhalla.matrix(points, [destination], *opts)[0]]
            return [None if a is None or b is None else a + b - direct for a, b in zip(to_cand, from_cand)]

        direct = 0 if open_ended else (valhalla.matrix([origin], [destination], *opts)[0][0][0] or 0)
        with ThreadPoolExecutor(max_workers=4) as pool:
            detours_by_stop = dict(zip(searches, pool.map(lambda i: screen(searches[i][2]), searches)))

        screened = []
        for index, stop in enumerate(stops):
            if stop["kind"] == "place":
                screened.append([stop["point"]])
                continue
            label, candidates, points = searches[index]
            scored = sorted((d, i) for i, d in enumerate(detours_by_stop[index]) if d is not None)
            if stop.get("maxDetourMinutes") is not None:
                allowed = [(d, i) for d, i in scored if d <= stop["maxDetourMinutes"] * 60]
                if not allowed:
                    self.step("search", label, found=len(candidates), position=stop["position"],
                              points=[[c["lng"], c["lat"]] for c in candidates], withinDetour=0)
                    best = scored[0][0] / 60 if scored else None
                    raise PlanningError(
                        f"No {stop['what']} within {stop['maxDetourMinutes']:g} min of the route"
                        + (f"; the closest adds {best:.1f} min." if best is not None else "."))
                scored = allowed
            keep = []
            for d, i in scored[:CANDIDATES_PER_ERRAND]:
                points[i]["screenDetourSeconds"] = round(d)
                keep.append(points[i])
            self.step("search", label, found=len(candidates), position=stop["position"],
                      points=[[c["lng"], c["lat"]] for c in candidates],
                      shortlist=[{"label": p["label"], "detourMinutes": round(p["screenDetourSeconds"] / 60, 1)}
                                 for p in keep])
            screened.append(keep)

        # Stage 2: full matrix over origin, shortlists, destination.
        nodes = [origin]
        stop_nodes = []
        for group in screened:
            stop_nodes.append(list(range(len(nodes), len(nodes) + len(group))))
            nodes.extend(group)
        if not open_ended:
            nodes.append(destination)
        dest_index = len(nodes) - 1
        durations, _ = valhalla.matrix(nodes, nodes, self.costing, self.costing_options or None,
                                       self.exclude_locations, self.exclude_polygons)
        matrix_ms = round((time.perf_counter() - t0) * 1000)

        dwell = {}
        for stop, idx in zip(stops, stop_nodes):
            for node in idx:
                dwell[node] = dwell_minutes(stop) * 60

        windows = {}
        for stop, idx in zip(stops, stop_nodes):
            close = self.clock_seconds(stop.get("closesAt"))
            if close is not None:
                for node in idx:
                    windows[node] = close - self.depart_clock()
        deadline_clock = self.clock_seconds(ir.get("arriveBy"))
        deadline = None if deadline_clock is None else deadline_clock - self.depart_clock()

        t0 = time.perf_counter()
        if open_ended:
            # No destination: the last errand ends the trip.
            layers = [[0], *stop_nodes]
            path, _ = solve_ordered(durations, layers, dwell)
            method = "Layered shortest path"
        elif ir["stopOrder"] == "as_listed":
            layers = [[0], *stop_nodes, [dest_index]]
            path, _ = solve_ordered(durations, layers, dwell)
            method = "Layered shortest path (exact, order fixed)"
        else:
            required = [idx[0] for stop, idx in zip(stops, stop_nodes) if stop["kind"] == "place"]
            groups = [idx for stop, idx in zip(stops, stop_nodes) if stop["kind"] == "errand"]
            result = solve_any_order(durations, 0, dest_index, required, groups, dwell, windows, deadline)
            if result is None:
                relaxed = solve_any_order(durations, 0, dest_index, required, groups, dwell)
                if relaxed is None:
                    raise PlanningError("Those stops can't all be reached.")
                late = (route_seconds(durations, relaxed[0], dwell) - (deadline or 0)) / 60
                self.step("solve", "OR-Tools: time windows infeasible", relaxedMinutes=round(late, 1))
                path = relaxed[0]
                method = "OR-Tools (time windows relaxed — cannot be met)"
            else:
                path = result[0]
                method = "OR-Tools routing solver (GTSP + time windows)"
        solve_ms = round((time.perf_counter() - t0) * 1000)

        total = route_seconds(durations, path, dwell)
        straight = durations[0][dest_index] if not open_ended else None
        self.step("solve", method, nodes=len(nodes), matrixMs=matrix_ms, solveMs=solve_ms,
                  totalMinutes=round(total / 60, 1) if total is not None else None,
                  directMinutes=round(straight / 60, 1) if straight else None,
                  candidatesPerStop=[len(idx) for idx in stop_nodes])

        inner = path[1:] if open_ended else path[1:-1]
        node_to_stop = {node: stop for stop, idx in zip(stops, stop_nodes) for node in idx}
        chosen = []
        for position, node in enumerate(inner):
            stop = node_to_stop[node]
            prev_node = path[position]
            next_node = path[position + 2] if position + 2 < len(path) else None
            detour_s = None
            if next_node is not None and None not in (durations[prev_node][node], durations[node][next_node],
                                                      durations[prev_node][next_node]):
                detour_s = durations[prev_node][node] + durations[node][next_node] - durations[prev_node][next_node]
            close = self.clock_seconds(stop.get("closesAt"))
            chosen.append(dict(
                nodes[node],
                kind=stop["kind"],
                what=stop.get("what"),
                maxDetourMinutes=stop.get("maxDetourMinutes"),
                afterMinutes=stop.get("afterMinutes"),
                afterWindowMinutes=stop.get("afterWindowMinutes"),
                detourSeconds=detour_s,
                dwellSeconds=dwell.get(node, 0),
                closesAtClock=close,
                side=stop.get("side", "any"),
            ))
        return chosen

    def fix_arrival_sides(self, origin, stops, destination, trip):
        """Make right-side stops actually arrive on the right.

        Valhalla's preferred_side is not trusted: on a test stop, "same" made
        the car arrive with the station on the LEFT. So each option is tried
        and kept only if the route's own geometry says the stop is on the right.
        """
        attempts = []
        for index, stop in enumerate(stops):
            if stop.get("side") != "right" or arrival_side(trip, index, stop) == "right":
                continue
            for option in ("same", "opposite"):
                stop["preferredSide"] = option
                candidate = valhalla.route(roads.insert_vias([origin, *stops, destination], self.via_points),
                                           self.costing,
                                           self.costing_options or None, self.exclude_locations,
                                           self.exclude_polygons)[0]
                got = arrival_side(candidate, index, stop)
                attempts.append({"stop": stop["label"], "preferredSide": option, "arrivesOn": got,
                                 "minutes": round(candidate["durationSeconds"] / 60, 1)})
                if got == "right":
                    trip = candidate
                    break
                stop.pop("preferredSide")
        if attempts:
            self.step("side", "Arrive on the right: try each approach, keep what the geometry confirms",
                      attempts=attempts)
        return trip

    def plan_meetup(self, origin, destination, started):
        """Meet someone driving separately, then go on together.

        Candidates come from both people's routes to the shared destination.
        For each: when can both be there (the later arrival), each person's
        wait and detour, and when the pair reaches the destination. Pick the
        earliest arrival together, then the least wasted time (waits + detours).
        """
        meetup = self.ir["meetup"]
        if destination is None:
            raise PlanningError("Where are you both headed?")
        friend = self.resolve(meetup["friendFrom"], "Friend starts")
        friend["label"] = f"Friend ({friend['label']})"
        ago = (meetup.get("friendLeftMinutesAgo") or 0) * 60
        what = meetup.get("what") or "coffee"

        mine = self.route_with_exclusions([origin, destination], "Your route")
        theirs = self.route_with_exclusions([friend, destination], "Friend's route")
        self.step("baseline", "Separate routes to the destination",
                  minutes=round(mine["durationSeconds"] / 60, 1), miles=round(mine["distanceMiles"], 1),
                  coordinates=mine["coordinates"], friendMinutes=round(theirs["durationSeconds"] / 60, 1))

        seen, candidates = set(), []
        for trip in (mine, theirs):
            for cand in places.search_along_route(trip["latlons"], what, max_offset_m=1500, limit=60):
                if cand["id"] not in seen:
                    seen.add(cand["id"])
                    candidates.append(cand)
        if not candidates:
            raise PlanningError(f"No {what} along either route to meet at.")
        points = [_point(c["lat"], c["lng"], c["label"], address=c.get("address"), category=c["category"],
                         locality=c.get("locality"), source="Overture", id=c["id"]) for c in candidates]

        t0 = time.perf_counter()
        opts = (self.costing, self.costing_options or None, self.exclude_locations, self.exclude_polygons)
        to_points, _ = valhalla.matrix([origin, friend], points, *opts)
        onward = [row[0] for row in valhalla.matrix(points, [destination], *opts)[0]]
        matrix_ms = round((time.perf_counter() - t0) * 1000)
        dwell = dwell_minutes({"kind": "errand", "what": what, "dwellMinutes": None}) * 60
        my_direct, their_direct = mine["durationSeconds"], theirs["durationSeconds"]

        options = []
        for i, point in enumerate(points):
            me_t, them_t, rest = to_points[0][i], to_points[1][i], onward[i]
            if None in (me_t, them_t, rest):
                continue
            them_arrive = max(0.0, them_t - ago)  # seconds from now
            meet = max(me_t, them_arrive)
            my_detour = me_t + rest - my_direct
            their_detour = them_t + rest - their_direct
            if meetup.get("maxDetourMinutes") is not None and \
                    max(my_detour, their_detour) > meetup["maxDetourMinutes"] * 60:
                continue
            together = meet + dwell + rest
            wasted = (meet - me_t) + (meet - them_arrive) + max(0, my_detour) + max(0, their_detour)
            options.append((together, wasted, i, {
                "meetSeconds": meet, "myWait": meet - me_t, "theirWait": meet - them_arrive,
                "myDetour": my_detour, "theirDetour": their_detour, "togetherArrival": together,
            }))
        if not options:
            raise PlanningError(f"No {what} where you can both meet within "
                                f"{meetup['maxDetourMinutes']:g} min of your routes.")
        options.sort(key=lambda o: (round(o[0] / 60), o[1]))
        _, _, best, stats = options[0]
        waits = [o[3]["theirWait"] for o in options[:10]] + [o[3]["myWait"] for o in options[:10]]
        if stats["theirWait"] >= 20 * 60 and max(waits) - min(w for w in waits if w > 0) < 10 * 60:
            self.notes.append(
                f"Your friend is about {round(stats['theirWait'] / 60)} min ahead on the same road, so they "
                "wait about that long wherever you meet: tell them to take a long lunch or leave later.")
        elif stats["myWait"] >= 20 * 60:
            self.notes.append(f"You're about {round(stats['myWait'] / 60)} min ahead; you'll wait wherever you meet.")
        spot = dict(points[best], kind="meetup", what=what, dwellSeconds=dwell,
                    maxDetourMinutes=meetup.get("maxDetourMinutes"), detourSeconds=stats["myDetour"])
        self.step("search", f"Meeting spots: {what} along both routes", found=len(points),
                  position="anywhere", points=[[p["lng"], p["lat"]] for p in points],
                  shortlist=[{"label": points[o[2]]["label"],
                              "detourMinutes": round((o[3]["myWait"] + o[3]["theirWait"]) / 60, 1)}
                             for o in options[:3]])
        self.step("solve", "Earliest arrival together, then least waiting + detour",
                  nodes=len(points) + 3, matrixMs=matrix_ms, solveMs=0,
                  totalMinutes=round(stats["togetherArrival"] / 60, 1))

        final = self.route_with_exclusions([origin, spot, destination], "Your route via the meeting spot")
        friend_leg = valhalla.route([friend, spot], *opts[:2], *opts[2:])[0]
        edges = valhalla.edges_for_route(final, self.costing)
        checks = verify(final, edges, self.ir, [spot], self.rules, self.storm_rings)
        checks = [c for c in checks if "within" not in c["name"]]  # detours are checked per person below
        bound = meetup.get("maxDetourMinutes")
        for who, detour in (("Your", stats["myDetour"]), ("Friend's", stats["theirDetour"])):
            minutes = max(0.0, detour / 60)
            checks.append({"name": f"{who} detour" + (f" within {bound:g} min" if bound is not None else ""),
                           "passed": bound is None or minutes <= bound + 0.5, "detail": f"{minutes:.1f} min"})
        # The friend's leg must actually reach the spot: same independent test.
        gap = verify(friend_leg, [], {"stopOrder": "as_listed"}, [dict(spot, label="the spot (friend)")], [])[0]
        checks.append({**gap, "name": "Friend's route reaches the spot"})
        self.step("verify", "Independent check of the final route", checks=checks)

        now_clock = self.depart_clock()
        clock = lambda seconds: (self.now.replace(hour=0, minute=0, second=0) +
                                 timedelta(seconds=now_clock + seconds)).strftime("%-I:%M%p").lower()
        meet_info = {
            "friend": friend,
            "friendRoute": friend_leg["coordinates"],
            "what": what,
            "meetAt": clock(stats["meetSeconds"]),
            "youArrive": clock(stats["meetSeconds"] - stats["myWait"]),
            "friendArrives": clock(stats["meetSeconds"] - stats["theirWait"]),
            "yourWaitMinutes": round(stats["myWait"] / 60),
            "friendWaitMinutes": round(stats["theirWait"] / 60),
            "yourDetourMinutes": round(max(0, stats["myDetour"]) / 60, 1),
            "friendDetourMinutes": round(max(0, stats["theirDetour"]) / 60, 1),
            "arriveTogether": clock(stats["togetherArrival"]),
            "alternatives": [{"label": points[o[2]]["label"], "address": points[o[2]].get("address"),
                              "locality": points[o[2]].get("locality"),
                              "arriveTogether": clock(o[3]["togetherArrival"]),
                              "waitMinutes": round((o[3]["myWait"] + o[3]["theirWait"]) / 60)}
                             for o in options[1:4]],
        }
        return {
            "origin": origin,
            "destination": destination,
            "stops": [spot],
            "route": public_trip(final),
            "baseline": public_trip(mine),
            "dwellSeconds": dwell,
            "areas": [],
            "excludedPoints": self.exclude_locations,
            "stormRings": self.storm_rings,
            "checks": checks,
            "allChecksPassed": all(check["passed"] for check in checks),
            "notes": self.notes,
            "meetup": meet_info,
            "navRequest": self.nav_request([origin, spot, destination]),
            "trace": self.trace,
            "plannerMs": round((time.perf_counter() - started) * 1000),
        }

    def apply_custom(self, origin, destination, fixed_points, attempts=2):
        """The model writes plan()/check() for what the IR can't say; run them
        in the sandbox, try the shaped route, and loop on failures."""
        if self.code_writer is None:
            raise PlanningError("That needs custom code, which needs the language model (use the describe box).")
        feedback, history, custom_stops = None, [], []
        info = {"origin": origin, "destination": destination, "stops": fixed_points, "request": self.ir["custom"]}
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            saved = (list(self.via_points), list(self.exclude_locations), list(self.exclude_polygons))
            code, entry = "", {"attempt": attempt, "summary": None, "code": ""}
            try:
                written = self.code_writer(feedback) or {}
                code = str(written.get("code") or "")
                entry.update(summary=written.get("summary"), code=code, modelMs=round((time.perf_counter() - started) * 1000))
                custom.validate(code)
                toolkit, log = custom.Toolkit(self, origin, destination), []
                shaped = custom.run(code, "plan", [info], toolkit, log) or {}
                if not isinstance(shaped, dict):
                    raise custom.CustomCodeError("plan() must return a dict")
                chosen = [dict(custom._pt(p), label=str(p.get("label") or "Stop"),
                               dwellMinutes=float(p.get("dwell_min") or 0) or None, source="custom code")
                          for p in (shaped.get("stops") or [])[:8]]
                data = shaped.get("data") or {}
                for p in custom.thin(shaped.get("via") or []):
                    self.via_points.append(dict(custom._pt(p), kind="through", label=p.get("label") or "via"))
                self.exclude_locations.extend(custom._pt(p) for p in shaped.get("avoid_points") or [])
                self.exclude_polygons.extend(shaped.get("avoid_polygons") or [])
                trial = self.route_with_exclusions([origin, *fixed_points, *chosen, destination],
                                                   "Custom route (trial)")
                passed, detail = custom.run(code, "check", [custom.route_view(trial, self.costing), data])
                entry.update(passed=bool(passed), detail=str(detail), toolCalls=toolkit.calls, log=log[-8:],
                             ms=round((time.perf_counter() - started) * 1000))
                history.append(entry)
                self.custom_code, self.custom_summary, self.custom_data = code, written.get("summary"), data
                custom_stops = chosen
                if passed:
                    break
                feedback = {"code": code, "error": f"check() returned False on the route plan() produced: {detail}"}
            except (custom.CustomCodeError, valhalla.ValhallaError, ValueError, TypeError) as error:
                self.via_points, self.exclude_locations, self.exclude_polygons = saved
                entry.update(passed=False, detail=str(error)[:400], ms=round((time.perf_counter() - started) * 1000))
                history.append(entry)
                feedback = {"code": code, "error": str(error)}
        self.step("custom", f"Custom code for: {self.ir['custom']}", attempts=history)
        if self.custom_code is None:
            # Plan the rest anyway; the verifier reports the unmet requirement.
            self.custom_failure = history[-1]["detail"][:200]
        return [dict(p) for p in custom_stops]

    def custom_check(self, final):
        """The model's own check(), run on the final route like any other check."""
        name = f"Custom: {self.ir['custom']}"
        try:
            passed, detail = custom.run(self.custom_code, "check",
                                        [custom.route_view(final, self.costing), self.custom_data or {}])
            return {"name": name, "passed": bool(passed), "detail": str(detail)[:200]}
        except custom.CustomCodeError as error:
            return {"name": name, "passed": False, "detail": str(error)[:200]}

    def via_for_road(self, name, origin, destination):
        """A through-point on the named road, facing the way the driver is
        going, chosen by real minutes (origin -> point -> destination)."""
        on_road = roads.road_points(name, roads.bbox_for([origin, destination]))
        candidates = roads.via_candidates(on_road, origin, destination)
        if not candidates:
            raise PlanningError(f"I couldn't find {name} anywhere between here and there.")
        opts = (self.costing, self.costing_options or None, self.exclude_locations, self.exclude_polygons)
        to_c = valhalla.matrix([origin], candidates, *opts)[0][0]
        from_c = [row[0] for row in valhalla.matrix(candidates, [destination], *opts)[0]]
        scored = [(a + b, c) for a, b, c in zip(to_c, from_c, candidates) if a is not None and b is not None]
        if not scored:
            raise PlanningError(f"I couldn't route onto {name} from here.")
        seconds, best = min(scored, key=lambda item: item[0])
        via = {"lat": best["lat"], "lng": best["lng"], "kind": "through", "heading": best["heading"],
               "roadClass": best["roadClass"], "label": name}
        self.step("via", f"Take {name}: a point on it, facing your direction",
                  candidates=len(candidates), found=len(on_road), minutes=round(seconds / 60, 1),
                  points=[[c["lng"], c["lat"]] for c in candidates])
        return via

    def nav_request(self, points):
        """Everything needed to rebuild this exact route for turn-by-turn,
        including the exclusions the loop found, so reroutes keep them."""
        keep = ("lat", "lng", "label", "preferredSide")
        via_keep = ("lat", "lng", "label", "kind", "heading", "roadClass")
        return {
            "points": [{k: p[k] for k in keep if k in p} for p in points],
            "via": [{k: v[k] for k in via_keep if k in v} for v in self.via_points],
            "costing": self.costing,
            "costingOptions": self.costing_options or None,
            "excludeLocations": self.exclude_locations,
            "excludePolygons": self.exclude_polygons,
        }

    def depart_clock(self):
        return self.now.hour * 3600 + self.now.minute * 60 + self.now.second


def public_trip(trip):
    if trip is None:
        return None
    return {key: value for key, value in trip.items() if key not in {"latlons", "legShapes"}}


def plan(ir, current_location=None, now=None, storm_rings=None, shared_locations=None, code_writer=None,
         picked=None):
    return Planner(ir, current_location, now, storm_rings, shared_locations, code_writer, picked).plan()
