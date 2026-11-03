import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActionIcon,
  Badge,
  Box,
  Button,
  Checkbox,
  Group,
  Loader,
  NumberInput,
  ScrollArea,
  Select,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconBox,
  IconBrain,
  IconCloudRain,
  IconCurrentLocation,
  IconMapPin,
  IconPlus,
  IconRefresh,
  IconRoute,
  IconSend,
  IconTrash,
} from '@tabler/icons-react';
import { Capacitor } from '@capacitor/core';
import { Geolocation } from '@capacitor/geolocation';
import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { DEMO_OPTIONS, DEMO_SCENARIOS, getDemoScenario } from './demoScenarios';
import './TurnByTurnNavigation.css';
import { apiUrl as planApiUrl, serverOrigin } from './PlannerApp';


const ROUTING_API_BASE = (import.meta.env.VITE_ROUTING_API_URL || '').replace(/\/+$/, '');

const PROFILE_OPTIONS = [
  { value: 'auto', label: 'Auto' },
  { value: 'truck', label: 'Truck' },
  { value: 'bicycle', label: 'Bicycle' },
  { value: 'pedestrian', label: 'Pedestrian' },
  { value: 'motorcycle', label: 'Motorcycle' },
  { value: 'bus', label: 'Bus' },
];

const DEFAULT_ORIGIN = {
  label: 'Houston',
  lat: 29.7604,
  lng: -95.3698,
};

const DEFAULT_DESTINATION = {
  label: 'Pasadena',
  lat: 29.7847,
  lng: -95.1292,
};

const INITIAL_AGENT_MESSAGES = [
  {
    id: 'agent-welcome',
    role: 'assistant',
    text: 'Tell me the whole drive in plain English. I can infer the travel mode, ordered stops, and whether to avoid dangerous weather.',
  },
];

const ROUTE_SOURCE_ID = 'a-to-b-route-source';
const ROUTE_LAYER_ID = 'a-to-b-route-layer';
const WEATHER_GRID_SOURCE_ID = 'weather-grid-source';
const WEATHER_GRID_FILL_LAYER_ID = 'weather-grid-fill-layer';
const WEATHER_GRID_LINE_LAYER_ID = 'weather-grid-line-layer';
const INCLUDE_BOX_SOURCE_ID = 'include-box-source';
const INCLUDE_BOX_FILL_LAYER_ID = 'include-box-fill-layer';
const INCLUDE_BOX_LINE_LAYER_ID = 'include-box-line-layer';
const AVOID_POLYGON_SOURCE_ID = 'avoid-polygon-source';
const AVOID_POLYGON_FILL_LAYER_ID = 'avoid-polygon-fill-layer';
const AVOID_POLYGON_LINE_LAYER_ID = 'avoid-polygon-line-layer';
const WEATHER_GRID_ROWS = 24;
const WEATHER_GRID_COLS = 24;
const WEATHER_GRID_SAMPLE_ROWS = 8;
const WEATHER_GRID_SAMPLE_COLS = 8;

const EMPTY_FEATURE_COLLECTION = {
  type: 'FeatureCollection',
  features: [],
};

const toNumber = (value) => {
  const next = Number(value);
  return Number.isFinite(next) ? next : null;
};

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

// Same rules as the planner's apiUrl (inside the app: always the laptop).
const routingApiUrl = (path) => (ROUTING_API_BASE ? planApiUrl(path) : `${serverOrigin() === window.location.origin ? '' : serverOrigin()}${path}`);

// Local basemap from TileServer GL (see PlannerApp's mapStyleUrl).
const defaultMapStyle = () => import.meta.env.VITE_TURN_BY_TURN_STYLE_URL
  || `${serverOrigin()}/tiles/styles/liberty/style.json`;

const formatDistance = (miles) => {
  const next = Number(miles);
  if (!Number.isFinite(next)) return '-';
  return `${next.toFixed(next >= 10 ? 0 : 1)} mi`;
};

