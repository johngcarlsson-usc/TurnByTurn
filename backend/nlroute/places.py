import os
import re
import threading

import duckdb
from shapely import STRtree

from .geo import LocalProjection, haversine_m


PLACES_PARQUET = os.environ.get(
    "PLACES_PARQUET", os.path.expanduser("~/geo/norcal/places.parquet"),
)

# Plain-language errand words -> Overture primary categories. The model emits
# the driver's own word ("coffee"); this table, not the model, decides what
# counts, so the mapping is inspectable and the same every run.
CATEGORY_SYNONYMS = {
    "coffee": ["coffee_shop", "cafe"],
    "cafe": ["cafe", "coffee_shop"],
    "tea": ["tea_room", "bubble_tea"],
    "boba": ["bubble_tea"],
    "gas": ["gas_station"],
    "fuel": ["gas_station"],
    "ev charger": ["ev_charging_station"],
    "charger": ["ev_charging_station"],
    "charging": ["ev_charging_station"],
    "pharmacy": ["pharmacy", "drugstore"],
    "drugstore": ["drugstore", "pharmacy"],
    "groceries": ["grocery_store", "supermarket"],
    "grocery": ["grocery_store", "supermarket"],
    "supermarket": ["supermarket", "grocery_store"],
    "food": ["restaurant", "fast_food_restaurant", "sandwich_shop", "burger_restaurant"],
    "lunch": ["restaurant", "sandwich_shop", "fast_food_restaurant", "deli"],
    "dinner": ["restaurant"],
    "breakfast": ["breakfast_and_brunch_restaurant", "cafe", "bagel_shop", "bakery"],
    "fast food": ["fast_food_restaurant", "burger_restaurant"],
    "drive through": ["fast_food_restaurant"],
    "burger": ["burger_restaurant", "fast_food_restaurant"],
    "pizza": ["pizza_restaurant"],
    "tacos": ["mexican_restaurant", "taco_restaurant"],
    "bakery": ["bakery"],
    "donuts": ["donuts"],
    "ice cream": ["ice_cream_shop"],
    "restroom": ["gas_station", "coffee_shop", "fast_food_restaurant"],
    "bathroom": ["gas_station", "coffee_shop", "fast_food_restaurant"],
    "atm": ["atms", "bank_credit_union"],
    "bank": ["bank_credit_union"],
    "car wash": ["car_wash"],
    "hardware": ["hardware_store"],
    "flowers": ["flowers_and_gifts_shop", "florist"],
    "post office": ["post_office"],
    "parking": ["parking"],
    "park": ["park"],
    "hospital": ["hospital"],
    "urgent care": ["urgent_care_clinic"],
    "vet": ["veterinarian"],
    "library": ["library"],
    "bookstore": ["bookstore"],
    "brewery": ["brewery"],
    "bar": ["bar", "pub", "cocktail_bar"],
    "wine": ["wine_bar", "winery", "liquor_store"],
    "hotel": ["hotel"],
    "convenience store": ["convenience_store"],
}


# Data-quality patch, deliberately visible: Overture tags these as the wrong
# category at high confidence (e.g. "Oportun", a lender, appears as `pharmacy`
# ~20 times at 0.92-0.99). No score threshold catches that, and neither can the
# route verifier: the stop IS where the data says a pharmacy is.
CATEGORY_DENYLIST = {
    "pharmacy": {"oportun"},
    "drugstore": {"oportun"},
    "coffee_shop": {"hills bros coffee plant",
                    "gano excel"},  # a direct-sales coffee company's business-park unit in Fairfield, not a café
}


def categories_for(what):
    """Overture categories for a driver's errand word, or [] if unknown."""
    key = re.sub(r"[^a-z ]+", "", (what or "").lower()).strip()
    if key in CATEGORY_SYNONYMS:
        return CATEGORY_SYNONYMS[key]
    for word, categories in CATEGORY_SYNONYMS.items():
        if re.search(rf"\b{re.escape(word)}s?\b", key):
            return categories
    return []


