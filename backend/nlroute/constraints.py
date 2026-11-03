"""The constraint IR: the only thing the language model produces.

The model never routes, searches, or optimizes. It fills in this schema, and
deterministic code turns each field into geometry (exclusions, corridors,
candidate sets) or a solver constraint. That split is what makes every
decision inspectable on screen and checkable afterwards.
"""

import json
import re


PROFILES = {"auto", "truck", "bicycle", "pedestrian", "motorcycle", "bus"}
STOP_POSITIONS = {"anywhere", "early", "middle", "late"}
STOP_SIDES = {"any", "right"}
ROAD_SCOPES = {"freeways", "all"}
TASK_KINDS = {"escort", "place", "errand"}

IR_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "origin": {"type": ["string", "null"]},
        "destination": {"type": ["string", "null"]},
        "returnToOrigin": {"type": "boolean"},
        "stops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["place", "errand"]},
                    "text": {"type": ["string", "null"]},
                    "what": {"type": ["string", "null"]},
                    "brands": {"type": "array", "items": {"type": "string"}},
                    "side": {"type": "string", "enum": sorted(STOP_SIDES)},
                    "maxDetourMinutes": {"type": ["number", "null"]},
                    "position": {"type": "string", "enum": sorted(STOP_POSITIONS)},
                    "afterMinutes": {"type": ["number", "null"]},
                    "dwellMinutes": {"type": ["number", "null"]},
                    "closesAt": {"type": ["string", "null"]},
                },
                "required": ["kind", "text", "what", "brands", "side", "maxDetourMinutes", "position",
                             "afterMinutes", "dwellMinutes", "closesAt"],
            },
        },
        "stopOrder": {"type": "string", "enum": ["as_listed", "any"]},
        "arriveBy": {"type": ["string", "null"]},
        "avoid": {
            "type": "object",
            "properties": {
                "highways": {"type": "boolean"},
                "tolls": {"type": "boolean"},
                "ferries": {"type": "boolean"},
                "storms": {"type": "boolean"},
                "areas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "radiusMiles": {"type": ["number", "null"]},
                            "roads": {"type": "string", "enum": sorted(ROAD_SCOPES)},
                        },
                        "required": ["text", "radiusMiles", "roads"],
                    },
                },
                "roads": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "near": {"type": ["string", "null"]},
                        },
                        "required": ["name", "near"],
                    },
                },
            },
            "required": ["highways", "tolls", "ferries", "storms", "areas", "roads"],
        },
        "useRoads": {"type": "array", "items": {"type": "string"}},
        "custom": {"type": ["string", "null"]},
        "meetup": {
            "type": "object",
            "properties": {
                "friendFrom": {"type": ["string", "null"]},
                "friendLeftMinutesAgo": {"type": ["number", "null"]},
                "what": {"type": ["string", "null"]},
                "maxDetourMinutes": {"type": ["number", "null"]},
            },
            "required": ["friendFrom", "friendLeftMinutesAgo", "what", "maxDetourMinutes"],
        },
        "team": {
            "type": "object",
            "properties": {
                "partner": {"type": ["string", "null"]},
                "partnerAt": {"type": ["string", "null"]},
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": sorted(TASK_KINDS)},
                            "who": {"type": ["string", "null"]},
                            "from": {"type": ["string", "null"]},
                            "to": {"type": ["string", "null"]},
                            "text": {"type": ["string", "null"]},
                            "what": {"type": ["string", "null"]},
                            "brands": {"type": "array", "items": {"type": "string"}},
                            "arriveBy": {"type": ["string", "null"]},
                            "dwellMinutes": {"type": ["number", "null"]},
                        },
                        "required": ["kind", "who", "from", "to", "text", "what", "brands", "arriveBy",
                                     "dwellMinutes"],
                    },
                },
            },
            "required": ["partner", "partnerAt", "tasks"],
        },
        "profile": {"type": "string", "enum": sorted(PROFILES)},
        "clarification": {"type": ["string", "null"]},
    },
    "required": ["origin", "destination", "returnToOrigin", "stops", "stopOrder", "arriveBy",
                 "avoid", "useRoads", "custom", "meetup", "team", "profile", "clarification"],
}

