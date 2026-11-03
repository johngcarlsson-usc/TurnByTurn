import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActionIcon, Badge, Button, Collapse, Group, Loader, ScrollArea, Select, Stack, Text,
  TextInput, Textarea, Tooltip, UnstyledButton,
} from '@mantine/core';
import {
  IconAlertTriangle, IconBrain, IconCar, IconCheck, IconChevronDown, IconChevronRight, IconCircleX, IconMapPin,
  IconCode, IconNavigation, IconPlayerPlay, IconRoute, IconSend, IconX,
} from '@tabler/icons-react';
import { Capacitor } from '@capacitor/core';
import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import ConstraintEditor, { EMPTY_IR } from './ConstraintEditor';
import { SCENARIO_OPTIONS, getPlannerScenario } from './plannerScenarios';
import './PlannerApp.css';

const API_BASE = (import.meta.env.VITE_ROUTING_API_URL || '').replace(/\/+$/, '');
// The Exagion app ships this page inside the APK (opens instantly, even with
// no signal); it reaches the planner, search and map tiles on the laptop.
const BACKEND_ORIGIN = (import.meta.env.VITE_BACKEND_ORIGIN || 'http://localhost:5055').replace(/\/+$/, '');
const BAY_AREA = { center: [-122.3, 37.62], zoom: 9 };
const STEP_MS = 900;
// Live requests without a stated start ("my friend just left…") begin here.
const DEMO_CURRENT_LOCATION = { lat: 37.7955, lng: -122.3937, label: 'Current location (Ferry Building)' };

const LOOPBACK = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/;
const PAGE_IS_LOCAL = ['localhost', '127.0.0.1'].includes(window.location.hostname);

const BUNDLED_IN_APP = Capacitor.isNativePlatform() && window.location.hostname === 'localhost';

// Where the backend, tiles and share pages live, as seen from this page.
export const serverOrigin = () => (BUNDLED_IN_APP ? BACKEND_ORIGIN : window.location.origin);

export const apiUrl = (path) => {
  if (BUNDLED_IN_APP) return `${BACKEND_ORIGIN}${path}`;
  // A loopback API address only means this machine; from a phone's browser
  // (over Tailscale) use the page's own origin, which proxies /api.
  if (API_BASE && !(LOOPBACK.test(API_BASE) && !PAGE_IS_LOCAL)) return `${API_BASE}${path}`;
  return path;
};

// The basemap comes from the local TileServer GL at /tiles (docs/local-services.md).
export const mapStyleUrl = () => import.meta.env.VITE_TURN_BY_TURN_STYLE_URL
  || `${serverOrigin()}/tiles/styles/liberty/style.json`;

const EMPTY = { type: 'FeatureCollection', features: [] };
const line = (coordinates, props = {}) => ({ type: 'Feature', properties: props, geometry: { type: 'LineString', coordinates } });
const ring = (coordinates, props = {}) => ({ type: 'Feature', properties: props, geometry: { type: 'Polygon', coordinates: [coordinates] } });
const point = (lng, lat, props = {}) => ({ type: 'Feature', properties: props, geometry: { type: 'Point', coordinates: [lng, lat] } });
const fc = (features) => ({ type: 'FeatureCollection', features });

export const formatMinutes = (seconds) => {
  const minutes = Math.round(Number(seconds) / 60);
  if (!Number.isFinite(minutes)) return '–';
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
};

const formatMiles = (miles) => `${Number(miles).toFixed(miles >= 10 ? 0 : 1)} mi`;

