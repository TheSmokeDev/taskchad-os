import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(__dirname, '..');
const repoRoot = path.resolve(desktopDir, '..', '..');
const smokeRoot = path.join(tmpdir(), `homie-electron-smoke-${Date.now()}`);
const userDataDir = path.join(smokeRoot, 'user-data');
const requestedReportPath = process.env.HOMIE_DESKTOP_SMOKE_REPORT
  ? path.resolve(process.env.HOMIE_DESKTOP_SMOKE_REPORT)
  : null;
const reportPath = requestedReportPath || path.join(smokeRoot, 'report.json');

const env = {
  ...process.env,
  HOMIE_DESKTOP_SMOKE: '1',
  HOMIE_DESKTOP_USER_DATA_DIR: userDataDir,
  HOMIE_DESKTOP_SMOKE_REPORT: reportPath,
  ORCHESTRATION_API_PORT: process.env.ORCHESTRATION_API_PORT || '45123',
  DASHBOARD_PORT: process.env.DASHBOARD_PORT || '33141',
  DASHBOARD_BIND: '127.0.0.1',
  DASHBOARD_DEV_MODE_NO_AUTH: 'true',
  DASHBOARD_STATIC_DIR: path.join(repoRoot, 'dashboard', 'web', 'dist'),
};

mkdirSync(smokeRoot, { recursive: true });

const electronBin = process.platform === 'win32'
  ? path.join(desktopDir, 'node_modules', '.bin', 'electron.cmd')
  : path.join(desktopDir, 'node_modules', '.bin', 'electron');

if (!existsSync(electronBin)) {
  console.error(`Electron binary not found at ${electronBin}. Run npm --prefix dashboard/desktop install first.`);
  process.exit(1);
}

const child = spawn(electronBin, ['.'], {
  cwd: desktopDir,
  env,
  stdio: ['ignore', 'pipe', 'pipe'],
  shell: process.platform === 'win32',
  windowsHide: true,
});

child.stdout.on('data', (chunk) => process.stdout.write(chunk));
child.stderr.on('data', (chunk) => process.stderr.write(chunk));

const exitCode = await new Promise((resolve) => {
  child.on('exit', (code) => resolve(code ?? 1));
  child.on('error', (error) => {
    console.error(error);
    resolve(1);
  });
});

let report = null;
if (existsSync(reportPath)) {
  report = JSON.parse(readFileSync(reportPath, 'utf8'));
  console.log(JSON.stringify(report, null, 2));
}

if (process.env.HOMIE_DESKTOP_KEEP_SMOKE !== '1' && !requestedReportPath) {
  rmSync(smokeRoot, { recursive: true, force: true });
}

process.exit(exitCode === 0 && report?.ok ? 0 : 1);