const formatDuration = (seconds) => {
  const next = Number(seconds);
  if (!Number.isFinite(next)) return '-';
  const minutes = Math.round(next / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rem = minutes % 60;
  return `${hours}h ${rem}m`;
};

const normalizePoint = (point, fallbackLabel) => {
  const lat = toNumber(point?.lat);
  const lng = toNumber(point?.lng);
  if (lat === null || lng === null) {
    throw new Error(`${fallbackLabel} coordinates are invalid.`);
  }
  return {
    label: point?.label || fallbackLabel,
    lat,
    lng,
  };
};

const normalizeIncludeBox = (box, index) => {
  const point = normalizePoint(box, `Include box ${index + 1}`);
  const sizeMiles = toNumber(box?.sizeMiles) ?? 5;
  return {
    id: box?.id || `include-${Date.now()}-${index}`,
    label: box?.label || `Include ${index + 1}`,
    lat: point.lat,
    lng: point.lng,
    sizeMiles: clamp(sizeMiles, 0.25, 100),
  };
};

const routeGeoJson = (route) => ({
  type: 'Feature',
  properties: {},
  geometry: {
    type: 'LineString',
    coordinates: (route?.geometry || []).map((coord) => [coord.lng, coord.lat]),
  },
});

const boxPolygonCoordinates = (box) => {
  const lat = Number(box.lat);
  const lng = Number(box.lng);
  const sizeMiles = clamp(Number(box.sizeMiles) || 5, 0.25, 100);
  const halfMiles = sizeMiles / 2;
  const latDelta = halfMiles / 69;
  const cosLat = Math.max(0.15, Math.abs(Math.cos((lat * Math.PI) / 180)));
  const lngDelta = halfMiles / (69 * cosLat);

  return [[
    [lng - lngDelta, lat - latDelta],
    [lng + lngDelta, lat - latDelta],
    [lng + lngDelta, lat + latDelta],
    [lng - lngDelta, lat + latDelta],
    [lng - lngDelta, lat - latDelta],
  ]];
};

const weatherCellCoordinates = (cell) => {
  const bounds = cell?.bounds || {};
  return [[
    [bounds.west, bounds.south],
    [bounds.east, bounds.south],
    [bounds.east, bounds.north],
    [bounds.west, bounds.north],
    [bounds.west, bounds.south],
  ]];
};

const includeBoxesGeoJson = (boxes) => ({
  type: 'FeatureCollection',
  features: boxes
    .filter((box) => toNumber(box.lat) !== null && toNumber(box.lng) !== null)
    .map((box) => ({
      type: 'Feature',
      properties: {
        id: box.id,
        label: box.label,
        sizeMiles: Number(box.sizeMiles) || 5,
      },
      geometry: {
        type: 'Polygon',
        coordinates: boxPolygonCoordinates(box),
      },
    })),
});

const weatherGridGeoJson = (grid) => ({
  type: 'FeatureCollection',
  features: (grid?.cells || [])
    .filter((cell) => cell?.bounds)
    .map((cell) => ({
      type: 'Feature',
      properties: {
        id: cell.id,
        row: cell.row,
        col: cell.col,
        severity: Number(cell.severity) || 0,
        category: cell.category || 'unknown',
        description: cell.description || '',
        temperatureF: Number(cell.temperatureF) || null,
        precipProbability: Number(cell.precipProbability) || 0,
        windGustMph: Number(cell.windGustMph) || 0,
      },
      geometry: {
        type: 'Polygon',
        coordinates: weatherCellCoordinates(cell),
      },
    })),
});

const stormCellsFromGrid = (cells = []) => (
  cells.filter((cell) => Number(cell?.severity) >= 3 && cell?.bounds)
);

const avoidPolygonsFromCells = (cells = []) => (
  stormCellsFromGrid(cells).slice(0, 20).map((cell) => {
    const { west, south, east, north } = cell.bounds;
    return [
      { lat: south, lng: west },
      { lat: south, lng: east },
      { lat: north, lng: east },
      { lat: north, lng: west },
      { lat: south, lng: west },
    ];
  })
);

const avoidPolygonsGeoJson = (polygons) => ({
  type: 'FeatureCollection',
  features: polygons.map((polygon, index) => ({
    type: 'Feature',
    properties: { id: `avoid-${index}` },
    geometry: {
      type: 'Polygon',
      coordinates: [polygon.map((point) => [point.lng, point.lat])],
    },
  })),
});

const routeBounds = (route, origin, destination, includeBoxes) => {
  const geometry = Array.isArray(route?.geometry) && route.geometry.length > 1
    ? route.geometry
    : [origin, destination, ...includeBoxes];
  const validPoints = geometry.filter((point) => (
    Number.isFinite(Number(point?.lat)) && Number.isFinite(Number(point?.lng))
  ));
  if (!validPoints.length) throw new Error('Weather grid needs valid route bounds.');

  let west = Math.min(...validPoints.map((point) => Number(point.lng)));
  let east = Math.max(...validPoints.map((point) => Number(point.lng)));
  let south = Math.min(...validPoints.map((point) => Number(point.lat)));
  let north = Math.max(...validPoints.map((point) => Number(point.lat)));
  const minSpan = 0.04;
  const lngSpan = Math.max(east - west, minSpan);
  const latSpan = Math.max(north - south, minSpan);
  const lngPad = lngSpan * 0.24;
  const latPad = latSpan * 0.24;

  west = clamp(west - lngPad, -180, 180);
  east = clamp(east + lngPad, -180, 180);
  south = clamp(south - latPad, -90, 90);
  north = clamp(north + latPad, -90, 90);

  if (east - west < minSpan) {
    const center = (east + west) / 2;
    west = clamp(center - minSpan / 2, -180, 180);
    east = clamp(center + minSpan / 2, -180, 180);
  }
  if (north - south < minSpan) {
    const center = (north + south) / 2;
    south = clamp(center - minSpan / 2, -90, 90);
    north = clamp(center + minSpan / 2, -90, 90);
  }

  return { west, south, east, north };
};

const splitAgentList = (value) => (
  value
    .split(/\s*,\s*|\s+\band\b\s+/i)
    .map((item) => item.replace(/[.;]+$/g, '').trim())
    .filter(Boolean)
);

const cleanPlaceText = (value) => (
  value
    .replace(/^(route|drive|go|navigate|take me)\s+/i, '')
    .replace(/[.;]+$/g, '')
    .trim()
);

const parseAgentCommand = (rawValue) => {
  const text = rawValue.trim().replace(/\s+/g, ' ');
  if (!text) throw new Error('Enter a route command.');

  const avoidStorms = /\bavoid(?:ing)?\s+(?:the\s+)?(?:storm|storms|weather|rain|heavy rain)\b/i.test(text);
  const profile = /\b(?:truck|trucker|semi|tanker|tractor-trailer|commercial vehicle|hazmat)\b/i.test(text)
    ? 'truck'
    : /\b(?:bike|bicycle|cycling)\b/i.test(text)
      ? 'bicycle'
      : /\b(?:walk|walking|pedestrian)\b/i.test(text)
        ? 'pedestrian'
        : /\b(?:motorcycle|motorbike)\b/i.test(text)
          ? 'motorcycle'
          : /\bbus\b/i.test(text)
            ? 'bus'
            : null;
  const includeTexts = [];
  const includeRegex = /\b(?:via|through|include|stop(?:\s+at)?|stops?\s+at)\s+(.+?)(?=\s+\b(?:avoid(?:ing)?|via|through|include|stop(?:\s+at)?|stops?\s+at)\b|$)/gi;
  let includeMatch = includeRegex.exec(text);
  while (includeMatch) {
    const value = includeMatch[1].replace(/\bavoid(?:ing)?\s+(?:the\s+)?(?:storm|storms|weather|rain|heavy rain).*$/i, '');
    includeTexts.push(...splitAgentList(value));
    includeMatch = includeRegex.exec(text);
  }

  const boundary = '(?=\\s+\\b(?:avoid(?:ing)?|via|through|include|stop|stops)\\b|$)';
  const fromToPattern = new RegExp(`\\bfrom\\s+(.+?)\\s+(?:to|->)\\s+(.+?)${boundary}`, 'i');
  const barePattern = new RegExp(`^(?:route|drive|go|navigate|take me)?\\s*(.+?)\\s+(?:to|->)\\s+(.+?)${boundary}`, 'i');
  const routeMatch = text.match(fromToPattern) || text.match(barePattern);

  if (!routeMatch) {
    if (avoidStorms) {
      return { avoidStorms, profile, originText: null, destinationText: null, includeTexts };
    }
    throw new Error('Use an origin-to-destination command.');
  }

  return {
    avoidStorms,
    profile,
    originText: cleanPlaceText(routeMatch[1]),
    destinationText: cleanPlaceText(routeMatch[2]),
    includeTexts: [...new Set(includeTexts.map(cleanPlaceText).filter(Boolean))],
  };
};

async function geocodeAddress(query) {
  const trimmed = query.trim();
  if (!trimmed) throw new Error('Enter a place or address first.');
  const response = await fetch(routingApiUrl(`/api/geocode?q=${encodeURIComponent(trimmed)}`));
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error || `Geocoder ${response.status}`);
  if (!data?.result) throw new Error(`No location found for "${trimmed}".`);

  return {
    label: data.result.label || trimmed,
    lng: data.result.lng,
    lat: data.result.lat,
  };
}