SYSTEM_PROMPT = """You turn a driver's spoken request into routing constraints for a navigation app in the San Francisco Bay Area.
You do NOT plan the route. You only fill in the JSON schema; software does the routing, searching, and optimizing.

Fields:
- origin: where the trip starts, exactly as the driver said it. null means "my current location".
- destination: where it ends. null if they only change the current trip. "home" and "work" are valid saved places.
- returnToOrigin: true for round trips ("and back", "then home" when they start at home).
- stops: every intermediate stop, in the order spoken.
  - kind "place": a specific named place or person to pick up ("Tartine Bakery", "pick up Sam at Stanford"). Put the place in text.
  - kind "errand": a TYPE of place, not a specific one ("coffee", "gas", "a pharmacy", "groceries", "EV charger"). Put the plain word in what, e.g. "coffee". Put chain names in brands only if they named some ("a Starbucks or a Peet's" -> what "coffee", brands ["Starbucks", "Peet's"]); otherwise brands is [].
  - side: "right" if the stop must be on the driver's side of the road ("on the right", "on my side", "so I don't have to turn across traffic", "no left turns into it"). Else "any".
  - maxDetourMinutes: only if they bound it ("no more than 5 minutes out of the way", "without backtracking" = 3). Else null.
  - position: "early" / "middle" / "late" if they said when ("halfway" = middle, "near the end" = late, "first thing" = early). Else "anywhere".
  - afterMinutes: how far into the drive, in minutes, if they said it as a time ("coffee in an hour" = 60, "after about 45 minutes" = 45, "in an hour and a half" = 90). Then position is "anywhere". Else null.
  - dwellMinutes: only if they said how long. closesAt: 24h "HH:MM" if they said a place closes or must be visited before a time.
- stopOrder: "as_listed" if they gave or implied an order ("first", "then", "after that"); "any" if they said any order, or listed errands with no order.
- arriveBy: 24h "HH:MM" deadline for reaching the destination ("home by 6" = "18:00"). null if none.
- avoid.highways / tolls / ferries: only for explicit requests ("no freeways", "avoid tolls", "no bridges with tolls" -> tolls).
- avoid.storms: true for storms, heavy rain, hail, flooding, bad weather.
- avoid.areas: neighbourhoods or zones to stay out of. text is the area ("downtown San Francisco", "the Mission"). roads: "freeways" if they only want to skip the freeway THROUGH that area ("skip the downtown freeway"), "all" to stay out of the area entirely. radiusMiles only if stated.
- avoid.roads: specific named roads or bridges ("the Bay Bridge", "I-880", "19th Avenue"). near: an area that limits where, else null.
- useRoads: roads they want to TAKE ("use 280", "take the 101", "go over the Golden Gate Bridge", "via Skyline Boulevard"). Freeway numbers as "I-280", "US-101", "CA-1" (I- for 80, 280, 580, 680, 880, 380, 238, 980; US- for 101, 50; else CA-). Else [].
- custom: ANY part of the request the other fields can't express, restated as a precise, checkable requirement (e.g. "no more than 3 left turns", "stay within 1 mile of the Pacific coast wherever possible", "pass by Golden Gate Park without stopping", "the route with the fewest traffic lights"). Software writes code for it, so don't drop a requirement just because no field fits. When the requirement decides WHICH or HOW MANY stops to make ("as many as fit in 40 minutes", "only if it's on the way", "whichever two are closest"), leave those stops out of stops and name them in custom instead. null if the other fields already cover everything; never repeat what they cover. The software always picks the stop with the smallest detour and the fastest route, so "keep the detour small", "minimal detour", "quickest way" need no custom (null).
- meetup: when they want to meet someone on the way who is driving separately. friendFrom: where that person is coming from ("my friend just left Davis" -> "Davis"); friendLeftMinutesAgo: 0 for "just left", a number if said, null if not stated; what: the kind of place to meet ("coffee", "lunch"), null if unsaid; maxDetourMinutes: a bound if they gave one ("no huge detour" = 15). The shared destination goes in destination. Otherwise all meetup fields are null.
- team: when the driver and someone else (a partner, spouse, roommate) split a list of tasks between them, each in their own car, and want the software to decide who does what. partner: how the driver calls them ("my wife" -> "wife"). partnerAt: where that person is now, only if said; null means use the location they share from their phone. tasks: every job either of them could do:
  - kind "escort": take someone from one place to another ("pick up our daughter from school and bring her to swim at Rossi Pool by 2"). who: the person ("daughter"); from / to: the places; arriveBy: 24h "HH:MM" deadline at to, else null.
  - kind "place": go to a specific named place; put it in text.
  - kind "errand": a TYPE of place, like stops: what ("bank", "pharmacy"), brands if they named chains ("CVS" -> what "pharmacy", brands ["CVS"]).
  Unused task fields are null (brands []). When team is used, origin is the driver's start (null = current location), destination is null, and stops is empty. Otherwise partner and partnerAt are null and tasks is [].
- profile: "auto" unless they're walking, cycling, on a motorcycle, bus, or truck.
- clarification: ONLY if no usable destination exists or the request is contradictory. Otherwise null.

If the context has currentConstraints, the driver's message changes them: return the full updated constraints, keeping everything they did not change.
If the context has a previousRequest and a question, the driver's message answers that question: return the full constraints for previousRequest with the answer applied.

Never invent places the driver did not mention. Keep names exactly as spoken. Resolve "downtown" to the downtown of the city in context (default San Francisco)."""

