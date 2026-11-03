import logging
import math
import os
import json
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from nlroute import agent as nl_agent
from nlroute import constraints as nl_constraints
from nlroute import custom as nl_custom
from nlroute import geocode as nl_geocode
from nlroute import planner as nl_planner
from nlroute.geo import circle_lonlat, haversine_m
from nlroute import valhalla as nl_valhalla
from nlroute.valhalla import ValhallaError


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def load_local_env(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.local")):
    """KEY=value lines from backend/.env.local (gitignored) for secrets like
    PLANNER_API_KEY. Real environment variables win."""
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


load_local_env()

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VALHALLA_ROUTE_URL = os.environ.get("VALHALLA_ROUTE_URL", "").strip()
VALHALLA_BASE_URL = os.environ.get("VALHALLA_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
VALHALLA_ROUTE_PATH = os.environ.get("VALHALLA_ROUTE_PATH", "/route")
VALHALLA_USER = os.environ.get("VALHALLA_USER", "").strip()
VALHALLA_PASS = os.environ.get("VALHALLA_PASS", "").strip()
VALHALLA_TIMEOUT_SECONDS = float(os.environ.get("VALHALLA_TIMEOUT_SECONDS", "20"))
VALHALLA_VERIFY_TLS = os.environ.get("VALHALLA_VERIFY_TLS", "0").strip().lower() in {"1", "true", "yes"}
NOMINATIM_SEARCH_URL = os.environ.get(
    "NOMINATIM_SEARCH_URL",
    "http://127.0.0.1:8088/search",
).strip()
GEOCODING_TIMEOUT_SECONDS = float(os.environ.get("GEOCODING_TIMEOUT_SECONDS", "12"))
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct").strip()
# "ollama" (local, default) or "openai" (any OpenAI-compatible hosted API).
PLANNER_PROVIDER = os.environ.get("PLANNER_PROVIDER", "ollama").strip().lower()
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "qwen3.6:35b-a3b").strip()
PLANNER_BASE_URL = os.environ.get("PLANNER_BASE_URL", "").strip().rstrip("/")
PLANNER_API_KEY = os.environ.get("PLANNER_API_KEY", "").strip()
# Hosted default follows the provider's recommended sampling (Beam: 0.7).
PLANNER_TEMPERATURE = float(os.environ.get("PLANNER_TEMPERATURE", "0.7"))
PLANNER_MODEL_TIMEOUT_SECONDS = float(os.environ.get("PLANNER_MODEL_TIMEOUT_SECONDS", "90"))
# Writing route code takes a reasoning model far longer than filling in the IR.
CODEGEN_TIMEOUT_SECONDS = float(os.environ.get("CODEGEN_TIMEOUT_SECONDS", "240"))
# One context size for every planner call: Ollama loads a separate copy of the model per num_ctx, and
# two copies of a 35B model (the extractor at 8k, the chat agent at 16k) crashed it on a 12 GB GPU.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "45"))
# No public services: weather needs a forecast server you run (e.g. a self-hosted
# Open-Meteo); without one the weather features report that they are off.
WEATHER_FORECAST_URL = os.environ.get("WEATHER_FORECAST_URL", "").strip()
WEATHER_TIMEOUT_SECONDS = float(os.environ.get("WEATHER_TIMEOUT_SECONDS", "12"))
MAX_WEATHER_POINTS = int(os.environ.get("MAX_WEATHER_POINTS", "6"))
MAX_WEATHER_GRID_CELLS = int(os.environ.get("MAX_WEATHER_GRID_CELLS", "900"))
MAX_WEATHER_GRID_SAMPLE_CELLS = int(os.environ.get("MAX_WEATHER_GRID_SAMPLE_CELLS", "100"))
WEATHER_GRID_BATCH_SIZE = int(os.environ.get("WEATHER_GRID_BATCH_SIZE", "64"))
WEATHER_GRID_WORKERS = int(os.environ.get("WEATHER_GRID_WORKERS", "1"))
VALHALLA_COSTING_PROFILES = {"auto", "truck", "bicycle", "pedestrian", "motorcycle", "bus"}

AGENT_SYSTEM_PROMPT = """You convert a driver's natural-language request into a route plan.
Return only one JSON object with exactly these fields:
{
  "originText": string or null,
  "destinationText": string or null,
  "includeTexts": array of strings,
  "avoidStorms": boolean,
  "profile": "auto" | "truck" | "bicycle" | "pedestrian" | "motorcycle" | "bus" | null,
  "clarification": string or null
}

Rules:
- Preserve complete place names and addresses.
- Put ordered stops, waypoints, pickups, and "via" places in includeTexts.
- Pick truck for semis, tankers, tractor-trailers, commercial trucks, hazmat, or truck-safe routing.
- Set avoidStorms for requests to avoid storms, severe weather, heavy rain, hail, flooding, or dangerous weather.
- If the request only changes weather/profile/stops for the current route, originText and destinationText may be null.
- Never invent a place. If a usable destination is absent, explain exactly what is missing in clarification.
- Do not include markdown or commentary outside the JSON object.
"""

WMO_WEATHER_CODES = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Severe thunderstorm with hail",
}


def get_valhalla_route_url():
    if VALHALLA_ROUTE_URL:
        return VALHALLA_ROUTE_URL
    path = VALHALLA_ROUTE_PATH if VALHALLA_ROUTE_PATH.startswith("/") else f"/{VALHALLA_ROUTE_PATH}"
    return f"{VALHALLA_BASE_URL}{path}"


def valhalla_auth():
    return (VALHALLA_USER, VALHALLA_PASS) if VALHALLA_USER or VALHALLA_PASS else None


def normalize_agent_plan(raw_plan):
    if not isinstance(raw_plan, dict):
        raise ValueError("The route model did not return a JSON object.")

    def clean_optional_text(value):
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned[:240] or None

    raw_stops = raw_plan.get("includeTexts") or []
    if not isinstance(raw_stops, list):
        raise ValueError("includeTexts must be a list.")

    profile = clean_optional_text(raw_plan.get("profile"))
    if profile:
        profile = profile.lower()
        if profile not in VALHALLA_COSTING_PROFILES:
            profile = None

    return {
        "originText": clean_optional_text(raw_plan.get("originText")),
        "destinationText": clean_optional_text(raw_plan.get("destinationText")),
        "includeTexts": [
            cleaned
            for cleaned in (clean_optional_text(value) for value in raw_stops[:10])
            if cleaned
        ],
        "avoidStorms": bool(raw_plan.get("avoidStorms")),
        "profile": profile,
        "clarification": clean_optional_text(raw_plan.get("clarification")),
    }


def parse_model_json(content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("The route model returned an empty response.")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError("The route model did not return valid JSON.")
        return json.loads(match.group(0))


def interpret_route_request(command, current_route=None):
    prompt = command.strip()
    if not prompt:
        raise ValueError("command is required.")

    current_context = current_route if isinstance(current_route, dict) else {}
    response = requests.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Current route context: {json.dumps(current_context, ensure_ascii=False)}\n"
                        f"Driver request: {prompt}"
                    ),
                },
            ],
            "options": {"temperature": 0},
        },
        timeout=OLLAMA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    content = (payload.get("message") or {}).get("content")
    return normalize_agent_plan(parse_model_json(content))


def haversine_miles(a, b):
    lat1 = math.radians(a["lat"])
    lat2 = math.radians(b["lat"])
    delta_lat = lat2 - lat1
    delta_lng = math.radians(b["lng"] - a["lng"])
    hav = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    )
    return 3958.8 * 2 * math.atan2(math.sqrt(hav), math.sqrt(1 - hav))


