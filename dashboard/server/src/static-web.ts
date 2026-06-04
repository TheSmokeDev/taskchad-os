import { existsSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import type { Context, Hono } from 'hono';

const CONTENT_TYPES: Record<string, string> = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
  '.webp': 'image/webp',
};

export function resolveStaticRoot(): string {
  const configured = (process.env.DASHBOARD_STATIC_DIR ?? '').trim();
  if (configured) return path.resolve(configured);
  return path.resolve(process.cwd(), '..', 'web', 'dist');
}

function responseForFile(c: Context, root: string, requestPath: string): Response | null {
  const relative = requestPath.replace(/^\/+/, '') || 'index.html';
  const candidate = path.resolve(root, relative);
  if (!candidate.startsWith(root + path.sep) && candidate !== root) {
    return c.text('Not found', 404);
  }
  if (!existsSync(candidate) || !statSync(candidate).isFile()) {
    return null;
  }
  const body = readFileSync(candidate);
  const contentType = CONTENT_TYPES[path.extname(candidate).toLowerCase()] ?? 'application/octet-stream';
  return new Response(body, {
    headers: {
      'content-type': contentType,
      'cache-control': requestPath.startsWith('/assets/')
        ? 'public, max-age=31536000, immutable'
        : 'no-store',
    },
  });
}

export function mountStaticWeb(app: Hono): void {
  app.get('/assets/*', (c) => {
    const root = resolveStaticRoot();
    if (!existsSync(root)) {
      return c.text('Dashboard web build is missing. Run npm --prefix dashboard/web run build.', 503);
    }
    return responseForFile(c, root, c.req.path) ?? c.text('Not found', 404);
  });

  app.get('*', (c) => {
    if (c.req.path.startsWith('/api/')) {
      return c.text('Not found', 404);
    }
    const root = resolveStaticRoot();
    if (!existsSync(root)) {
      return c.text('Dashboard web build is missing. Run npm --prefix dashboard/web run build.', 503);
    }
    return responseForFile(c, root, c.req.path)
      ?? responseForFile(c, root, '/index.html')
      ?? c.text('Not found', 404);
  });
}
