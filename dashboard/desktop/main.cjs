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
    startPath: '/',
    autoStart: true,
  };
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1220,
    height: 780,
    minWidth: 980,
    minHeight: 640,
    show: false,
    title: 'The Homie Desktop',
    backgroundColor: '#101214',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: false,
    },
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  return mainWindow;
}

async function loadFallbackShell(reason) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  await mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  if (reason) {
    broadcast({
      type: 'error',
      source: 'desktop',
      message: reason,
      timestamp: new Date().toISOString(),
    });
  }
  if (!mainWindow.isVisible()) mainWindow.show();
}

async function loadDashboardInWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const targetUrl = stackManager.targetUrl();
  const pythonHealthUrl = `http://${stackManager.config.bind}:${stackManager.config.apiPort}/api/health`;
  const honoHealthUrl = `http://${stackManager.config.bind}:${stackManager.config.dashboardPort}/api/health`;
  const pythonReady = await waitForEndpoint(pythonHealthUrl);
  if (!pythonReady.ok) return false;
  const honoReady = await waitForEndpoint(honoHealthUrl);
  if (!honoReady.ok) return false;
  const ready = await waitForEndpoint(targetUrl, {
    match: (text) => text.includes('<div id="app"') || text.includes('The Homie Dashboard'),
  });
  if (!ready.ok) return false;
  await mainWindow.loadURL(targetUrl);
  if (!mainWindow.isVisible()) mainWindow.show();
  return true;
}

async function loadDashboardPath(routePath) {
  const normalizedPath = routePath.startsWith('/') ? routePath : `/${routePath}`;
  const targetUrl = `http://${stackManager.config.bind}:${stackManager.config.dashboardPort}${normalizedPath}`;
  await mainWindow.loadURL(targetUrl);
  return targetUrl;
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
  ipcMain.handle('dashboard:open', async () => {
    const targetUrl = stackManager.targetUrl();
    if (mainWindow && !mainWindow.isDestroyed()) {
      await mainWindow.loadURL(targetUrl);
    } else {
      await shell.openExternal(targetUrl);
    }
    return { targetUrl };
  });
  ipcMain.handle('operating-room:open', async () => {
    const targetUrl = await loadDashboardPath('/teams');
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

async function waitForRendererProbe(script, match, options = {}) {
  const timeoutMs = options.timeoutMs ?? 15000;
  const startedAt = Date.now();
  let lastResult = null;
  while (Date.now() - startedAt < timeoutMs) {
    lastResult = await mainWindow.webContents.executeJavaScript(script);
    if (!match || match(lastResult)) return lastResult;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return lastResult;
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
      artifactKind: process.env.HOMIE_DESKTOP_ARTIFACT_KIND || (app.isPackaged ? 'packaged' : 'dev'),
      appPath: app.getAppPath(),
      resourcesPath: process.resourcesPath || null,
    },
    paths: { ...stackManager.paths },
    renderer: null,
    routes: {},
    dashboard: null,
    pythonHealth: null,
    honoHealth: null,
    beforeStop: null,
    afterStop: null,
    error: null,
  };
  try {
    report.renderer = await waitForRendererProbe(`
      ({
        title: document.title,
        hasDashboardRoot: Boolean(document.querySelector('#app')),
        hasDesktopBridge: Boolean(window.homieDesktop),
        hasDesktopControls: document.body.innerText.toLowerCase().includes('desktop stack'),
        hasMissionControl: document.body.innerText.includes('Mission Control'),
        text: document.body.innerText
      })
    `, (result) => result?.hasDashboardRoot && result?.hasDesktopBridge && result?.hasDesktopControls);
    const routeExpectations = {
      '/mission': 'Mission Control',
      '/chat': 'Chat',
      '/mobile': 'Mobile Access',
      '/browser': 'Browser Viewer',
      '/work': 'Work Queue',
      '/convoy': 'Convoy',
      '/teams': 'Operating Room',
    };
    for (const [routePath, expectedText] of Object.entries(routeExpectations)) {
      const routeUrl = await loadDashboardPath(routePath);
      const result = await waitForRendererProbe(`
        ({
          url: window.location.href,
          hasDashboardRoot: Boolean(document.querySelector('#app')),
          hasDesktopBridge: Boolean(window.homieDesktop),
          hasDesktopControls: document.body.innerText.toLowerCase().includes('desktop stack'),
          hasExpectedText: document.body.innerText.includes(${JSON.stringify(expectedText)}),
          hasRawFetchError: /typeerror:\\s*failed to fetch|failed to fetch/i.test(document.body.innerText),
          text: document.body.innerText
        })
      `, (probe) => probe?.hasDashboardRoot && probe?.hasDesktopBridge && probe?.hasDesktopControls && probe?.hasExpectedText && !probe?.hasRawFetchError);
      report.routes[routePath] = {
        ok: Boolean(result?.hasDashboardRoot && result?.hasDesktopBridge && result?.hasDesktopControls && result?.hasExpectedText && !result?.hasRawFetchError),
        url: routeUrl,
        expectedText,
        hasRawFetchError: Boolean(result?.hasRawFetchError),
      };
    }
    report.dashboard = await waitForEndpoint(stackManager.targetUrl(), {
      match: (text) => text.includes('<div id="app"') || text.includes('The Homie Dashboard'),
    });
    const pythonHealthUrl = `http://${stackManager.config.bind}:${stackManager.config.apiPort}/api/health`;
    const honoHealthUrl = `http://${stackManager.config.bind}:${stackManager.config.dashboardPort}/api/health`;
    report.pythonHealth = await waitForEndpoint(pythonHealthUrl);
    report.honoHealth = await waitForEndpoint(honoHealthUrl);
    report.beforeStop = stackManager.status();
    report.ok = Boolean(
      report.renderer?.hasDashboardRoot
      && report.renderer?.hasDesktopBridge
      && report.renderer?.hasDesktopControls
      && report.renderer?.hasMissionControl
      && report.beforeStop?.running
      && report.beforeStop?.services?.every((service) => service.running)
      && Object.values(report.routes).every((route) => route.ok)
      && report.dashboard?.ok
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
      await loadFallbackShell(error instanceof Error ? error.message : String(error));
    }
  }
  if (!mainWindow?.isVisible()) {
    const loaded = stackManager.isRunning() ? await loadDashboardInWindow() : false;
    if (!loaded) {
      await loadFallbackShell('Dashboard did not become ready. Showing local fallback controls.');
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