def direct_path_miles(points):
    total = 0
    for idx in range(len(points) - 1):
        total += haversine_miles(points[idx], points[idx + 1])
    return total


def decode_valhalla_shape(shape, precision=6):
    if not shape:
        return []

    coordinates = []
    index = 0
    lat = 0
    lng = 0
    factor = 10 ** precision

    while index < len(shape):
        shift = 0
        result = 0
        while True:
            value = ord(shape[index]) - 63
            index += 1
            result |= (value & 0x1F) << shift
            shift += 5
            if value < 0x20:
                break
        lat += ~(result >> 1) if result & 1 else result >> 1

        shift = 0
        result = 0
        while True:
            value = ord(shape[index]) - 63
            index += 1
            result |= (value & 0x1F) << shift
            shift += 5
            if value < 0x20:
                break
        lng += ~(result >> 1) if result & 1 else result >> 1

        coordinates.append({"lat": lat / factor, "lng": lng / factor})

    return coordinates


def normalize_route_point(raw_point, label):
    if not isinstance(raw_point, dict):
        raise ValueError(f"{label} must be an object with lat and lng.")

    lat = parse_float(raw_point.get("lat"))
    lng = parse_float(raw_point.get("lng"))
    if lat is None or lng is None:
        raise ValueError(f"{label} must include numeric lat and lng.")
    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        raise ValueError(f"{label} coordinates are out of range.")

    return {"lat": lat, "lng": lng}


def normalize_include_box(raw_box, index):
    if not isinstance(raw_box, dict):
        raise ValueError(f"include box {index + 1} must be an object.")

    point = normalize_route_point(raw_box, f"include box {index + 1}")
    size_miles = parse_float(raw_box.get("sizeMiles"))
    if size_miles is None:
        size_miles = 5
    size_miles = max(0.25, min(size_miles, 100))
    label = str(raw_box.get("label") or f"Include {index + 1}").strip()[:80]

    return {
        **point,
        "id": str(raw_box.get("id") or f"include-{index + 1}")[:80],
        "label": label,
        "sizeMiles": size_miles,
    }


def normalize_avoid_polygons(raw_polygons):
    if not raw_polygons:
        return []
    if not isinstance(raw_polygons, list):
        raise ValueError("avoidPolygons must be a list.")
    if len(raw_polygons) > 20:
        raise ValueError("avoidPolygons is limited to 20 polygons.")

    normalized = []
    for polygon_index, raw_polygon in enumerate(raw_polygons):
        if not isinstance(raw_polygon, list) or len(raw_polygon) < 3:
            raise ValueError(f"avoid polygon {polygon_index + 1} must contain at least 3 points.")
        if len(raw_polygon) > 12:
            raise ValueError(f"avoid polygon {polygon_index + 1} is limited to 12 points.")

        ring = []
        for point_index, raw_point in enumerate(raw_polygon):
            if not isinstance(raw_point, dict):
                raise ValueError(f"avoid polygon {polygon_index + 1} point {point_index + 1} must be an object.")
            lng_value = raw_point.get("lng", raw_point.get("lon"))
            ring.append(normalize_route_point({
                "lat": raw_point.get("lat"),
                "lng": lng_value,
            }, f"avoid polygon {polygon_index + 1} point {point_index + 1}"))

        if ring[0] != ring[-1]:
            ring.append(dict(ring[0]))
        normalized.append(ring)

    return normalized


def normalize_grid_bounds(raw_bounds):
    if not isinstance(raw_bounds, dict):
        raise ValueError("bounds must include west, south, east, and north.")

    west = parse_float(raw_bounds.get("west"))
    south = parse_float(raw_bounds.get("south"))
    east = parse_float(raw_bounds.get("east"))
    north = parse_float(raw_bounds.get("north"))
    if None in {west, south, east, north}:
        raise ValueError("bounds must include numeric west, south, east, and north.")
    if south < -90 or north > 90 or west < -180 or east > 180:
        raise ValueError("bounds coordinates are out of range.")
    if west >= east or south >= north:
        raise ValueError("bounds must have west < east and south < north.")

    return {"west": west, "south": south, "east": east, "north": north}


def route_summary_value(trip, key):
    summary = trip.get("summary") or {}
    value = parse_float(summary.get(key))
    if value is not None:
        return value
    return sum(parse_float((leg.get("summary") or {}).get(key)) or 0 for leg in trip.get("legs") or [])


def normalize_valhalla_route(payload):
    trip = payload.get("trip") or {}
    legs = trip.get("legs") or []
    geometry = []
    steps = []

    for leg_index, leg in enumerate(legs):
        leg_shape = decode_valhalla_shape(leg.get("shape"))
        if leg_shape:
            if geometry and leg_shape[0] == geometry[-1]:
                geometry.extend(leg_shape[1:])
            else:
                geometry.extend(leg_shape)

        for maneuver in leg.get("maneuvers") or []:
            instruction = (
                maneuver.get("instruction")
                or maneuver.get("verbal_pre_transition_instruction")
                or maneuver.get("verbal_post_transition_instruction")
                or ""
            )
            if not instruction:
                continue
            steps.append({
                "instruction": instruction,
                "streetNames": maneuver.get("street_names") or [],
                "distanceMiles": parse_float(maneuver.get("length")) or 0,
                "durationSeconds": parse_float(maneuver.get("time")) or 0,
                "beginShapeIndex": maneuver.get("begin_shape_index"),
                "endShapeIndex": maneuver.get("end_shape_index"),
                "legIndex": leg_index,
                "type": maneuver.get("type"),
            })

    if not geometry and trip.get("shape"):
        geometry = decode_valhalla_shape(trip.get("shape"))

    if not geometry:
        raise ValueError("Valhalla did not return route geometry.")

    return {
        "distanceMiles": route_summary_value(trip, "length"),
        "durationSeconds": route_summary_value(trip, "time"),
        "geometry": geometry,
        "steps": steps,
        "source": "Valhalla",
        "rawStatus": payload.get("status"),
        "rawStatusMessage": payload.get("status_message"),
    }


def weather_description(code):
    try:
        return WMO_WEATHER_CODES.get(int(code), "Unknown")
    except (TypeError, ValueError):
        return "Unknown"


def parse_float(value):
    try:
        parsed = float(value)
        return parsed if parsed == parsed else None
    except (TypeError, ValueError):
        return None


def normalize_weather_point(raw_point, index):
    if not isinstance(raw_point, dict):
        raise ValueError(f"Weather point {index + 1} must be an object.")

    lat = parse_float(raw_point.get("lat"))
    lng = parse_float(raw_point.get("lng"))
    if lat is None or lng is None:
        raise ValueError(f"Weather point {index + 1} must include numeric lat and lng.")
    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        raise ValueError(f"Weather point {index + 1} coordinates are out of range.")

    label = str(raw_point.get("label") or f"Point {index + 1}").strip()[:64]
    eta_minutes = parse_float(raw_point.get("etaMinutes"))

    return {
        "label": label,
        "lat": lat,
        "lng": lng,
        "etaMinutes": max(0, eta_minutes or 0),
    }


