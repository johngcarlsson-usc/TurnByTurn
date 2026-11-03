"""Independent verification of a finished route against the constraint IR.

Works only from the final line and a FRESH map-match of it, never from the
planner's own bookkeeping. A constraint the planner silently dropped (an
exclusion Valhalla rejected, a stop the solver skipped, a deadline it could
not meet) shows up here as a failed check instead of a plausible-looking route.
"""

from shapely.geometry import Polygon

from .geo import LocalProjection, decode_shape
from .rules import MOTORWAY_CLASSES, edge_geometry


STOP_TOLERANCE_M = 150


def _check(name, passed, detail):
    return {"name": name, "passed": bool(passed), "detail": detail}


def _fmt_clock(seconds_from_midnight):
    seconds_from_midnight = int(seconds_from_midnight) % (24 * 3600)
    hours, rem = divmod(seconds_from_midnight, 3600)
    suffix = "am" if hours < 12 else "pm"
    return f"{(hours % 12) or 12}:{rem // 60:02d}{suffix}"


# "coffee in an hour": the planner searches the stretch driven within this many minutes either side
# of the time asked (whichever is larger; widened 2x, 3x if empty), and the check holds the chosen stop
# to the window it used
AFTER_WINDOW_MIN, AFTER_WINDOW_FRACTION = 5, 0.10
AFTER_SLACK_MIN = 5  # the final route's timing differs a little from the base route's (the stop, the detour)


def arrival_side(trip, stop_index, stop):
    """Side of the road a stop is on as the car arrives, from the leg's own
    geometry: the heading over the last 15 m of the leg into the stop."""
    shapes = trip.get("legShapes") or []
    if stop_index >= len(shapes):
        return None
    pts = decode_shape(shapes[stop_index])
    proj = LocalProjection(stop["lat"])
    line = proj.line(pts)
    if line.length == 0:
        return None
    point = proj.point(stop["lat"], stop["lng"])
    a = line.interpolate(max(0.0, line.length - 15))
    b = line.interpolate(line.length)
    cross = (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)
    return "right" if cross < 0 else "left"


def verify(trip, edges, ir, stops, rules, storm_rings=None, depart_clock_s=None, deadline_clock_s=None):
    """Return a list of checks. stops: chosen stops in visiting order, each with
    lat/lng/label and optional maxDetourMinutes, detourSeconds, closesAtClock."""
    checks = []
    latlons = trip["latlons"]
    proj = LocalProjection(sum(p[0] for p in latlons) / len(latlons))
    line = proj.line(latlons)

    positions = []
    for stop in stops:
        point = proj.point(stop["lat"], stop["lng"])
        gap = line.distance(point)
        positions.append(line.project(point))
        checks.append(_check(
            f"Stops at {stop['label']}",
            gap <= STOP_TOLERANCE_M,
            f"route passes {gap:.0f} m from it",
        ))
    sides = trip.get("sidesOfStreet") or []
    for index, stop in enumerate(stops):
        if stop.get("side") == "right":
            # Our geometry decides; Valhalla's own report is shown alongside.
            own = arrival_side(trip, index, stop)
            reported = sides[index + 1] if index + 1 < len(sides) else None
            checks.append(_check(
                f"{stop['label']} is on the right",
                own == "right",
                f"arrive with it on the {own or '?'} (Valhalla: {reported or '?'})",
            ))
    if ir.get("stopOrder") == "as_listed" and len(stops) > 1:
        in_order = all(a <= b for a, b in zip(positions, positions[1:]))
        checks.append(_check("Stops in the order asked", in_order,
                             " → ".join(stop["label"] for stop in stops)))

    for rule in rules:
        hits = [edge for edge in edges if rule.violates(edge)]
        names = sorted({name for edge in hits for name in edge["names"]})[:4]
        checks.append(_check(
            rule.describe()[:1].upper() + rule.describe()[1:],
            not hits,
            "clean" if not hits else f"{len(hits)} segment(s) on {', '.join(names) or 'unnamed road'}",
        ))

    avoid = ir.get("avoid", {})
    if avoid.get("highways"):
        hits = [edge for edge in edges if edge.get("roadClass") in MOTORWAY_CLASSES]
        checks.append(_check("No freeways", not hits, "clean" if not hits else f"{len(hits)} freeway segment(s)"))
    if avoid.get("tolls"):
        hits = [edge for edge in edges if edge.get("toll")]
        names = sorted({name for edge in hits for name in edge["names"]})[:3]
        checks.append(_check("No tolls", not hits, "clean" if not hits else f"toll on {', '.join(names) or 'a segment'}"))
    if avoid.get("ferries"):
        hits = [edge for edge in edges if edge.get("use") == "ferry"]
        checks.append(_check("No ferries", not hits, "clean" if not hits else "uses a ferry"))
    if storm_rings:
        storms = [Polygon(ring) for ring in storm_rings]
        hits = [edge for edge in edges if any(edge_geometry(edge).intersects(storm) for storm in storms)]
        checks.append(_check("Stays out of storm cells", not hits,
                             "clean" if not hits else f"{len(hits)} segment(s) inside a storm cell"))

    for stop in stops:
        if stop.get("maxDetourMinutes") is not None and stop.get("detourSeconds") is not None:
            detour_min = stop["detourSeconds"] / 60
            checks.append(_check(
                f"{stop['label']} within {stop['maxDetourMinutes']:g} min detour",
                detour_min <= stop["maxDetourMinutes"] + 0.5,
                f"adds {max(0.0, detour_min):.1f} min",
            ))

    elapsed = 0.0
    for index, stop in enumerate(stops):
        elapsed += trip["legDurations"][index] if index < len(trip["legDurations"]) else 0
        if stop.get("afterMinutes"):
            target = stop["afterMinutes"]
            window = stop.get("afterWindowMinutes") or max(AFTER_WINDOW_MIN, AFTER_WINDOW_FRACTION * target)
            checks.append(_check(
                f"{stop['label']} about {target:g} min in",
                abs(elapsed / 60 - target) <= window + AFTER_SLACK_MIN,
                f"reached {elapsed / 60:.0f} min in (searched ±{window:g} min)",
            ))
        elapsed += stop.get("dwellSeconds", 0)

    if depart_clock_s is not None:
        clock = depart_clock_s
        for index, stop in enumerate(stops):
            clock += trip["legDurations"][index] if index < len(trip["legDurations"]) else 0
            if stop.get("closesAtClock") is not None:
                checks.append(_check(
                    f"Reach {stop['label']} before {_fmt_clock(stop['closesAtClock'])}",
                    clock <= stop["closesAtClock"],
                    f"arrive {_fmt_clock(clock)}",
                ))
            clock += stop.get("dwellSeconds", 0)
        if deadline_clock_s is not None:
            if len(trip["legDurations"]) > len(stops):
                clock += trip["legDurations"][len(stops)]
            checks.append(_check(
                f"Arrive by {_fmt_clock(deadline_clock_s)}",
                clock <= deadline_clock_s,
                f"arrive {_fmt_clock(clock)}",
            ))
    return checks
