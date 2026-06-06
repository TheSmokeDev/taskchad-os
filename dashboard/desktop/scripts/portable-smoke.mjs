import { existsSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(__dirname, '..');
const distDir = path.join(desktopDir, 'dist');

function findNewestPortableExe() {
  if (!existsSync(distDir)) return null;
  const candidates = readdirSync(distDir)
    .filter((name) => /^The-Homie-Desktop-.*\.exe$/i.test(name))
    .map((name) => {
      const fullPath = path.join(distDir, name);
      return { fullPath, mtimeMs: statSync(fullPath).mtimeMs };
    })
    .sort((a, b) => b.mtimeMs - a.mtimeMs);
  return candidates[0]?.fullPath ?? null;
}

if (!process.env.HOMIE_DESKTOP_PACKAGE_EXE) {
  const portableExe = findNewestPortableExe();
  if (!portableExe) {
    console.error(`Portable desktop executable not found in ${distDir}. Run npm --prefix dashboard/desktop run package:win:portable first.`);
    process.exit(1);
  }
  process.env.HOMIE_DESKTOP_PACKAGE_EXE = portableExe;
}

process.env.HOMIE_DESKTOP_ARTIFACT_KIND = 'portable';
process.env.ORCHESTRATION_API_PORT ||= '45136';
process.env.DASHBOARD_PORT ||= '33154';

await import('./packaged-smoke.mjs');
