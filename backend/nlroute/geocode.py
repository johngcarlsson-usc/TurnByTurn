"""Towns and lakes via Nominatim (OpenStreetMap), run locally.

Overture places has businesses and landmarks but no towns: "Davis" resolves to
a stadium in San Francisco and "Truckee" to the DMV. OSM's Nominatim knows
towns, so it is asked for town/lake-scale matches only. It runs on this
machine (docs/local-services.md); nothing here calls a public server.
"""

import os
import re
import threading

import requests


NOMINATIM_SEARCH_URL = os.environ.get("NOMINATIM_SEARCH_URL", "http://127.0.0.1:8088/search")
NORCAL_VIEWBOX = "-123.2,39.8,-119.5,36.9"
TOWN_MAX_RANK = 20  # Nominatim place_rank: 16 = city/town, 20 = lake/large area, 30 = POI

_lock = threading.Lock()
_cache = {}


def _norm(text):
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()


def lookup_area(text):
    """A town or lake matching `text`, or None. Never raises on network errors."""
    key = " ".join(_norm(text))
    if not key:
        return None
    with _lock:
        if key in _cache:
            return _cache[key]
        try:
            results = requests.get(
                NOMINATIM_SEARCH_URL,
                # Prefer Northern California; the rest of the state still matches.
                params={"q": text, "format": "jsonv2", "limit": 3, "countrycodes": "us",
                        "viewbox": NORCAL_VIEWBOX},
                timeout=4,
            ).json()
        except (requests.RequestException, ValueError):
            return None
        found = None
        wanted = set(_norm(text)) - {"the", "city", "of"}
        for result in results:
            if int(result.get("place_rank") or 99) > TOWN_MAX_RANK:
                continue
            if wanted and not wanted <= set(_norm(result.get("name"))):
                continue
            found = {
                "label": result.get("name"),
                "lat": float(result["lat"]),
                "lng": float(result["lon"]),
                "kind": "water" if result.get("category") in {"water", "natural"} else "town",
            }
            break
        _cache[key] = found
        return found


# ------------------------------------------------------------------ Photon
# Photon (komoot) indexes the same Nominatim database for search-as-you-type:
# partial words, typos, extra words ("John W Sinnott elementary") and a bias
# toward the driver's position. Runs locally (docs: scripts/local_services.sh).
PHOTON_URL = os.environ.get("PHOTON_URL", "http://127.0.0.1:2322").rstrip("/")
TOWN_VALUES = {"city", "town", "village", "hamlet", "suburb"}
WATER_VALUES = {"water", "lake", "reservoir", "bay"}
# Stops named after a place ("Ferry Building" tram stop) shadow the place itself;
# a bank's ATM shadows the branch.
TRANSIT_TAGS = ["!amenity:atm", "!railway:tram_stop", "!railway:halt", "!highway:bus_stop", "!public_transport:platform",
                "!public_transport:stop_position"]
TRANSIT_WORDS = {"station", "stop", "bart", "caltrain", "muni", "platform"}


# A request that names what kind of place it is ("Oakland airport") wants a place that IS one:
# Photon ranks "Oakland Airport Executive Hotel" (a hotel) above the airport itself.
KIND_WORDS = {
    "airport": {"aeroway/aerodrome"}, "school": {"amenity/school", "building/school"},
    "elementary": {"amenity/school", "building/school"}, "university": {"amenity/university"},
    "college": {"amenity/college", "amenity/university"}, "hospital": {"amenity/hospital", "building/hospital"},
    "library": {"amenity/library"}, "stadium": {"leisure/stadium", "building/stadium"},
    "hotel": {"tourism/hotel"}, "park": {"leisure/park"}, "mall": {"shop/mall"},
}


def prefer_kind(text, hits):
    """hits with those whose OSM tag is the kind of place the text names moved first (order otherwise kept)."""
    wanted = set().union(*(KIND_WORDS.get(w, set()) for w in _norm(text)))
    if not wanted:
        return hits
    return [h for h in hits if h["category"] in wanted] + [h for h in hits if h["category"] not in wanted]


def _photon_item(feature):
    props, (lng, lat) = feature["properties"], feature["geometry"]["coordinates"]
    street = " ".join(p for p in (props.get("housenumber"), props.get("street")) if p)
    name = props.get("name") or street or props.get("city")
    detail = ", ".join(p for p in (street if props.get("name") else None, props.get("city") or props.get("county"),
                                   props.get("state")) if p and p != name)
    key, value = props.get("osm_key"), props.get("osm_value")
    if key == "place" and value in TOWN_VALUES:
        kind = "town"
    elif key in {"natural", "water"} and value in WATER_VALUES:
        kind = "water"
    else:
        kind = "place"
    return {"label": name, "detail": detail, "lat": lat, "lng": lng, "kind": kind, "category": f"{key}/{value}"}


def photon_search(text, near=None, limit=6):
    """Ranked matches for (possibly partial) text: [{"label", "detail", "lat",
    "lng", "kind": town|water|place, "category"}]. [] if Photon is down."""
    params = [("q", text), ("limit", limit), ("lang", "en")]
    if near:
        params += [("lat", near["lat"]), ("lon", near["lng"])]
    if not TRANSIT_WORDS & set(_norm(text)):
        params += [("osm_tag", tag) for tag in TRANSIT_TAGS]
    try:
        response = requests.get(f"{PHOTON_URL}/api", params=params, timeout=3)
        response.raise_for_status()
        return prefer_kind(text, [_photon_item(f) for f in response.json().get("features", []) if f.get("geometry")])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return []
