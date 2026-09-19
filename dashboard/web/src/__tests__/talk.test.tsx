/**
 * talk.test.tsx — Talk mode UI surface tests.
 *
 * Coverage:
 *   - Talk.tsx renders readiness from GET /api/talk/status (humanized auth
 *     source, model, voice picker defaulted to status.voice).
 *   - Not-configured status renders the Python `detail` text as the CTA and
 *     disables Start.
 *   - Kill-switch-disabled status renders the warning and disables Start.
 *   - Start click without RTCPeerConnection (happy-dom has no WebRTC)
 *     surfaces a friendly error instead of crashing — and never POSTs a
 *     session mint.
 *   - Static contract: page wired into App.tsx router + lib/routes.ts nav,
 *     and the WebRTC handshake literals live in Talk.tsx.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/preact';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Talk, TalkRealtimeTransport } from '@/pages/Talk';

const WEB_SRC = join(__dirname, '..');

function statusPayload(overrides: Record<string, unknown> = {}) {
  return {
    ok: true,
    configured: true,
    source: 'codex-oauth',
    detail: 'Signed in via Codex OAuth.',
    model: 'gpt-4o-realtime-preview',
    voice: 'marin',
    voices: ['marin', 'cedar'],
    killSwitchVoiceDisabled: false,
    ...overrides,
  };
}

function stubStatusFetch(payload: unknown) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.startsWith('/api/talk/status')) {
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }
    return new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  });
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe('talk UI surface — static contract', () => {
  it('Talk.tsx carries the WebRTC handshake + talk API literals', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    expect(src).toContain('/api/talk/status');
    expect(src).toContain('/api/talk/session');
    expect(src).toContain('RTCPeerConnection');
    expect(src).toContain("createDataChannel('oai-events')");
    expect(src).toContain('application/sdp');
    expect(src).toContain('getUserMedia');
    expect(src).toContain('response.output_audio_transcript.delta');
    expect(src).toContain('conversation.item.input_audio_transcription.completed');
  });

  it('Talk.tsx relays model function calls to the Python tool endpoint', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    expect(src).toContain('response.function_call_arguments.done');
    expect(src).toContain('/api/talk/tool');
    expect(src).toContain('conversation.item.create');
    expect(src).toContain('function_call_output');
    expect(src).toContain('response.create');
  });

  it('Talk.tsx posts the session-end vault debrief with keepalive', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    expect(src).toContain('/api/talk/flush');
    expect(src).toContain('keepalive: true');
    expect(src).toContain('pagehide');
  });

  it('Talk.tsx re-arms the flush guard on 200 {status:error} receipts', () => {
    // The endpoint returns HTTP 200 even for server-side spawn failures —
    // res.ok alone must NOT be treated as delivery (codex R2 high). The
    // re-arm is session-scoped so a late failure can't poison a new session.
    const src = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    expect(src).toMatch(/status.*===.*'error'/);
    expect(src).toContain('attemptSessionId');
    expect(src).toMatch(/\.catch\(rearm\)/);
  });

  it('Talk.tsx polls async runs of every kind and injects results for speech', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    expect(src).toContain('WORK_STARTED');
    expect(src).toContain('/api/talk/runs/');
    expect(src).toContain('pollRun');
    expect(src).toContain('input_text');
  });

  it('Talk.tsx caps each run kind and re-scans results so chained runs keep polling', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    // An Archon build must outlive a skill's cap, or results stop being spoken.
    expect(src).toContain('RUN_POLL_CAPS_MS');
    expect(src).toMatch(/archon:\s*10_800_000/);
    expect(src).toMatch(/agent:\s*2_700_000/);
    // A finished run can announce a follow-on run (skill -> agent handoff).
    expect(src).toContain('watchForRun');
  });

  it('the browser sentinel regex matches the Python registry format', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    const match = /const WORK_STARTED_RE = (\/.+\/);/.exec(src);
    expect(match).not.toBeNull();
    // eslint-disable-next-line no-eval
    const re = eval(match![1]) as RegExp;
    // Literal copy of talk_runs.started_sentinel() output.
    const parsed = re.exec('WORK_STARTED #12 kind=archon (archon-clutch) trailing words');
    expect(parsed).not.toBeNull();
    expect(parsed![1]).toBe('12');
    expect(parsed![2]).toBe('archon');
  });

  it('Talk page is registered in the router and nav registry', () => {
    const appSrc = readFileSync(join(WEB_SRC, 'App.tsx'), 'utf-8');
    expect(appSrc).toContain("path=\"/talk\"");
    expect(appSrc).toContain('<Talk />');
    const routesSrc = readFileSync(join(WEB_SRC, 'lib', 'routes.ts'), 'utf-8');
    expect(routesSrc).toContain("path: '/talk'");
    expect(routesSrc).toContain("label: 'Talk'");
  });
});

describe('Talk page', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders readiness from /api/talk/status with a defaulted voice picker', async () => {
    stubStatusFetch(statusPayload());
    render(<Talk />);

    await screen.findByText('Codex OAuth (ChatGPT sign-in)');
    expect(screen.getByText('gpt-4o-realtime-preview')).toBeInTheDocument();
    expect(screen.getByText('Ready via Codex OAuth (ChatGPT sign-in)')).toBeInTheDocument();

    const select = screen.getByLabelText('Voice') as HTMLSelectElement;
    expect(select.value).toBe('marin');
    expect(select.options.length).toBe(2);
  });

  it('renders the Python detail text as the call-to-action when not configured', async () => {
    stubStatusFetch(statusPayload({
      configured: false,
      source: null,
      detail: 'Add an OpenAI API key under Settings to enable Talk mode.',
    }));
    render(<Talk />);

    await screen.findByText('Add an OpenAI API key under Settings to enable Talk mode.');
    expect(screen.getByRole('button', { name: /start/i })).toBeDisabled();
  });

  it('renders the kill-switch warning and disables Start', async () => {
    stubStatusFetch(statusPayload({ killSwitchVoiceDisabled: true }));
    render(<Talk />);

    await screen.findByText(/Voice kill-switch is disabled/);
    expect(screen.getByRole('button', { name: /start/i })).toBeDisabled();
  });

  it('surfaces a friendly error when WebRTC is unavailable (no session mint POST)', async () => {
    const fetchMock = stubStatusFetch(statusPayload());
    render(<Talk />);

    // Wait for the status load so Start becomes enabled.
    await screen.findByText('Codex OAuth (ChatGPT sign-in)');
    const start = screen.getByRole('button', { name: /start/i });
    expect(start).toBeEnabled();
    // happy-dom defines neither RTCPeerConnection nor navigator.mediaDevices.
    expect(typeof RTCPeerConnection).toBe('undefined');

    fireEvent.click(start);
    await screen.findByText(/WebRTC and microphone access/);

    const sessionCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).startsWith('/api/talk/session'),
    );
    expect(sessionCalls).toEqual([]);
  });

  it('never posts a session-end flush when no session ever started', async () => {
    const fetchMock = stubStatusFetch(statusPayload());
    const { unmount } = render(<Talk />);
    await screen.findByText('Codex OAuth (ChatGPT sign-in)');

    // A failed start (no WebRTC here) never assigns a flush session id …
    fireEvent.click(screen.getByRole('button', { name: /start/i }));
    await screen.findByText(/WebRTC and microphone access/);
    // … so neither pagehide nor unmount may fire the debrief endpoint.
    window.dispatchEvent(new Event('pagehide'));
    unmount();

    const flushCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).startsWith('/api/talk/flush'),
    );
    expect(flushCalls).toEqual([]);
  });
});


describe('Talk function-call correlation', () => {
  it('sends the same namespaced Realtime call ID on repeated transport attempts', async () => {
    const fetchMock = stubStatusFetch(statusPayload());
    const transport = new TalkRealtimeTransport({} as never, {
      onStatus: vi.fn(), onTranscript: vi.fn(), onError: vi.fn(),
    });
    // Invoke the real relay without starting WebRTC or requesting a microphone.
    const relay = transport as unknown as {
      handleFunctionCall: (event: { call_id: string; name: string; arguments: string }) => Promise<void>;
    };
    const event = { call_id: 'call-123', name: 'memory_search', arguments: '{"query":"test"}' };
    await relay.handleFunctionCall(event);
    await relay.handleFunctionCall(event);
    const requests = fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/talk/tool'));
    expect(requests).toHaveLength(2);
    for (const request of requests) {
      const init = (request as unknown as [RequestInfo, RequestInit])[1];
      expect(JSON.parse(String(init.body))).toMatchObject({
        name: 'memory_search', originKey: 'realtime:call-123', arguments: { query: 'test' },
      });
    }
  });
});
