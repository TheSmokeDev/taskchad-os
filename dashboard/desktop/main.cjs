const path = require('node:path');
const fs = require('node:fs');
const { app, BrowserWindow, ipcMain, shell } = require('electron');
const { ConfigStore } = require('./lib/config-store.cjs');
const { DesktopStackManager } = require('./lib/process-manager.cjs');

let mainWindow = null;
let configStore = null;
let stackManager = null;
const smokeMode = process.env.HOMIE_DESKTOP_SMOKE === '1';
let quittingAfterStop = false;

if (process.env.HOMIE_DESKTOP_USER_DATA_DIR) {
  app.setPath('userData', path.resolve(process.env.HOMIE_DESKTOP_USER_DATA_DIR));
}

function defaultConfigFromEnv() {
  return {
    apiPort: process.env.ORCHESTRATION_API_PORT,
    dashboardPort: process.env.DASHBOARD_PORT,
    bind: process.env.DASHBOARD_BIND,
    startPath: '/teams',
    autoStart: true,
  };
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1220,
    height: 780,
    minWidth: 980,
    minHeight: 640,
    title: 'The Homie Operating Room',
    backgroundColor: '#101214',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: false,
    },
  });

  await mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  return mainWindow;
}

function broadcast(event) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('stack:event', event);
  }
}

function wireIpc() {
  ipcMain.handle('config:get', () => configStore.load());
  ipcMain.handle('config:save', (_event, nextConfig) => {
    const saved = configStore.save(nextConfig);
    stackManager.updateConfig(saved);
    broadcast({ type: 'config', config: saved });
    return saved;
  });
  ipcMain.handle('stack:status', () => stackManager.status());
  ipcMain.handle('stack:start', async () => stackManager.start());
  ipcMain.handle('stack:stop', async () => stackManager.stop());
  ipcMain.handle('operating-room:open', async () => {
    const targetUrl = stackManager.targetUrl();
    await shell.openExternal(targetUrl);
    return { targetUrl };
  });
}

async function waitForEndpoint(url, options = {}) {
  const timeoutMs = options.timeoutMs ?? 45000;
  const match = options.match;
  const startedAt = Date.now();
  let lastError = 'not attempted';
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const response = await fetch(url);
      const text = await response.text();
      if (response.ok && (!match || match(text, response))) {
        return {
          ok: true,
          status: response.status,
          elapsedMs: Date.now() - startedAt,
        };
      }
      lastError = `status=${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return {
    ok: false,
    status: null,
    elapsedMs: Date.now() - startedAt,
    error: lastError,
  };
}

async function runSmoke() {
  const reportPath = process.env.HOMIE_DESKTOP_SMOKE_REPORT
    ? path.resolve(process.env.HOMIE_DESKTOP_SMOKE_REPORT)
    : path.join(app.getPath('userData'), 'desktop-smoke-report.json');
  const report = {
    ok: false,
    startedAt: new Date().toISOString(),
    targetUrl: stackManager.targetUrl(),
    package: {
      isPackaged: app.isPackaged,
      appPath: app.getAppPath(),
      resourcesPath: process.resourcesPath || null,
    },
    paths: { ...stackManager.paths },
    renderer: null,
    teams: null,
    pythonHealth: null,
    honoHealth: null,
    beforeStop: null,
    afterStop: null,
    error: null,
  };
  try {
    report.renderer = await mainWindow.webContents.executeJavaScript(`
      ({
        title: document.title,
        hasStart: Boolean(document.querySelector('#start')),
        hasStop: Boolean(document.querySelector('#stop')),
        hasOpen: Boolean(document.querySelector('#open')),
        hasLogs: Boolean(document.querySelector('#logs')),
        text: document.body.innerText
      })
    `);
    report.teams = await waitForEndpoint(stackManager.targetUrl(), {
      match: (text) => text.includes('<div id="root"') || text.includes('The Homie Dashboard'),
    });
    const pythonHealthUrl = `http://${stackManager.config.bind}:${stackManager.config.apiPort}/api/health`;
    const honoHealthUrl = `http://${stackManager.config.bind}:${stackManager.config.dashboardPort}/api/health`;
    report.pythonHealth = await waitForEndpoint(pythonHealthUrl);
    report.honoHealth = await waitForEndpoint(honoHealthUrl);
    report.beforeStop = stackManager.status();
    report.ok = Boolean(
      report.renderer?.hasStart
      && report.renderer?.hasStop
      && report.renderer?.hasOpen
      && report.renderer?.hasLogs
      && report.beforeStop?.running
      && report.beforeStop?.services?.every((service) => service.running)
      && report.teams?.ok
      && report.pythonHealth?.ok
      && report.honoHealth?.ok
    );
  } catch (error) {
    report.error = error instanceof Error ? error.stack || error.message : String(error);
  } finally {
    report.afterStop = await stackManager.stop();
    report.finishedAt = new Date().toISOString();
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
    quittingAfterStop = true;
    app.exit(report.ok ? 0 : 1);
  }
}

app.whenReady().then(async () => {
  configStore = new ConfigStore(app.getPath('userData'), defaultConfigFromEnv());
  stackManager = new DesktopStackManager(configStore.load());
  stackManager.on('event', broadcast);
  wireIpc();
  await createWindow();
  if (configStore.load().autoStart) {
    try {
      await stackManager.start();
    } catch (error) {
      broadcast({
        type: 'error',
        source: 'desktop',
        message: error instanceof Error ? error.message : String(error),
        timestamp: new Date().toISOString(),
      });
    }
  }
  if (smokeMode) {
    await runSmoke();
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', async (event) => {
  if (quittingAfterStop || !stackManager || !stackManager.isRunning()) return;
  event.preventDefault();
  await stackManager.stop();
  app.exit(0);
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});
