import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { GhostViewer } from '@/pages/GhostViewer';

const WEB_SRC = join(__dirname, '..');

function pngResponse(): Response {
  return new Response(new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])]), {
    status: 200,
    headers: {
      'content-type': 'image/png',
      'x-ghost-screen-width': '1080',
      'x-ghost-screen-height': '2400',
    },
  });
}

describe('Ghost Viewer page', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:ghost-screen'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('captures the ghost screen and renders it as an image', async () => {
    globalThis.fetch = vi.fn(async () => pngResponse()) as unknown as typeof fetch;
    render(<GhostViewer />);

    fireEvent.click(screen.getByText('Capture'));

    await waitFor(() => {
      const img = document.querySelector('img[alt="Ghost device screen"]') as HTMLImageElement;
      expect(img).not.toBeNull();
      expect(img.src).toContain('blob:ghost-screen');
    });
  });

  it('sends NORMALIZED tap coords, never device pixels', async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/api/ghost-viewer/screen')) return pngResponse();
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }) as unknown as typeof fetch;

    render(<GhostViewer />);
    fireEvent.click(screen.getByText('Capture'));
    const img = await waitFor(() => {
      const el = document.querySelector('img[alt="Ghost device screen"]') as HTMLImageElement;
      expect(el).not.toBeNull();
      return el;
    });

    // A 300x600 rendered image, click at (150, 150) -> normalized (0.5, 0.25).
    vi.spyOn(img, 'getBoundingClientRect').mockReturnValue({
      left: 0, top: 0, width: 300, height: 600, right: 300, bottom: 600, x: 0, y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    fireEvent.click(img, { clientX: 150, clientY: 150 });

    await waitFor(() => {
      const tap = calls.find((c) => c.url.includes('/api/ghost-viewer/tap'));
      expect(tap).toBeDefined();
      expect(tap!.body).toEqual({ x: 0.5, y: 0.25 });
    });
  });

  it('surfaces a 403 as a ghost-disabled hint', async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify({ detail: 'Ghost is disabled' }), {
        status: 403,
        headers: { 'content-type': 'application/json' },
      }),
    ) as unknown as typeof fetch;

    render(<GhostViewer />);
    fireEvent.click(screen.getByText('Capture'));

    await waitFor(() => {
      expect(screen.getByText(/HOMIE_GHOST_ENABLED/)).not.toBeNull();
    });
  });

  it('is registered in the shell and drives only the ghost surface', () => {
    const page = readFileSync(join(WEB_SRC, 'pages', 'GhostViewer.tsx'), 'utf-8');
    const routes = readFileSync(join(WEB_SRC, 'lib', 'routes.ts'), 'utf-8');
    const app = readFileSync(join(WEB_SRC, 'App.tsx'), 'utf-8');

    expect(page).toContain('/api/ghost-viewer/screen');
    expect(page).toContain('/api/ghost-viewer/tap');
    expect(page).toContain('/api/ghost-viewer/app/launch');
    expect(page).toContain('/api/ghost-viewer/app/install');
    // Never a target param — this page is ghost-only by construction.
    expect(page).not.toContain('?target=');
    expect(routes).toContain("path: '/ghost'");
    expect(app).toContain('<Route path="/ghost"><GhostViewer /></Route>');
  });
});
