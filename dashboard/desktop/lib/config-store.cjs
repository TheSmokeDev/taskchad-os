const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_CONFIG = Object.freeze({
  apiPort: 4322,
  dashboardPort: 3141,
  bind: '127.0.0.1',
  startPath: '/teams',
  autoStart: true,
});

function normalizePort(value, fallback) {
  const port = Number.parseInt(String(value), 10);
  if (Number.isNaN(port) || port <= 0 || port > 65535) return fallback;
  return port;
}

function normalizeConfig(input = {}) {
  return {
    apiPort: normalizePort(input.apiPort, DEFAULT_CONFIG.apiPort),
    dashboardPort: normalizePort(input.dashboardPort, DEFAULT_CONFIG.dashboardPort),
    bind: String(input.bind || DEFAULT_CONFIG.bind).trim() || DEFAULT_CONFIG.bind,
    startPath: String(input.startPath || DEFAULT_CONFIG.startPath).startsWith('/')
      ? String(input.startPath || DEFAULT_CONFIG.startPath)
      : DEFAULT_CONFIG.startPath,
    autoStart: typeof input.autoStart === 'boolean' ? input.autoStart : DEFAULT_CONFIG.autoStart,
  };
}

class ConfigStore {
  constructor(userDataDir, defaultConfig = DEFAULT_CONFIG) {
    this.userDataDir = userDataDir;
    this.configPath = path.join(userDataDir, 'desktop-config.json');
    this.defaultConfig = normalizeConfig(defaultConfig);
  }

  load() {
    try {
      if (!fs.existsSync(this.configPath)) return { ...this.defaultConfig };
      const parsed = JSON.parse(fs.readFileSync(this.configPath, 'utf8'));
      return normalizeConfig(parsed);
    } catch {
      return { ...this.defaultConfig };
    }
  }

  save(nextConfig) {
    const config = normalizeConfig(nextConfig);
    fs.mkdirSync(this.userDataDir, { recursive: true });
    fs.writeFileSync(this.configPath, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
    return config;
  }
}

module.exports = {
  ConfigStore,
  DEFAULT_CONFIG,
  normalizeConfig,
};