_local = threading.local()


def _con():
    con = getattr(_local, "con", None)
    if con is None:
        con = duckdb.connect()
        _local.con = con
    return con


def _rows(sql, params):
    cursor = _con().execute(sql, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _norm(text):
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\b(inc|llc|ltd|co|corp|company|the|of|and|a|an)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_score(query, candidate):
    """Token overlap with an extra-token penalty.

    Containment alone cannot separate "Home Depot" from "home depot DC 5695":
    the query appears in full in both. Penalising tokens the candidate has
    beyond the query is what picks the store over the distribution centre.
    """
    q, c = _norm(query).split(), _norm(candidate).split()
    if not q or not c:
        return 0.0
    qs, cs = set(q), set(c)
    inter = len(qs & cs)
    if not inter:
        return 0.0
    score = 0.6 * inter / len(qs | cs) + 0.4 * inter / len(qs)
    extra = max(0, len(cs) - len(qs))
    return score - 0.12 * extra


def place_dict(row):
    return {
        "id": row["id"],
        "label": row["name"],
        "category": row["category"],
        "brand": row.get("brand"),
        "address": row.get("address"),
        "locality": row.get("locality"),
        "lat": row["lat"],
        "lng": row["lon"],
        "source": "Overture",
    }


def resolve_place(text, near=None, radius_m=60000, limit=5):
    """Resolve a named place ("Ferry Building", "Oakland airport") via Overture.

    Returns ranked candidates; the caller decides whether the top one is
    confident enough or whether to ask the driver.
    """
    tokens = [t for t in _norm(text).split() if len(t) > 2] or _norm(text).split()
    if not tokens:
        return []
    probe = max(tokens, key=len)
    params = [f"%{probe}%"]
    where = "lower(name) LIKE ?"
    if near:
        d_lat = radius_m / 110540.0
        d_lon = d_lat * 1.3
        where += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        params += [near["lat"] - d_lat, near["lat"] + d_lat, near["lng"] - d_lon, near["lng"] + d_lon]
    rows = _rows(
        f"SELECT id, name, category, brand, address, locality, lat, lon, confidence "
        f"FROM read_parquet('{PLACES_PARQUET}') WHERE {where} LIMIT 2000",
        params,
    )
    locality_tokens = set(_norm(text).split())
    scored = []
    for row in rows:
        score = name_score(text, row["name"])
        # Text like "Safeway in Daly City" names the locality, not the store.
        if row.get("locality") and set(_norm(row["locality"]).split()) <= locality_tokens:
            score += 0.35
        score += 0.1 * (row.get("confidence") or 0)
        if near:
            score -= haversine_m(near["lat"], near["lng"], row["lat"], row["lon"]) / 400000.0
        if score > 0.2:
            scored.append((score, row))
    scored.sort(key=lambda item: -item[0])
    # Duplicate listings of one place often disagree on where it is ("Golden
    # Gate Park" appears at a Presidio address and at the city's centroid).
    # Among same-name ties, lead with the medoid: the one nearest the others.
    if scored:
        top_name = _norm(scored[0][1]["name"])
        ties = [item for item in scored if item[0] >= scored[0][0] - 0.1 and _norm(item[1]["name"]) == top_name]
        if len(ties) >= 3:
            medoid = min(ties, key=lambda a: sum(
                haversine_m(a[1]["lat"], a[1]["lon"], b[1]["lat"], b[1]["lon"]) for b in ties))
            scored.remove(medoid)
            scored.insert(0, medoid)
    return [{**place_dict(row), "score": round(score, 3)} for score, row in scored[:limit]]


def _squash(column):
    """SQL expression: lowercase alphanumerics only, so "Peet’s" = "peets"."""
    return f"regexp_replace(lower(coalesce({column}, '')), '[^a-z0-9]', '', 'g')"


def _side_of(line, along, point):
    """Which side of the travel direction a point is on: "right" or "left".

    The sign of the cross product between the route's heading at the nearest
    point and the vector to the place; negative is to the right.
    """
    if line.length == 0:
        return None
    a = line.interpolate(max(0.0, along - 5))
    b = line.interpolate(min(line.length, along + 5))
    cross = (b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)
    return "right" if cross < 0 else "left"


def search_along_route(latlons, what, brands=None, max_offset_m=1500, limit=40):
    """Places of an errand type near a route line, nearest to the line first.

    Straight-line offset is only a prefilter: "on the way" is decided later
    by real detour minutes from a Valhalla matrix, because a cafe 200 m from
    a freeway can still be a ten-minute detour.
    """
    categories = categories_for(what)
    lats = [p[0] for p in latlons]
    lons = [p[1] for p in latlons]
    pad_lat = max_offset_m / 110540.0
    pad_lon = pad_lat * 1.3
    params = [min(lats) - pad_lat, max(lats) + pad_lat, min(lons) - pad_lon, max(lons) + pad_lon]
    filters = []
    if categories:
        filters.append(f"category IN ({', '.join('?' for _ in categories)})")
        params += categories
    squashed = [re.sub(r"[^a-z0-9]", "", b.lower()) for b in (brands or [])]
    squashed = [b for b in squashed if b]
    if squashed:
        filters.append("(" + " OR ".join(
            f"{_squash('brand')} LIKE ? OR {_squash('name')} LIKE ?" for _ in squashed) + ")")
        for b in squashed:
            params += [f"%{b}%", f"%{b}%"]
    if not filters:
        filters.append("lower(name) LIKE ?")
        params.append(f"%{(what or '').lower()}%")
    rows = _rows(
        f"SELECT id, name, category, brand, address, locality, lat, lon, confidence "
        f"FROM read_parquet('{PLACES_PARQUET}') "
        f"WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND {' AND '.join(filters)} "
        f"AND coalesce(confidence, 0) >= 0.5",
        params,
    )
    if not rows:
        return []

    proj = LocalProjection(sum(lats) / len(lats))
    line = proj.line(latlons)
    # Chunk the route so the nearest-segment lookup stays cheap on long trips.
    chunks = [proj.line(latlons[i:i + 25]) for i in range(0, max(1, len(latlons) - 1), 24)]
    chunks = [c for c in chunks if c.length > 0] or [line]
    tree = STRtree(chunks)

    results = []
    wants_branch = categories == CATEGORY_SYNONYMS["bank"]
    for row in rows:
        if _norm(row["name"]) in CATEGORY_DENYLIST.get(row["category"], ()):
            continue
        if wants_branch and re.search(r"\batms?\b", _norm(row["name"])):
            continue  # "the bank" means a branch, not a cash machine
        point = proj.point(row["lat"], row["lon"])
        nearest = chunks[tree.nearest(point)]
        offset = nearest.distance(point)
        if offset <= max_offset_m:
            along = line.project(point)
            results.append({
                **place_dict(row),
                "offsetMeters": round(offset),
                "alongRouteFraction": round(along / line.length, 4) if line.length else 0,
                "side": _side_of(line, along, point),
            })
    # Dense downtowns would otherwise fill every slot near one end of the
    # trip; keep the nearest few from each tenth of the route instead.
    buckets = 10
    per_bucket = max(1, limit // buckets)
    by_bucket = {}
    for result in sorted(results, key=lambda r: r["offsetMeters"]):
        bucket = min(buckets - 1, int(result["alongRouteFraction"] * buckets))
        by_bucket.setdefault(bucket, []).append(result)
    spread = [r for bucket in sorted(by_bucket) for r in by_bucket[bucket][:per_bucket]]
    leftovers = [r for r in sorted(results, key=lambda r: r["offsetMeters"]) if r not in spread]
    return (spread + leftovers)[:limit]
