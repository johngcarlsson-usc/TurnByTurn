# Turn by Turn

Turn by Turn is a standalone web and Capacitor navigation app with its own frontend, backend, dependencies, native projects, branding, and release lifecycle.

Its default stack is open-source and API-key-free:

- Qwen2.5 7B Instruct through local Ollama for natural-language route planning
- Valhalla for routing
- Nominatim (local, with Wikipedia importance and TIGER addresses), Photon for search-as-you-type, and Overture places (DuckDB) for geocoding
- MapLibre GL JS with Planetiler vector tiles served by TileServer GL
- Weather only from a forecast server you run (off by default)

Everything runs on this machine; nothing calls a public web service at
runtime. Downloads happen once, at setup:

- `scripts/local_services.sh nominatim <osm.pbf>`: Nominatim on `127.0.0.1:8088`
- `scripts/local_services.sh photon`: Photon on `127.0.0.1:2322`, indexed from Nominatim
- `scripts/local_services.sh app`: the production build for phones, at `/` over Tailscale
- `scripts/local_services.sh map <osm.pbf>`: tiles, style, fonts and sprites; TileServer GL on `127.0.0.1:8089`, published at `/tiles`

The app supports origin-to-destination routing, ordered required stops, travel-profile inference, a weather grid, and storm-cell avoidance. The rehearsed demo picker uses checked-in structured plans so recorded demos do not depend on an LLM returning the same JSON every time.

## Install

The app owns its JavaScript and Python dependencies:

```bash
cd TurnByTurn
npm install
python3 -m venv backend/venv
backend/venv/bin/pip install -r backend/requirements.txt
```

Install Ollama, then download the default local model:

```bash
ollama pull qwen2.5:7b-instruct
```

Run a local Valhalla service with the map tiles for the region you want to demo. The app expects its route endpoint at `http://127.0.0.1:8002/route` by default.

## Run

From this repository root:

```bash
npm start
```

This launches the Flask API at `http://127.0.0.1:5055` and the Vite UI at `http://127.0.0.1:5174`, selecting the next available ports when needed.

For a recording, choose a scenario under **Rehearsed demo** and click **Run demo**. The request shown in the chat is natural language, but its interpretation is deterministic. Typed requests go through local Qwen; if Ollama is unavailable, simple requests fall back to the offline parser.

## Configuration

Backend variables:

- `OLLAMA_BASE_URL`: local Ollama base URL, default `http://127.0.0.1:11434`
- `OLLAMA_MODEL`: open model name, default `qwen2.5:7b-instruct`
- `OLLAMA_TIMEOUT_SECONDS`: model timeout, default `45`
- `VALHALLA_ROUTE_URL`: exact Valhalla `/route` endpoint
- `VALHALLA_BASE_URL`: Valhalla base URL, default `http://127.0.0.1:8002`
- `VALHALLA_ROUTE_PATH`: route path, default `/route`
- `VALHALLA_USER` / `VALHALLA_PASS`: optional Basic auth; empty by default
- `VALHALLA_VERIFY_TLS`: verify upstream TLS when set to `1`
- `NOMINATIM_SEARCH_URL`: local Nominatim search endpoint, default `http://127.0.0.1:8088/search`
- `WEATHER_FORECAST_URL`: Open-Meteo-compatible forecast endpoint you host; empty = weather off
- `WEATHER_TIMEOUT_SECONDS`: weather timeout, default `12`
- `MAX_WEATHER_GRID_CELLS`: rendered weather cells, default `900`
- `MAX_WEATHER_GRID_SAMPLE_CELLS`: actual weather samples, default `100`

Frontend variables:

- `VITE_ROUTING_API_URL`: backend URL, needed for native/physical-device builds
- `VITE_TURN_BY_TURN_STYLE_URL`: MapLibre style; defaults to OpenFreeMap Liberty

No Mapbox or proprietary LLM key is used.

## Native iOS and Android

The native projects, Capacitor configuration, and bundle identity are all inside this app. From the repository root:

```bash
npm run mobile:ios
npm run mobile:android
```

The app ID is `io.turnbyturn.navigation`. Native builds still need the Python backend reachable from the device. iOS Simulator defaults to `http://127.0.0.1:5055`; Android Emulator defaults to `http://10.0.2.2:5055`. Set `VITE_ROUTING_API_URL` for a physical device or deployed backend.

## Natural-language planner

The default screen (`/`) is the constraint planner; the original screen is at `/#legacy`.

A request goes through one pipeline: the model fills a typed constraint schema
(`backend/nlroute/constraints.py`), and deterministic code turns it into a route:

- `places.py`: Overture places queried in place with DuckDB (`PLACES_PARQUET`, default `~/geo/bayarea/places.parquet`)
- `planner.py`: resolution, avoidance geometry, the route → map-match → exclude loop, errand search
- `solver.py`: exact layered shortest path for ordered stops; OR-Tools GTSP with time windows for "any order"
- `verify.py`: independent checks of the final line from a fresh `trace_attributes` map-match

Endpoints: `POST /api/agent/plan` (`{command}`, uses the model) and `POST /api/agent/plan-ir`
(`{ir}`, the rehearsed path with no model). Rehearsed scenarios live in `src/plannerScenarios.js`;
deep-link one with `/?scenario=coffee&autorun=1` (add `&mode=live` to send the spoken query to the model).

Planner model settings (`backend/.env.local`, gitignored, is loaded automatically):

- `PLANNER_PROVIDER`: `ollama` (default, local) or `openai` (any OpenAI-compatible API)
- `PLANNER_MODEL`: default `qwen3.6:35b-a3b`
- `PLANNER_BASE_URL`, `PLANNER_API_KEY`: for `openai`

Valhalla service limits the planner needs (in `valhalla.json`): `max_exclude_polygons_length` well above
the 10 km default (storm cells), and `max_exclude_locations` above 50 (the exclusion loop).

Saved places ("home", "work") are in `backend/saved_places.json`.

## Demo material

The UI scenarios live in `src/demoScenarios.js`. A presenter-oriented script with suggested narration and expected behavior is in [`docs/demo-driving-queries.md`](docs/demo-driving-queries.md).
