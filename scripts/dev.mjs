import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';

const root = process.cwd();
const npmCmd = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const routingVenvPython = process.platform === 'win32'
    ? join(root, 'backend', 'venv', 'Scripts', 'python.exe')
    : join(root, 'backend', 'venv', 'bin', 'python');
const pythonCmd = process.env.PYTHON
    || (existsSync(routingVenvPython) ? routingVenvPython : null)
    || 'python3';

const children = [];
let shuttingDown = false;

function parsePort(value, fallback) {
    if (!value) return fallback;
    const port = Number.parseInt(value, 10);
    return Number.isInteger(port) && port > 0 && port < 65536 ? port : fallback;
}

function canListen(port, host) {
    return new Promise((resolve, reject) => {
        const server = createServer();
        server.once('error', (error) => {
            if (error.code === 'EADDRINUSE') {
                resolve(false);
                return;
            }
            reject(error);
        });
        server.once('listening', () => {
            server.close(() => resolve(true));
        });
        server.listen(port, host);
    });
}

async function findAvailablePort(startPort, host) {
    for (let port = startPort; port < 65536; port += 1) {
        if (await canListen(port, host)) return port;
    }
    throw new Error(`No available port found at or above ${startPort}.`);
}

function start(name, command, args, env = {}) {
    const child = spawn(command, args, {
        cwd: root,
        env: { ...process.env, ...env },
        stdio: ['ignore', 'pipe', 'pipe'],
    });

    children.push(child);

    const prefix = `[${name}]`;
    child.stdout.on('data', (chunk) => {
        process.stdout.write(`${prefix} ${chunk}`);
    });
    child.stderr.on('data', (chunk) => {
        process.stderr.write(`${prefix} ${chunk}`);
    });
    child.on('exit', (code, signal) => {
        if (shuttingDown) return;
        shuttingDown = true;
        console.log(`${prefix} exited${signal ? ` (${signal})` : ` with code ${code}`}. Stopping routing app.`);
        stopAll();
        process.exit(code || 0);
    });

    return child;
}

function stopAll() {
    for (const child of children) {
        if (!child.killed) child.kill('SIGTERM');
    }
}

process.on('SIGINT', () => {
    shuttingDown = true;
    stopAll();
    process.exit(0);
});

process.on('SIGTERM', () => {
    shuttingDown = true;
    stopAll();
    process.exit(0);
});

async function main() {
    const backendHost = process.env.ROUTING_BACKEND_HOST || '127.0.0.1';
    const frontendHost = process.env.ROUTING_FRONTEND_HOST || '127.0.0.1';
    const preferredBackendPort = parsePort(process.env.ROUTING_BACKEND_PORT || process.env.PORT, 5055);
    const preferredFrontendPort = parsePort(process.env.ROUTING_FRONTEND_PORT, 5174);
    const backendPort = await findAvailablePort(preferredBackendPort, backendHost);
    const frontendPort = await findAvailablePort(preferredFrontendPort, frontendHost);
    const routingApiUrl = `http://${backendHost}:${backendPort}`;

    console.log('Starting standalone Turn by Turn app...');
    if (backendPort !== preferredBackendPort) {
        console.log(`Routing backend port ${preferredBackendPort} is in use, using ${backendPort} instead.`);
    }
    if (frontendPort !== preferredFrontendPort) {
        console.log(`Routing frontend port ${preferredFrontendPort} is in use, using ${frontendPort} instead.`);
    }
    console.log(`Routing API target: ${routingApiUrl}`);

    start('turn-by-turn-backend', pythonCmd, ['backend/server.py'], {
        ROUTING_BACKEND_HOST: backendHost,
        ROUTING_BACKEND_PORT: String(backendPort),
        FLASK_USE_RELOADER: process.env.FLASK_USE_RELOADER ?? '1',
        PORT: String(backendPort),
    });
    start('turn-by-turn-ui', npmCmd, [
        'run',
        'dev:ui',
        '--',
        '--host',
        frontendHost,
        '--port',
        String(frontendPort),
    ], {
        ROUTING_FRONTEND_PORT: String(frontendPort),
        VITE_ROUTING_API_URL: routingApiUrl,
    });
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