// Map layers, bottom to top. Each owns one GeoJSON source of the same id.
const LAYERS = [
  { id: 'areas', type: 'fill', paint: { 'fill-color': '#d9480f', 'fill-opacity': 0.12 } },
  { id: 'areas-outline', source: 'areas', type: 'line', paint: { 'line-color': '#d9480f', 'line-width': 2, 'line-dasharray': [2, 2] } },
  { id: 'storms', type: 'fill', paint: { 'fill-color': '#7048e8', 'fill-opacity': 0.22 } },
  { id: 'storms-outline', source: 'storms', type: 'line', paint: { 'line-color': '#7048e8', 'line-width': 2 } },
  { id: 'baseline', type: 'line', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': '#868e96', 'line-width': 5, 'line-opacity': 0.75, 'line-dasharray': [1.5, 1.5] } },
  { id: 'attempts', type: 'line', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': '#f76707', 'line-width': 4, 'line-opacity': 0.7 } },
  { id: 'candidates', type: 'circle', paint: { 'circle-radius': 3.5, 'circle-color': '#ffffff', 'circle-stroke-color': '#5f3dc4', 'circle-stroke-width': 1.5 } },
  { id: 'excluded', type: 'circle', paint: { 'circle-radius': 4, 'circle-color': '#e03131', 'circle-stroke-color': '#fff', 'circle-stroke-width': 1 } },
  { id: 'friend', type: 'line', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': '#ae3ec9', 'line-width': 5, 'line-opacity': 0.85 } },
  { id: 'route-casing', source: 'route', type: 'line', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': '#0b3d91', 'line-width': 9 } },
  { id: 'route', type: 'line', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': '#1c7ed6', 'line-width': 6 } },
];
const SOURCES = ['areas', 'storms', 'baseline', 'attempts', 'candidates', 'excluded', 'friend', 'route'];

const traceSummary = (step) => {
  switch (step.stage) {
    case 'interpret': return 'The model filled in the constraint schema below; nothing else came from the model.';
    case 'resolve': return `${step.match.label}${step.match.locality ? `, ${step.match.locality}` : ''} · Overture ${step.match.category || ''}`
      + (step.ambiguous ? ' · ambiguous' : '');
    case 'geometry': return [
      ...(step.areas || []).map((a) => `${a.label}: ${a.radiusMiles} mi circle (${a.roads === 'freeways' ? 'freeways only' : 'all roads'})`),
      ...(step.roads || []),
      step.stormCells ? `${step.stormCells} storm cell(s) → exclude polygons` : null,
      Object.keys(step.costingOptions || {}).length ? `Valhalla costing: ${JSON.stringify(step.costingOptions)}` : null,
    ].filter(Boolean).join(' · ');
    case 'baseline': return `${step.minutes} min, ${step.miles} mi with no constraints`;
    case 'exclude': return step.rounds.map((r) => (r.violations
      ? `#${r.attempt}: ${r.minutes} min, ${r.violations} bad segment(s) on ${r.roads.join(', ') || 'unnamed road'}`
      : `#${r.attempt}: ${r.minutes} min, clean`)).join(' → ');
    case 'search': return `${step.found} candidates near the route`
      + (step.shortlist ? ` · best: ${step.shortlist.slice(0, 3).map((s) => `${s.label} (+${s.detourMinutes} min)`).join(', ')}` : '');
    case 'solve': return [
      step.nodes ? `${step.nodes} nodes` : null,
      step.matrixMs != null ? `matrix ${step.matrixMs} ms` : null,
      step.solveMs != null ? `solve ${step.solveMs} ms` : null,
      step.totalMinutes != null ? `${step.totalMinutes} min incl. stops` : null,
    ].filter(Boolean).join(' · ');
    case 'side': return step.rejected
      ? `Rejected: ${step.rejected.join(', ')}`
      : step.attempts.map((a) => `${a.stop} via preferred_side=${a.preferredSide}: arrives on the ${a.arrivesOn}`).join(' · ');
    case 'verify': return `${step.checks.filter((c) => c.passed).length}/${step.checks.length} checks passed`;
    case 'via': return `${step.found} points on it found in the routing graph · best adds ${step.minutes} min to the trip`;
    case 'custom': return step.attempts.map((a) => `#${a.attempt}: ${a.passed ? 'passed' : 'failed'} · ${a.detail}`
      + (a.toolCalls != null ? ` · ${a.toolCalls} routing calls` : '')).join(' → ');
    default: return '';
  }
};

const STAGE_ICON = {
  interpret: IconBrain, resolve: IconMapPin, geometry: IconAlertTriangle, baseline: IconRoute,
  exclude: IconX, side: IconX, search: IconMapPin, solve: IconRoute, verify: IconCheck,
  via: IconRoute, custom: IconCode,
};

const IrView = ({ ir }) => {
  const chips = [];
  if (ir.origin) chips.push(['From', ir.origin]);
  if (ir.destination) chips.push(['To', ir.destination]);
  if (ir.returnToOrigin) chips.push(['Round trip', 'yes']);
  ir.stops.forEach((s, i) => chips.push([`Stop ${i + 1}`, s.kind === 'place' ? s.text : [
    s.what, s.brands?.length && `(${s.brands.join(' or ')})`, s.side === 'right' && 'on the right',
    s.position !== 'anywhere' && s.position,
    s.maxDetourMinutes != null && `≤${s.maxDetourMinutes} min detour`, s.closesAt && `before ${s.closesAt}`,
  ].filter(Boolean).join(' ')]));
  if (ir.stops.length > 1) chips.push(['Order', ir.stopOrder === 'any' ? 'optimize' : 'as spoken']);
  if (ir.arriveBy) chips.push(['Arrive by', ir.arriveBy]);
  (ir.useRoads || []).forEach((r) => chips.push(['Take', r]));
  if (ir.custom) chips.push(['Custom code', ir.custom]);
  const a = ir.avoid;
  ['highways', 'tolls', 'ferries', 'storms'].forEach((k) => a[k] && chips.push(['Avoid', k]));
  a.areas.forEach((x) => chips.push(['Avoid', `${x.roads === 'freeways' ? 'freeways in ' : ''}${x.text}`]));
  a.roads.forEach((x) => chips.push(['Avoid', x.name + (x.near ? ` near ${x.near}` : '')]));
  const m = ir.meetup || {};
  if (m.friendFrom) {
    chips.push(['Meet friend from', m.friendFrom]);
    if (m.friendLeftMinutesAgo != null) chips.push(['They left', m.friendLeftMinutesAgo ? `${m.friendLeftMinutesAgo} min ago` : 'just now']);
    if (m.what) chips.push(['Meet at', m.what]);
    if (m.maxDetourMinutes != null) chips.push(['Detour', `≤${m.maxDetourMinutes} min each`]);
  }
  const team = ir.team || {};
  if (team.tasks?.length) {
    chips.push(['Split with', `${team.partner || 'partner'}${team.partnerAt ? ` (at ${team.partnerAt})` : ' (shared location)'}`]);
    team.tasks.forEach((t, i) => chips.push([`Task ${i + 1}`, t.kind === 'escort'
      ? `${t.who || 'someone'}: ${t.from} → ${t.to}${t.arriveBy ? ` by ${t.arriveBy}` : ''}`
      : t.kind === 'place' ? t.text : `${t.what}${t.brands?.length ? ` (${t.brands.join(' or ')})` : ''}`]));
  }
  if (ir.profile !== 'auto') chips.push(['Vehicle', ir.profile]);
  return (
    <div className="ir-chips">
      {chips.map(([k, v], i) => (
        <span className="ir-chip" key={`${k}-${i}`}><b>{k}</b> {v}</span>
      ))}
    </div>
  );
};

const PlannerApp = ({ onNavigate }) => {
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const markersRef = useRef([]);
  const timersRef = useRef([]);
  const [mapReady, setMapReady] = useState(false);
  // The editable constraints: filled by the form, by the model, or by a demo.
  const [ir, setIr] = useState(EMPTY_IR);
  const [showEditor, setShowEditor] = useState(false);
  const [scenarioId, setScenarioId] = useState(null);
  const [query, setQuery] = useState('');
  const [transcript, setTranscript] = useState([]);
  const transcriptRef = useRef([]);
  const chatThreadRef = useRef(null);
  const requestInFlightRef = useRef(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [showTrace, setShowTrace] = useState(true);
  const [showSteps, setShowSteps] = useState(false);
  const [phase, setPhase] = useState('');
  useEffect(() => {
    const thread = chatThreadRef.current;
    if (thread) thread.scrollTop = thread.scrollHeight;
  }, [transcript, loading]);

  const scenario = useMemo(() => (scenarioId ? getPlannerScenario(scenarioId) : null), [scenarioId]);
  const gpsMarkerRef = useRef(null);
  const [gpsFix, setGpsFix] = useState(null);
  const autorunRef = useRef(new URLSearchParams(window.location.search));

  useEffect(() => {
    const map = new maplibregl.Map({
      container: mapContainerRef.current,
      style: mapStyleUrl(),
      center: BAY_AREA.center,
      zoom: BAY_AREA.zoom,
      attributionControl: { compact: false },
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');
    map.on('load', () => {
      SOURCES.forEach((id) => map.addSource(id, { type: 'geojson', data: EMPTY }));
      LAYERS.forEach((layer) => map.addLayer({ ...layer, source: layer.source || layer.id }));
      setMapReady(true);
    });
    mapRef.current = map;
    return () => map.remove();
  }, []);

  // Phone GPS. In the Exagion app the page reads the phone's own location
  // (Wi-Fi assisted, so it works indoors) and shares it with the backend;
  // elsewhere it follows the phone through the backend (the app or
  // /beacon.html). This runs from the start, independent of the map, so a slow
  // or failed map never hides the driver's position from the planner.
  useEffect(() => {
    let ownFixAt = 0;
    let watchId = null;
    if (Capacitor.isNativePlatform() && navigator.geolocation) {
      let lastPost = 0;
      watchId = navigator.geolocation.watchPosition((p) => {
        const c = p.coords;
        const fix = { lat: c.latitude, lng: c.longitude, accuracy: c.accuracy, heading: c.heading, speed: c.speed, timestamp: p.timestamp };
        ownFixAt = Date.now();
        setGpsFix(fix);
        if (ownFixAt - lastPost > 5000) {
          lastPost = ownFixAt;
          fetch(apiUrl('/api/gps'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fix) })
            .catch(() => {});
        }
      }, () => {}, { enableHighAccuracy: true, maximumAge: 10000, timeout: 30000 });
    }
    const poll = async () => {
      if (Date.now() - ownFixAt < 30000) return;
      try {
        const gps = await (await fetch(apiUrl('/api/gps/latest'))).json();
        if (gps.fix && gps.ageSeconds <= 120) setGpsFix(gps.fix);
      } catch { /* no beacon */ }
    };
    poll();
    const timer = setInterval(poll, 3000);
    return () => {
      clearInterval(timer);
      if (watchId !== null) navigator.geolocation.clearWatch(watchId);
    };
  }, []);

  // The fix as a blue dot once the map is up; the first one centers the map.
  const centeredRef = useRef(false);
  useEffect(() => {
    if (!mapReady || !gpsFix) return;
    if (!gpsMarkerRef.current) {
      const el = document.createElement('div');
      el.className = 'gps-dot';
      gpsMarkerRef.current = new maplibregl.Marker({ element: el }).setLngLat([gpsFix.lng, gpsFix.lat]).addTo(mapRef.current);
    }
    gpsMarkerRef.current.setLngLat([gpsFix.lng, gpsFix.lat]);
    if (!centeredRef.current && !autorunRef.current.get('scenario')) {
      centeredRef.current = true;
      mapRef.current.flyTo({ center: [gpsFix.lng, gpsFix.lat], zoom: 14, duration: 800 });
    }
  }, [mapReady, gpsFix]);

  const partnerMarkerRef = useRef(null);
  const [partnerFix, setPartnerFix] = useState(null);
  useEffect(() => {
    if (!mapReady) return undefined;
    const poll = async () => {
      try {
        const gps = await (await fetch(apiUrl('/api/gps/latest?who=partner'))).json();
        if (!gps.fix || gps.ageSeconds > 900) {
          partnerMarkerRef.current?.remove();
          partnerMarkerRef.current = null;
          setPartnerFix(null);
          return;
        }
        setPartnerFix({ ...gps.fix, ageSeconds: gps.ageSeconds });
        if (!partnerMarkerRef.current) {
          const el = document.createElement('div');
          el.className = 'gps-dot gps-dot-partner';
          el.title = 'Shared location';
          partnerMarkerRef.current = new maplibregl.Marker({ element: el }).setLngLat([gps.fix.lng, gps.fix.lat]).addTo(mapRef.current);
        }
        partnerMarkerRef.current.setLngLat([gps.fix.lng, gps.fix.lat]);
      } catch { /* nothing shared */ }
    };
    poll();
    const timer = setInterval(poll, 4000);
    return () => clearInterval(timer);
  }, [mapReady]);

  const setData = useCallback((id, data) => {
    mapRef.current?.getSource(id)?.setData(data);
  }, []);

  const clearTimers = () => {
    timersRef.current.forEach(clearTimeout);
    timersRef.current = [];
  };

  const drawMarkers = useCallback((res) => {
    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];
    const map = mapRef.current;
    const add = (p, cls, text, title) => {
      const el = document.createElement('div');
      el.className = `pin ${cls}`;
      el.textContent = text;
      el.title = title;
      markersRef.current.push(new maplibregl.Marker({ element: el }).setLngLat([p.lng, p.lat]).addTo(map));
    };
    if (res.team) {
      res.team.people.forEach((person) => {
        const cls = person.key === 'me' ? 'pin-origin' : 'pin-friend';
        add(person.start, cls, person.name[0], person.start.label);
        person.stops.forEach((s, i) => add(s, `pin-stop ${person.key === 'me' ? '' : 'pin-stop-partner'}`,
          String(i + 1), `${s.task}: ${s.label} (${s.arriveAt})`));
      });
      return;
    }
    add(res.origin, 'pin-origin', 'A', res.origin.label);
    if (res.meetup) add(res.meetup.friend, 'pin-friend', 'F', res.meetup.friend.label);
    res.stops.forEach((s, i) => add(s, 'pin-stop', String(i + 1), s.label));
    add(res.destination, 'pin-dest', 'B', res.destination.label);
  }, []);

  // Replays the planner's own trace on the map: baseline, each exclusion
  // attempt, candidates, then the verified route. This is the talk's
  // "language becomes constraints, constraints become geometry" moment.
  const playResult = useCallback((res) => {
    clearTimers();
    const map = mapRef.current;
    SOURCES.forEach((id) => setData(id, EMPTY));
    drawMarkers(res);
    const all = [
      ...res.route.coordinates, ...(res.baseline?.coordinates || []),
      ...res.areas.flatMap((a) => a.ring), ...res.stormRings.flat(), ...(res.meetup?.friendRoute || []),
      ...(res.team?.people || []).flatMap((p) => p.route?.coordinates || []),
    ];
    if (all.length) {
      const bounds = all.reduce((b, c) => b.extend(c), new maplibregl.LngLatBounds(all[0], all[0]));
      const panel = window.innerWidth > 760 ? 460 : 0;
      map.fitBounds(bounds, { padding: { top: 60, bottom: 60, left: panel + 40, right: 60 }, duration: 700 });
    }
    const steps = [];
    const at = (fn, label) => steps.push({ fn, label });
    if (res.areas.length) at(() => setData('areas', fc(res.areas.map((a) => ring(a.ring)))), 'Constraint → polygon');
    if (res.stormRings.length) at(() => setData('storms', fc(res.stormRings.map((r) => ring(r)))), 'Storm cells → exclusion');
    if (res.baseline) at(() => setData('baseline', fc([line(res.baseline.coordinates)])), 'Unconstrained route');
    res.trace.filter((s) => s.stage === 'exclude').forEach((s) => {
      s.rounds.filter((r) => r.violations > 0).forEach((r) => at(() => {
        setData('attempts', fc([line(r.coordinates)]));
      }, `Attempt ${r.attempt}: ${r.violations} bad segment(s) on ${r.roads.slice(0, 2).join(', ') || 'unnamed'}`));
    });
    const searchPoints = res.trace.filter((s) => s.stage === 'search').flatMap((s) => s.points || []);
    if (searchPoints.length) at(() => setData('candidates', fc(searchPoints.map(([lng, lat]) => point(lng, lat)))),
      `${searchPoints.length} candidates along the corridor`);
    if (res.meetup) at(() => setData('friend', fc([line(res.meetup.friendRoute)])), 'Friend’s route to the meeting spot');
    const others = (res.team?.people || []).filter((p) => p.key !== res.team.lead && p.route);
    others.forEach((p) => at(() => setData('friend', fc([line(p.route.coordinates)])), `${p.name}’s part`));
    at(() => {
      setData('attempts', EMPTY);
      setData('excluded', fc(res.excludedPoints.map((p) => point(p.lng, p.lat))));
      setData('route', fc([line(res.route.coordinates)]));
    }, res.team ? (res.allChecksPassed ? 'Both parts verified' : 'Verifier found problems')
      : res.allChecksPassed ? 'Verified route' : 'Route (verifier found problems)');
    steps.forEach((step, i) => {
      timersRef.current.push(setTimeout(() => {
        step.fn();
        setPhase(step.label);
      }, 700 + i * STEP_MS));
    });
  }, [drawMarkers, setData]);

  const currentLocation = () => (gpsFixRef.current
    ? { lat: gpsFixRef.current.lat, lng: gpsFixRef.current.lng, label: 'Current location (phone GPS)' }
    : DEMO_CURRENT_LOCATION);

  // kind 'route': plan the constraints as they stand in the form.
  // kind 'describe': continue the conversation and draw any plan it returns.
  const run = async (kind, override) => {
    if (requestInFlightRef.current) return;
    const plannedIr = override?.ir || irRef.current;
    const extras = override?.scenario || scenarioRef.current || {};
    const words = override?.query ?? queryRef.current;
    if (kind === 'describe' && !words.trim()) return;
    if (kind === 'route' && !plannedIr.destination && !plannedIr.stops.length && !plannedIr.team?.tasks?.length) {
      setError('Enter a destination.');
      return;
    }
    requestInFlightRef.current = true;
    setLoading(true);
    setError('');
    setPhase(kind === 'describe' ? 'Asking the model…' : 'Planning…');
    clearTimers();
    const previousTranscript = transcriptRef.current;
    try {
      const nextTranscript = [...previousTranscript, { role: 'user', content: words.trim() }];
      if (kind === 'describe') {
        transcriptRef.current = nextTranscript;
        setTranscript(nextTranscript);
        setQuery('');
      }
      const body = kind === 'describe'
        ? {
          transcript: nextTranscript,
          currentLocation: currentLocation(),
          // a rehearsed demo's clock ("17:15"), or ?now=17:15; real time otherwise
          now: extras.now || autorunRef.current.get('now') || undefined,
        }
        : { ir: plannedIr, currentLocation: currentLocation(), demoStorms: extras.demoStorms, now: extras.now };
      body.picked = pickedRef.current;
      if (kind === 'describe') {
        timersRef.current.push(setTimeout(() => setPhase('Still working on your request…'), 12000));
      }
      const response = await fetch(apiUrl(kind === 'describe' ? '/api/agent/chat' : '/api/agent/plan-ir'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      let data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
      clearTimers();
      if (kind === 'describe') {
        const updated = [...nextTranscript, ...data.messages];
        transcriptRef.current = updated;
        setTranscript(updated);
        if (!data.plan) {
          setPhase('');
          return;
        }
        data = data.plan;
      }
      if (kind === 'describe' && data.ir) setIr({ ...EMPTY_IR, ...data.ir });
      setResult({ ...data, query: words, kind: kind === 'describe' ? 'live' : 'form' });
      if (data.clarification) {
        setPhase('');
        return;
      }
      playResult(data);
      if (autorunRef.current.get('drive') === '1' && onNavigate) {
        // ?drive=1: show the plan briefly, then start driving (for recordings).
        timersRef.current.push(setTimeout(() => onNavigate(data), 6000));
      }
    } catch (err) {
      clearTimers();
      if (kind === 'describe') {
        transcriptRef.current = previousTranscript;
        setTranscript(previousTranscript);
        setQuery(words);
      }
      setError(err.message);
      setPhase('');
    } finally {
      requestInFlightRef.current = false;
      setLoading(false);
    }
  };

  // Deep links for slides: ?scenario=coffee&autorun=1 (add &mode=live to send
  // the spoken request to the model instead of the checked-in constraints).
  useEffect(() => {
    if (!mapReady) return;
    const params = autorunRef.current;
    const id = params.get('scenario');
    if (!id) return;
    const next = getPlannerScenario(id);
    setScenarioId(next.id);
    const live = params.get('mode') === 'live';
    if (!live) setIr(next.ir);
    if (params.get('autorun') === '1') {
      setTimeout(() => runRef.current(live ? 'describe' : 'route',
        { ir: live ? EMPTY_IR : next.ir, scenario: next, query: next.query }), 300);
    } else if (live) {
      setQuery(next.query);
    }
  }, [mapReady]);

  // A demo loads its constraints into the form and plans them right away.
  const pickScenario = (id) => {
    if (!id) return;
    const next = getPlannerScenario(id);
    setScenarioId(next.id);
    setIr(next.ir);
    run('route', { ir: next.ir, scenario: next, query: next.query });
  };

  const [sentToCar, setSentToCar] = useState(null);
  const postTrip = async (who, part) => {
    const stops = [...part.stops, part.destination];
    const response = await fetch(apiUrl(`/api/trip/active?who=${who}`), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        navRequest: part.navRequest,
        destination: part.destination.label,
        stops: stops.map((s) => s.task || s.label),
        summary: part.summary,
      }),
    });
    if (!response.ok) throw new Error((await response.json()).error || `HTTP ${response.status}`);
  };
  // A team member's part in the shape the hand-off wants.
  const teamPart = (person) => ({
    navRequest: person.navRequest,
    stops: person.stops.slice(0, -1),
    destination: person.stops[person.stops.length - 1],
    summary: `${person.stops.length} stop(s) · done ${person.doneAt}`,
  });
  const myTeamPart = result?.team?.people.find((p) => p.key === 'me' && p.route);
  const sendToCar = async () => {
    const r = result;
    try {
      await postTrip('me', r.team ? teamPart(myTeamPart) : {
        navRequest: r.navRequest, stops: r.stops, destination: r.destination,
        summary: `${formatMinutes(r.route.durationSeconds)} · ${formatMiles(r.route.distanceMiles)}`,
      });
      setSentToCar(r);
    } catch (err) {
      setError(`Couldn’t send to the car: ${err.message}`);
    }
  };

  // Share links (Glympse-style): one expiring link per request, texted to the
  // partner. It asks for their location, then shows the trip sent to them.
  const [share, setShare] = useState(null);
  const [sentToPartner, setSentToPartner] = useState(null);
  const shareUrl = (s) => `${serverOrigin()}${s.path}`;
  const textLink = (s, message) => {
    navigator.clipboard?.writeText(shareUrl(s)).catch(() => {});
    if (/Android|iPhone|iPad/i.test(navigator.userAgent)) {
      window.location.href = `sms:?&body=${encodeURIComponent(`${message} ${shareUrl(s)}`)}`;
    }
  };
  const askForLocation = async (name) => {
    try {
      const response = await fetch(apiUrl('/api/share'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ who: 'partner', name, minutes: 30 }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      setShare(data);
      textLink(data, 'Can you share your location for the next 30 min so we can split the errands?');
    } catch (err) {
      setError(`Couldn’t make a share link: ${err.message}`);
    }
  };
  const sendToPartner = async (person) => {
    try {
      await postTrip('partner', teamPart(person));
      setSentToPartner(result);
      if (share && share.expiresAt * 1000 > Date.now()) textLink(share, 'Here’s your part of the errands:');
      else await askForLocation(person.name);
    } catch (err) {
      setError(`Couldn’t send it: ${err.message}`);
    }
  };

  const editIr = (next) => {
    setIr(next);
    setScenarioId(null);  // hand edits leave the demo's simulated storm/clock behind
  };

  const runRef = useRef(run);
  runRef.current = run;
  const scenarioRef = useRef(scenario);
  scenarioRef.current = scenario;
  const queryRef = useRef(query);
  queryRef.current = query;
  const irRef = useRef(ir);
  irRef.current = ir;
  const gpsFixRef = useRef(gpsFix);
  gpsFixRef.current = gpsFix;

  // Search-as-you-type for "Where to?" (local Photon). A picked suggestion's
  // exact spot goes with the plan, so the planner doesn't search again.
  const [suggestions, setSuggestions] = useState([]);
  const pickedRef = useRef({});
  const lastPickedRef = useRef(null);
  useEffect(() => {
    const text = (ir.destination || '').trim();
    if (text.length < 2 || text === lastPickedRef.current) { setSuggestions([]); return undefined; }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      const here = currentLocation();
      const params = new URLSearchParams({ q: text, lat: here.lat, lng: here.lng });
      try {
        const response = await fetch(apiUrl(`/api/autocomplete?${params}`), { signal: controller.signal });
        const { results = [] } = await response.json();
        const seen = new Set();
        setSuggestions(results.map((r) => ({ ...r, key: r.detail ? `${r.label} · ${r.detail}` : r.label }))
          .filter((r) => !seen.has(r.key) && seen.add(r.key)));
      } catch { /* aborted or offline: keep typing */ }
    }, 180);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [ir.destination]);  // eslint-disable-line react-hooks/exhaustive-deps
  const [searching, setSearching] = useState(false);
  const pickSuggestion = (hit) => {
    pickedRef.current[hit.label.toLowerCase()] = { lat: hit.lat, lng: hit.lng, label: hit.label };
    lastPickedRef.current = hit.label;
    setSuggestions([]);
    setSearching(false);
    document.activeElement?.blur();  // close the keyboard
    editIr({ ...irRef.current, destination: hit.label });
  };

  // Phone: the panel is a bottom sheet; swipe it down (or tap the handle) to see the map.
  const [sheetCollapsed, setSheetCollapsed] = useState(false);
  const touchStartRef = useRef(null);
  const sheetTouch = {
    onTouchStart: (e) => { touchStartRef.current = e.touches[0].clientY; },
    onTouchEnd: (e) => {
      const dy = e.changedTouches[0].clientY - (touchStartRef.current ?? e.changedTouches[0].clientY);
      if (dy > 40) setSheetCollapsed(true);
      else if (dy < -40) setSheetCollapsed(false);
      touchStartRef.current = null;
    },
  };

  const route = result?.route;
  const baseline = result?.baseline;
  const totalSeconds = route ? route.durationSeconds + (result.dwellSeconds || 0) : 0;
  const deltaMinutes = route && baseline ? Math.round((route.durationSeconds - baseline.durationSeconds) / 60) : null;

  return (
    <div className="planner">
      <div ref={mapContainerRef} className="planner-map" />
      {phase && <div className="phase-banner">{phase}</div>}

      <aside className={`planner-panel${sheetCollapsed && !searching ? ' collapsed' : ''}${searching ? ' searching' : ''}`}>
        <div className="sheet-handle" {...sheetTouch} onClick={() => setSheetCollapsed((v) => !v)}
          role="button" aria-label={sheetCollapsed ? 'Expand panel' : 'Collapse panel'} />
        <Group justify="space-between" mb="xs" className="panel-header" {...sheetTouch}>
          <Group gap={8}>
            <img src="/exagion-192.png" alt="" width={24} height={24} style={{ borderRadius: 6 }} />
            <Text fw={700} size="lg">Exagion</Text>
          </Group>
          <Group gap={6}>
            {result && !result.clarification && (!result.team || myTeamPart) && (
              <Tooltip label="Hand this trip to the Android Auto app">
                <Button size="xs" variant="light" leftSection={<IconCar size={14} />} onClick={sendToCar}>
                  {sentToCar === result ? 'Sent' : 'Car'}
                </Button>
              </Tooltip>
            )}
            {result && !result.clarification && onNavigate && (!result.team || myTeamPart) && (
              <Button size="xs" leftSection={<IconNavigation size={14} />}
                onClick={() => onNavigate(result.team ? { ...teamPart(myTeamPart), origin: myTeamPart.start } : result)}>Drive</Button>
            )}
            <Select size="xs" w={150} placeholder="Demos" data={SCENARIO_OPTIONS} value={scenarioId}
              onChange={pickScenario} comboboxProps={{ withinPortal: true, width: 300, position: 'bottom-end' }} />
          </Group>
        </Group>

        <div className="where-to">
          <TextInput variant="unstyled" size="md" placeholder="Where to?" leftSection={<IconMapPin size={18} />}
            value={ir.destination || ''} style={{ flex: 1 }}
            onChange={(e) => editIr({ ...irRef.current, destination: e.currentTarget.value || null })}
            onFocus={() => setSearching(true)}
            onBlur={() => setSearching(false)}
            onKeyDown={(e) => {
              if (e.key !== 'Enter') return;
              if (suggestions.length) pickSuggestion(suggestions[0]);
              else { e.currentTarget.blur(); run('route'); }
            }} />
          <Tooltip label="Route with these constraints">
            <ActionIcon size="lg" radius="xl" onClick={() => run('route')} loading={loading}>
              <IconPlayerPlay size={18} />
            </ActionIcon>
          </Tooltip>
        </div>
        {searching && suggestions.length > 0 && (
          // A plain list in the panel, not a floating popup: popups re-position
          // as the phone keyboard resizes the page, which made the panel jump.
          <div className="suggestions">
            {suggestions.map((s) => (
              <UnstyledButton key={s.key} className="suggestion"
                onMouseDown={(e) => e.preventDefault()}  // keep focus until the pick lands
                onClick={() => pickSuggestion(s)}>
                <Text size="sm" lineClamp={1}>{s.label}</Text>
                {s.detail && <Text size="xs" c="dimmed" lineClamp={1}>{s.detail}</Text>}
              </UnstyledButton>
            ))}
          </div>
        )}
        <Text size="xs" c="dimmed" mt={4} ml={4}>
          From {ir.origin || (gpsFix ? 'your phone’s GPS location' : 'the Ferry Building (no phone GPS)')}
        </Text>

        <UnstyledButton onClick={() => setShowEditor((v) => !v)} className="section-toggle" mt={8}>
          {showEditor ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
          <Text fw={600} size="sm">Constraints</Text>
          <Text size="xs" c="dimmed">
            {[ir.stops.length && `${ir.stops.length} stop(s)`,
              (ir.avoid.areas.length + ir.avoid.roads.length + ['highways', 'tolls', 'ferries', 'storms'].filter((k) => ir.avoid[k]).length) && 'avoidances',
              ir.arriveBy && `by ${ir.arriveBy}`, ir.meetup.friendFrom && 'meetup', ir.team?.tasks?.length && 'split with partner'].filter(Boolean).join(' · ') || 'none'}
          </Text>
        </UnstyledButton>
        <Collapse in={showEditor}>
          <ScrollArea.Autosize mah={360} type="auto" offsetScrollbars mt={6}>
            <ConstraintEditor ir={ir} onChange={editIr} />
          </ScrollArea.Autosize>
        </Collapse>

        <div className="chat-thread" ref={chatThreadRef} role="log" aria-label="Trip conversation" aria-live="polite">
          {transcript.filter((message) => ['user', 'assistant'].includes(message.role) && message.content).map((message, index) => (
            <div key={index} className={`chat-message chat-message-${message.role}`}>
              <Text size="xs" fw={600}>{message.role === 'user' ? 'You' : 'Exagion'}</Text>
              <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{message.content}</Text>
            </div>
          ))}
          {loading && <Text size="sm" c="dimmed" role="status">Working…</Text>}
        </div>
        <div className="query-box">
          <Textarea value={query} onChange={(e) => setQuery(e.currentTarget.value)} autosize minRows={1} maxRows={4}
            variant="unstyled" aria-label="Message Exagion" disabled={loading}
            placeholder={transcript.length ? 'Ask a follow-up or change the trip…' : 'Ask about your trip: “If I leave at 3, can I be back by 4:30?”'}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (query.trim()) run('describe'); } }} />
          <Tooltip label="Send message">
            <ActionIcon size="lg" radius="xl" variant="light" aria-label="Send message" disabled={!query.trim()}
              onClick={() => query.trim() && run('describe')} loading={loading}>
              <IconSend size={18} />
            </ActionIcon>
          </Tooltip>
        </div>
        {scenario?.note && <Text size="xs" c="dimmed" mt={6}>{scenario.note}</Text>}
        {error && <Text size="sm" c="red" mt="sm">{error}</Text>}

        {result && (
          <ScrollArea className="results" type="auto" offsetScrollbars>
            {result.ir && (
              <div className="section">
                <Group gap={6} mb={4}>
                  <IconBrain size={16} />
                  <Text fw={600} size="sm">Constraints</Text>
                  <Badge size="xs" variant="light" color={result.kind === 'live' ? 'grape' : 'gray'}>
                    {result.kind === 'live' ? `from words · ${result.model} · ${result.modelSeconds}s` : 'from the form'}
                  </Badge>
                </Group>
                <IrView ir={result.ir} />
              </div>
            )}

            {result.clarification ? (
              <div className="section clarification">
                <IconAlertTriangle size={18} />
                <div>
                  <Text size="sm">{result.clarification}</Text>
                </div>
              </div>
            ) : (
              <>
                {result.team && (() => {
                  const c = result.team.comparison;
                  const alone = c.alone.filter((a) => a.seconds != null);
                  const best = alone.length ? alone.reduce((a, b) => (a.seconds <= b.seconds ? a : b)) : null;
                  const partner = result.team.people.find((p) => p.key !== 'me');
                  return (
                    <div className="section team">
                      <div className="summary">
                        <div>
                          <Text className="big-time">Done {c.doneAt}</Text>
                          <Text size="sm" c="dimmed">{formatMinutes(c.togetherSeconds)} between you, driving and stops</Text>
                        </div>
                        {best && <Badge size="lg" variant="light" color="teal">{best.name} alone: {best.doneAt}</Badge>}
                      </div>
                      {result.team.people.map((person) => (
                        <div className="team-person" key={person.key}>
                          <Group justify="space-between" mb={4}>
                            <Group gap={6}>
                              <span className={`dot ${person.key === 'me' ? 'dot-a' : 'dot-friend'}`}>{person.name[0]}</span>
                              <Text fw={600} size="sm">{person.name}</Text>
                              <Text size="xs" c="dimmed">{person.stops.length ? `done ${person.doneAt}` : 'nothing to do'}</Text>
                            </Group>
                            {person.key !== 'me' && person.route && (
                              <Button size="compact-xs" variant="light" color="grape" onClick={() => sendToPartner(person)}>
                                {sentToPartner === result ? 'Sent' : `Send to ${person.name}`}
                              </Button>
                            )}
                          </Group>
                          {person.stops.map((s, i) => (
                            <div className="stop-row" key={i}>
                              <span className={`dot ${person.key === 'me' ? 'dot-stop' : 'dot-stop-partner'}`}>{i + 1}</span>
                              <span>
                                {s.task}: {s.label}
                                <Text span size="xs" c="dimmed"> · {s.arriveAt}{s.address ? ` · ${s.address}` : ''}</Text>
                              </span>
                            </div>
                          ))}
                        </div>
                      ))}
                      <Group gap={6} mt={6}>
                        <Button size="compact-xs" variant="subtle" onClick={() => askForLocation(partner.name)}>
                          Ask {partner.name} for location
                        </Button>
                        {partnerFix && <Text size="xs" c="dimmed">shared {Math.round(partnerFix.ageSeconds / 60)} min ago</Text>}
                      </Group>
                      {share && (
                        <Text size="xs" c="dimmed" style={{ wordBreak: 'break-all' }}>
                          Link (copied; expires in 30 min): {shareUrl(share)}
                        </Text>
                      )}
                    </div>
                  );
                })()}

                {!result.team && <div className="section summary">
                  <div>
                    <Text className="big-time">{formatMinutes(totalSeconds)}</Text>
                    <Text size="sm" c="dimmed">
                      {formatMiles(route.distanceMiles)} · {formatMinutes(route.durationSeconds)} driving
                      {result.dwellSeconds ? ` + ${formatMinutes(result.dwellSeconds)} at stops` : ''}
                    </Text>
                    {result.traffic && (
                      <Text size="xs" c={result.traffic.on ? 'teal' : 'dimmed'}>{result.traffic.label}</Text>
                    )}
                  </div>
                  {deltaMinutes != null && (
                    <Badge size="lg" variant="light" color={deltaMinutes > 0 ? 'orange' : 'teal'}>
                      {deltaMinutes > 0 ? `+${deltaMinutes}` : deltaMinutes} min vs. plain
                    </Badge>
                  )}
                </div>}

                {result.meetup && (
                  <div className="section meetup">
                    <Text fw={600} size="sm" mb={4}>Meet at {result.stops[0].label}{result.stops[0].locality ? `, ${result.stops[0].locality}` : ''} · {result.meetup.meetAt}</Text>
                    <div className="meet-grid">
                      <span className="dot dot-a">A</span>
                      <Text size="xs">You arrive {result.meetup.youArrive} · wait {result.meetup.yourWaitMinutes} min · detour {result.meetup.yourDetourMinutes} min</Text>
                      <span className="dot dot-friend">F</span>
                      <Text size="xs">Friend arrives {result.meetup.friendArrives} · wait {result.meetup.friendWaitMinutes} min · detour {result.meetup.friendDetourMinutes} min</Text>
                    </div>
                    <Text size="xs" c="dimmed" mt={4}>Together at {result.destination.label} by {result.meetup.arriveTogether}.
                      {result.meetup.alternatives.length ? ` Also good: ${result.meetup.alternatives.map((a) => `${a.label}${a.locality ? ` (${a.locality})` : ''}`).join(', ')}.` : ''}</Text>
                  </div>
                )}

                <div className="section">
                  <Group gap={6} mb={6}>
                    {result.allChecksPassed ? <IconCheck size={16} color="#2f9e44" /> : <IconCircleX size={16} color="#e03131" />}
                    <Text fw={600} size="sm">Independent check</Text>
                  </Group>
                  {result.checks.length === 0 && <Text size="xs" c="dimmed">No constraints to check.</Text>}
                  {result.checks.map((c, i) => (
                    <div key={i} className={`check ${c.passed ? 'pass' : 'fail'}`}>
                      {c.passed ? <IconCheck size={14} /> : <IconX size={14} />}
                      <span>{c.name}</span>
                      <span className="check-detail">{c.detail}</span>
                    </div>
                  ))}
                  {result.notes?.map((n, i) => <Text key={i} size="xs" c="orange.8" mt={4}>{n}</Text>)}
                </div>

                {!result.team && <div className="section">
                  <Text fw={600} size="sm" mb={6}>Stops</Text>
                  <div className="stop-row"><span className="dot dot-a">A</span>{result.origin.label}</div>
                  {result.stops.map((s, i) => (
                    <div className="stop-row" key={i}>
                      <span className="dot dot-stop">{i + 1}</span>
                      <span>
                        {s.label}
                        <Text span size="xs" c="dimmed">
                          {s.locality || s.address ? ` · ${s.address || s.locality}` : ''}
                          {s.detourSeconds != null ? ` · +${Math.max(0, s.detourSeconds / 60).toFixed(1)} min` : ''}
                        </Text>
                      </span>
                    </div>
                  ))}
                  <div className="stop-row"><span className="dot dot-b">B</span>{result.destination.label}</div>
                </div>}

                <div className="section">
                  <UnstyledButton onClick={() => setShowTrace((v) => !v)} className="section-toggle">
                    {showTrace ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
                    <Text fw={600} size="sm">Show your work</Text>
                    <Text size="xs" c="dimmed">{result.plannerMs} ms planning</Text>
                  </UnstyledButton>
                  <Collapse in={showTrace}>
                    <div className="trace">
                      {result.trace.filter((s) => s.stage !== 'interpret').map((step, i) => {
                        const Icon = STAGE_ICON[step.stage] || IconRoute;
                        return (
                          <div className="trace-step" key={i}>
                            <Icon size={14} />
                            <div>
                              <Text size="xs" fw={600}>{step.title}</Text>
                              <Text size="xs" c="dimmed">{traceSummary(step)}</Text>
                              {step.stage === 'custom' && step.attempts.length > 0 && (
                                <>
                                  {step.attempts[step.attempts.length - 1].summary && (
                                    <Text size="xs" mt={2}>{step.attempts[step.attempts.length - 1].summary}</Text>
                                  )}
                                  <pre className="custom-code">{step.attempts[step.attempts.length - 1].code}</pre>
                                </>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </Collapse>
                </div>

                <div className="section">
                  <UnstyledButton onClick={() => setShowSteps((v) => !v)} className="section-toggle">
                    {showSteps ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
                    <Text fw={600} size="sm">Directions</Text>
                    <Text size="xs" c="dimmed">{route.steps.length} maneuvers</Text>
                  </UnstyledButton>
                  <Collapse in={showSteps}>
                    {route.steps.map((s, i) => (
                      <div className="direction" key={i}>
                        <Text size="xs">{s.instruction}</Text>
                        <Text size="xs" c="dimmed">{s.distanceMiles ? formatMiles(s.distanceMiles) : ''}</Text>
                      </div>
                    ))}
                  </Collapse>
                </div>
              </>
            )}
          </ScrollArea>
        )}
        {!result && !loading && (
          <Stack gap={4} mt="md">
            <Text size="xs" c="dimmed">Valhalla · Overture places in DuckDB · OR-Tools · OpenStreetMap / OpenFreeMap</Text>
          </Stack>
        )}
        {loading && <Group mt="md" gap="xs"><Loader size="xs" /><Text size="sm">{phase}</Text></Group>}
      </aside>
    </div>
  );
};

export default PlannerApp;
