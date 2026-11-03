import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import basicSsl from '@vitejs/plugin-basic-ssl';

const appDir = dirname(fileURLToPath(import.meta.url));
const routingApiTarget = process.env.VITE_ROUTING_API_URL || 'http://localhost:5055';
const routingFrontendPort = Number.parseInt(process.env.ROUTING_FRONTEND_PORT || '5174', 10);

export default defineConfig({
  root: appDir,
  // VITE_HTTPS=1 serves over HTTPS with a self-signed certificate: phones only
  // share GPS with secure pages, so the phone beacon needs it on the LAN.
  plugins: [react(), ...(process.env.VITE_HTTPS === '1' ? [basicSsl()] : [])],
  // MapLibre 6 loads its worker relative to its own module URL; pre-bundling
  // moves the module into .vite/deps and the worker URL no longer resolves.
  optimizeDeps: {
    exclude: ['maplibre-gl'],
  },
  server: {
    port: Number.isInteger(routingFrontendPort) ? routingFrontendPort : 5174,
    // Reached from the phone through `tailscale serve` (https://<host>.<tailnet>.ts.net).
    allowedHosts: ['.ts.net'],
    proxy: {
      '/api': {
        target: routingApiTarget,
        changeOrigin: true,
        secure: false,
      },
      // TileServer GL (basemap); over Tailscale, `tailscale serve` maps /tiles itself.
      '/tiles': {
        target: process.env.TILESERVER_URL || 'http://127.0.0.1:8089',
        rewrite: (path) => path.replace(/^\/tiles/, ''),
      },
    },
  },
  preview: {
    port: 4174,
  },
  build: {
    outDir: resolve(appDir, 'dist'),
    emptyOutDir: true,
  },
});
