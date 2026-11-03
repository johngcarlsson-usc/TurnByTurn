# Driving demo: turn-by-turn with real or simulated GPS

Drive mode uses Ferrostar's core (Rust → WASM, `src/nav/ferrostar.js`) for step advance, spoken
instructions, and off-route detection, on the app's own MapLibre view (`src/DriveView.jsx`). Routes come
from `POST /api/nav/route`, which asks Valhalla for the plan's exact route in OSRM format
(banner + voice instructions), keeping every exclusion the planner found, so reroutes keep the constraints.

## Simulated drive (no phone, good for recordings)

Open a rehearsed scenario straight into driving:

    http://127.0.0.1:5175/?scenario=right-side-gas&autorun=1&drive=1

It shows the plan for ~6 s, then drives along the route at each step's real average speed.
The footer switches 1× / 4× / 10× and mutes the voice. Record the screen (e.g. OBS) with system audio.

## Phone GPS

The phone streams its location from a small page, `public/beacon.html`, to `POST /api/gps`; the drive view
listens on `GET /api/gps/stream` (Server-Sent Events). Phones only share GPS with HTTPS pages.

What worked (Galaxy S25 as the laptop's hotspot): **Tailscale** on both devices.

1. Laptop: `sudo tailscale up --operator=$USER` (once), then `tailscale serve --bg 5175`.
   That serves the app at `https://<laptop>.<tailnet>.ts.net` with a real certificate.
2. Phone (Tailscale on): open `https://<laptop>.<tailnet>.ts.net/beacon.html`, tap **Start sharing location**,
   allow precise location, keep the page open with the screen on.
3. Laptop: plan a route and press **Drive**. It starts on **Phone GPS** when a fix is fresh; the footer shows
   the fix age. Off the route, it reroutes from where you are, with the same constraints.

The planner's home screen also centers on the phone's position (blue dot), and live requests start there.

What didn't work: the phone reaching the laptop directly over its own hotspot (`https://<laptop-ip>:5176`,
self-signed via `VITE_HTTPS=1`). No connection ever arrived at the laptop, even for plain HTTP, so it is a
phone-side routing issue, not the app. ADB `reverse` over USB is the other fallback.

## In the car

- Laptop on the phone's hotspot (internet for map tiles and a hosted model), plugged into car power.
- Keep the laptop awake: `systemd-inhibit --what=sleep:idle sleep 4h` in a terminal.
- Mount the phone with the beacon page open; the passenger runs the laptop and the recording.