def closest_hourly_snapshot(hourly, eta_minutes):
    times = hourly.get("time") or []
    if not times:
        return None

    now = datetime.now()
    target = now + timedelta(minutes=eta_minutes or 0)
    best_index = 0
    best_delta = None

    for idx, raw_time in enumerate(times):
        try:
            parsed = datetime.fromisoformat(str(raw_time))
        except ValueError:
            continue
        delta = abs((parsed - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_index = idx

    def hourly_value(name):
        values = hourly.get(name) or []
        return values[best_index] if best_index < len(values) else None

    code = hourly_value("weather_code")
    return {
        "time": times[best_index],
        "temperatureF": hourly_value("temperature_2m"),
        "precipProbability": hourly_value("precipitation_probability"),
        "precipitationIn": hourly_value("precipitation"),
        "windMph": hourly_value("wind_speed_10m"),
        "windGustMph": hourly_value("wind_gusts_10m"),
        "visibilityMeters": hourly_value("visibility"),
        "weatherCode": code,
        "description": weather_description(code),
    }


def weather_alerts(current, eta):
    alerts = []
    checks = [("Now", current), ("ETA", eta)]

    for prefix, snapshot in checks:
        if not snapshot:
            continue
        code = snapshot.get("weatherCode")
        precip_probability = parse_float(snapshot.get("precipProbability"))
        precipitation = parse_float(snapshot.get("precipitationIn"))
        gust = parse_float(snapshot.get("windGustMph"))
        visibility = parse_float(snapshot.get("visibilityMeters"))

        if code in {95, 96, 99}:
            alerts.append(f"{prefix}: thunderstorm risk")
        elif code in {65, 67, 75, 82, 86}:
            alerts.append(f"{prefix}: heavy precipitation")
        elif code in {45, 48}:
            alerts.append(f"{prefix}: fog")

        if precip_probability is not None and precip_probability >= 50:
            alerts.append(f"{prefix}: {round(precip_probability)}% precipitation chance")
        elif precipitation is not None and precipitation >= 0.05:
            alerts.append(f"{prefix}: active precipitation")

        if gust is not None and gust >= 35:
            alerts.append(f"{prefix}: gusts {round(gust)} mph")

        if visibility is not None and visibility <= 1609:
            alerts.append(f"{prefix}: low visibility")

    return list(dict.fromkeys(alerts))


def weather_grid_severity(snapshot):
    if not snapshot:
        return {"severity": 0, "category": "unknown", "description": "Unknown"}

    code = snapshot.get("weatherCode")
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None

    precip_probability = parse_float(snapshot.get("precipProbability"))
    precipitation = parse_float(snapshot.get("precipitationIn"))
    gust = parse_float(snapshot.get("windGustMph"))
    visibility = parse_float(snapshot.get("visibilityMeters"))

    severity = 0
    category = "clear"

    if code in {95, 96, 99}:
        severity = 4
        category = "storm"
    elif code in {65, 67, 75, 82, 86}:
        severity = max(severity, 3)
        category = "heavy precipitation"
    elif code in {61, 63, 66, 71, 73, 80, 81, 85}:
        severity = max(severity, 2)
        category = "precipitation"
    elif code in {45, 48}:
        severity = max(severity, 2)
        category = "low visibility"
    elif code in {51, 53, 55, 56, 57}:
        severity = max(severity, 1)
        category = "drizzle"
    elif code in {2, 3}:
        severity = max(severity, 1)
        category = "cloud cover"

    if precip_probability is not None:
        if precip_probability >= 70:
            severity = max(severity, 3)
            category = "precipitation risk"
        elif precip_probability >= 45:
            severity = max(severity, 2)
            category = "precipitation risk"
        elif precip_probability >= 25:
            severity = max(severity, 1)

    if precipitation is not None:
        if precipitation >= 0.15:
            severity = max(severity, 3)
            category = "active precipitation"
        elif precipitation >= 0.03:
            severity = max(severity, 2)
            category = "active precipitation"

    if gust is not None:
        if gust >= 45:
            severity = max(severity, 3)
            category = "wind"
        elif gust >= 35:
            severity = max(severity, 2)
            category = "wind"

    if visibility is not None and visibility <= 1609:
        severity = max(severity, 3)
        category = "low visibility"

    return {
        "severity": severity,
        "category": category,
        "description": snapshot.get("description") or weather_description(code),
    }


def weather_forecast_params(points):
    params = {
        "latitude": ",".join(str(point["lat"]) for point in points),
        "longitude": ",".join(str(point["lng"]) for point in points),
        "current": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "precipitation",
            "weather_code",
            "cloud_cover",
            "wind_speed_10m",
            "wind_direction_10m",
            "wind_gusts_10m",
            "is_day",
        ]),
        "hourly": ",".join([
            "temperature_2m",
            "precipitation_probability",
            "precipitation",
            "weather_code",
            "wind_speed_10m",
            "wind_gusts_10m",
            "visibility",
        ]),
        "forecast_hours": "12",
        "timezone": "auto",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
    }
    return params


def weather_payload_to_point(point, payload):
    current_raw = payload.get("current") or {}
    current_code = current_raw.get("weather_code")
    current = {
        "time": current_raw.get("time"),
        "temperatureF": current_raw.get("temperature_2m"),
        "apparentTemperatureF": current_raw.get("apparent_temperature"),
        "humidity": current_raw.get("relative_humidity_2m"),
        "precipitationIn": current_raw.get("precipitation"),
        "cloudCover": current_raw.get("cloud_cover"),
        "windMph": current_raw.get("wind_speed_10m"),
        "windDirection": current_raw.get("wind_direction_10m"),
        "windGustMph": current_raw.get("wind_gusts_10m"),
        "isDay": current_raw.get("is_day"),
        "weatherCode": current_code,
        "description": weather_description(current_code),
    }
    eta = closest_hourly_snapshot(payload.get("hourly") or {}, point["etaMinutes"])

    return {
        **point,
        "timezone": payload.get("timezone"),
        "timezoneAbbreviation": payload.get("timezone_abbreviation"),
        "current": current,
        "eta": eta,
        "alerts": weather_alerts(current, eta),
    }


def chunked_points(points, size):
    size = max(1, size)
    for start in range(0, len(points), size):
        yield start, points[start:start + size]


def fetch_weather_for_points_batch(points):
    if not points:
        return []
    if not WEATHER_FORECAST_URL:
        raise ValueError("Weather is off: no local forecast server is configured (WEATHER_FORECAST_URL).")

    response = requests.get(
        WEATHER_FORECAST_URL,
        params=weather_forecast_params(points),
        timeout=WEATHER_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    payloads = payload if isinstance(payload, list) else [payload]
    if len(payloads) != len(points):
        raise ValueError("Weather service returned an unexpected location count.")

    return [
        weather_payload_to_point(point, payloads[index])
        for index, point in enumerate(points)
    ]


def fetch_weather_for_points(points):
    if not points:
        return []

    batch_size = max(1, WEATHER_GRID_BATCH_SIZE)
    if len(points) <= batch_size:
        return fetch_weather_for_points_batch(points)

    chunks = list(chunked_points(points, batch_size))
    results = [None] * len(points)
    worker_count = max(1, min(WEATHER_GRID_WORKERS, len(chunks)))

    def fetch_chunk(item):
        start, chunk = item
        return start, fetch_weather_for_points_batch(chunk)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for start, chunk_result in executor.map(fetch_chunk, chunks):
            results[start:start + len(chunk_result)] = chunk_result

    return results


def fetch_weather_for_point(point):
    return fetch_weather_for_points([point])[0]


def build_weather_grid_cell(row, col, bounds, lat_step, lng_step, weather_point):
    south = bounds["south"] + row * lat_step
    north = south + lat_step
    west = bounds["west"] + col * lng_step
    east = west + lng_step
    center = {
        "label": f"Grid {row + 1}-{col + 1}",
        "lat": south + lat_step / 2,
        "lng": west + lng_step / 2,
        "etaMinutes": 0,
    }
    snapshot = weather_point.get("eta") or weather_point.get("current")
    severity = weather_grid_severity(snapshot)

    return {
        "id": f"{row}-{col}",
        "row": row,
        "col": col,
        "center": {"lat": center["lat"], "lng": center["lng"]},
        "bounds": {"west": west, "south": south, "east": east, "north": north},
        "severity": severity["severity"],
        "category": severity["category"],
        "description": severity["description"],
        "temperatureF": snapshot.get("temperatureF") if snapshot else None,
        "precipProbability": snapshot.get("precipProbability") if snapshot else None,
        "windGustMph": snapshot.get("windGustMph") if snapshot else None,
        "alerts": weather_point.get("alerts", []),
    }


def build_weather_grid_point(row, col, bounds, lat_step, lng_step, label_prefix="Grid"):
    return {
        "label": f"{label_prefix} {row + 1}-{col + 1}",
        "lat": bounds["south"] + row * lat_step + lat_step / 2,
        "lng": bounds["west"] + col * lng_step + lng_step / 2,
        "etaMinutes": 0,
    }


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "turn-by-turn",
        "openSourceStack": {
            "routing": "Valhalla",
            "languageModel": OLLAMA_MODEL,
            "modelRuntime": "Ollama",
            "geocoding": "Nominatim (local) + Overture places",
            "mapTiles": "Planetiler + TileServer GL (local)",
            "mapRendering": "MapLibre GL JS",
            "weather": WEATHER_FORECAST_URL or "off",
        },
    })


