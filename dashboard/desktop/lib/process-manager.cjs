const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const { commandName, resolveDesktopPaths } = require('./desktop-paths.cjs');
const { normalizeConfig } = require('./config-store.cjs');

const MAX_LOG_LINES = 500;

function nowIso() {
  return new Date().toISOString();
}

function buildStackCommands(config, paths = resolveDesktopPaths()) {
  const normalized = normalizeConfig(config);
  const env = {
    ...process.env,
    ORCHESTRATION_API_PORT: String(normalized.apiPort),
    DASHBOARD_PORT: String(normalized.dashboardPort),
    DASHBOARD_BIND: normalized.bind,
    DASHBOARD_DEV_MODE_NO_AUTH: 'true',
    DASHBOARD_STATIC_DIR: paths.webDistDir,
    FRAMEWORK_API_URL: `http://${normalized.bind}:${normalized.apiPort}`,
  };
  return [
    {
      name: 'python-api',
      cwd: paths.scriptsDir,
      command: commandName('uv'),
      args: ['run', 'python', '-m', 'orchestration.run_api'],
      env,
    },
    {
      name: 'hono-dashboard',
      cwd: paths.serverDir,
      command: commandName('npm'),
      args: ['run', 'start'],
      env,
    },
  ];
}

class DesktopStackManager extends EventEmitter {
  constructor(config, options = {}) {
    super();
    this.config = normalizeConfig(config);
    this.paths = options.paths || resolveDesktopPaths();
    this.spawnFn = options.spawnFn || spawn;
    this.processes = new Map();
    this.logs = [];
  }

  updateConfig(config) {
    if (this.isRunning()) {
      throw new Error('Stop the desktop stack before changing ports.');
    }
    this.config = normalizeConfig(config);
  }

  targetUrl() {
    return `http://${this.config.bind}:${this.config.dashboardPort}${this.config.startPath}`;
  }

  isRunning() {
    return [...this.processes.values()].some((entry) => entry.process.exitCode === null);
  }

  status() {
    const services = buildStackCommands(this.config, this.paths).map((command) => {
      const entry = this.processes.get(command.name);
      const running = Boolean(entry && entry.process.exitCode === null);
      return {
        name: command.name,
        running,
        pid: running ? entry.process.pid : null,
        cwd: command.cwd,
      };
    });
    return {
      running: this.isRunning(),
      targetUrl: this.targetUrl(),
      config: { ...this.config },
      services,
      logs: [...this.logs],
    };
  }

  async start() {
    if (this.isRunning()) return this.status();
    for (const command of buildStackCommands(this.config, this.paths)) {
      this.appendLog(command.name, `starting ${command.command} ${command.args.join(' ')}`);
      const child = this.spawnFn(command.command, command.args, {
        cwd: command.cwd,
        env: command.env,
        shell: process.platform === 'win32',
        windowsHide: true,
      });
      this.processes.set(command.name, { command, process: child });
      child.stdout?.on('data', (chunk) => this.appendLog(command.name, chunk.toString()));
      child.stderr?.on('data', (chunk) => this.appendLog(command.name, chunk.toString()));
      child.on('exit', (code, signal) => {
        this.appendLog(command.name, `exited code=${code ?? 'null'} signal=${signal ?? 'null'}`);
        this.emitStatus();
      });
    }
    this.emitStatus();
    return this.status();
  }

  async stop() {
    const entries = [...this.processes.values()].reverse();
    for (const entry of entries) {
      const child = entry.process;
      if (child.exitCode !== null) continue;
      this.appendLog(entry.command.name, 'stopping');
      if (process.platform === 'win32' && child.pid) {
        await new Promise((resolve) => {
          const killer = this.spawnFn('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
            windowsHide: true,
          });
          killer.on('exit', resolve);
          killer.on('error', resolve);
        });
      } else {
        child.kill('SIGTERM');
      }
    }
    this.processes.clear();
    this.emitStatus();
    return this.status();
  }

  appendLog(source, message) {
    const lines = String(message)
      .split(/\r?\n/)
      .map((line) => line.trimEnd())
      .filter(Boolean);
    for (const line of lines) {
      this.logs.push({ timestamp: nowIso(), source, message: line });
    }
    if (this.logs.length > MAX_LOG_LINES) {
      this.logs.splice(0, this.logs.length - MAX_LOG_LINES);
    }
    this.emit('event', { type: 'log', source, lines, timestamp: nowIso() });
  }

  emitStatus() {
    this.emit('event', { type: 'status', status: this.status(), timestamp: nowIso() });
  }
}

module.exports = {
  DesktopStackManager,
  buildStackCommands,
};
