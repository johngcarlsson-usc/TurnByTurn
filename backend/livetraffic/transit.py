"""Bus positions from 511 SF Bay's regional GTFS-realtime feed (key: FT_SF511_API_KEY).

The research doc's arterial probe: buses run on exactly the streets where we
have no trustworthy live speed, and their travel times between successive
positions follow general traffic there. A bus also stops for passengers, so a
raw bus speed is not a car speed; the usable signal is a bus's travel time on a
street stretch compared with buses' usual time on that stretch at that hour of
the week. That needs weeks of history, so for now this source is only logged
(run.HISTORY_DIR, "transit-YYYY-MM-DD.jsonl.gz") and does not change routing.

511 allows 60 requests an hour per key, shared with the traffic events
(sf511.py, 12 an hour); run.EVERY_S polls this every 2 minutes (30 an hour).
"""

import os

import requests
from google.transit import gtfs_realtime_pb2

from . import _load_local_env

URL = "https://api.511.org/transit/vehiclepositions"
AGENCY = "RG"  # the regional feed: every operator 511 carries


def poll():
    """{"agency:vehicle": [lat, lon, unix time, route_id, trip_id, speed m/s or None]}."""
    _load_local_env()
    response = requests.get(URL, params={"api_key": os.environ["FT_SF511_API_KEY"], "agency": AGENCY}, timeout=60)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    out = {}
    for entity in feed.entity:
        if not entity.HasField("vehicle") or not entity.vehicle.HasField("position"):
            continue
        v = entity.vehicle
        agency = v.trip.route_id.split(":")[0] if ":" in v.trip.route_id else ""
        out[f"{agency}:{v.vehicle.id or entity.id}"] = [
            round(v.position.latitude, 6), round(v.position.longitude, 6), v.timestamp or feed.header.timestamp,
            v.trip.route_id, v.trip.trip_id, round(v.position.speed, 1) if v.position.HasField("speed") else None]
    return out, {"vehicles": len(out), "feedTime": feed.header.timestamp}
