import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('electron smoke script uses isolated ports and temp user data', () => {
  const source = readFileSync(new URL('../scripts/electron-smoke.mjs', import.meta.url), 'utf8');

  assert.match(source, /HOMIE_DESKTOP_SMOKE/);
  assert.match(source, /HOMIE_DESKTOP_USER_DATA_DIR/);
  assert.match(source, /45123/);
  assert.match(source, /33141/);
  assert.match(source, /DASHBOARD_STATIC_DIR/);
});

test('packaged smoke script launches the no-admin Windows artifact on clean ports', () => {
  const source = readFileSync(new URL('../scripts/packaged-smoke.mjs', import.meta.url), 'utf8');

  assert.match(source, /win-unpacked/);
  assert.match(source, /The Homie Desktop\.exe/);
  assert.match(source, /HOMIE_REPO_ROOT/);
  assert.match(source, /45124/);
  assert.match(source, /33142/);
  assert.match(source, /isPackaged/);
  assert.match(source, /resources\/dashboard-web/);
});

test('portable smoke script launches the portable Windows artifact on clean ports', () => {
  const source = readFileSync(new URL('../scripts/portable-smoke.mjs', import.meta.url), 'utf8');

  assert.match(source, /The-Homie-Desktop-.*\\.exe/);
  assert.match(source, /HOMIE_DESKTOP_PACKAGE_EXE/);
  assert.match(source, /HOMIE_DESKTOP_ARTIFACT_KIND/);
  assert.match(source, /portable/);
  assert.match(source, /45136/);
  assert.match(source, /33154/);
  assert.match(source, /packaged-smoke\.mjs/);
});

test('electron smoke checks dashboard routes for raw fetch errors', () => {
  const source = readFileSync(new URL('../main.cjs', import.meta.url), 'utf8');

  assert.match(source, /hasRawFetchError/);
  assert.match(source, /typeerror:\\\\s\*failed to fetch/i);
  assert.match(source, /!probe\?\.hasRawFetchError/);
});

test('electron smoke sends a dashboard chat slash command', () => {
  const source = readFileSync(new URL('../main.cjs', import.meta.url), 'utf8');

  assert.match(source, /loadDashboardPath\('\/chat'\)/);
  assert.match(source, /textarea\.value = '\/provider'/);
  assert.match(source, /Runtime Provider Status/);
  assert.match(source, /report\.chat/);
});

test('electron smoke matrix covers browser and teams dashboard routes', () => {
  const source = readFileSync(new URL('../main.cjs', import.meta.url), 'utf8');

  assert.match(source, /'\/browser': 'Browser Viewer'/);
  assert.match(source, /'\/teams': 'Operating Room'/);
  assert.match(source, /hasRawFetchError/);
});

test('electron-builder config uses asInvoker packaging and bundled web assets', () => {
  const source = readFileSync(new URL('../electron-builder.cjs', import.meta.url), 'utf8');
  const packageJson = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));

  assert.match(source, /requestedExecutionLevel: 'asInvoker'/);
  assert.match(source, /signAndEditExecutable: false/);
  assert.match(source, /\.\.\/web\/dist/);
  assert.equal(packageJson.scripts['package:win'], 'npm run build:web && electron-builder --win dir --config electron-builder.cjs');
  assert.equal(packageJson.scripts['package:win:portable'], 'npm run build:web && electron-builder --win portable --config electron-builder.cjs');
  assert.equal(packageJson.scripts['smoke:package'], 'node scripts/packaged-smoke.mjs');
  assert.equal(packageJson.scripts['smoke:portable'], 'node scripts/portable-smoke.mjs');
});