async function interpretAgentCommand(command, currentRoute) {
  const response = await fetch(routingApiUrl('/api/agent/interpret'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command, currentRoute }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error || `Route model ${response.status}`);
  if (!data?.plan) throw new Error('The local route model did not return a plan.');
  return data;
}

const getBrowserCurrentPosition = () => (
  new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error('Browser location is not available.'));
      return;
    }

    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: true,
      timeout: 15000,
    });
  })
);

const hasLocationPermission = (permissions) => (
  permissions?.location === 'granted' || permissions?.location === 'limited'
);

const getNativeCurrentPosition = async () => {
  let permissions = await Geolocation.checkPermissions();
  if (!hasLocationPermission(permissions)) {
    permissions = await Geolocation.requestPermissions({ permissions: ['location'] });
  }
  if (!hasLocationPermission(permissions)) {
    throw new Error('Location permission was not granted.');
  }

  return Geolocation.getCurrentPosition({
    enableHighAccuracy: true,
    timeout: 15000,
    maximumAge: 30000,
  });
};

const getCurrentPosition = () => (
  Capacitor.isNativePlatform()
    ? getNativeCurrentPosition()
    : getBrowserCurrentPosition()
);

const TurnByTurnNavigation = () => {
  const mapHostRef = useRef(null);
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const originMarkerRef = useRef(null);
  const destinationMarkerRef = useRef(null);
  const weatherPopupRef = useRef(null);
  const autoSolveTimerRef = useRef(null);
  const routeRequestIdRef = useRef(0);
  const weatherGridRequestIdRef = useRef(0);
  const suppressNextAutoSolveRef = useRef(false);
  const stateRef = useRef({});

  const [mapReady, setMapReady] = useState(false);
  const [origin, setOrigin] = useState(DEFAULT_ORIGIN);
  const [destination, setDestination] = useState(DEFAULT_DESTINATION);
  const [originQuery, setOriginQuery] = useState(DEFAULT_ORIGIN.label);
  const [destinationQuery, setDestinationQuery] = useState(DEFAULT_DESTINATION.label);
  const [profile, setProfile] = useState('auto');
  const [route, setRoute] = useState(null);
  const [loadingRoute, setLoadingRoute] = useState(false);
  const [routeError, setRouteError] = useState('');
  const [loadingOrigin, setLoadingOrigin] = useState(false);
  const [loadingDestination, setLoadingDestination] = useState(false);
  const [includeBoxes, setIncludeBoxes] = useState([]);
  const [avoidStorms, setAvoidStorms] = useState(false);
  const [weatherGrid, setWeatherGrid] = useState(null);
  const [loadingWeather, setLoadingWeather] = useState(false);
  const [weatherError, setWeatherError] = useState('');
  const [agentInput, setAgentInput] = useState(DEMO_SCENARIOS[0].query);
  const [agentMessages, setAgentMessages] = useState(INITIAL_AGENT_MESSAGES);
  const [agentLoading, setAgentLoading] = useState(false);
  const [agentStatus, setAgentStatus] = useState('Ready');
  const [agentRuntime, setAgentRuntime] = useState('Local Qwen via Ollama');
  const [demoScenarioId, setDemoScenarioId] = useState(DEMO_SCENARIOS[0].id);

  const selectedDemoScenario = useMemo(
    () => getDemoScenario(demoScenarioId),
    [demoScenarioId],
  );

  useEffect(() => {
    stateRef.current = {
      origin,
      destination,
      includeBoxes,
      route,
      weatherGrid,
      avoidStorms,
      profile,
    };
  }, [avoidStorms, destination, includeBoxes, origin, profile, route, weatherGrid]);

  const includeBoxesKey = useMemo(() => (
    includeBoxes
      .map((box) => `${box.id}:${box.lat}:${box.lng}:${box.sizeMiles}:${box.label}`)
      .join('|')
  ), [includeBoxes]);

  const stormCells = useMemo(() => stormCellsFromGrid(weatherGrid?.cells || []), [weatherGrid]);
  const currentAvoidPolygons = useMemo(() => (
    avoidStorms ? avoidPolygonsFromCells(weatherGrid?.cells || []) : []
  ), [avoidStorms, weatherGrid]);

  const summary = useMemo(() => {
    if (!route) return null;
    return {
      distance: formatDistance(route.distanceMiles),
      duration: formatDuration(route.durationSeconds),
      steps: Array.isArray(route.steps) ? route.steps.length : 0,
    };
  }, [route]);

  const gridSummary = useMemo(() => {
    const cells = weatherGrid?.cells || [];
    return {
      total: cells.length,
      watched: cells.filter((cell) => Number(cell.severity) > 0 && Number(cell.severity) < 3).length,
      blocked: cells.filter((cell) => Number(cell.severity) >= 3).length,
    };
  }, [weatherGrid]);

  const fitMapToPoints = useCallback((points, padding = 72) => {
    const map = mapRef.current;
    const validPoints = points.filter((point) => (
      Number.isFinite(Number(point?.lat)) && Number.isFinite(Number(point?.lng))
    ));
    if (!map || validPoints.length === 0) return;

    const bounds = new maplibregl.LngLatBounds();
    validPoints.forEach((point) => bounds.extend([Number(point.lng), Number(point.lat)]));
    map.fitBounds(bounds, {
      padding,
      maxZoom: validPoints.length === 1 ? 12 : 15,
      duration: 600,
    });
  }, []);

  const drawRoute = useCallback((nextRoute) => {
    const map = mapRef.current;
    if (!map || !Array.isArray(nextRoute?.geometry) || nextRoute.geometry.length < 2) return;

    const data = routeGeoJson(nextRoute);
    const existingSource = map.getSource(ROUTE_SOURCE_ID);
    if (existingSource) {
      existingSource.setData(data);
    } else {
      map.addSource(ROUTE_SOURCE_ID, {
        type: 'geojson',
        data,
      });
      map.addLayer({
        id: ROUTE_LAYER_ID,
        type: 'line',
        source: ROUTE_SOURCE_ID,
        layout: {
          'line-join': 'round',
          'line-cap': 'round',
        },
        paint: {
          'line-color': '#1c5d99',
          'line-width': 5,
          'line-opacity': 0.92,
        },
      });
    }

    fitMapToPoints([
      ...nextRoute.geometry,
      ...stateRef.current.includeBoxes,
    ], 72);
  }, [fitMapToPoints]);

  const drawWeatherGrid = useCallback((grid) => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    const data = weatherGridGeoJson(grid);
    const source = map.getSource(WEATHER_GRID_SOURCE_ID);
    if (source) {
      source.setData(data);
      return;
    }

    map.addSource(WEATHER_GRID_SOURCE_ID, {
      type: 'geojson',
      data,
    });
    const beforeRoute = map.getLayer(ROUTE_LAYER_ID) ? ROUTE_LAYER_ID : undefined;
    map.addLayer({
      id: WEATHER_GRID_FILL_LAYER_ID,
      type: 'fill',
      source: WEATHER_GRID_SOURCE_ID,
      paint: {
        'fill-color': [
          'match',
          ['get', 'severity'],
          4,
          '#b91c1c',
          3,
          '#ef4444',
          2,
          '#f59e0b',
          1,
          '#38bdf8',
          '#d7edf3',
        ],
        'fill-opacity': [
          'case',
          ['>=', ['get', 'severity'], 3],
          0.48,
          ['==', ['get', 'severity'], 2],
          0.34,
          ['==', ['get', 'severity'], 1],
          0.22,
          0.1,
        ],
      },
    }, beforeRoute);
    map.addLayer({
      id: WEATHER_GRID_LINE_LAYER_ID,
      type: 'line',
      source: WEATHER_GRID_SOURCE_ID,
      paint: {
        'line-color': '#334155',
        'line-width': [
          'case',
          ['>=', ['get', 'severity'], 3],
          1.4,
          0.8,
        ],
        'line-opacity': 0.38,
      },
    }, beforeRoute);

    weatherPopupRef.current = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 10,
    });

    map.on('mousemove', WEATHER_GRID_FILL_LAYER_ID, (event) => {
      const feature = event.features?.[0];
      if (!feature) return;
      const props = feature.properties || {};
      const temperature = Number(props.temperatureF);
      const rain = Number(props.precipProbability);
      const gust = Number(props.windGustMph);
      const row = Number(props.row) + 1;
      const col = Number(props.col) + 1;
      map.getCanvas().style.cursor = 'crosshair';
      weatherPopupRef.current
        .setLngLat(event.lngLat)
        .setHTML(`
          <div class="route-weather-popup">
            <strong>Cell ${row}-${col}</strong>
            <span>${props.description || props.category || 'Weather'}</span>
            <span>Severity ${props.severity ?? '-'}</span>
            <span>${Number.isFinite(temperature) ? `${Math.round(temperature)} F` : '-'} temp</span>
            <span>${Number.isFinite(rain) ? `${Math.round(rain)}%` : '-'} rain</span>
            <span>${Number.isFinite(gust) ? `${Math.round(gust)} mph` : '-'} gust</span>
          </div>
        `)
        .addTo(map);
    });

    map.on('mouseleave', WEATHER_GRID_FILL_LAYER_ID, () => {
      map.getCanvas().style.cursor = '';
      weatherPopupRef.current?.remove();
    });
  }, []);

  const drawIncludeBoxes = useCallback((boxes) => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    const data = includeBoxesGeoJson(boxes);
    const source = map.getSource(INCLUDE_BOX_SOURCE_ID);
    if (source) {
      source.setData(data);
      return;
    }

    map.addSource(INCLUDE_BOX_SOURCE_ID, {
      type: 'geojson',
      data,
    });
    map.addLayer({
      id: INCLUDE_BOX_FILL_LAYER_ID,
      type: 'fill',
      source: INCLUDE_BOX_SOURCE_ID,
      paint: {
        'fill-color': '#1c5d99',
        'fill-opacity': 0.13,
      },
    });
    map.addLayer({
      id: INCLUDE_BOX_LINE_LAYER_ID,
      type: 'line',
      source: INCLUDE_BOX_SOURCE_ID,
      paint: {
        'line-color': '#1c5d99',
        'line-width': 2,
        'line-dasharray': [2, 1],
      },
    });
  }, []);

  const drawAvoidPolygons = useCallback((polygons) => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    const data = avoidPolygonsGeoJson(polygons);
    const source = map.getSource(AVOID_POLYGON_SOURCE_ID);
    if (source) {
      source.setData(data);
      return;
    }

    map.addSource(AVOID_POLYGON_SOURCE_ID, {
      type: 'geojson',
      data,
    });
    map.addLayer({
      id: AVOID_POLYGON_FILL_LAYER_ID,
      type: 'fill',
      source: AVOID_POLYGON_SOURCE_ID,
      paint: {
        'fill-color': '#7f1d1d',
        'fill-opacity': 0.2,
      },
    });
    map.addLayer({
      id: AVOID_POLYGON_LINE_LAYER_ID,
      type: 'line',
      source: AVOID_POLYGON_SOURCE_ID,
      paint: {
        'line-color': '#7f1d1d',
        'line-width': 2.6,
      },
    });
  }, []);

  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return undefined;

    const map = new maplibregl.Map({
      container: mapContainerRef.current,
      style: defaultMapStyle(),
      center: [DEFAULT_ORIGIN.lng, DEFAULT_ORIGIN.lat],
      zoom: 9,
    });

    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');

    map.on('load', () => setMapReady(true));
    map.on('error', (event) => {
      console.warn('Route planner MapLibre error', event?.error || event);
    });

    const resizeObserver = new ResizeObserver(() => map.resize());
    if (mapHostRef.current) resizeObserver.observe(mapHostRef.current);

    return () => {
      if (autoSolveTimerRef.current) window.clearTimeout(autoSolveTimerRef.current);
      resizeObserver.disconnect();
      setMapReady(false);
      originMarkerRef.current?.remove();
      destinationMarkerRef.current?.remove();
      weatherPopupRef.current?.remove();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map) return;
    if (!originMarkerRef.current) {
      originMarkerRef.current = new maplibregl.Marker({ color: '#1c5d99' });
    }
    originMarkerRef.current.setLngLat([origin.lng, origin.lat]).addTo(map);
  }, [mapReady, origin.lat, origin.lng]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map) return;
    if (!destinationMarkerRef.current) {
      destinationMarkerRef.current = new maplibregl.Marker({ color: '#d9480f' });
    }
    destinationMarkerRef.current.setLngLat([destination.lng, destination.lat]).addTo(map);
  }, [destination.lat, destination.lng, mapReady]);

  useEffect(() => {
    if (!mapReady) return;
    drawWeatherGrid(weatherGrid);
  }, [drawWeatherGrid, mapReady, weatherGrid]);

  useEffect(() => {
    if (!mapReady) return;
    drawIncludeBoxes(includeBoxes);
  }, [drawIncludeBoxes, includeBoxes, includeBoxesKey, mapReady]);

  useEffect(() => {
    if (!mapReady) return;
    drawAvoidPolygons(currentAvoidPolygons);
  }, [currentAvoidPolygons, drawAvoidPolygons, mapReady]);

  const setPointCoordinate = useCallback((kind, field, value) => {
    const setter = kind === 'origin' ? setOrigin : setDestination;
    setter((current) => ({
      ...current,
      [field]: value,
    }));
  }, []);

  const resolveAddress = useCallback(async (kind) => {
    const isOrigin = kind === 'origin';
    const query = isOrigin ? originQuery : destinationQuery;
    const setLoading = isOrigin ? setLoadingOrigin : setLoadingDestination;
    const setPoint = isOrigin ? setOrigin : setDestination;

    setLoading(true);
    setRouteError('');
    try {
      const point = await geocodeAddress(query);
      setPoint(point);
      if (isOrigin) {
        setOriginQuery(point.label);
      } else {
        setDestinationQuery(point.label);
      }
      return point;
    } catch (error) {
      setRouteError(error.message || String(error));
      return null;
    } finally {
      setLoading(false);
    }
  }, [destinationQuery, originQuery]);

  const useCurrentLocationAsOrigin = useCallback(async () => {
    setLoadingOrigin(true);
    setRouteError('');

    try {
      const position = await getCurrentPosition();
      const nextOrigin = {
        label: 'Current location',
        lat: position.coords.latitude,
        lng: position.coords.longitude,
      };
      setOrigin(nextOrigin);
      setOriginQuery(nextOrigin.label);
    } catch (error) {
      setRouteError(error.message || 'Could not read current location.');
    } finally {
      setLoadingOrigin(false);
    }
  }, []);

  const loadWeatherGridFor = useCallback(async (
    nextRoute = stateRef.current.route,
    nextOrigin = stateRef.current.origin,
    nextDestination = stateRef.current.destination,
    nextIncludeBoxes = stateRef.current.includeBoxes,
  ) => {
    const requestId = weatherGridRequestIdRef.current + 1;
    weatherGridRequestIdRef.current = requestId;
    setLoadingWeather(true);
    setWeatherError('');

    try {
      const bounds = routeBounds(nextRoute, nextOrigin, nextDestination, nextIncludeBoxes || []);
      const response = await fetch(routingApiUrl('/api/weather/grid'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bounds,
          rows: WEATHER_GRID_ROWS,
          cols: WEATHER_GRID_COLS,
          sampleRows: WEATHER_GRID_SAMPLE_ROWS,
          sampleCols: WEATHER_GRID_SAMPLE_COLS,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.error || `Weather grid ${response.status}`);
      if (requestId !== weatherGridRequestIdRef.current) return data;
      setWeatherGrid(data);
      drawWeatherGrid(data);
      return data;
    } catch (error) {
      if (requestId === weatherGridRequestIdRef.current) {
        console.error('Weather grid failed', error);
        setWeatherError(error.message || String(error));
      }
      return null;
    } finally {
      if (requestId === weatherGridRequestIdRef.current) {
        setLoadingWeather(false);
      }
    }
  }, [drawWeatherGrid]);

  const solveRoute = useCallback(async (overrides = {}) => {
    if (!mapReady) return null;

    const requestId = routeRequestIdRef.current + 1;
    routeRequestIdRef.current = requestId;
    setLoadingRoute(true);
    setRouteError('');
    setWeatherError('');

    try {
      const nextOrigin = normalizePoint(overrides.origin ?? stateRef.current.origin, 'Origin');
      const nextDestination = normalizePoint(overrides.destination ?? stateRef.current.destination, 'Destination');
      const nextIncludeBoxes = (overrides.includeBoxes ?? stateRef.current.includeBoxes ?? [])
        .map((box, index) => normalizeIncludeBox(box, index));
      const shouldAvoidStorms = overrides.avoidStorms ?? stateRef.current.avoidStorms;
      const selectedWeatherCells = overrides.avoidCells ?? stateRef.current.weatherGrid?.cells ?? [];
      const avoidPolygons = shouldAvoidStorms ? avoidPolygonsFromCells(selectedWeatherCells) : [];

      const response = await fetch(routingApiUrl('/api/routes/a-to-b'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          origin: nextOrigin,
          destination: nextDestination,
          includeBoxes: nextIncludeBoxes,
          avoidPolygons,
          profile: overrides.profile ?? stateRef.current.profile,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.error || `Route ${response.status}`);
      if (!data?.route) throw new Error('Routing backend did not return a route.');
      if (requestId !== routeRequestIdRef.current) return data.route;

      setRoute(data.route);
      drawRoute(data.route);
      void loadWeatherGridFor(data.route, nextOrigin, nextDestination, nextIncludeBoxes);
      return data.route;
    } catch (error) {
      if (requestId !== routeRequestIdRef.current) return null;
      console.error('A-to-B route failed', error);
      setRouteError(error.message || String(error));
      void loadWeatherGridFor(null, stateRef.current.origin, stateRef.current.destination, stateRef.current.includeBoxes);
      return null;
    } finally {
      if (requestId === routeRequestIdRef.current) {
        setLoadingRoute(false);
      }
    }
  }, [drawRoute, loadWeatherGridFor, mapReady]);

  const addIncludeBox = useCallback((point = null, label = null) => {
    const routeGeometry = stateRef.current.route?.geometry || [];
    const midpoint = point || routeGeometry[Math.floor(routeGeometry.length / 2)] || {
      lat: (Number(stateRef.current.origin.lat) + Number(stateRef.current.destination.lat)) / 2,
      lng: (Number(stateRef.current.origin.lng) + Number(stateRef.current.destination.lng)) / 2,
    };
    setIncludeBoxes((current) => ([
      ...current,
      {
        id: `include-${Date.now()}-${current.length}`,
        label: label || `Include ${current.length + 1}`,
        lat: Number(midpoint.lat),
        lng: Number(midpoint.lng),
        sizeMiles: 5,
      },
    ]));
  }, []);

  const updateIncludeBox = useCallback((id, patch) => {
    setIncludeBoxes((current) => current.map((box) => (
      box.id === id ? { ...box, ...patch } : box
    )));
  }, []);

  const removeIncludeBox = useCallback((id) => {
    setIncludeBoxes((current) => current.filter((box) => box.id !== id));
  }, []);

  const handleAvoidStormsChange = useCallback(async (checked) => {
    suppressNextAutoSolveRef.current = true;
    setAvoidStorms(checked);
    if (checked) {
      const grid = await loadWeatherGridFor();
      await solveRoute({ avoidStorms: true, avoidCells: grid?.cells });
    } else {
      await solveRoute({ avoidStorms: false });
    }
  }, [loadWeatherGridFor, solveRoute]);

  const pushAgentMessage = useCallback((role, text) => {
    setAgentMessages((current) => ([
      ...current.slice(-9),
      {
        id: `agent-${Date.now()}-${current.length}`,
        role,
        text,
      },
    ]));
  }, []);

  const sendAgentMessage = useCallback(async (demoScenario = null) => {
    const rehearsedScenario = demoScenario?.plan ? demoScenario : null;
    const command = (rehearsedScenario?.query || agentInput).trim();
    if (!command || agentLoading) return;

    pushAgentMessage('user', command);
    setAgentInput('');
    setAgentLoading(true);
    setAgentStatus('Working');
    setRouteError('');
    pushAgentMessage('assistant', 'Got it. I am turning that into route inputs now.');

    try {
      let parsed;
      if (rehearsedScenario) {
        parsed = rehearsedScenario.plan;
        setAgentRuntime('Rehearsed deterministic plan');
        pushAgentMessage('assistant', 'Using the rehearsed route plan so this recording is repeatable.');
      } else {
        try {
          const interpreted = await interpretAgentCommand(command, {
            origin: stateRef.current.origin?.label,
            destination: stateRef.current.destination?.label,
            stops: stateRef.current.includeBoxes?.map((stop) => stop.label),
            profile: stateRef.current.profile,
            avoidStorms: stateRef.current.avoidStorms,
          });
          parsed = interpreted.plan;
          setAgentRuntime(`${interpreted.model} via ${interpreted.runtime}`);
        } catch (modelError) {
          parsed = parseAgentCommand(command);
          setAgentRuntime('Built-in offline parser');
          pushAgentMessage('assistant', `The local model is offline, so I used the basic offline parser. ${modelError.message}`);
        }
      }

      if (parsed.clarification) throw new Error(parsed.clarification);
      if ((parsed.originText && !parsed.destinationText) || (!parsed.originText && parsed.destinationText)) {
        throw new Error('Please give both an origin and a destination.');
      }

      let nextOrigin = stateRef.current.origin;
      let nextDestination = stateRef.current.destination;
      let nextIncludeBoxes = parsed.originText && parsed.destinationText
        ? []
        : stateRef.current.includeBoxes;
      const nextProfile = parsed.profile || stateRef.current.profile || 'auto';

      if (parsed.originText && parsed.destinationText) {
        pushAgentMessage('assistant', `Finding ${parsed.originText} and ${parsed.destinationText}.`);
        const [geocodedOrigin, geocodedDestination] = await Promise.all([
          geocodeAddress(parsed.originText),
          geocodeAddress(parsed.destinationText),
        ]);
        nextOrigin = geocodedOrigin;
        nextDestination = geocodedDestination;
      }

      if (parsed.includeTexts.length > 0) {
        pushAgentMessage('assistant', `Adding ${parsed.includeTexts.length} required stop${parsed.includeTexts.length === 1 ? '' : 's'} as include boxes.`);
        const stops = await Promise.all(parsed.includeTexts.map((text) => geocodeAddress(text)));
        nextIncludeBoxes = stops.map((stop, index) => ({
          id: `include-agent-${Date.now()}-${index}`,
          label: stop.label,
          lat: stop.lat,
          lng: stop.lng,
          sizeMiles: 5,
        }));
      }

      suppressNextAutoSolveRef.current = true;
      setOrigin(nextOrigin);
      setOriginQuery(nextOrigin.label);
      setDestination(nextDestination);
      setDestinationQuery(nextDestination.label);
      setIncludeBoxes(nextIncludeBoxes);
      setAvoidStorms(parsed.avoidStorms);
      setProfile(nextProfile);

      pushAgentMessage('assistant', parsed.avoidStorms ? 'Checking the weather grid before solving around storm cells.' : 'Checking the weather grid, then solving the route.');
      const grid = await loadWeatherGridFor(null, nextOrigin, nextDestination, nextIncludeBoxes);
      pushAgentMessage('assistant', 'Solving the Valhalla route now.');
      const solved = await solveRoute({
        origin: nextOrigin,
        destination: nextDestination,
        includeBoxes: nextIncludeBoxes,
        profile: nextProfile,
        avoidStorms: parsed.avoidStorms,
        avoidCells: grid?.cells,
      });
      setAgentStatus(solved ? 'Done' : 'No route');
      if (solved) {
        const warning = solved.avoidanceWarning ? ` ${solved.avoidanceWarning}` : '';
        pushAgentMessage(
          'assistant',
          `Done. Route is ${formatDistance(solved.distanceMiles)} and ${formatDuration(solved.durationSeconds)} with ${(solved.steps || []).length} maneuvers.${warning}`,
        );
      } else {
        pushAgentMessage('assistant', 'I could not get a route back for that command.');
      }
    } catch (error) {
      setAgentStatus('Needs input');
      setRouteError(error.message || String(error));
      pushAgentMessage('assistant', error.message || String(error));
    } finally {
      setAgentLoading(false);
    }
  }, [agentInput, agentLoading, loadWeatherGridFor, pushAgentMessage, solveRoute]);

  useEffect(() => {
    if (!mapReady) return undefined;
    if (toNumber(origin.lat) === null || toNumber(origin.lng) === null) return undefined;
    if (toNumber(destination.lat) === null || toNumber(destination.lng) === null) return undefined;

    if (suppressNextAutoSolveRef.current) {
      suppressNextAutoSolveRef.current = false;
      return undefined;
    }

    if (autoSolveTimerRef.current) window.clearTimeout(autoSolveTimerRef.current);
    autoSolveTimerRef.current = window.setTimeout(() => {
      void solveRoute();
    }, 700);

    return () => {
      if (autoSolveTimerRef.current) window.clearTimeout(autoSolveTimerRef.current);
    };
  }, [
    avoidStorms,
    destination.lat,
    destination.lng,
    includeBoxesKey,
    mapReady,
    origin.lat,
    origin.lng,
    profile,
    solveRoute,
  ]);

  return (
    <Box className="route-planner">
      <Box className="route-map-shell" ref={mapHostRef}>
        <div ref={mapContainerRef} className="route-map-canvas" />

        {!mapReady && (
          <Box className="route-map-loading">
            <Loader size="sm" />
            <Text size="sm" fw={600}>Loading map</Text>
          </Box>
        )}

        <Group className="route-status-bar" gap="xs">
          <Badge variant="filled" color={routeError ? 'red' : route ? 'green' : 'blue'}>
            {routeError ? 'Route failed' : route ? 'Route ready' : 'Ready'}
          </Badge>
          {summary && (
            <>
              <Badge variant="light" color="gray">{summary.distance}</Badge>
              <Badge variant="light" color="gray">{summary.duration}</Badge>
              <Badge variant="light" color="gray">{summary.steps} steps</Badge>
            </>
          )}
          {avoidStorms && (
            <Badge variant="filled" color={stormCells.length > 0 ? 'red' : 'green'}>
              {stormCells.length} storm cells
            </Badge>
          )}
        </Group>

        <Box className="route-weather-legend">
          <Group gap="xs" align="center">
            <IconCloudRain size={17} />
            <Text size="sm" fw={700}>Weather grid</Text>
            {loadingWeather && <Loader size={14} />}
          </Group>
          <Box className="route-grid-key" mt={8}>
            <span className="grid-key-cell grid-key-cell-clear" />
            <span className="grid-key-cell grid-key-cell-watch" />
            <span className="grid-key-cell grid-key-cell-caution" />
            <span className="grid-key-cell grid-key-cell-blocked" />
          </Box>
        </Box>
      </Box>

      <Box className="route-control-panel">
        <Stack gap="md">
          <Group justify="space-between" align="center">
            <Group gap="xs">
              <IconRoute size={20} />
              <Text fw={700}>A-to-B route</Text>
            </Group>
            <Tooltip label="Fit route">
              <ActionIcon
                variant="subtle"
                color="gray"
                disabled={!route}
                onClick={() => fitMapToPoints(route?.geometry || [origin, destination], 72)}
                aria-label="Fit route"
              >
                <IconRefresh size={18} />
              </ActionIcon>
            </Tooltip>
          </Group>

          <Box className="route-panel route-agent-panel">
            <Stack gap="xs">
              <Group justify="space-between" align="center">
                <Group gap="xs">
                  <IconBrain size={18} />
                  <Text size="sm" fw={700}>Open route agent</Text>
                </Group>
                <Group gap={6}>
                  <Badge color="teal" variant="light">Open-source</Badge>
                  {agentStatus && (
                    <Badge color={agentStatus === 'Needs input' ? 'red' : agentStatus === 'Working' ? 'blue' : 'green'} variant="light">
                      {agentStatus}
                    </Badge>
                  )}
                </Group>
              </Group>

              <Box className="route-demo-picker">
                <Stack gap={6}>
                  <Group gap="xs" wrap="nowrap" align="end">
                    <Select
                      label="Rehearsed demo"
                      value={demoScenarioId}
                      data={DEMO_OPTIONS}
                      onChange={(value) => {
                        const scenario = getDemoScenario(value);
                        setDemoScenarioId(value || '');
                        if (scenario) setAgentInput(scenario.query);
                      }}
                      allowDeselect={false}
                      className="route-agent-input"
                    />
                    <Button
                      variant="light"
                      onClick={() => void sendAgentMessage(selectedDemoScenario)}
                      disabled={agentLoading || !mapReady || !selectedDemoScenario}
                    >
                      Run demo
                    </Button>
                  </Group>
                  {selectedDemoScenario && (
                    <Text size="xs" c="dimmed">{selectedDemoScenario.talkingPoints}</Text>
                  )}
                </Stack>
              </Box>

              <ScrollArea className="route-agent-chat" type="auto">
                <Stack gap={8}>
                  {agentMessages.map((message) => (
                    <Box
                      className={`route-agent-message route-agent-message-${message.role}`}
                      key={message.id}
                    >
                      <Text size="xs" fw={700}>
                        {message.role === 'user' ? 'You' : 'Agent'}
                      </Text>
                      <Text size="sm">{message.text}</Text>
                    </Box>
                  ))}
                  {agentLoading && (
                    <Box className="route-agent-message route-agent-message-assistant">
                      <Group gap={6}>
                        <Loader size={12} />
                        <Text size="sm">Working...</Text>
                      </Group>
                    </Box>
                  )}
                </Stack>
              </ScrollArea>

              <Group gap="xs" wrap="nowrap" align="end">
                <TextInput
                  label="Ask"
                  value={agentInput}
                  onChange={(event) => setAgentInput(event.currentTarget.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault();
                      void sendAgentMessage(null);
                    }
                  }}
                  className="route-agent-input"
                />
                <Tooltip label="Send command">
                  <ActionIcon
                    variant="filled"
                    color="blue"
                    size={36}
                    onClick={() => void sendAgentMessage(null)}
                    disabled={agentLoading || !mapReady || !agentInput.trim()}
                    aria-label="Send route command"
                  >
                    {agentLoading ? <Loader size={15} color="white" /> : <IconSend size={17} />}
                  </ActionIcon>
                </Tooltip>
              </Group>
              <Text size="xs" c="dimmed">Interpreter: {agentRuntime}</Text>
            </Stack>
          </Box>

          <Box className="route-panel">
            <Stack gap="xs">
              <Group justify="space-between" align="center">
                <Text size="sm" fw={700}>Origin</Text>
                <Button
                  size="xs"
                  variant="subtle"
                  leftSection={<IconCurrentLocation size={14} />}
                  onClick={useCurrentLocationAsOrigin}
                  loading={loadingOrigin}
                >
                  Current
                </Button>
              </Group>
              <Group align="end" gap="xs" wrap="nowrap">
                <TextInput
                  label="Place"
                  value={originQuery}
                  onChange={(event) => setOriginQuery(event.currentTarget.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void resolveAddress('origin');
                  }}
                  className="route-place-input"
                />
                <Button
                  variant="light"
                  onClick={() => resolveAddress('origin')}
                  loading={loadingOrigin}
                >
                  Find
                </Button>
              </Group>
              <Group grow>
                <NumberInput
                  label="Lat"
                  value={origin.lat}
                  onChange={(value) => setPointCoordinate('origin', 'lat', value)}
                  decimalScale={6}
                  leftSection={<IconMapPin size={15} />}
                />
                <NumberInput
                  label="Lng"
                  value={origin.lng}
                  onChange={(value) => setPointCoordinate('origin', 'lng', value)}
                  decimalScale={6}
                />
              </Group>
            </Stack>
          </Box>

          <Box className="route-panel">
            <Stack gap="xs">
              <Text size="sm" fw={700}>Destination</Text>
              <Group align="end" gap="xs" wrap="nowrap">
                <TextInput
                  label="Place"
                  value={destinationQuery}
                  onChange={(event) => setDestinationQuery(event.currentTarget.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void resolveAddress('destination');
                  }}
                  className="route-place-input"
                />
                <Button
                  variant="light"
                  onClick={() => resolveAddress('destination')}
                  loading={loadingDestination}
                >
                  Find
                </Button>
              </Group>
              <Group grow>
                <NumberInput
                  label="Lat"
                  value={destination.lat}
                  onChange={(value) => setPointCoordinate('destination', 'lat', value)}
                  decimalScale={6}
                  leftSection={<IconMapPin size={15} />}
                />
                <NumberInput
                  label="Lng"
                  value={destination.lng}
                  onChange={(value) => setPointCoordinate('destination', 'lng', value)}
                  decimalScale={6}
                />
              </Group>
            </Stack>
          </Box>

          <Box className="route-panel route-include-panel">
            <Stack gap="xs">
              <Group justify="space-between" align="center">
                <Group gap="xs">
                  <IconBox size={18} />
                  <Text size="sm" fw={700}>Include boxes</Text>
                  <Badge variant="light" color="blue">{includeBoxes.length}</Badge>
                </Group>
                <Tooltip label="Add include box">
                  <ActionIcon
                    variant="light"
                    color="blue"
                    onClick={() => addIncludeBox()}
                    aria-label="Add include box"
                  >
                    <IconPlus size={17} />
                  </ActionIcon>
                </Tooltip>
              </Group>

              {includeBoxes.length === 0 && (
                <Box className="route-empty-row">
                  <Text size="xs" c="dimmed">None</Text>
                </Box>
              )}

              {includeBoxes.map((box, index) => (
                <Box className="route-include-row" key={box.id}>
                  <Group justify="space-between" align="center" mb={8}>
                    <TextInput
                      label={`Box ${index + 1}`}
                      value={box.label}
                      onChange={(event) => updateIncludeBox(box.id, { label: event.currentTarget.value })}
                      className="route-place-input"
                    />
                    <Tooltip label="Remove include box">
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        mt={24}
                        onClick={() => removeIncludeBox(box.id)}
                        aria-label="Remove include box"
                      >
                        <IconTrash size={17} />
                      </ActionIcon>
                    </Tooltip>
                  </Group>
                  <Group grow>
                    <NumberInput
                      label="Lat"
                      value={box.lat}
                      onChange={(value) => updateIncludeBox(box.id, { lat: value })}
                      decimalScale={6}
                    />
                    <NumberInput
                      label="Lng"
                      value={box.lng}
                      onChange={(value) => updateIncludeBox(box.id, { lng: value })}
                      decimalScale={6}
                    />
                    <NumberInput
                      label="Miles"
                      value={box.sizeMiles}
                      min={0.25}
                      max={100}
                      step={0.25}
                      onChange={(value) => updateIncludeBox(box.id, { sizeMiles: value })}
                    />
                  </Group>
                </Box>
              ))}
            </Stack>
          </Box>

          <Group align="end" grow>
            <Select
              label="Profile"
              value={profile}
              data={PROFILE_OPTIONS}
              onChange={(value) => setProfile(value || 'auto')}
              allowDeselect={false}
            />
            <Button
              leftSection={loadingRoute ? <Loader size={14} /> : <IconRoute size={17} />}
              onClick={() => solveRoute()}
              disabled={!mapReady || loadingRoute}
            >
              {loadingRoute ? 'Solving' : 'Solve route'}
            </Button>
          </Group>

          {summary && (
            <Box className="route-summary-panel">
              <Group gap="xs">
                <Badge variant="filled" color="blue">{summary.distance}</Badge>
                <Badge variant="light" color="blue">{summary.duration}</Badge>
                <Badge variant="light" color="gray">{summary.steps} maneuver(s)</Badge>
              </Group>
            </Box>
          )}

          {route?.avoidanceWarning && (
            <Box className="route-warning">
              <Group gap={6} align="flex-start" wrap="nowrap">
                <IconAlertTriangle size={15} />
                <Text size="xs">{route.avoidanceWarning}</Text>
              </Group>
            </Box>
          )}

          <Box className="route-weather-panel">
            <Stack gap="xs">
              <Group justify="space-between" align="center">
                <Group gap="xs">
                  <IconCloudRain size={18} />
                  <Text size="sm" fw={700}>Weather occlusion</Text>
                  {gridSummary.blocked > 0 ? (
                    <Badge color="red" variant="light">{gridSummary.blocked} blocked</Badge>
                  ) : weatherGrid ? (
                    <Badge color="green" variant="light">Open</Badge>
                  ) : null}
                </Group>
                <Tooltip label="Refresh weather grid">
                  <ActionIcon
                    variant="light"
                    color="blue"
                    onClick={() => loadWeatherGridFor()}
                    disabled={loadingWeather}
                    aria-label="Refresh weather grid"
                  >
                    {loadingWeather ? <Loader size={14} /> : <IconRefresh size={17} />}
                  </ActionIcon>
                </Tooltip>
              </Group>

              <Checkbox
                checked={avoidStorms}
                onChange={(event) => void handleAvoidStormsChange(event.currentTarget.checked)}
                label="Avoid storm cells"
              />

              <Box className="route-grid-metrics">
                <Box>
                  <Text size="xs" c="dimmed">Cells</Text>
                  <Text size="lg" fw={800}>{gridSummary.total}</Text>
                </Box>
                <Box>
                  <Text size="xs" c="dimmed">Watch</Text>
                  <Text size="lg" fw={800}>{gridSummary.watched}</Text>
                </Box>
                <Box>
                  <Text size="xs" c="dimmed">Blocked</Text>
                  <Text size="lg" fw={800}>{gridSummary.blocked}</Text>
                </Box>
              </Box>

              {weatherError && (
                <Box className="route-weather-error">
                  <Group gap={6} align="flex-start" wrap="nowrap">
                    <IconAlertTriangle size={15} />
                    <Text size="xs">{weatherError}</Text>
                  </Group>
                </Box>
              )}
            </Stack>
          </Box>

          {route?.steps?.length > 0 && (
            <Box className="route-steps-panel">
              <Text size="sm" fw={700} mb="xs">Maneuvers</Text>
              <Stack gap={6}>
                {route.steps.slice(0, 24).map((step, index) => (
                  <Group className="route-step" gap="xs" align="flex-start" wrap="nowrap" key={`${index}-${step.instruction}`}>
                    <Badge variant="light" color="gray">{index + 1}</Badge>
                    <Box>
                      <Text size="sm">{step.instruction}</Text>
                      <Text size="xs" c="dimmed">
                        {formatDistance(step.distanceMiles)} - {formatDuration(step.durationSeconds)}
                      </Text>
                    </Box>
                  </Group>
                ))}
              </Stack>
            </Box>
          )}

          {routeError && (
            <Box className="route-error">
              <Text size="sm" fw={700}>Route error</Text>
              <Text size="sm">{routeError}</Text>
            </Box>
          )}
        </Stack>
      </Box>
    </Box>
  );
};

export default TurnByTurnNavigation;
