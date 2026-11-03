"""Custom route code: the escape hatch for requests the IR can't express.

The fixed IR fields cover common requests. Anything else ("no more than three
left turns", "stay within a mile of the ocean", "go past a park") is put in
`custom` by the first model call; a second call writes two small Python
functions against the toolkit below:

    plan(ctx)     -> {"stops": [...], "via": [...], "avoid_points": [...],
                      "avoid_polygons": [...], "data": {...}}
                     shapes the route (all keys optional; may be omitted)
    check(route, data) -> (passed, detail)
                     decides whether a finished route meets the request

The code runs in sandbox_runner.py: a separate process with no network, no
imports and resource limits; its toolkit calls are relayed here and answered
with the same local Valhalla / Overture / DuckDB functions the planner uses.
check() runs on the final route next to the built-in verifier checks, and a
failure is shown to the model with its own code for another try.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import time

from . import places, roads, valhalla

MAX_VIA = 15
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox_runner.py")
WALL_SECONDS = 45
MAX_ROUTE_POINTS = 400

MANEUVER_TYPES = {
    1: "start", 2: "start_right", 3: "start_left", 4: "destination", 5: "destination_right",
    6: "destination_left", 7: "becomes", 8: "continue", 9: "slight_right", 10: "right", 11: "sharp_right",
    12: "uturn_right", 13: "uturn_left", 14: "sharp_left", 15: "left", 16: "slight_left", 17: "ramp_straight",
    18: "ramp_right", 19: "ramp_left", 20: "exit_right", 21: "exit_left", 22: "stay_straight", 23: "stay_right",
    24: "stay_left", 25: "merge", 26: "roundabout_enter", 27: "roundabout_exit", 28: "ferry_enter",
    29: "ferry_exit", 37: "merge_right", 38: "merge_left",
}

API_DOC = '''You write Python for a navigation planner. The request below could not be expressed with
the planner's fixed constraint fields, so you write code that shapes and checks the route.

Define two functions (plain Python 3; no imports, no classes, no names starting with "_"):

def plan(ctx):
    """Optional. Shape the route. Return a dict with any of:
      "stops":          [point, ...]  places to STOP at, in visiting order, each with "label" and
                                       "dwell_min" (minutes there). Use when the requirement decides
                                       which or how many stops to make.
      "data":           {...}         anything check() needs to know (what plan chose, budgets). JSON only.
      "via":            [point, ...]  at most 15 points the route must pass THROUGH (no stop). A point may
                                       carry "heading" (0-359, direction of travel) to pick a side
                                       of a divided road.
      "avoid_points":   [point, ...]  road locations the route must not use.
      "avoid_polygons": [ring, ...]   areas to stay out of; ring = [[lng, lat], ...] closed.
    Return {} if routing normally and checking is enough."""

def check(route, data):
    """Required. Return (passed: bool, detail: str) for a finished route. data is plan()'s "data"
    ({} without one). plan() and check() run separately: share state only through data."""

ctx (only in plan):
  ctx.origin, ctx.destination      always points {"lat", "lng", "label"} (already resolved; never geocode
                                   them); ctx.stops: list of points
  ctx.request                      the requirement in words
  ctx.route(points, avoid_points=None, avoid_polygons=None) -> route   (points in order, 2 to 17)
  ctx.alternatives(start, end, n=3) -> [route, ...]   the engine's different routes between two points
  ctx.matrix(sources, targets)     -> [[seconds or None, ...], ...]
  ctx.road_points(name)            -> points on a named road near the trip ("I-280", "Skyline Boulevard"),
                                      each with "heading" (direction of that carriageway) and "roadClass"
  ctx.places(what, near, radius_m=3000, brands=None, limit=20) -> places of a type near a point
  ctx.places_along(route, what, max_offset_m=1000, brands=None, limit=30) -> places near a route line
  ctx.geocode(text)                -> point for a place name
  A place is {"lat", "lng", "label", "category", "brand", "address"}.

route (a dict, from ctx.route / ctx.alternatives and as check's argument):
  "duration_s", "distance_mi"
  "coordinates": [[lng, lat], ...]   (thinned to <= 400 points)
  "edges": [{"names": [...], "road_class": "motorway"|"trunk"|"primary"|"secondary"|"tertiary"|
             "unclassified"|"residential"|"service_other", "length_mi", "lat", "lng", "speed_kph",
             "toll", "bridge", "tunnel", "use"}]   every road segment driven, in order
  "steps": [{"turn_deg": signed turn angle (negative = left, positive = right, None at start/end),
             "type": maneuver type such as "left", "slight_right", "exit_left", "merge", "continue",
             "instruction", "streets": [...], "distance_mi"}]

Helpers (no ctx needed): math, dist_m(a, b), bearing(a, b), circle(center, radius_m, n=24) -> ring,
point_in_ring(point, ring). print(...) shows up in the planner's log.

Optimization (OR-Tools, already loaded as `ortools`; for choosing among options, sequencing, budgets):
  ortools.CpModel(), ortools.CpSolver(), ortools.OPTIMAL / FEASIBLE / INFEASIBLE   (CP-SAT integer programs)
  ortools.LinearSolver.CreateSolver("SCIP" | "CBC" | "GLOP")                       (MIP / LP)
  ortools.RoutingIndexManager(n, vehicles, depot), ortools.RoutingModel(manager),
  ortools.default_routing_parameters(), ortools.FirstSolutionStrategy, ortools.LocalSearchMetaheuristic
  Get data with ctx (matrix, routes, places) first, then solve; costs must be integers for CP-SAT/routing.

Write SHORT, direct code (aim for under 40 lines total). The inputs are well-formed: no defensive type
checks, no fallbacks for impossible cases. Example for "the route with the fewest highway miles":

def plan(ctx):
    options = ctx.alternatives(ctx.origin, ctx.destination, n=4)
    best = min(options, key=lambda r: sum(e["length_mi"] for e in r["edges"] if e["road_class"] == "motorway"))
    pts = best["coordinates"][::max(1, len(best["coordinates"]) // 12)][1:-1]
    return {"via": [{"lat": lat, "lng": lng} for lng, lat in pts]}

def check(route, data):
    miles = sum(e["length_mi"] for e in route["edges"] if e["road_class"] == "motorway")
    return True, f"{miles:.1f} highway miles"

Stops you return are visited in order; route["duration_s"] is driving time only (add dwell yourself).

Rules: plan() may call ctx as much as it needs (each call is a real routing request, about 0.1-1 s; keep
it under ~40 calls). check() gets only the route and must not call ctx. Be faithful to the request: check
exactly what was asked, and make plan() actually achieve it rather than only checking.'''

CODE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "code": {"type": "string"},
    },
    "required": ["summary", "code"],
}

FORBIDDEN_NAMES = {
    "eval", "exec", "open", "compile", "setattr", "delattr", "globals", "locals", "vars",
    "input", "breakpoint", "type", "object", "super", "memoryview", "classmethod", "staticmethod",
    "property", "help", "dir", "id", "hash", "format", "bytes", "bytearray", "exit", "quit",
}


class CustomCodeError(ValueError):
    """Code that failed validation or raised; the message goes back to the model."""


def codegen_messages(request_text, ir, feedback=None):
    known = {k: v for k, v in ir.items() if k not in {"clarification", "custom"}}
    user = (f"Requirement: {ir.get('custom') or request_text}\n"
            f"Driver's words: {request_text}\n"
            f"Constraints already handled by the planner (don't redo them): {json.dumps(known)}\n"
            "Return JSON: {\"summary\": one sentence on how the code meets the requirement, "
            "\"code\": the Python source}.")
    if feedback:
        user += f"\n\nYour previous attempt:\n{feedback['code']}\n\nIt failed: {feedback['error']}\nFix it."
    return [{"role": "system", "content": API_DOC}, {"role": "user", "content": user}]


def validate(code):
    """Reject anything outside plain functions over the toolkit."""
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        raise CustomCodeError(f"SyntaxError: {error}") from error
    safe_lookups = set()
    for node in ast.walk(tree):
        # getattr/hasattr only with a literal, public attribute name: the same
        # rule as dot access, so they can't reach __class__ and friends.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
            name = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str) and not name.value.startswith("_")):
                raise CustomCodeError(f"{node.func.id}() needs a literal attribute name not starting with '_'")
            safe_lookups.add(id(node.func))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in {"getattr", "hasattr"} and id(node) not in safe_lookups:
            raise CustomCodeError(f"{node.id} may only be called directly")
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef,
                             ast.With, ast.AsyncWith, ast.AsyncFunctionDef, ast.Await)):
            raise CustomCodeError(f"{type(node).__name__} is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise CustomCodeError(f"attribute {node.attr!r} is not allowed")
        if isinstance(node, ast.Name) and ((node.id.startswith("_") and node.id != "_") or node.id in FORBIDDEN_NAMES):
            raise CustomCodeError(f"name {node.id!r} is not allowed")
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_"):
            raise CustomCodeError(f"function name {node.name!r} is not allowed")
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "check" not in names:
        raise CustomCodeError("define check(route)")


def _thin(coords):
    step = max(1, len(coords) // MAX_ROUTE_POINTS)
    out = coords[::step]
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def route_view(trip, costing):
    """A route as the custom code sees it (plain JSON)."""
    edges = []
    for e in valhalla.edges_for_route(trip, costing):
        mid = e["latlons"][len(e["latlons"]) // 2] if e.get("latlons") else (None, None)
        edges.append({"names": e.get("names") or [], "road_class": e.get("roadClass"),
                      "length_mi": round(e.get("lengthMiles") or 0, 3), "lat": mid[0], "lng": mid[1],
                      "speed_kph": e.get("speed"), "toll": e.get("toll"), "bridge": e.get("bridge"),
                      "tunnel": e.get("tunnel"), "use": e.get("use")})
    steps = []
    for s in trip.get("steps") or []:
        before, after = s.get("bearingBefore"), s.get("bearingAfter")
        turn = None if before is None or after is None else ((after - before + 540) % 360) - 180
        steps.append({"type": MANEUVER_TYPES.get(s.get("type"), str(s.get("type"))), "turn_deg": turn,
                      "instruction": s.get("instruction"), "streets": s.get("streetNames") or [],
                      "distance_mi": round(s.get("distanceMiles") or 0, 3)})
    return {"duration_s": round(trip["durationSeconds"]), "distance_mi": round(trip["distanceMiles"], 2),
            "coordinates": _thin(trip["coordinates"]), "edges": edges, "steps": steps}


def thin(points, limit=MAX_VIA):
    """Evenly keep at most `limit` points (the engine takes 20 locations)."""
    if len(points) <= limit:
        return list(points)
    return [points[round(i * (len(points) - 1) / (limit - 1))] for i in range(limit)]


def _pt(p):
    if not isinstance(p, dict) or "lat" not in p or "lng" not in p:
        raise ValueError("points are {'lat': ..., 'lng': ...} dicts")
    out = {"lat": float(p["lat"]), "lng": float(p["lng"])}
    for key in ("label", "heading", "kind"):
        if p.get(key) is not None:
            out[key] = p[key]
    return out


class Toolkit:
    """The parent-side implementations of ctx's methods."""

    def __init__(self, planner, origin, destination):
        self.planner = planner
        self.origin = origin
        self.destination = destination
        self.calls = 0

    def _opts(self, avoid_points=None, avoid_polygons=None):
        p = self.planner
        return (p.costing, p.costing_options or None,
                p.exclude_locations + [_pt(x) for x in (avoid_points or [])],
                p.exclude_polygons + list(avoid_polygons or []))

    def route(self, points, avoid_points=None, avoid_polygons=None):
        pts = [_pt(x) for x in points]
        if len(pts) > MAX_VIA + 2:
            raise ValueError(f"{len(pts)} points; the limit is {MAX_VIA + 2} (start, end and {MAX_VIA} via)")
        for p in pts[1:-1]:
            p.setdefault("kind", "through")
        trip = valhalla.route(pts, *self._opts(avoid_points, avoid_polygons))[0]
        return route_view(trip, self.planner.costing)

    def alternatives(self, start, end, n=3):
        trips = valhalla.route([_pt(start), _pt(end)], *self._opts(), alternates=max(0, min(int(n), 4) - 1))
        return [route_view(t, self.planner.costing) for t in trips]

    def matrix(self, sources, targets):
        return valhalla.matrix([_pt(x) for x in sources], [_pt(x) for x in targets], *self._opts())[0]

    def road_points(self, name):
        bbox = roads.bbox_for([self.origin, self.destination] if self.destination else [self.origin])
        return roads.road_points(str(name), bbox)

    def places(self, what, near, radius_m=3000, brands=None, limit=20):
        c = _pt(near)
        found = places.search_along_route([(c["lat"], c["lng"]), (c["lat"] + 1e-4, c["lng"])], str(what),
                                          brands, max_offset_m=float(radius_m), limit=int(limit))
        return [{k: f.get(k) for k in ("lat", "lng", "label", "category", "brand", "address")} for f in found]

    def places_along(self, coordinates, what, max_offset_m=1000, brands=None, limit=30):
        latlons = [(lat, lng) for lng, lat in coordinates]
        found = places.search_along_route(latlons, str(what), brands, max_offset_m=float(max_offset_m),
                                          limit=int(limit))
        return [{k: f.get(k) for k in ("lat", "lng", "label", "category", "brand", "address")} for f in found]

    def geocode(self, text):
        p = self.planner.resolve(str(text), "Code asked for")
        return {"lat": p["lat"], "lng": p["lng"], "label": p["label"]}


