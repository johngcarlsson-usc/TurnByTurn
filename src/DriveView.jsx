import { useCallback, useEffect, useRef, useState } from 'react';
import { ActionIcon, Button, Group, SegmentedControl, Text, Tooltip } from '@mantine/core';
import {
  IconArrowBearLeft, IconArrowBearRight, IconArrowLeft, IconArrowRight, IconArrowUp, IconArrowUpRight,
  IconArrowUpLeft, IconFlag, IconRotate, IconRotateClockwise, IconVolume, IconVolumeOff, IconX,
} from '@tabler/icons-react';
import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { NAV_CONFIG, loadFerrostar, parseValhallaOsrm } from './nav/ferrostar';
import { mapStyleUrl } from './PlannerApp';
import './DriveView.css';

const SIM_STEP_M = 10;
const EMPTY = { type: 'FeatureCollection', features: [] };

const lineFc = (coords) => ({
  type: 'FeatureCollection',
  features: [{ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: coords } }],
});

const formatDistance = (meters) => {
  const feet = meters * 3.28084;
  if (feet < 1000) return `${Math.max(0, Math.round(feet / 50) * 50)} ft`;
  const miles = meters / 1609.34;
  return `${miles < 10 ? miles.toFixed(1) : Math.round(miles)} mi`;
};

const maneuverIcon = (content) => {
  const type = content?.maneuverType;
  const mod = content?.maneuverModifier;
  if (type === 'arrive') return IconFlag;
  if (mod === 'uturn') return IconRotate;
  if (type === 'roundabout' || type === 'rotary') return IconRotateClockwise;
  switch (mod) {
    case 'left': case 'sharp left': return IconArrowLeft;
    case 'right': case 'sharp right': return IconArrowRight;
    case 'slight left': return type === 'fork' || type === 'off ramp' ? IconArrowBearLeft : IconArrowUpLeft;
    case 'slight right': return type === 'fork' || type === 'off ramp' ? IconArrowBearRight : IconArrowUpRight;
    default: return IconArrowUp;
  }
};

// Average speed of the step we're on, from the route itself, so a simulated
// drive moves at a believable pace for each road.
const stepSpeed = (step) => {
  if (!step || !step.duration || !step.distance) return 13;
  return Math.min(35, Math.max(4, step.distance / step.duration));
};

const toUserLocation = (fix) => {
  const ms = fix.timestamp || Date.now();
  return {
    coordinates: { lat: fix.lat, lng: fix.lng },
    horizontalAccuracy: Math.max(1, fix.accuracy || 20),
    courseOverGround: fix.heading == null || Number.isNaN(fix.heading) ? undefined : { degrees: Math.round(fix.heading), accuracy: undefined },
    speed: fix.speed == null || Number.isNaN(fix.speed) ? undefined : { value: fix.speed, accuracy: undefined },
    timestamp: { secs_since_epoch: Math.floor(ms / 1000), nanos_since_epoch: (ms % 1000) * 1e6 },
  };
};