@app.route("/api/autocomplete", methods=["GET"])
def autocomplete():
    """Search-as-you-type suggestions from the local Photon."""
    query = str(request.args.get("q") or "").strip()[:200]
    if len(query) < 2:
        return jsonify({"results": []})
    lat, lng = parse_float(request.args.get("lat")), parse_float(request.args.get("lng"))
    near = {"lat": lat, "lng": lng} if lat is not None and lng is not None else nl_planner.BAY_AREA_CENTER
    # Keep Photon's order, but a match 400 km away shouldn't sit above the
    # nearby one the driver is still typing ("john sinn" -> the school, not a road in Rancho Mirage).
    hits = nl_geocode.photon_search(query, near=near, limit=10)
    hits.sort(key=lambda h: haversine_m(near["lat"], near["lng"], h["lat"], h["lng"]) > 150000)
    return jsonify({"results": hits[:6]})


@app.route("/api/geocode", methods=["GET"])
def geocode():
    try:
        query = str(request.args.get("q") or "").strip()
        if not query:
            raise ValueError("q is required.")
        if len(query) > 240:
            raise ValueError("q is limited to 240 characters.")

        response = requests.get(
            NOMINATIM_SEARCH_URL,
            params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            headers={"Accept": "application/json"},
            timeout=GEOCODING_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json()
        if not isinstance(results, list) or not results:
            return jsonify({"error": f'No location found for "{query}".'}), 404

        result = results[0]
        lat = parse_float(result.get("lat"))
        lng = parse_float(result.get("lon"))
        if lat is None or lng is None:
            raise ValueError("Geocoder returned invalid coordinates.")
        return jsonify({
            "result": {
                "label": result.get("display_name") or query,
                "lat": lat,
                "lng": lng,
            },
            "source": "Nominatim / OpenStreetMap (local)",
        })
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        logger.error("Geocoding request failed: %s", error, exc_info=True)
        return jsonify({"error": "Local Nominatim is not responding", "details": str(error)}), 502


@app.route("/api/agent/interpret", methods=["POST"])
def interpret_agent_command():
    try:
        body = request.get_json(silent=True) or {}
        command = str(body.get("command") or "").strip()
        if len(command) > 1200:
            raise ValueError("command is limited to 1200 characters.")
        plan = interpret_route_request(command, body.get("currentRoute"))
        return jsonify({"plan": plan, "runtime": "Ollama", "model": OLLAMA_MODEL})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        logger.warning("Local route model is unavailable: %s", error)
        return jsonify({
            "error": (
                f'Local model "{OLLAMA_MODEL}" is unavailable. '
                f"Start Ollama at {OLLAMA_BASE_URL} or use a rehearsed demo scenario."
            ),
        }), 503
    except Exception as error:
        logger.error("Route model interpretation failed: %s", error, exc_info=True)
        return jsonify({"error": str(error)}), 500


@app.route("/api/routes/a-to-b", methods=["POST"])
def route_a_to_b():
    """
    Simple A-to-B Valhalla route solver for the prototype.

    The frontend sends origin/destination coordinates. This endpoint calls
    Valhalla directly and returns a normalized line, summary, and maneuvers so
    the UI does not need Ferrostar just to draw a route.
    """
    try:
        body = request.get_json(silent=True) or {}
        origin = normalize_route_point(body.get("origin"), "origin")
        destination = normalize_route_point(body.get("destination"), "destination")
        raw_include_boxes = body.get("includeBoxes") or body.get("includeStops") or []
        if not isinstance(raw_include_boxes, list):
            raise ValueError("includeBoxes must be a list.")
        if len(raw_include_boxes) > 10:
            raise ValueError("includeBoxes is limited to 10 stops.")
        include_boxes = [
            normalize_include_box(include_box, idx)
            for idx, include_box in enumerate(raw_include_boxes)
        ]
        avoid_polygons = normalize_avoid_polygons(body.get("avoidPolygons") or [])
        profile = str(body.get("profile") or "auto").strip().lower()
        if profile not in VALHALLA_COSTING_PROFILES:
            raise ValueError(f"profile must be one of {', '.join(sorted(VALHALLA_COSTING_PROFILES))}.")

        options = body.get("options") if isinstance(body.get("options"), dict) else {}
        locations = [
            {"lat": origin["lat"], "lon": origin["lng"], "type": "break"},
            *[
                {"lat": stop["lat"], "lon": stop["lng"], "type": "break", "name": stop["label"]}
                for stop in include_boxes
            ],
            {"lat": destination["lat"], "lon": destination["lng"], "type": "break"},
        ]
        valhalla_payload = {
            "locations": locations,
            "costing": profile,
            "directions_options": {"units": "miles"},
        }
        if avoid_polygons:
            valhalla_payload["exclude_polygons"] = [
                [[point["lng"], point["lat"]] for point in polygon]
                for polygon in avoid_polygons
            ]
        for key, value in options.items():
            if key not in {"locations", "exclude_polygons"}:
                valhalla_payload[key] = value

        target_url = get_valhalla_route_url()

        def call_valhalla(payload):
            logger.info("Solving A-to-B route through Valhalla: %s", target_url)
            return requests.post(
                target_url,
                json=payload,
                auth=valhalla_auth(),
                headers={
                    "Accept": "application/json",
                    "ngrok-skip-browser-warning": "1",
                },
                timeout=VALHALLA_TIMEOUT_SECONDS,
                verify=VALHALLA_VERIFY_TLS,
            )

        avoidance_warning = None
        response = call_valhalla(valhalla_payload)

        if response.status_code != 200 and avoid_polygons:
            logger.warning(
                "Valhalla rejected avoid polygons, retrying without excludes: %s %s",
                response.status_code,
                response.text,
            )
            fallback_payload = dict(valhalla_payload)
            fallback_payload.pop("exclude_polygons", None)
            response = call_valhalla(fallback_payload)
            avoidance_warning = (
                "Storm excludes were not accepted by Valhalla; "
                "returned the required-stop route without storm excludes."
            )

        if response.status_code != 200:
            logger.error("Valhalla route error: %s %s", response.status_code, response.text)
            return jsonify({
                "error": "Valhalla route failed",
                "details": response.text,
            }), response.status_code

        normalized_route = normalize_valhalla_route(response.json())

        if avoid_polygons:
            direct_miles = direct_path_miles([origin, *include_boxes, destination])
            max_reasonable_miles = max(100, direct_miles * 6)
            if normalized_route["distanceMiles"] > max_reasonable_miles:
                logger.warning(
                    "Avoid polygons produced an impractical route: %.1f mi for %.1f mi direct path",
                    normalized_route["distanceMiles"],
                    direct_miles,
                )
                fallback_payload = dict(valhalla_payload)
                fallback_payload.pop("exclude_polygons", None)
                fallback_response = call_valhalla(fallback_payload)
                if fallback_response.status_code != 200:
                    logger.error(
                        "Valhalla fallback route error: %s %s",
                        fallback_response.status_code,
                        fallback_response.text,
                    )
                    return jsonify({
                        "error": "Valhalla route failed",
                        "details": fallback_response.text,
                    }), fallback_response.status_code
                normalized_route = normalize_valhalla_route(fallback_response.json())
                avoidance_warning = (
                    "Storm avoidance produced an impractical detour; "
                    "returned the required-stop route without storm excludes."
                )

        if avoidance_warning:
            normalized_route["avoidanceWarning"] = avoidance_warning

        return jsonify({
            "route": normalized_route,
            "origin": origin,
            "destination": destination,
            "includeBoxes": include_boxes,
            "avoidPolygonCount": len(avoid_polygons),
            "profile": profile,
        })

    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        logger.error("A-to-B route request failed: %s", error, exc_info=True)
        return jsonify({"error": "Valhalla route request failed", "details": str(error)}), 502
    except Exception as error:
        logger.error("A-to-B route failed: %s", error, exc_info=True)
        return jsonify({"error": str(error)}), 500


@app.route("/api/weather/route", methods=["POST"])
def route_weather():
    """
    Return weather snapshots for turn-by-turn route sample points.

    The routing app sends origin/mid-route/destination coordinates and ETAs.
    This endpoint is deliberately local to the routing prototype and does not
    depend on the solver backend.
    """
    try:
        body = request.get_json(silent=True) or {}
        raw_points = body.get("points") or []
        if not isinstance(raw_points, list) or not raw_points:
            return jsonify({"error": "points must be a non-empty list"}), 400
        if len(raw_points) > MAX_WEATHER_POINTS:
            return jsonify({"error": f"points is limited to {MAX_WEATHER_POINTS} entries"}), 400

        points = [normalize_weather_point(point, idx) for idx, point in enumerate(raw_points)]
        weather_points = [fetch_weather_for_point(point) for point in points]
        all_alerts = list(dict.fromkeys(alert for point in weather_points for alert in point.get("alerts", [])))

        return jsonify({
            "source": "Open-Meteo",
            "points": weather_points,
            "alerts": all_alerts,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        })

    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        logger.error("Weather request failed: %s", error, exc_info=True)
        return jsonify({"error": "Weather service failed", "details": str(error)}), 502
    except Exception as error:
        logger.error("Route weather failed: %s", error, exc_info=True)
        return jsonify({"error": str(error)}), 500


@app.route("/api/weather/grid", methods=["POST"])
def weather_grid():
    """
    Return a bounded weather grid for map occlusion and storm avoidance.

    The frontend paints the returned cells directly over the map. Severe cells
    can also be sent back to Valhalla as avoid polygons on the next route solve.
    """
    try:
        body = request.get_json(silent=True) or {}
        bounds = normalize_grid_bounds(body.get("bounds"))
        rows = int(parse_float(body.get("rows")) or 24)
        cols = int(parse_float(body.get("cols")) or 24)
        rows = max(1, min(rows, 30))
        cols = max(1, min(cols, 30))
        if rows * cols > MAX_WEATHER_GRID_CELLS:
            raise ValueError(f"weather grid is limited to {MAX_WEATHER_GRID_CELLS} cells.")
        sample_rows = int(parse_float(body.get("sampleRows")) or min(rows, 8))
        sample_cols = int(parse_float(body.get("sampleCols")) or min(cols, 8))
        sample_rows = max(1, min(sample_rows, rows))
        sample_cols = max(1, min(sample_cols, cols))
        if sample_rows * sample_cols > MAX_WEATHER_GRID_SAMPLE_CELLS:
            raise ValueError(f"weather grid samples are limited to {MAX_WEATHER_GRID_SAMPLE_CELLS} cells.")

        lat_step = (bounds["north"] - bounds["south"]) / rows
        lng_step = (bounds["east"] - bounds["west"]) / cols
        sample_lat_step = (bounds["north"] - bounds["south"]) / sample_rows
        sample_lng_step = (bounds["east"] - bounds["west"]) / sample_cols
        coordinates = [(row, col) for row in range(rows) for col in range(cols)]
        sample_coordinates = [
            (row, col)
            for row in range(sample_rows)
            for col in range(sample_cols)
        ]
        sample_points = [
            build_weather_grid_point(row, col, bounds, sample_lat_step, sample_lng_step, "Sample")
            for row, col in sample_coordinates
        ]
        weather_points = fetch_weather_for_points(sample_points)
        weather_point_by_sample = {
            f"{row}-{col}": weather_points[index]
            for index, (row, col) in enumerate(sample_coordinates)
        }

        def nearest_sample(row, col):
            sample_row = min(sample_rows - 1, int((row + 0.5) * sample_rows / rows))
            sample_col = min(sample_cols - 1, int((col + 0.5) * sample_cols / cols))
            return weather_point_by_sample[f"{sample_row}-{sample_col}"]

        cells = [
            build_weather_grid_cell(row, col, bounds, lat_step, lng_step, nearest_sample(row, col))
            for row, col in coordinates
        ]

        all_alerts = list(dict.fromkeys(alert for cell in cells for alert in cell.get("alerts", [])))
        return jsonify({
            "source": "Open-Meteo",
            "bounds": bounds,
            "rows": rows,
            "cols": cols,
            "sampleRows": sample_rows,
            "sampleCols": sample_cols,
            "sampleCellCount": len(sample_points),
            "interpolated": sample_rows != rows or sample_cols != cols,
            "cells": cells,
            "alerts": all_alerts,
            "stormCellCount": sum(1 for cell in cells if cell["severity"] >= 3),
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        })

    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        logger.error("Weather grid request failed: %s", error, exc_info=True)
        return jsonify({"error": "Weather service failed", "details": str(error)}), 502
    except Exception as error:
        logger.error("Weather grid failed: %s", error, exc_info=True)
        return jsonify({"error": str(error)}), 500


def chat_json(messages, schema, schema_name, timeout=None):
    """One model call held to a JSON schema (Ollama structured outputs, or
    OpenAI-style json_schema). Returns the parsed object."""
    if PLANNER_PROVIDER == "openai":
        if not PLANNER_BASE_URL or not PLANNER_API_KEY:
            raise ValueError("Set PLANNER_BASE_URL and PLANNER_API_KEY in backend/.env.local.")
        response = requests.post(
            f"{PLANNER_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {PLANNER_API_KEY}"},
            json={
                "model": PLANNER_MODEL,
                "messages": messages,
                "temperature": PLANNER_TEMPERATURE,
                "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema}},
            },
            timeout=timeout or PLANNER_MODEL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    else:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": PLANNER_MODEL,
                "stream": False,
                "think": False,
                "format": schema,
                "messages": messages,
                "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
                "keep_alive": "30m",
            },
            timeout=timeout or PLANNER_MODEL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = (response.json().get("message") or {}).get("content")
    return parse_model_json(content)


