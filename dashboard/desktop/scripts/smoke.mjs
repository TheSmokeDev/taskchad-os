import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { buildStackCommands } = require('../lib/process-manager.cjs');
const { DEFAULT_CONFIG } = require('../lib/config-store.cjs');

const commands = buildStackCommands(DEFAULT_CONFIG);
const names = commands.map((command) => command.name);
const ok = names.includes('python-api') && names.includes('hono-dashboard');

console.log(JSON.stringify({
  ok,
  targetUrl: `http://${DEFAULT_CONFIG.bind}:${DEFAULT_CONFIG.dashboardPort}${DEFAULT_CONFIG.startPath}`,
  commands: names,
}, null, 2));

process.exit(ok ? 0 : 1);
