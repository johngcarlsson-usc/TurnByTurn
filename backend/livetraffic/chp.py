"""CHP incidents (media.chp.ca.gov/sa_xml/sa.xml, public, no key).

Each active log becomes a slowdown on the catalog edges of its road and
direction near the incident and a little upstream, cleared when CHP closes
the log. The impact per incident type is NOT published anywhere; the factors
below are tunable assumptions (the research doc notes capacity-reduction
factors "need hand tuning; no sourced model was found").
"""

import math
import re
import xml.etree.ElementTree as ET

import requests

FEED = "https://media.chp.ca.gov/sa_xml/sa.xml"

# (pattern in LogType, speed factor or "closed"); first match wins. Tunable assumptions.
IMPACT = [
    (r"closure", "closed"),
    (r"sig ?alert", 0.3),
    (r"collision.*(inj|1141|enrt|ambulance)", 0.5),
    (r"collision", 0.7),
    (r"hazard|debris|disabled|stall", 0.85),
]
REACH_UPSTREAM_M = 800    # queue behind the incident
# A closure closes only the edges at the incident; the queue behind it is slowed,
# not closed, so exits before the incident stay usable. Tunable assumption:
# as slow as a sig alert.
QUEUE_BEHIND_CLOSURE = 0.3
NOT_MAINLINE = {"ramp", "turn_channel"}  # Valhalla edge uses that are not the named road itself
REACH_DOWNSTREAM_M = 150
MATCH_RADIUS_M = 250      # the incident point must be this close to the road

DIRECTIONS = {"N": 0, "E": 90, "S": 180, "W": 270}


def _logs(content):
    """Every complete <Log> element. The feed is sometimes served cut off
    mid-file (being rewritten upstream); complete entries are still good."""
    try:
        return list(ET.fromstring(content).iter("Log"))
    except ET.ParseError:
        blocks = re.findall(rb"<Log\b.*?</Log>", content, re.S)
        return [ET.fromstring(block) for block in blocks]


def fetch():
    incidents = []
    for log in _logs(requests.get(FEED, timeout=20).content):
        text = {child.tag: (child.text or "").strip().strip('"') for child in log}
        match = re.fullmatch(r"(\d+):(\d+)", text.get("LATLON", ""))
        if not match:
            continue
        lat, lon = int(match.group(1)) / 1e6, -int(match.group(2)) / 1e6  # longitude sign is implied
        incidents.append({"id": log.get("ID"), "type": text.get("LogType", ""), "location": text.get("Location", ""),
                          "desc": text.get("LocationDesc", ""), "time": text.get("LogTime", ""),
                          "lat": lat, "lon": lon})
    return incidents


def impact(incident):
    for pattern, effect in IMPACT:
        if re.search(pattern, incident["type"], re.I):
            return effect
    return None


def road_and_direction(incident):
    """("I 80", 270) from "I80 W / Ashby Ave"; None parts when not stated."""
    first = incident["location"].split("/")[0].strip()
    m = re.match(r"(?i)(I|Us|Sr|Hwy)\s*-?(\d+)\s*([NSEW])?\b", first)
    road = heading = None
    if m:
        prefix = {"i": "I", "us": "US", "sr": "CA", "hwy": "CA"}[m.group(1).lower()]
        road = f"{prefix} {m.group(2)}"
        if m.group(3):
            heading = DIRECTIONS[m.group(3).upper()]
    d = re.search(r"\b([NSEW])B\b", incident["desc"])
    if heading is None and d:
        heading = DIRECTIONS[d.group(1)]
    return road, heading


def _local_m(lat0, lon0, lat, lon):
    k = math.cos(math.radians(lat0))
    return (lon - lon0) * 111320 * k, (lat - lat0) * 110540


def _bearing(a, b):
    x, y = b[0] - a[0], b[1] - a[1]
    return math.degrees(math.atan2(x, y)) % 360


def affected_edges(incident, edges):
    """{edge id: metres from the incident} on the incident's road and direction, within the reach window."""
    road, heading = road_and_direction(incident)
    return edges_near(incident["lat"], incident["lon"], road, heading, edges)


def effect_on(effect, metres):
    """A closure closes the edges at the point; further back the queue is only slowed."""
    if effect == "closed" and metres > MATCH_RADIUS_M:
        return QUEUE_BEHIND_CLOSURE
    return effect


def edges_near(lat, lon, road, heading, edges):
    """{edge id: metres from the point} for catalog edges on `road` ("I 80", or None
    for any) heading `heading` (degrees, or None for both ways) at the point and up
    to REACH_UPSTREAM_M behind it. Ramps never carry the named road itself.
    Shared by every point-incident source (CHP, 511)."""
    incident = {"lat": lat, "lon": lon}
    hits = {}
    for edge in edges:
        if road and edge.get("use") in NOT_MAINLINE:
            continue
        if road and not any(n.replace("-", " ").upper().startswith(road.upper()) for n in edge["names"]):
            continue
        pts = [_local_m(incident["lat"], incident["lon"], lat, lon) for lat, lon in edge["shape"]]
        nearest = min(math.hypot(x, y) for x, y in pts)
        if nearest > (MATCH_RADIUS_M if not road else REACH_UPSTREAM_M):
            continue
        travel = _bearing(pts[0], pts[-1])
        if heading is not None and min(abs(travel - heading), 360 - abs(travel - heading)) > 60:
            continue  # the other carriageway
        # upstream = the incident lies ahead of the edge along its direction of travel
        ahead = (pts[-1][0] * math.sin(math.radians(travel)) + pts[-1][1] * math.cos(math.radians(travel)))
        if nearest <= MATCH_RADIUS_M or (ahead < 0 and nearest <= REACH_UPSTREAM_M) or nearest <= REACH_DOWNSTREAM_M:
            hits[edge["id"]] = nearest
    return hits


def poll(edges):
    """{edge id: factor or "closed"} for every active incident with a known impact."""
    effects, used = {}, []
    region = [e for e in edges if e["road_class"] in {"motorway", "trunk", "primary"}]
    for incident in fetch():
        effect = impact(incident)
        if effect is None:
            continue
        if not (37.0 < incident["lat"] < 38.3 and -122.8 < incident["lon"] < -121.4):
            continue  # outside the catalog's region (Bay Area)
        hit = affected_edges(incident, region)
        if not hit:
            continue
        used.append({**incident, "effect": effect, "edges": len(hit)})
        for edge_id, metres in hit.items():
            here, prev = effect_on(effect, metres), effects.get(edge_id)
            effects[edge_id] = "closed" if "closed" in (prev, here) else min(prev or 1.0, here)
    return effects, used