def interpret_constraints(command, context=None):
    """Natural language -> constraint IR."""
    started = datetime.now(timezone.utc)
    messages = nl_constraints.model_messages(command, context, inline_examples=PLANNER_PROVIDER == "openai")
    ir = nl_constraints.normalize_ir(chat_json(messages, nl_constraints.IR_JSON_SCHEMA, "route_constraints"))
    seconds = (datetime.now(timezone.utc) - started).total_seconds()
    return ir, round(seconds, 2)


def write_custom_code(request_text, ir, feedback=None):
    """Natural language -> sandboxed Python for what the IR can't express."""
    messages = nl_custom.codegen_messages(request_text, ir, feedback)
    try:
        return chat_json(messages, nl_custom.CODE_JSON_SCHEMA, "custom_route_code",
                         timeout=CODEGEN_TIMEOUT_SECONDS)
    except (requests.RequestException, ValueError) as error:
        raise nl_custom.CustomCodeError(f"the model couldn't write code ({type(error).__name__})") from error


def storm_rings_for_route(latlons, rows=6, cols=6):
    """Severe cells from a live Open-Meteo grid over the route's bounding box."""
    lats = [p[0] for p in latlons]
    lngs = [p[1] for p in latlons]
    pad = 0.05
    bounds = {"south": min(lats) - pad, "north": max(lats) + pad,
              "west": min(lngs) - pad, "east": max(lngs) + pad}
    lat_step = (bounds["north"] - bounds["south"]) / rows
    lng_step = (bounds["east"] - bounds["west"]) / cols
    coords = [(r, c) for r in range(rows) for c in range(cols)]
    points = [build_weather_grid_point(r, c, bounds, lat_step, lng_step) for r, c in coords]
    weather = fetch_weather_for_points(points)
    rings = []
    for (r, c), point in zip(coords, weather):
        cell = build_weather_grid_cell(r, c, bounds, lat_step, lng_step, point)
        if cell["severity"] >= 3:
            b = cell["bounds"]
            rings.append([[b["west"], b["south"]], [b["east"], b["south"]], [b["east"], b["north"]],
                          [b["west"], b["north"]], [b["west"], b["south"]]])
    return rings