FEW_SHOT = [
    (
        "Get me from the Ferry Building to Stanford, I need coffee on the way but don't make me backtrack.",
        {
            "origin": "Ferry Building", "destination": "Stanford University", "returnToOrigin": False,
            "stops": [{"kind": "errand", "text": None, "what": "coffee", "brands": [], "side": "any", "maxDetourMinutes": 3,
                       "position": "anywhere", "afterMinutes": None, "dwellMinutes": None, "closesAt": None}],
            "stopOrder": "as_listed", "arriveBy": None,
            "avoid": {"highways": False, "tolls": False, "ferries": False, "storms": False, "areas": [], "roads": []},
            "useRoads": [], "custom": None,
            "meetup": {"friendFrom": None, "friendLeftMinutesAgo": None, "what": None, "maxDetourMinutes": None},
            "team": {"partner": None, "partnerAt": None, "tasks": []},
            "profile": "auto", "clarification": None,
        },
    ),
    (
        "Take me to Oakland airport but skip the downtown freeway and stay off the Bay Bridge.",
        {
            "origin": None, "destination": "Oakland International Airport", "returnToOrigin": False, "stops": [],
            "stopOrder": "as_listed", "arriveBy": None,
            "avoid": {"highways": False, "tolls": False, "ferries": False, "storms": False,
                      "areas": [{"text": "downtown San Francisco", "radiusMiles": None, "roads": "freeways"}],
                      "roads": [{"name": "Bay Bridge", "near": None}]},
            "useRoads": [], "custom": None,
            "meetup": {"friendFrom": None, "friendLeftMinutesAgo": None, "what": None, "maxDetourMinutes": None},
            "team": {"partner": None, "partnerAt": None, "tasks": []},
            "profile": "auto", "clarification": None,
        },
    ),
    (
        "My wife's at Stonestown. Between us we need to get our son from Alvarado Elementary to soccer at Kezar Stadium by 3:30, and someone has to hit a CVS.",
        {
            "origin": None, "destination": None, "returnToOrigin": False, "stops": [],
            "stopOrder": "any", "arriveBy": None,
            "avoid": {"highways": False, "tolls": False, "ferries": False, "storms": False, "areas": [], "roads": []},
            "useRoads": [], "custom": None,
            "meetup": {"friendFrom": None, "friendLeftMinutesAgo": None, "what": None, "maxDetourMinutes": None},
            "team": {"partner": "wife", "partnerAt": "Stonestown Galleria", "tasks": [
                {"kind": "escort", "who": "son", "from": "Alvarado Elementary School", "to": "Kezar Stadium",
                 "text": None, "what": None, "brands": [], "arriveBy": "15:30", "dwellMinutes": None},
                {"kind": "errand", "who": None, "from": None, "to": None, "text": None, "what": "pharmacy",
                 "brands": ["CVS"], "arriveBy": None, "dwellMinutes": None},
            ]},
            "profile": "auto", "clarification": None,
        },
    ),
]


