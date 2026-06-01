import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { MobileAccess } from '@/pages/MobileAccess';

const WEB_SRC = join(__dirname, '..');

function mobileAccessPayload() {
  return {
    status: 'ready',
    mode: 'read_only',
    tailscale: {
      available: true,
      backend_state: 'Running',
      hostname: 'Smoke',
      dns_name: 'homie.tailnet.test',
      ips: ['100.64.0.10', 'fd7a:115c:a1e0::1'],
      primary_ip: '100.64.0.10',
      error: null,
    },
    dashboard: {
      web_port: 5173,
      request_host: '100.64.0.10:5173',
      urls: {
        root: 'http://100.64.0.10:5173/',
        browser: 'http://100.64.0.10:5173/browser',
        teams: 'http://100.64.0.10:5173/teams',
        mobile: 'http://100.64.0.10:5173/mobile',
      },
      bind_hint: 'npm run dev -- --host 100.64.0.10',
    },
    serve: {
      available: true,
      enabled: true,
      http: true,
      https: true,
      hosts: ['homie.tailnet.test:80'],
      ports: [{ port: '80', http: true, https: false }],
      error: null,
    },
    controls: {
      mutates_tailscale: false,
      mutates_browser: false,
    },
  };
}

describe('Mobile Access page', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify(mobileAccessPayload()), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    ) as any;
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn(async () => undefined) },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders copyable tailnet dashboard URLs from the Python status endpoint', async () => {
    render(<MobileAccess />);

    expect(await screen.findByText('http://100.64.0.10:5173/browser')).toBeInTheDocument();
    expect(screen.getByText('http://100.64.0.10:5173/teams')).toBeInTheDocument();
    expect(screen.getByText('homie.tailnet.test')).toBeInTheDocument();
    expect(screen.getByText('npm run dev -- --host 100.64.0.10')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Copy Browser Viewer'));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('http://100.64.0.10:5173/browser');
    });
  });

  it('falls back to document copy on non-secure local origins', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    });
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    });

    render(<MobileAccess />);

    await screen.findByText('http://100.64.0.10:5173/teams');
    fireEvent.click(screen.getByTitle('Copy Teams'));
    await waitFor(() => {
      expect(execCommand).toHaveBeenCalledWith('copy');
    });
  });

  it('keeps the page registered and read-only', () => {
    const page = readFileSync(join(WEB_SRC, 'pages', 'MobileAccess.tsx'), 'utf-8');
    const routes = readFileSync(join(WEB_SRC, 'lib', 'routes.ts'), 'utf-8');
    const app = readFileSync(join(WEB_SRC, 'App.tsx'), 'utf-8');

    expect(page).toContain('/api/dashboard/mobile-access');
    expect(page).toContain('mutates_tailscale');
    expect(page).toContain('mutates_browser');
    expect(page).not.toContain('apiPost');
    expect(page).not.toContain('apiPatch');
    expect(routes).toContain("path: '/mobile'");
    expect(app).toContain('<Route path="/mobile"><MobileAccess /></Route>');
  });
});
