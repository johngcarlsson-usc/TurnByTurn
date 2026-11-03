"""Child process that runs model-written route code (see custom.py).

It is started with `python -I`, in its own network namespace when the system
allows it, with CPU, memory and file-size limits. The code has already passed
an AST check (no imports, no dunder access, no eval/open). Its only way to
reach the routing engine is `ctx`, whose methods are relayed to the parent as
JSON lines on stdout and answered on stdin.

Protocol (one JSON object per line):
  parent -> child  {"code": str, "func": "plan" | "check", "args": [...]}
  child  -> parent {"call": name, "args": [...], "kwargs": {...}}   (tool call)
  parent -> child  {"ok": value} | {"error": message}
  child  -> parent {"log": text}                                  (print)
  child  -> parent {"result": value} | {"error": message}         (done)
"""

import json
import math
import resource
import sys
from types import SimpleNamespace

from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from ortools.linear_solver import pywraplp
from ortools.sat.python import cp_model

resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
resource.setrlimit(resource.RLIMIT_AS, (3 << 30, 3 << 30))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

_out = sys.stdout
_in = sys.stdin


def _send(obj):
    _out.write(json.dumps(obj) + "\n")
    _out.flush()


def _call(name, *args, **kwargs):
    _send({"call": name, "args": list(args), "kwargs": kwargs})
    reply = json.loads(_in.readline())
    if "error" in reply:
        raise RuntimeError(f"{name}: {reply['error']}")
    return reply["ok"]


class Ctx:
    """What the code can do. Points are {"lat", "lng"} dicts."""

    def __init__(self, info):
        self.origin = info.get("origin")
        self.destination = info.get("destination")
        self.stops = info.get("stops") or []
        self.request = info.get("request")

    def route(self, points, avoid_points=None, avoid_polygons=None):
        return _call("route", points, avoid_points=avoid_points, avoid_polygons=avoid_polygons)

    def alternatives(self, start, end, n=3):
        return _call("alternatives", start, end, n=n)

    def matrix(self, sources, targets):
        return _call("matrix", sources, targets)

    def road_points(self, name):
        return _call("road_points", name)

    def places(self, what, near, radius_m=3000, brands=None, limit=20):
        return _call("places", what, near, radius_m=radius_m, brands=brands, limit=limit)

    def places_along(self, route, what, max_offset_m=1000, brands=None, limit=30):
        return _call("places_along", route.get("coordinates") if isinstance(route, dict) else route,
                     what, max_offset_m=max_offset_m, brands=brands, limit=limit)

    def geocode(self, text):
        return _call("geocode", text)


def dist_m(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlng = math.radians(b["lng"] - a["lng"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def bearing(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlng = math.radians(b["lng"] - a["lng"])
    x = math.sin(dlng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def circle(center, radius_m, n=24):
    """Polygon ring [[lng, lat], ...] around a point (for avoid_polygons)."""
    ring = []
    for i in range(n + 1):
        t = 2 * math.pi * i / n
        dlat = radius_m * math.cos(t) / 111320
        dlng = radius_m * math.sin(t) / (111320 * math.cos(math.radians(center["lat"])))
        ring.append([center["lng"] + dlng, center["lat"] + dlat])
    return ring


def point_in_ring(point, ring):
    x, y, inside = point["lng"], point["lat"], False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


SAFE_BUILTINS = {name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
                 for name in ("abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
                              "isinstance", "len", "list", "map", "max", "min", "range", "reversed", "round",
                              "getattr", "hasattr", "divmod", "pow", "frozenset", "bin", "hex", "chr", "ord",
                              "set", "sorted", "str", "sum", "tuple", "zip", "Exception", "ValueError",
                              "RuntimeError", "KeyError", "IndexError", "ZeroDivisionError", "True", "False", "None")
                 if (name in __builtins__ if isinstance(__builtins__, dict) else hasattr(__builtins__, name))}


# OR-Tools for the code, as solver classes and constants only: a module object
# would expose whatever it imported (os, sys) as plain attributes.
ORTOOLS = SimpleNamespace(
    CpModel=cp_model.CpModel, CpSolver=cp_model.CpSolver, OPTIMAL=cp_model.OPTIMAL,
    FEASIBLE=cp_model.FEASIBLE, INFEASIBLE=cp_model.INFEASIBLE, LinearExpr=cp_model.LinearExpr,
    LinearSolver=pywraplp.Solver,
    RoutingIndexManager=pywrapcp.RoutingIndexManager, RoutingModel=pywrapcp.RoutingModel,
    default_routing_parameters=pywrapcp.DefaultRoutingSearchParameters,
    FirstSolutionStrategy=routing_enums_pb2.FirstSolutionStrategy,
    LocalSearchMetaheuristic=routing_enums_pb2.LocalSearchMetaheuristic,
)


def _print(*parts):
    _send({"log": " ".join(str(p) for p in parts)[:500]})


def main():
    job = json.loads(_in.readline())
    env = {"__builtins__": {**SAFE_BUILTINS, "print": _print}, "math": math, "ortools": ORTOOLS, "dist_m": dist_m,
           "bearing": bearing, "circle": circle, "point_in_ring": point_in_ring}
    try:
        exec(compile(job["code"], "<route-code>", "exec"), env)
        func = env.get(job["func"])
        if func is None:
            _send({"result": None})
            return
        args = list(job.get("args") or [])
        if job["func"] == "plan":
            args = [Ctx(args[0])]
        elif func.__code__.co_argcount < len(args):
            args = args[:func.__code__.co_argcount]  # check(route) without the data argument
        _send({"result": func(*args)})
    except Exception as error:  # report to the parent, which shows it to the model
        _send({"error": f"{type(error).__name__}: {error}"[:600]})


if __name__ == "__main__":
    main()
