const api = window.homieDesktop;

const fields = {
  apiPort: document.querySelector('#apiPort'),
  dashboardPort: document.querySelector('#dashboardPort'),
  bind: document.querySelector('#bind'),
  startPath: document.querySelector('#startPath'),
  autoStart: document.querySelector('#autoStart'),
};
const servicesEl = document.querySelector('#services');
const logsEl = document.querySelector('#logs');
const runningEl = document.querySelector('#running');
const targetEl = document.querySelector('#targetUrl');
const logCountEl = document.querySelector('#log-count');

function setConfig(config) {
  fields.apiPort.value = config.apiPort;
  fields.dashboardPort.value = config.dashboardPort;
  fields.bind.value = config.bind;
  fields.startPath.value = config.startPath;
  fields.autoStart.checked = config.autoStart;
}

function collectConfig() {
  return {
    apiPort: fields.apiPort.value,
    dashboardPort: fields.dashboardPort.value,
    bind: fields.bind.value,
    startPath: fields.startPath.value,
    autoStart: fields.autoStart.checked,
  };
}

function renderStatus(status) {
  runningEl.textContent = status.running ? 'Running' : 'Stopped';
  runningEl.className = status.running ? 'good' : 'muted';
  targetEl.textContent = status.targetUrl;
  servicesEl.innerHTML = '';
  for (const service of status.services) {
    const row = document.createElement('div');
    row.className = 'service';
    row.innerHTML = `
      <span>${service.name}</span>
      <strong class="${service.running ? 'good' : 'muted'}">${service.running ? `PID ${service.pid}` : 'Stopped'}</strong>
    `;
    servicesEl.appendChild(row);
  }
  logsEl.textContent = status.logs
    .map((line) => `[${line.timestamp}] ${line.source}: ${line.message}`)
    .join('\n');
  logCountEl.textContent = `${status.logs.length} lines`;
  logsEl.scrollTop = logsEl.scrollHeight;
}

async function refresh() {
  const status = await api.status();
  setConfig(status.config);
  renderStatus(status);
}

document.querySelector('#config').addEventListener('submit', async (event) => {
  event.preventDefault();
  const config = await api.saveConfig(collectConfig());
  setConfig(config);
  await refresh();
});

document.querySelector('#start').addEventListener('click', async () => {
  renderStatus(await api.startStack());
});

document.querySelector('#stop').addEventListener('click', async () => {
  renderStatus(await api.stopStack());
});

document.querySelector('#open').addEventListener('click', async () => {
  await api.openDashboard();
});

api.onStackEvent((event) => {
  if (event.type === 'status') {
    renderStatus(event.status);
  } else {
    refresh();
  }
});

refresh();
