import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/preact';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { BrowserViewer } from '@/pages/BrowserViewer';

const WEB_SRC = join(__dirname, '..');

function statusPayload(overrides: Record<string, unknown> = {}) {
  return {
    mode: 'read_only',
    readiness: {
      status: 'ready',
      cdp_port: 9222,
      cdp_reachable: true,
      browser: 'Chrome/126',
      visible_guard: 'visible',
      tab_count: 2,
      reason: 'ready',
    },
    stream: {
      enabled: true,
      connected: true,
      port: 31137,
      screencasting: false,
      reason: 'ready',
      direct_ws_url: 'ws://127.0.0.1:31137',
      ...(overrides.stream as Record<string, unknown> | undefined),
    },
    controls: {
      browser_input: false,
      navigation: false,
    },
    ...overrides,
  };
}

describe('Browser Viewer page', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:browser-viewer'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders base64 JPEG frames from the read-only stream', async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify(statusPayload()), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    ) as any;

    class MockWebSocket {
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: (() => void) | null = null;

      constructor(public url: string) {
        window.setTimeout(() => {
          this.onopen?.();
          this.onmessage?.({
            data: JSON.stringify({ type: 'frame', data: 'ZmFrZS1qcGVn' }),
          });
        }, 0);
      }

      close() {}
    }
    vi.stubGlobal('WebSocket', MockWebSocket);

    render(<BrowserViewer />);

    const img = await screen.findByAltText('Browser viewport') as HTMLImageElement;
    await waitFor(() => {
      expect(img.src).toContain('data:image/jpeg;base64,ZmFrZS1qcGVn');
    });
    expect(screen.getByText('Browser Viewer')).toBeInTheDocument();
    expect(screen.getByText('Browser Input')).toBeInTheDocument();
    expect(screen.getByText('Navigation')).toBeInTheDocument();
  });

  it('falls back to screenshot capture when direct stream URL is absent', async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === '/api/browser-viewer/screenshot') {
        return new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }), {
          status: 200,
          headers: { 'content-type': 'image/png' },
        });
      }
      return new Response(JSON.stringify(statusPayload({
        stream: {
          enabled: true,
          connected: true,
          port: 31137,
          screencasting: false,
          reason: 'ready',
        },
      })), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }) as any;

    render(<BrowserViewer />);

    const img = await screen.findByAltText('Browser viewport') as HTMLImageElement;
    await waitFor(() => {
      expect(img.src).toContain('blob:browser-viewer');
    });
    expect(fetch).toHaveBeenCalledWith('/api/browser-viewer/screenshot', expect.any(Object));
  });

  it('keeps the page read-only and registered in the shell', () => {
    const page = readFileSync(join(WEB_SRC, 'pages', 'BrowserViewer.tsx'), 'utf-8');
    const routes = readFileSync(join(WEB_SRC, 'lib', 'routes.ts'), 'utf-8');
    const app = readFileSync(join(WEB_SRC, 'App.tsx'), 'utf-8');

    expect(page).toContain('/api/browser-viewer/status');
    expect(page).toContain('/api/browser-viewer/screenshot');
    expect(page).toContain('/api/browser-viewer/stream/enable');
    expect(page).toContain('/api/browser-viewer/stream/disable');
    expect(page).not.toMatch(/\.send\s*\(/);
    expect(page).not.toContain('input_mouse');
    expect(page).not.toContain('input_keyboard');
    expect(routes).toContain("path: '/browser'");
    expect(app).toContain('<Route path="/browser"><BrowserViewer /></Route>');
  });
});
