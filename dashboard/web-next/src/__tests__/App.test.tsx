import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

describe('React donor foundation', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/');
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes('/api/agents')) {
          return jsonResponse({
            agents: [
              {
                id: 'main',
                name: 'Main Homie',
                description: 'The local co-founder',
                model: 'auto',
                running: true,
                lane: 'generic',
              },
              {
                id: 'sales',
                name: 'Sales Homie',
                description: 'Pipeline and revenue',
                model: 'auto',
                running: false,
                lane: 'generic',
              },
            ],
          });
        }
        if (path.includes('/history')) return jsonResponse({ turns: [] });
        if (path.includes('/browser-viewer/status')) {
          return jsonResponse({
            mode: 'read_only',
            readiness: {
              status: 'ready',
              cdp_port: 18222,
              cdp_reachable: true,
              browser: 'Chromium',
              visible_guard: 'visible',
              tab_count: 2,
              reason: '',
            },
            stream: {
              enabled: false,
              connected: false,
              port: null,
              screencasting: false,
            },
            controls: { browser_input: false, navigation: false },
          });
        }
        if (path.includes('/browser-viewer/screenshot')) {
          return new Response(new Blob(['frame'], { type: 'image/png' }), { status: 200 });
        }
        return jsonResponse({ ok: true });
      }),
    );
  });

  it('renders Homie personas in the donor shell without granting computer control', async () => {
    render(<App />);
    expect(await screen.findAllByText('Main Homie')).not.toHaveLength(0);
    expect(screen.getByRole('textbox', { name: 'Message Main Homie' })).toBeInTheDocument();
    expect(screen.getByText('read only')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Take the wheel · locked' })).toBeDisabled();
    expect(screen.getByText('Local authority')).toBeInTheDocument();
  });
});