def _clean_text(value, limit=200):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _clean_time(value):
    text = _clean_text(value, 16)
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _clean_number(value, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def normalize_ir(raw):
    """Coerce model output into a well-formed IR. Never trusts the model's
    types; anything malformed becomes the neutral default, not an error."""
    if not isinstance(raw, dict):
        raise ValueError("The model did not return a JSON object.")
    avoid = raw.get("avoid") if isinstance(raw.get("avoid"), dict) else {}
    stops = []
    for stop in (raw.get("stops") or [])[:8]:
        if not isinstance(stop, dict):
            continue
        kind = stop.get("kind") if stop.get("kind") in {"place", "errand"} else None
        text = _clean_text(stop.get("text"))
        what = _clean_text(stop.get("what"), 60)
        if kind is None:
            kind = "errand" if what and not text else "place"
        if kind == "place" and not text:
            if not what:
                continue
            kind = "errand"
        if kind == "errand" and not what:
            if not text:
                continue
            what = text
        position = stop.get("position") if stop.get("position") in STOP_POSITIONS else "anywhere"
        raw_brands = stop.get("brands")
        if not isinstance(raw_brands, list):
            raw_brands = [stop.get("brand")] if stop.get("brand") else []
        brands = [b for b in (_clean_text(x, 60) for x in raw_brands[:4]) if b]
        stops.append({
            "kind": kind,
            "text": text if kind == "place" else None,
            "what": what.lower() if kind == "errand" else None,
            "brands": brands if kind == "errand" else [],
            "side": stop.get("side") if kind == "errand" and stop.get("side") in STOP_SIDES else "any",
            "maxDetourMinutes": _clean_number(stop.get("maxDetourMinutes"), 0, 120),
            "position": position,
            "afterMinutes": _clean_number(stop.get("afterMinutes"), 1, 600) if kind == "errand" else None,
            "dwellMinutes": _clean_number(stop.get("dwellMinutes"), 0, 240),
            "closesAt": _clean_time(stop.get("closesAt")),
        })
    areas = []
    for area in (avoid.get("areas") or [])[:5]:
        if isinstance(area, dict) and _clean_text(area.get("text")):
            areas.append({
                "text": _clean_text(area.get("text")),
                "radiusMiles": _clean_number(area.get("radiusMiles"), 0.1, 20),
                "roads": area.get("roads") if area.get("roads") in ROAD_SCOPES else "all",
            })
    roads = []
    for road in (avoid.get("roads") or [])[:5]:
        if isinstance(road, dict) and _clean_text(road.get("name")):
            roads.append({"name": _clean_text(road.get("name")), "near": _clean_text(road.get("near"))})
    meetup = raw.get("meetup") if isinstance(raw.get("meetup"), dict) else {}
    team = raw.get("team") if isinstance(raw.get("team"), dict) else {}
    tasks = []
    for task in (team.get("tasks") or [])[:8]:
        if not isinstance(task, dict):
            continue
        kind = task.get("kind") if task.get("kind") in TASK_KINDS else None
        clean = {key: _clean_text(task.get(key)) for key in ("who", "from", "to", "text")}
        what = _clean_text(task.get("what"), 60)
        brands = [b for b in (_clean_text(x, 60) for x in (task.get("brands") or [])[:4]
                              if isinstance(task.get("brands"), list)) if b]
        if kind is None:
            kind = "escort" if clean["from"] and clean["to"] else ("place" if clean["text"] else "errand")
        if kind == "escort" and not (clean["from"] and clean["to"]):
            continue
        if kind == "place" and not clean["text"]:
            continue
        if kind == "errand" and not what:
            if not clean["text"]:
                continue
            what = clean["text"]
        tasks.append({
            "kind": kind,
            "who": clean["who"] if kind == "escort" else None,
            "from": clean["from"] if kind == "escort" else None,
            "to": clean["to"] if kind == "escort" else None,
            "text": clean["text"] if kind == "place" else None,
            "what": what.lower() if kind == "errand" else None,
            "brands": brands if kind == "errand" else [],
            "arriveBy": _clean_time(task.get("arriveBy")),
            "dwellMinutes": _clean_number(task.get("dwellMinutes"), 0, 240),
        })
    profile = str(raw.get("profile") or "auto").lower()
    return {
        "origin": _clean_text(raw.get("origin")),
        "destination": _clean_text(raw.get("destination")),
        "returnToOrigin": bool(raw.get("returnToOrigin")),
        "stops": stops,
        "stopOrder": raw.get("stopOrder") if raw.get("stopOrder") in {"as_listed", "any"} else "as_listed",
        "arriveBy": _clean_time(raw.get("arriveBy")),
        "avoid": {
            "highways": bool(avoid.get("highways")),
            "tolls": bool(avoid.get("tolls")),
            "ferries": bool(avoid.get("ferries")),
            "storms": bool(avoid.get("storms")),
            "areas": areas,
            "roads": roads,
        },
        "custom": _clean_text(raw.get("custom"), 400),
        "useRoads": [r for r in (_clean_text(x, 60) for x in (raw.get("useRoads") or [])[:3]
                                 if isinstance(raw.get("useRoads"), list)) if r],
        "meetup": {
            "friendFrom": _clean_text(meetup.get("friendFrom")),
            "friendLeftMinutesAgo": _clean_number(meetup.get("friendLeftMinutesAgo"), 0, 600),
            "what": (_clean_text(meetup.get("what"), 60) or "").lower() or None,
            "maxDetourMinutes": _clean_number(meetup.get("maxDetourMinutes"), 0, 180),
        },
        "team": {
            "partner": _clean_text(team.get("partner"), 40) if tasks else None,
            "partnerAt": _clean_text(team.get("partnerAt")) if tasks else None,
            "tasks": tasks,
        },
        "profile": profile if profile in PROFILES else "auto",
        "clarification": _clean_text(raw.get("clarification"), 400),
    }


def dwell_minutes(stop):
    """Time at a stop: only what the driver said ("15 minutes at Costco"), never a guess."""
    return stop.get("dwellMinutes") or 0


def model_messages(command, context=None, inline_examples=False):
    """Chat messages for the model.

    inline_examples puts the worked examples in the system prompt instead of
    as earlier user/assistant turns. Reasoning models such as Reflection's
    Beam reject earlier assistant turns that lack their own reasoning_content.
    """
    prefix = f"Context: {json.dumps(context, ensure_ascii=False)}\n" if context else ""
    if inline_examples:
        examples = "\n\n".join(
            f"Example request: {example}\nExample JSON: {json.dumps(answer)}" for example, answer in FEW_SHOT
        )
        return [
            {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{examples}"},
            {"role": "user", "content": f"{prefix}{command}"},
        ]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example, answer in FEW_SHOT:
        messages.append({"role": "user", "content": example})
        messages.append({"role": "assistant", "content": json.dumps(answer)})
    messages.append({"role": "user", "content": f"{prefix}{command}"})
    return messages