// shareQuery ("t=<token>") marks a partner driving from a share link: the
// default source is then this phone's own GPS, mirrored back to the planner.
const DriveView = ({ plan, apiUrl, onExit, shareQuery = '' }) => {
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const puckRef = useRef(null);
  const navRef = useRef({});
  const spokenRef = useRef(new Set());
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [trip, setTrip] = useState(null);
  const [speedUp, setSpeedUp] = useState('1');
  const [muted, setMuted] = useState(false);
  const [rerouting, setRerouting] = useState(false);
  // 'sim' drives along the route; 'gps' follows the phone beacon (/beacon.html);
  // 'device' uses this browser's own location.
  const [source, setSource] = useState(shareQuery ? 'device' : 'sim');
  const [gpsAge, setGpsAge] = useState(null);

  // Start on the phone's GPS when the beacon is streaming.
  useEffect(() => {
    if (shareQuery) return;
    fetch(apiUrl('/api/gps/latest')).then((r) => r.json())
      .then((gps) => { if (gps.fix && gps.ageSeconds < 30) setSource('gps'); })
      .catch(() => {});
  }, [apiUrl, shareQuery]);
  const speedRef = useRef(1);
  const mutedRef = useRef(false);
  speedRef.current = Number(speedUp);
  mutedRef.current = muted;

  const speak = useCallback((instruction) => {
    if (!instruction || mutedRef.current || !window.speechSynthesis) return;
    if (spokenRef.current.has(instruction.utteranceId)) return;
    spokenRef.current.add(instruction.utteranceId);
    const utterance = new SpeechSynthesisUtterance(instruction.text);
    utterance.rate = 1.05;
    window.speechSynthesis.speak(utterance);
  }, []);

  const fetchRoute = useCallback(async (body) => {
    const response = await fetch(apiUrl('/api/nav/route'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const text = await response.text();
    if (!response.ok) throw new Error(JSON.parse(text).error || `HTTP ${response.status}`);
    return text;
  }, [apiUrl]);

  const startRoute = useCallback((ferrostar, route, fromLocation) => {
    const nav = navRef.current;
    nav.controller?.free?.();
    nav.route = route;
    nav.controller = new ferrostar.NavigationController(route, NAV_CONFIG, false);
    nav.sim = ferrostar.locationSimulationFromRoute(route, SIM_STEP_M, 'None');
    nav.state = nav.controller.getInitialState(fromLocation || nav.sim.current_location);
    mapRef.current?.getSource('nav-route')?.setData(lineFc(route.geometry.map((c) => [c.lng, c.lat])));
  }, []);

  // Map + first route.
  useEffect(() => {
    const map = new maplibregl.Map({
      container: mapContainerRef.current, style: mapStyleUrl(), center: [-122.4, 37.78], zoom: 15, pitch: 60,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    let cancelled = false;
    map.on('load', async () => {
      map.addSource('nav-route', { type: 'geojson', data: EMPTY });
      map.addLayer({ id: 'nav-route-casing', type: 'line', source: 'nav-route',
        layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#0b3d91', 'line-width': 14 } });
      map.addLayer({ id: 'nav-route', type: 'line', source: 'nav-route',
        layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#1c7ed6', 'line-width': 9 } });
      const el = document.createElement('div');
      el.className = 'nav-puck';
      puckRef.current = new maplibregl.Marker({ element: el, rotationAlignment: 'map', pitchAlignment: 'map' })
        .setLngLat([plan.origin.lng, plan.origin.lat]).addTo(map);
      plan.stops.forEach((s, i) => {
        const pin = document.createElement('div');
        pin.className = 'nav-stop';
        pin.textContent = String(i + 1);
        new maplibregl.Marker({ element: pin }).setLngLat([s.lng, s.lat]).addTo(map);
      });
      try {
        const ferrostar = await loadFerrostar();
        const text = await fetchRoute({ navRequest: plan.navRequest });
        if (cancelled) return;
        navRef.current.ferrostar = ferrostar;
        startRoute(ferrostar, parseValhallaOsrm(ferrostar, text));
        setReady(true);
      } catch (err) {
        setError(err.message);
      }
    });
    return () => {
      cancelled = true;
      window.speechSynthesis?.cancel();
      clearTimeout(navRef.current.timer);
      map.remove();
    };
  }, [plan, fetchRoute, startRoute]);

  const reroute = useCallback(async (location) => {
    const nav = navRef.current;
    setRerouting(true);
    try {
      const remaining = nav.state.tripState.Navigating?.remainingWaypoints?.length ?? 1;
      const text = await fetchRoute({
        navRequest: plan.navRequest,
        from: { lat: location.coordinates.lat, lng: location.coordinates.lng },
        remainingPoints: Math.max(1, remaining),
      });
      startRoute(nav.ferrostar, parseValhallaOsrm(nav.ferrostar, text), location);
    } finally {
      setRerouting(false);
    }
  }, [fetchRoute, plan, startRoute]);

  // One location update: through the controller, then camera, puck, banner, voice.
  const onLocation = useCallback((location) => {
    const nav = navRef.current;
    nav.state = nav.controller.updateUserLocation(location, nav.state);
    const ts = nav.state.tripState;
    const navigating = ts.Navigating;
    const shown = navigating ? navigating.snappedUserLocation : location;
    const lngLat = [shown.coordinates.lng, shown.coordinates.lat];
    const bearing = shown.courseOverGround?.degrees ?? mapRef.current.getBearing();
    puckRef.current?.setLngLat(lngLat).setRotation(bearing);
    mapRef.current?.easeTo({ center: lngLat, bearing, pitch: 60, zoom: 17, duration: 900, easing: (t) => t,
      padding: { top: 260, bottom: 60, left: 0, right: 0 } });
    setTrip(ts);
    if (navigating) {
      speak(navigating.spokenInstruction);
      if (navigating.deviation?.Deviation && !nav.rerouting) {
        nav.rerouting = true;
        reroute(location).finally(() => { nav.rerouting = false; });
      }
    }
    return ts;
  }, [reroute, speak]);

  // Simulated driving: advance along the route at each step's real average
  // speed (times the speed-up), so the recording looks like a real drive.
  useEffect(() => {
    if (!ready || source !== 'sim') return undefined;
    const nav = navRef.current;
    const tick = () => {
      const ferrostar = nav.ferrostar;
      if (!nav.sim || nav.sim.remaining_locations.length === 0) return;
      nav.sim = ferrostar.advanceLocationSimulation(nav.sim);
      const ts = onLocation(nav.sim.current_location);
      if (ts.Complete) return;
      const step = ts.Navigating?.remainingSteps?.[0];
      const interval = (SIM_STEP_M / stepSpeed(step)) * 1000 / speedRef.current;
      nav.timer = setTimeout(tick, interval);
    };
    nav.timer = setTimeout(tick, 1500);
    return () => clearTimeout(nav.timer);
  }, [ready, source, onLocation]);

  // Phone GPS: fixes relayed through the backend as Server-Sent Events.
  useEffect(() => {
    if (!ready || source !== 'gps') return undefined;
    let lastFix = null;
    const stream = new EventSource(apiUrl('/api/gps/stream'));
    stream.onmessage = (event) => {
      lastFix = Date.now();
      onLocation(toUserLocation(JSON.parse(event.data)));
    };
    const ages = setInterval(() => setGpsAge(lastFix ? Math.round((Date.now() - lastFix) / 1000) : null), 1000);
    return () => { stream.close(); clearInterval(ages); };
  }, [ready, source, apiUrl, onLocation]);

  // This phone's own GPS, mirrored to the backend so the other driver's
  // planner can see where this car is.
  useEffect(() => {
    if (!ready || source !== 'device' || !navigator.geolocation) return undefined;
    let lastFix = null;
    let lastPost = 0;
    const id = navigator.geolocation.watchPosition((p) => {
      const c = p.coords;
      const fix = { lat: c.latitude, lng: c.longitude, accuracy: c.accuracy, heading: c.heading, speed: c.speed, timestamp: p.timestamp };
      lastFix = Date.now();
      onLocation(toUserLocation(fix));
      if (lastFix - lastPost > 3000) {
        lastPost = lastFix;
        fetch(apiUrl(`/api/gps${shareQuery ? `?${shareQuery}` : ''}`), {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fix),
        }).catch(() => {});
      }
    }, (e) => setError(`Location: ${e.message}`), { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 });
    const ages = setInterval(() => setGpsAge(lastFix ? Math.round((Date.now() - lastFix) / 1000) : null), 1000);
    return () => { navigator.geolocation.clearWatch(id); clearInterval(ages); };
  }, [ready, source, apiUrl, onLocation, shareQuery]);

  const navigating = trip?.Navigating;
  const visual = navigating?.visualInstruction;
  const ManeuverIcon = maneuverIcon(visual?.primaryContent);
  const progress = navigating?.progress;
  const eta = progress ? new Date(Date.now() + progress.durationRemaining * 1000) : null;
  const nextStop = navigating ? plan.stops[plan.stops.length - (navigating.remainingWaypoints.length - 1)] : null;

  return (
    <div className="drive">
      <div ref={mapContainerRef} className="drive-map" />

      {trip?.Complete ? (
        <div className="drive-banner arrived">
          <IconFlag size={56} />
          <div>
            <div className="banner-distance">Arrived</div>
            <div className="banner-text">{plan.destination.label}</div>
          </div>
        </div>
      ) : visual ? (
        <div className="drive-banner">
          <ManeuverIcon size={64} stroke={2.4} />
          <div>
            <div className="banner-distance">{formatDistance(progress.distanceToNextManeuver)}</div>
            <div className="banner-text">{visual.primaryContent.text}</div>
            {visual.secondaryContent?.text && <div className="banner-sub">{visual.secondaryContent.text}</div>}
          </div>
        </div>
      ) : (
        <div className="drive-banner"><div className="banner-text">{error || 'Starting navigation…'}</div></div>
      )}
      {rerouting && <div className="drive-reroute">Rerouting with the same constraints…</div>}

      <div className="drive-footer">
        <div>
          <Text className="footer-eta">{eta ? eta.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '–'}</Text>
          <Text size="sm" c="dimmed">
            {progress ? `${Math.round(progress.durationRemaining / 60)} min · ${formatDistance(progress.distanceRemaining)}` : ''}
            {nextStop && nextStop !== plan.destination ? ` · next: ${nextStop.label}` : ''}
          </Text>
        </div>
        <Group gap="xs">
          <SegmentedControl size="xs" value={source} onChange={setSource}
            data={[{ value: 'sim', label: 'Simulated' }, { value: 'gps', label: 'Phone GPS' }, { value: 'device', label: 'This phone' }]} />
          {source === 'sim' ? (
            <SegmentedControl size="xs" value={speedUp} onChange={setSpeedUp}
              data={[{ value: '1', label: '1×' }, { value: '4', label: '4×' }, { value: '10', label: '10×' }]} />
          ) : (
            <Text size="xs" c={gpsAge != null && gpsAge < 5 ? 'teal' : 'red'}>
              {gpsAge == null ? 'waiting for phone…' : `fix ${gpsAge}s ago`}
            </Text>
          )}
          <Tooltip label={muted ? 'Unmute' : 'Mute'}>
            <ActionIcon variant="light" size="lg" onClick={() => setMuted((m) => !m)}>
              {muted ? <IconVolumeOff size={18} /> : <IconVolume size={18} />}
            </ActionIcon>
          </Tooltip>
          <Button color="red" variant="light" leftSection={<IconX size={16} />} onClick={onExit}>End</Button>
        </Group>
      </div>
    </div>
  );
};

export default DriveView;
