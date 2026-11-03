import React from 'react';
import ReactDOM from 'react-dom/client';
import { setWorkerUrl } from 'maplibre-gl';
// MapLibre finds its worker next to its own module, which only holds in dev;
// importing it with ?url makes the production build ship it too.
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import App from './App';
import './styles.css';

setWorkerUrl(maplibreWorkerUrl);

ReactDOM.createRoot(document.getElementById('root')).render(
  <App />
);
