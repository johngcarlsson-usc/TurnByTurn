"""Road-level avoidance rules, shared by the exclusion loop and the verifier.

A rule is a predicate over a map-matched edge. The planner uses it to decide
which edges to exclude next; the verifier uses the same definition against a
fresh trace of the final line, so "avoided" means one thing everywhere.
"""

import re

from shapely.geometry import LineString, Point, Polygon


MOTORWAY_CLASSES = {"motorway", "trunk"}


def normalize_road_name(name):
    text = (name or "").lower().replace("–", " ").replace("-", " ")
    text = re.sub(r"\binterstate\b", "i", text)
    text = re.sub(r"\b(highway|hwy|route|state route|sr)\s+(\d+)", r"ca \2", text)
    text = re.sub(r"\bus\s*(\d+)", r"us \1", text)
    text = re.sub(r"\bi\s*(\d+)", r"i \1", text)
    text = re.sub(r"\b(north|south|east|west|northbound|southbound|eastbound|westbound)\b", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    # Keep road-type words ("Bay Bridge" must not match "Bay Street"); only
    # expand abbreviations so "19th Ave" and "19th Avenue" compare equal.
    for short, full in (("st", "street"), ("ave", "avenue"), ("blvd", "boulevard"), ("fwy", "freeway"),
                        ("expy", "expressway"), ("rd", "road"), ("dr", "drive")):
        text = re.sub(rf"\b{short}\b", full, text)
    text = re.sub(r"\bthe\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def road_name_matches(wanted, edge_names):
    """True when an edge carries the named road.

    Refs must match exactly ("i 80" never matches "i 880"); word names match
    when every wanted word appears ("bay" matches "san francisco oakland bay").
    """
    target = normalize_road_name(wanted)
    if not target:
        return False
    is_ref = bool(re.fullmatch(r"(i|us|ca) \d+", target))
    for name in edge_names or []:
        candidate = normalize_road_name(name)
        if is_ref:
            if candidate == target:
                return True
        elif set(target.split()) <= set(candidate.split()):
            return True
    return False


def edge_geometry(edge):
    pts = [(lon, lat) for lat, lon in edge["latlons"]]
    if len(pts) == 1:
        return Point(pts[0])
    return LineString(pts)


class AvoidArea:
    """Stay out of an area: entirely, or only off its freeways."""

    def __init__(self, label, ring_lonlat, roads):
        self.label = label
        self.ring = ring_lonlat
        self.polygon = Polygon(ring_lonlat)
        self.roads = roads

    def violates(self, edge):
        if self.roads == "freeways" and edge.get("roadClass") not in MOTORWAY_CLASSES:
            return False
        return edge_geometry(edge).intersects(self.polygon)

    def describe(self):
        scope = "freeways in" if self.roads == "freeways" else "roads in"
        return f"no {scope} {self.label}"


class AvoidRoad:
    """Stay off a named road, optionally only within an area."""

    def __init__(self, name, within=None, within_label=None):
        self.name = name
        self.within = Polygon(within) if within else None
        self.within_label = within_label

    def violates(self, edge):
        if not road_name_matches(self.name, edge.get("names")):
            return False
        return self.within is None or edge_geometry(edge).intersects(self.within)

    def describe(self):
        return f"not on {self.name}" + (f" near {self.within_label}" if self.within_label else "")


class AvoidBridge:
    """Stay off a bridge by footprint, not by name.

    OSM keeps a bridge's own name in `bridge:name`, which the routing graph
    drops: the Bay Bridge's edges are just "I 80 East". So a famous bridge is
    matched as "a bridge edge inside its known footprint".
    """

    def __init__(self, name, footprint):
        self.name = name
        self.footprint = footprint

    def violates(self, edge):
        return bool(edge.get("bridge")) and edge_geometry(edge).intersects(self.footprint)

    def describe(self):
        return f"not on the {self.name}"