def _sandbox_cmd():
    cmd = [sys.executable, "-I", RUNNER]
    # Its own network namespace where unprivileged namespaces are allowed:
    # the code then has no network at all, whatever it tries.
    if shutil.which("unshare") and subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode == 0:
        cmd = ["unshare", "-rn", *cmd]
    return cmd


_CMD = None


def run(code, func, args, toolkit=None, log=None):
    """Run func from code in the sandbox; returns its result."""
    global _CMD
    if _CMD is None:
        _CMD = _sandbox_cmd()
    proc = subprocess.Popen(_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={"PATH": "/usr/bin:/bin"}, cwd="/")
    deadline = time.monotonic() + WALL_SECONDS
    try:
        proc.stdin.write(json.dumps({"code": code, "func": func, "args": args}) + "\n")
        proc.stdin.flush()
        while True:
            if time.monotonic() > deadline:
                raise CustomCodeError(f"{func}() took longer than {WALL_SECONDS} s")
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read()[-400:]
                raise CustomCodeError(f"{func}() crashed: {err or 'no output (CPU or memory limit?)'}")
            msg = json.loads(line)
            if "call" in msg:
                if toolkit is None:
                    reply = {"error": "check() can't call ctx"}
                elif msg["call"] not in {"route", "alternatives", "matrix", "road_points", "places",
                                         "places_along", "geocode"}:
                    reply = {"error": f"unknown tool {msg['call']}"}
                else:
                    toolkit.calls += 1
                    if toolkit.calls > 60:
                        reply = {"error": "too many routing calls (limit 60)"}
                    else:
                        try:
                            reply = {"ok": getattr(toolkit, msg["call"])(*msg.get("args", []),
                                                                         **(msg.get("kwargs") or {}))}
                        except Exception as error:  # the code sees the error, like any API
                            reply = {"error": f"{type(error).__name__}: {error}"[:300]}
                proc.stdin.write(json.dumps(reply) + "\n")
                proc.stdin.flush()
            elif "log" in msg:
                if log is not None:
                    log.append(msg["log"])
            elif "error" in msg:
                raise CustomCodeError(f"{func}() raised {msg['error']}")
            else:
                return msg.get("result")
    finally:
        proc.kill()
        proc.wait()
