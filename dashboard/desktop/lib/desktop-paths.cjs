const path = require('node:path');
const fs = require('node:fs');

function resolveRepoRoot(startDir = __dirname) {
  const explicitRoot = firstExistingRepoRoot([
    process.env.HOMIE_REPO_ROOT,
    process.env.THEHOMIE_DIR,
  ]);
  if (explicitRoot) return explicitRoot;

  const discoveredRoot = firstExistingRepoRoot([
    process.cwd(),
    startDir,
    process.resourcesPath,
    process.resourcesPath ? path.dirname(process.resourcesPath) : null,
  ]);
  if (discoveredRoot) return discoveredRoot;

  return path.resolve(startDir, '..', '..', '..');
}

function firstExistingRepoRoot(candidates) {
  for (const candidate of candidates) {
    if (!candidate) continue;
    const found = findRepoRoot(candidate);
    if (found) return found;
  }
  return null;
}

function findRepoRoot(startDir) {
  let current = path.resolve(startDir);
  if (path.basename(current).endsWith('.asar')) {
    current = path.dirname(current);
  }

  while (true) {
    if (isRepoRoot(current)) return current;
    const parent = path.dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

function isRepoRoot(candidate) {
  return Boolean(
    candidate
    && fs.existsSync(path.join(candidate, '.claude', 'scripts'))
    && fs.existsSync(path.join(candidate, 'dashboard', 'server'))
    && fs.existsSync(path.join(candidate, 'dashboard', 'web'))
  );
}

function resolveBundledWebDistDir(options = {}) {
  const explicit = process.env.HOMIE_DESKTOP_WEB_DIST_DIR || process.env.DASHBOARD_STATIC_DIR;
  if (explicit) return path.resolve(explicit);

  const resourcesPath = options.resourcesPath || process.resourcesPath;
  if (!resourcesPath) return null;

  const bundled = path.join(resourcesPath, 'dashboard-web');
  return fs.existsSync(bundled) ? bundled : null;
}

function resolveDesktopPaths(root = resolveRepoRoot(), options = {}) {
  return {
    root,
    scriptsDir: path.join(root, '.claude', 'scripts'),
    serverDir: path.join(root, 'dashboard', 'server'),
    webDistDir: resolveBundledWebDistDir(options) || path.join(root, 'dashboard', 'web', 'dist'),
  };
}

function commandName(name) {
  return name;
}

module.exports = {
  commandName,
  findRepoRoot,
  resolveDesktopPaths,
  resolveRepoRoot,
};
