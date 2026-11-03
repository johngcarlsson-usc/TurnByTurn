# Android Auto

The Android app (`android/`) is the Capacitor shell plus a native Android Auto navigation app.

- **Phone screen**: the planner web app, loaded live from the backend (`server.url` in
  `capacitor.config.json`, a Tailscale HTTPS address).
- **Car screen** (`android/app/src/main/java/io/turnbyturn/navigation/car/`): adapted from
  Ferrostar's Android Auto demo.
  - `CarAppNavigationView` (Ferrostar `ui-maplibre-car-app`) draws the MapLibre map on the car surface.
  - `NavigationTemplateBuilder` / `NavigationManagerBridge` (Ferrostar `car-app`) build the
    maneuver/lane/ETA template and keep Android Auto's trip state current.
  - `FerrostarCore` gets routes from the backend's `/api/nav/route` through a `CustomRouteProvider`,
    so every reroute keeps the planner's exclusions.

## Hand-off

1. Plan a trip in the planner (phone or laptop) and press **Car**. The backend stores it at
   `/api/trip/active`.
2. The car screen shows "To <destination>" with **Start** (and **Simulate**). Start fetches the route
   from the phone's current location through the planned stops.
3. While the app runs, the phone's position is posted to `/api/gps`, so the laptop's drive view
   (Phone GPS) and the planner's blue dot follow it.

## Build

Needs JDK 21 and the Android SDK (platform 36, build-tools 36).

    npm run build && npx cap sync android
    cd android && JAVA_HOME=<jdk-21> ./gradlew assembleDebug
    # -> android/app/build/outputs/apk/debug/app-debug.apk

The backend address is baked in as `BuildConfig.BACKEND_URL`; override with
`./gradlew assembleDebug -PbackendUrl=https://<host>.<tailnet>.ts.net`.

## Install on the phone (no Play Store)

1. Copy the APK to the phone (e.g. serve it and download it in Chrome) and open it; allow Chrome to
   "install unknown apps" when asked.
2. Open **Exagion** once and allow location (and notifications).
3. Android Auto developer mode: phone Settings → Connected devices → **Android Auto** → scroll to
   **Version** and tap it ~10 times → OK. Then ⋮ → **Developer settings** → enable **Unknown sources**.
   Sideloaded apps only appear on the car screen with this on.
4. Connect to the car. Exagion appears in the car's app launcher. If it doesn't: Android Auto settings → **Customize launcher** and make sure Exagion is checked; after turning on Unknown sources, unplug and reconnect (or restart the phone).

Testing without a car: Google's Desktop Head Unit (DHU, in the SDK's `extras;google;auto`)
needs an `adb` connection to the phone.
