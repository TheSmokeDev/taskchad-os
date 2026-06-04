import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(__dirname, '..');
const repoRoot = path.resolve(desktopDir, '..', '..');
const smokeRoot = path.join(tmpdir(), `homie-packaged-smoke-${Date.now()}`);
const userDataDir = path.join(smokeRoot, 'user-data');
const requestedReportPath = process.env.HOMIE_DESKTOP_SMOKE_REPORT
  ? path.resolve(process.env.HOMIE_DESKTOP_SMOKE_REPORT)
  : null;
const reportPath = requestedReportPath || path.join(smokeRoot, 'report.json');
const packagedExe = process.env.HOMIE_DESKTOP_PACKAGE_EXE
  ? path.resolve(process.env.HOMIE_DESKTOP_PACKAGE_EXE)
  : path.join(desktopDir, 'dist', 'win-unpacked', 'The Homie Desktop.exe');

if (process.platform !== 'win32') {
  console.error('Packaged Desktop smoke is Windows-only.');
  process.exit(1);
}

if (!existsSync(packagedExe)) {
  console.error(`Packaged desktop executable not found at ${packagedExe}. Run npm --prefix dashboard/desktop run package:win first.`);
  process.exit(1);
}

const env = {
  ...process.env,
  HOMIE_REPO_ROOT: repoRoot,
  HOMIE_DESKTOP_SMOKE: '1',
  HOMIE_DESKTOP_USER_DATA_DIR: userDataDir,
  HOMIE_DESKTOP_SMOKE_REPORT: reportPath,
  ORCHESTRATION_API_PORT: process.env.ORCHESTRATION_API_PORT || '45124',
  DASHBOARD_PORT: process.env.DASHBOARD_PORT || '33142',
  DASHBOARD_BIND: '127.0.0.1',
  DASHBOARD_DEV_MODE_NO_AUTH: 'true',
};

mkdirSync(smokeRoot, { recursive: true });

const child = spawn(packagedExe, [], {
  cwd: repoRoot,
  env,
  stdio: ['ignore', 'pipe', 'pipe'],
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

const webDistDir = String(report?.paths?.webDistDir || '').replaceAll('\\', '/');
const ok = Boolean(
  exitCode === 0
  && report?.ok
  && report?.package?.isPackaged
  && webDistDir.endsWith('/resources/dashboard-web')
);

process.exit(ok ? 0 : 1);