def demo_storm_rings(raw):
    """Simulated storm cells for rehearsals: [{lat, lng, radiusMiles}]."""
    rings = []
    for cell in (raw or [])[:5]:
        lat, lng = parse_float(cell.get("lat")), parse_float(cell.get("lng"))
        radius = parse_float(cell.get("radiusMiles")) or 2.0
        if lat is not None and lng is not None:
            rings.append(circle_lonlat(lat, lng, min(radius, 15) * 1609.34, segments=24))
    return rings


def run_planner(ir, body):
    current = body.get("currentLocation")
    current = normalize_route_point(current, "currentLocation") if current else None
    storm_rings = demo_storm_rings(body.get("demoStorms"))
    storm_source = "simulated" if storm_rings else None
    if ir["avoid"]["storms"] and not storm_rings:
        # Live weather needs the trip's extent; route the plain trip once to get it.
        probe = nl_planner.Planner(ir, current).probe_extent()
        if probe:
            try:
                storm_rings = storm_rings_for_route(probe)
                storm_source = "Open-Meteo (live)"
            except (requests.RequestException, ValueError) as error:
                logger.warning("Live weather unavailable: %s", error)
                storm_source = "unavailable"
    now = None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(body.get("now") or ""))
    if match:
        # Rehearsals pin the clock so "home by 6:15" behaves the same at any hour.
        now = datetime.now(nl_planner.LOCAL_TZ).replace(
            hour=int(match.group(1)) % 24, minute=int(match.group(2)), second=0, microsecond=0)
    request_text = str(body.get("command") or ir.get("custom") or "")
    result = nl_planner.plan(ir, current_location=current, now=now, storm_rings=storm_rings,
                             shared_locations=shared_locations(),
                             code_writer=lambda feedback: write_custom_code(request_text, ir, feedback),
                             picked=body.get("picked") if isinstance(body.get("picked"), dict) else None)
    result["clock"] = (now or datetime.now(nl_planner.LOCAL_TZ)).strftime("%H:%M")
    result["stormSource"] = storm_source
    # Which speeds the route used: live (Google colors, CHP) or typical.
    result["traffic"] = nl_valhalla.live_traffic()
    return result


# ------------------------------------------------------------- chat agent
# A conversation with the model, which calls the planner and friends as tools
# (nlroute/agent.py). The app keeps the transcript and sends it each turn.
def _json_object(arguments):
    """Tool-call arguments as a dict, whether given as a dict or a JSON string."""
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def chat_with_tools(messages, tools):
    """One model turn that may call tools. Returns an OpenAI-style assistant message."""
    if PLANNER_PROVIDER == "openai":
        if not PLANNER_BASE_URL or not PLANNER_API_KEY:
            raise ValueError("Set PLANNER_BASE_URL and PLANNER_API_KEY in backend/.env.local.")
        response = requests.post(
            f"{PLANNER_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {PLANNER_API_KEY}"},
            json={"model": PLANNER_MODEL, "messages": messages, "tools": tools,
                  "temperature": PLANNER_TEMPERATURE},
            timeout=PLANNER_MODEL_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            logger.error("Chat model rejected the request: %s", response.text[:500])
        response.raise_for_status()
        return response.json()["choices"][0]["message"]
    # The transcript keeps OpenAI's shape (tool-call arguments as a JSON string); Ollama wants them as
    # objects and answers 400 otherwise, so every turn after a tool call failed with a local model.
    ollama_messages = [
        {**m, "tool_calls": [{**c, "function": {**c["function"], "arguments": _json_object(c["function"].get("arguments"))}}
                             for c in m["tool_calls"]]} if m.get("tool_calls") else m
        for m in messages]
    response = requests.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json={"model": PLANNER_MODEL, "stream": False, "messages": ollama_messages, "tools": tools,
              "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX}, "keep_alive": "30m"},
        timeout=PLANNER_MODEL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    message = response.json().get("message") or {}
    # Ollama gives arguments as objects and no call ids; match the OpenAI shape.
    calls = [{"id": f"call-{i}", "type": "function",
              "function": {"name": c["function"]["name"], "arguments": json.dumps(c["function"].get("arguments") or {})}}
             for i, c in enumerate(message.get("tool_calls") or [])]
    return {"role": "assistant", "content": message.get("content") or "", **({"tool_calls": calls} if calls else {})}


HERE_WORDS = {"current location", "my location", "here", "me", "where i am", "my position"}


def demo_now(value):
    """Today at "HH:MM" in local time (a rehearsed demo's clock), else the real time."""
    now = datetime.now(nl_planner.LOCAL_TZ)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    return now.replace(hour=int(match.group(1)) % 24, minute=int(match.group(2)), second=0, microsecond=0) \
        if match else now


def agent_tools(current, picked, holder, base_now=None):
    """The agent's tools for one turn; plan_trip leaves its full result in holder["plan"].
    base_now: the clock "now" means (a demo's set time), else the real time."""
    def resolve(text):
        if str(text).strip().lower() in HERE_WORDS:
            if not current:
                raise ValueError("The driver's location is unknown; ask where they are starting from.")
            return {**current, "label": "current location"}
        coords = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", str(text))
        if coords:  # models often pass the driver's position back as "lat, lng"
            return {"lat": float(coords.group(1)), "lng": float(coords.group(2)), "label": str(text).strip()}
        planner = nl_planner.Planner(nl_constraints.normalize_ir({}), current, picked=picked)
        return planner.resolve(str(text), "place")

    def clock(depart_at):
        now = base_now or datetime.now(nl_planner.LOCAL_TZ)
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(depart_at or "").strip())
        if not match:
            return now
        return now.replace(hour=int(match.group(1)) % 24, minute=int(match.group(2)), second=0, microsecond=0)

    def plan_trip(request, depart_at=None):
        ir, model_seconds = interpret_constraints(request)
        body = {"currentLocation": current, "picked": picked, "command": request,
                "now": clock(depart_at).strftime("%H:%M") if depart_at or base_now else None}
        try:
            result = run_planner(ir, body)
        except nl_planner.PlanningError as error:
            return {"question": str(error)}
        holder["plan"] = {**result, "ir": ir, "model": PLANNER_MODEL, "modelSeconds": model_seconds,
                          "query": request}
        route = result["route"]
        depart = clock(result["clock"])
        total = route["durationSeconds"] + (result.get("dwellSeconds") or 0)
        return {
            "from": result["origin"].get("label"), "to": result["destination"].get("label"),
            "stops": [s.get("label") for s in result.get("stops") or []],
            "depart": depart.strftime("%H:%M"),
            "arrive": (depart + timedelta(seconds=total)).strftime("%H:%M"),
            "drive_minutes": round(route["durationSeconds"] / 60),
            "time_at_stops_minutes": round((result.get("dwellSeconds") or 0) / 60),
            "miles": round(route.get("distanceMiles") or 0, 1),
            "checks": [f"{'passed' if c['passed'] else 'FAILED'}: {c['name']} ({c.get('detail')})"
                       for c in result.get("checks") or []],
            "shown_on_map": True,
            "traffic": result["traffic"]["label"],
        }

    def drive_time(origin, destination, depart_at=None):
        try:
            a, b = resolve(origin), resolve(destination)
        except nl_planner.PlanningError as error:
            return {"question": str(error)}
        trip = nl_valhalla.route([a, b])[0]
        depart = clock(depart_at)
        return {"from": a.get("label"), "to": b.get("label"), "depart": depart.strftime("%H:%M"),
                "drive_minutes": round(trip["durationSeconds"] / 60),
                "arrive": (depart + timedelta(seconds=trip["durationSeconds"])).strftime("%H:%M"),
                "miles": round(trip.get("distanceMiles") or 0, 1), "traffic": nl_valhalla.live_traffic()["label"]}

    def search_places(query, near=None):
        try:
            center = resolve(near) if near else (current or nl_planner.BAY_AREA_CENTER)
        except nl_planner.PlanningError as error:
            return {"question": str(error)}
        hits = nl_geocode.photon_search(str(query), near=center, limit=6)
        return {"results": [{"name": h["label"], "where": h["detail"],
                             "km_away": round(haversine_m(center["lat"], center["lng"], h["lat"], h["lng"]) / 1000, 1)}
                            for h in hits]}

    return {"plan_trip": plan_trip, "drive_time": drive_time, "search_places": search_places}


