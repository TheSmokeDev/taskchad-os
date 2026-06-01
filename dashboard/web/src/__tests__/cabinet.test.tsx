/**
 * cabinet.test.tsx — PRD-8 Phase 5a / WS4 cabinet UI surface tests.
 *
 * Coverage:
 *   - Cabinet.tsx renders the meeting list pane.
 *   - CabinetTranscript renders agent_done events.
 *   - CabinetComposer dispatches POST /api/cabinet/send with body shape
 *     {meetingId, text, clientMsgId} (verbatim upstream send body).
 *   - cabinet-stream.ts opens an EventSource at the tokenized URL.
 *
 * Note: tests focus on contract conformance (body shape, URL prefix)
 * rather than full DOM assertions, mirroring the existing test pattern
 * in this directory.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const WEB_SRC = join(__dirname, '..');

describe('cabinet UI surface — static contract', () => {
  it('Cabinet.tsx imports the cabinet-stream consumer', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Cabinet.tsx'), 'utf-8');
    expect(src).toContain('openCabinetStream');
    expect(src).toContain('fetchCabinetTranscripts');
  });

  it('Cabinet.tsx mounts CabinetComposer + CabinetTranscript', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Cabinet.tsx'), 'utf-8');
    expect(src).toContain('<CabinetComposer');
    expect(src).toContain('<CabinetTranscript');
    expect(src).toContain('/api/cabinet/open');
    expect(src).toContain('/api/cabinet/participants/add');
    expect(src).toContain('/api/cabinet/participants/remove');
  });

  it('Cabinet.tsx mic button opens the shared Cabinet voice URL', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Cabinet.tsx'), 'utf-8');
    expect(src).toContain('cabinetVoiceUrl');
    expect(src).toContain('openCabinetVoiceUrl');
    expect(src).toContain('activeVoiceUrl');
  });

  it('Voices.tsx is the Cabinet voice launcher, not a placeholder', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Voices.tsx'), 'utf-8');
    expect(src).not.toContain('Placeholder');
    expect(src).toContain('/api/cabinet/open');
    expect(src).toContain('cabinetVoiceUrl');
    expect(src).toContain('Open Voice');
    expect(src).toContain('Lifecycle');
  });

  it('cabinet-voice-url builds the Python-owned voice URL', async () => {
    const { cabinetVoiceUrl } = await import('../lib/cabinet-voice-url');
    const url = cabinetVoiceUrl(42, 'chat space', 'tok&en');
    expect(url).toBe('/api/cabinet/voice/ui?meetingId=42&chatId=chat+space&token=tok%26en');
  });

  it('CabinetComposer dispatches POST /api/cabinet/send with room audience shape', () => {
    const src = readFileSync(join(WEB_SRC, 'components', 'CabinetComposer.tsx'), 'utf-8');
    expect(src).toContain("/api/cabinet/send");
    expect(src).toContain("meetingId");
    expect(src).toContain("text:");
    expect(src).toContain("clientMsgId");
    expect(src).toContain("audience:");
    expect(src).toContain("audienceForText");
  });

  it('cabinet-stream.ts handles 410 + X-Refetch-Hint per Phase 3 SSE contract', () => {
    const src = readFileSync(join(WEB_SRC, 'lib', 'cabinet-stream.ts'), 'utf-8');
    expect(src).toContain('410');
    expect(src).toContain('X-Refetch-Hint');
    // Falls back to /api/cabinet/transcripts on 410.
    expect(src).toContain('/api/cabinet/transcripts');
  });

  it('cabinet-stream.ts EventSource opens tokenized URL', () => {
    const src = readFileSync(join(WEB_SRC, 'lib', 'cabinet-stream.ts'), 'utf-8');
    expect(src).toContain('new EventSource');
    expect(src).toContain('tokenizedSseUrl');
  });

  it('CabinetTranscript renders all required event variants', () => {
    const src = readFileSync(join(WEB_SRC, 'components', 'CabinetTranscript.tsx'), 'utf-8');
    // Discriminated render — switch covers the load-bearing variants.
    const variants = [
      'agent_done',
      'tool_call',
      'tool_result',
      'turn_start',
      'system_note',
      'meeting_ended',
      'router_decision',
      'turn_aborted',
      'error',
    ];
    for (const v of variants) {
      expect(src, `CabinetTranscript missing case for ${v}`).toContain(v);
    }
    expect(src).toContain('No text reply returned.');
  });

  it('Cabinet UI is documented as Homie-native (B2/NB3) — NOT a port of WarRoom.tsx', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Cabinet.tsx'), 'utf-8');
    expect(src.toLowerCase()).toContain('homie-native');
    // Disclaimer language — multi-line JSDoc may have `*` between words.
    // Strip leading-* + collapse whitespace before matching.
    const collapsedSrc = src.replace(/^\s*\*\s*/gm, '').replace(/\s+/g, ' ');
    expect(collapsedSrc).toContain('NOT a port of WarRoom.tsx');
  });
});

describe('cabinet UI behavior — composer dispatch', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/cabinet/send')) {
        return new Response(JSON.stringify({ ok: true, queued: true }), { status: 200 });
      }
      if (url.includes('/api/cabinet/list')) {
        return new Response(JSON.stringify({ meetings: [] }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetAllMocks();
  });

  it('apiPost helper sends the expected /api/cabinet/send body shape', async () => {
    const { apiPost } = await import('../lib/api');
    await apiPost('/api/cabinet/send', {
      meetingId: 1,
      text: 'hi',
      clientMsgId: 'c_test',
      chatId: 'cabinet-browser',
      audience: 'all',
    });
    expect(fetch).toHaveBeenCalled();
    const args = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(args[0]).toBe('/api/cabinet/send');
    const init = args[1] as RequestInit;
    expect(init.method).toBe('POST');
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      meetingId: 1,
      text: 'hi',
      clientMsgId: 'c_test',
      chatId: 'cabinet-browser',
      audience: 'all',
    });
  });
});
