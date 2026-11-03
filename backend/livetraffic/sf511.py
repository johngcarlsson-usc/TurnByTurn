"""511 SF Bay traffic events (api.511.org, free token FT_SF511_API_KEY).

Official Caltrans / agency events with a lane state per road and direction:
CLOSED becomes a closure; partial closures become slowdowns (tunable
assumptions, like CHP's). The token allows 60 requests an hour; one poll every
5 minutes uses 12.
"""

import json
import os
import re

import requests

from .chp import DIRECTIONS, edges_near, effect_on

URL = "https://api.511.org/traffic/events"
# lane state -> speed factor or "closed" (tunable assumptions)
STATE_IMPACT = {"CLOSED": "closed", "SINGLE_LANE_ALTERNATING": 0.4, "SOME_LANES_CLOSED": 0.7}


def fetch():
    key = os.environ.get("FT_SF511_API_KEY")
    if not key:
        raise RuntimeError("FT_SF511_API_KEY is not set")
    response = requests.get(URL, params={"api_key": key, "format": "json", "status": "ACTIVE", "limit": 500},
                            timeout=30)
    response.raise_for_status()
    return json.loads(response.content.decode("utf-8-sig")).get("events", [])


def road_name(name):
    """"I-280 N" -> "I 280"; "CA-92 E" -> "CA 92"; "US-101" -> "US 101"."""
    m = re.match(r"(?i)(I|US|CA|SR)-?\s*(\d+)", name or "")
    if not m:
        return None
    prefix = {"sr": "CA"}.get(m.group(1).lower(), m.group(1).upper())
    return f"{prefix} {m.group(2)}"


def headings(direction):
    """"Southbound" -> [180]; "Eastbound and Westbound" -> [None] (both ways)."""
    found = [DIRECTIONS[d[0].upper()] for d in re.findall(r"(?i)\b(north|south|east|west)bound", direction or "")]
    return [found[0]] if len(found) == 1 else [None]


def poll(edges):
    effects, used = {}, []
    region = [e for e in edges if e["road_class"] in {"motorway", "trunk", "primary", "secondary"}]
    for event in fetch():
        geo = event.get("geography") or {}
        if geo.get("type") != "Point":
            continue
        lon, lat = geo["coordinates"][:2]
        for road in event.get("roads", []):
            effect = STATE_IMPACT.get(road.get("state"))
            if effect is None:
                continue
            hit = {}
            for heading in headings(road.get("direction")):
                hit.update(edges_near(lat, lon, road_name(road.get("name")), heading, region))
            if not hit:
                continue
            used.append({"id": event.get("id"), "type": event.get("event_type"), "headline": event.get("headline"),
                         "state": road.get("state"), "effect": effect, "edges": len(hit)})
            for edge_id, metres in hit.items():
                here, prev = effect_on(effect, metres), effects.get(edge_id)
                effects[edge_id] = "closed" if "closed" in (prev, here) else min(prev or 1.0, here)
    return effects, used