@app.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    """One chat turn: {transcript: [...messages, new user message], currentLocation, picked, now?}.
    now ("HH:MM", optional): the clock for a rehearsed demo; real time otherwise."""
    body = request.get_json(silent=True) or {}
    transcript = [m for m in (body.get("transcript") or [])
                  if isinstance(m, dict) and m.get("role") in {"user", "assistant", "tool"}][-40:]
    while transcript and transcript[0]["role"] == "tool":
        transcript.pop(0)  # a tool result cut off from its call
    if not transcript or transcript[-1]["role"] != "user":
        return jsonify({"error": "transcript must end with the driver's message"}), 400
    current = body.get("currentLocation")
    try:
        current = normalize_route_point(current, "currentLocation") if current else None
        picked = body.get("picked") if isinstance(body.get("picked"), dict) else None
        holder = {}
        label = (f"{current['lat']:.4f}, {current['lng']:.4f} (phone GPS)" if current else "unknown")
        now = demo_now(body.get("now"))
        added, steps = nl_agent.run_turn(transcript, chat_with_tools,
                                         agent_tools(current, picked, holder, now if body.get("now") else None),
                                         now=now, location_label=label)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        logger.error("Chat model call failed: %s", error)
        return jsonify({"error": "The language model is not responding."}), 502
    return jsonify({"messages": added, "reply": added[-1]["content"], "steps": steps, "plan": holder.get("plan")})


@app.route("/api/agent/plan", methods=["POST"])
def agent_plan():
    """Natural language -> constraints (local model) -> verified route."""
    body = request.get_json(silent=True) or {}
    command = str(body.get("command") or "").strip()
    try:
        if not command:
            raise ValueError("command is required.")
        if len(command) > 1200:
            raise ValueError("command is limited to 1200 characters.")
        ir, model_seconds = interpret_constraints(command, body.get("context"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        logger.warning("Planner model unavailable: %s", error)
        detail = getattr(getattr(error, "response", None), "text", "") or str(error)
        return jsonify({"error": f'Planner model "{PLANNER_MODEL}" request failed: {detail[:400]}'}), 503
    try:
        result = run_planner(ir, body)
    except nl_planner.PlanningError as error:
        return jsonify({"ir": ir, "model": PLANNER_MODEL, "modelSeconds": model_seconds,
                        "clarification": str(error)}), 200
    except ValhallaError as error:
        return jsonify({"ir": ir, "error": f"Valhalla: {error}"}), 502
    return jsonify({**result, "ir": ir, "model": PLANNER_MODEL, "modelSeconds": model_seconds})


@app.route("/api/agent/plan-ir", methods=["POST"])
def agent_plan_ir():
    """Rehearsed path: a checked-in constraint IR, no model in the loop."""
    body = request.get_json(silent=True) or {}
    try:
        ir = nl_constraints.normalize_ir(body.get("ir"))
        result = run_planner(ir, body)
    except nl_planner.PlanningError as error:
        return jsonify({"ir": body.get("ir"), "clarification": str(error)}), 200
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except ValhallaError as error:
        return jsonify({"error": f"Valhalla: {error}"}), 502
    return jsonify({**result, "ir": ir, "model": None, "modelSeconds": 0})


# Phone GPS relay: a phone (the beacon page or the Android app) POSTs fixes,
# the laptop's drive view listens on a Server-Sent Events stream. In memory.
# ?who= keeps each person's phone apart ("me" by default, "partner" for the
# second driver in two-person plans).
_gps_lock = threading.Condition()
_gps_latest = {"fixes": {}, "seq": 0}


# Share links (like Glympse): "Ask for location" mints an unguessable token
# that stands in for a person for a limited time. The partner's phone needs
# only the link, no account or app; when it expires their fixes are dropped.
_shares = {}
SHARE_MINUTES_DEFAULT = 30


def gps_who():
    """Whose phone or trip a request is about: a live share token, else ?who=."""
    token = request.args.get("t")
    if token:
        share = _shares.get(token)
        if not share or share["expiresAt"] < datetime.now(timezone.utc).timestamp():
            raise PermissionError("This share link has expired. Ask for a new one.")
        return share["who"]
    who = re.sub(r"[^a-z0-9_-]", "", str(request.args.get("who") or "me").lower())[:24]
    return who or "me"


def expire_shares():
    now = datetime.now(timezone.utc).timestamp()
    for token, share in list(_shares.items()):
        if share["expiresAt"] < now:
            del _shares[token]
            if not any(s["who"] == share["who"] for s in _shares.values()):
                _gps_latest["fixes"].pop(share["who"], None)


@app.errorhandler(PermissionError)
def share_expired(error):
    return jsonify({"error": str(error)}), 410


@app.route("/api/share", methods=["POST"])
def create_share():
    """Mint a share link for a person ("partner"): text it to them; they open
    it to share their location, and the same link later shows the trip sent
    to them."""
    body = request.get_json(silent=True) or {}
    who = re.sub(r"[^a-z0-9_-]", "", str(body.get("who") or "partner").lower())[:24] or "partner"
    minutes = max(5, min(240, parse_float(body.get("minutes")) or SHARE_MINUTES_DEFAULT))
    expire_shares()
    token = secrets.token_urlsafe(12)
    expires = datetime.now(timezone.utc).timestamp() + minutes * 60
    _shares[token] = {"who": who, "name": str(body.get("name") or who)[:40], "expiresAt": expires}
    return jsonify({"token": token, "who": who, "minutes": minutes,
                    "path": f"/share.html?t={token}", "expiresAt": expires})


@app.route("/api/share/info", methods=["GET"])
def share_info():
    who = gps_who()
    share = _shares[request.args["t"]] if request.args.get("t") else {"name": who, "expiresAt": None}
    return jsonify({"who": who, "name": share["name"], "expiresAt": share["expiresAt"]})


def shared_locations():
    """Each person's latest fix with its age, for the planner."""
    expire_shares()
    now = datetime.now(timezone.utc).timestamp()
    return {who: dict(fix, ageSeconds=now - fix["receivedAt"]) for who, fix in _gps_latest["fixes"].items()}


@app.route("/api/gps", methods=["POST"])
def gps_post():
    body = request.get_json(silent=True) or {}
    lat, lng = parse_float(body.get("lat")), parse_float(body.get("lng"))
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng are required"}), 400
    fix = {
        "lat": lat, "lng": lng,
        "accuracy": parse_float(body.get("accuracy")) or 20.0,
        "heading": parse_float(body.get("heading")),
        "speed": parse_float(body.get("speed")),
        "timestamp": parse_float(body.get("timestamp")) or datetime.now(timezone.utc).timestamp() * 1000,
        "receivedAt": datetime.now(timezone.utc).timestamp(),
    }
    who = gps_who()
    with _gps_lock:
        _gps_latest["fixes"][who] = fix
        _gps_latest["seq"] += 1
        _gps_lock.notify_all()
    return jsonify({"ok": True, "seq": _gps_latest["seq"], "who": who})


@app.route("/api/gps/latest", methods=["GET"])
def gps_latest():
    fix = _gps_latest["fixes"].get(gps_who())
    age = None if fix is None else datetime.now(timezone.utc).timestamp() - fix["receivedAt"]
    return jsonify({"fix": fix, "ageSeconds": age})


@app.route("/api/gps/stream", methods=["GET"])
def gps_stream():
    who = gps_who()

    def events():
        seen = None
        while True:
            with _gps_lock:
                _gps_lock.wait_for(lambda: _gps_latest["fixes"].get(who) is not seen, timeout=15)
                fix = _gps_latest["fixes"].get(who)
            if fix is seen or fix is None:
                yield ": keepalive\n\n"
                continue
            seen = fix
            yield f"data: {json.dumps(fix)}\n\n"
    return app.response_class(events(), mimetype="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# The trip handed off for driving: "Car" in the planner stores it here, and the
# Android Auto app (or the partner's phone, ?who=partner) polls for it.
# In memory; one active trip per person.
_active_trips = {"trips": {}, "id": 0}


@app.route("/api/trip/active", methods=["GET", "POST", "DELETE"])
def active_trip():
    who = gps_who()
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        nav = body.get("navRequest") or {}
        if len(nav.get("points") or []) < 2:
            return jsonify({"error": "navRequest with at least two points is required"}), 400
        _active_trips["id"] += 1
        _active_trips["trips"][who] = {
            "id": _active_trips["id"],
            "navRequest": nav,
            "destination": str(body.get("destination") or "Destination")[:120],
            "stops": [str(s)[:120] for s in (body.get("stops") or [])][:10],
            "summary": str(body.get("summary") or "")[:200],
            "from": str(body.get("from") or "")[:60] or None,
            "sentAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    elif request.method == "DELETE":
        _active_trips["trips"].pop(who, None)
    return jsonify({"trip": _active_trips["trips"].get(who)})


ANDROID_APK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "android", "app", "build", "outputs", "apk", "debug", "app-debug.apk")


@app.route("/api/download/turn-by-turn.apk", methods=["GET"])
def download_apk():
    """The latest debug build, for sideloading onto the phone over Tailscale."""
    if not os.path.exists(ANDROID_APK):
        return jsonify({"error": "No Android build yet: run ./gradlew assembleDebug in android/."}), 404
    return send_file(ANDROID_APK, mimetype="application/vnd.android.package-archive",
                     as_attachment=True, download_name="turn-by-turn.apk")


@app.route("/api/nav/route", methods=["POST"])
def nav_route():
    """Turn-by-turn route (OSRM format) for a plan's navRequest.

    With `from`, the route restarts there (a reroute) through the remaining
    points, keeping every exclusion the planner found.
    """
    body = request.get_json(silent=True) or {}
    nav = body.get("navRequest") or {}
    try:
        points = [normalize_route_point(p, "point") | ({"preferredSide": p["preferredSide"]}
                                                      if p.get("preferredSide") else {})
                  for p in nav.get("points") or []]
        remaining = body.get("remainingPoints")
        if body.get("from"):
            start = normalize_route_point(body["from"], "from")
            rest = points[1:] if remaining is None else points[len(points) - int(remaining):]
            points = [start, *rest]
        if len(points) < 2:
            raise ValueError("navRequest needs at least two points.")
        text = nl_valhalla.route_osrm_text(
            points, nav.get("costing") or "auto", nav.get("costingOptions"),
            [normalize_route_point(p, "exclude") for p in nav.get("excludeLocations") or []],
            nav.get("excludePolygons") or None)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (ValhallaError, requests.RequestException) as error:
        return jsonify({"error": f"Valhalla: {error}"}), 502
    return app.response_class(text, mimetype="application/json")


@app.route("/api/valhalla/route", methods=["POST"])
def proxy_valhalla_route():
    """
    Raw Valhalla route proxy for compatibility and experiments.

    The A-to-B UI uses /api/routes/a-to-b, which normalizes the Valhalla route.
    This route preserves the upstream response body instead of re-serializing.
    """
    try:
        target_url = get_valhalla_route_url()
        raw_body = request.get_data() or b"{}"
        logger.info("Proxying Valhalla route request to: %s", target_url)

        response = requests.post(
            target_url,
            data=raw_body,
            auth=valhalla_auth(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "ngrok-skip-browser-warning": "1",
            },
            timeout=VALHALLA_TIMEOUT_SECONDS,
            verify=VALHALLA_VERIFY_TLS,
        )

        if response.status_code != 200:
            logger.error("Valhalla Proxy Error: %s %s", response.status_code, response.text)

        return app.response_class(
            response=response.content,
            status=response.status_code,
            content_type=response.headers.get("content-type", "application/json"),
        )

    except Exception as error:
        logger.error("Valhalla proxy failed: %s", error, exc_info=True)
        return jsonify({"error": str(error)}), 500


if __name__ == "__main__":
    host = os.environ.get("ROUTING_BACKEND_HOST") or os.environ.get("BACKEND_HOST", "127.0.0.1")
    port = int(os.environ.get("ROUTING_BACKEND_PORT") or os.environ.get("PORT", "5055"))
    debug = os.environ.get("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)
