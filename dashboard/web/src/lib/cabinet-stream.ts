/**
 * Cabinet SSE consumer — Homie-native (PRD-8 Phase 5a / WS4.2).
 *
 * NOT a port of `chat-stream.ts:22-67` (that file uses plain EventSource
 * without Last-Event-ID/410). Built on the Phase 3 conversation SSE
 * pattern (`.claude/scripts/dashboard_api.py:1825-1920`).
 *
 * Behaviors:
 *   - EventSource reconnect honors Last-Event-ID via standard browser
 *     EventSource header (browser appends automatically across reconnects).
 *   - On 410 Gone, fall back to `/api/cabinet/transcripts?meetingId=` paginated
 *     refetch honoring the `X-Refetch-Hint` response header.
 *   - Discriminated render: switch over `event.type` for each of the 20
 *     CabinetEvent variants (camelCase wire fields preserved).
 */

import { signal } from '@preact/signals';
import { tokenizedSseUrl, dashboardToken, ApiError } from './api';
import { translateCabinetEventOutbound } from './translate-personas';

export type CabinetEventType =
  | 'meeting_state'
  | 'turn_start'
  | 'status_update'
  | 'router_decision'
  | 'agent_selected'
  | 'agent_typing'
  | 'agent_chunk'
  | 'agent_done'
  | 'intervention_skipped'
  | 'tool_call'
  | 'tool_result'
  | 'turn_complete'
  | 'turn_aborted'
  | 'system_note'
  | 'divider'
  | 'meeting_state_update'
  | 'meeting_ended'
  | 'replay_gap'
  | 'error'
  | 'ping';

export interface CabinetEvent {
  type: CabinetEventType;
  [k: string]: unknown;
}

export interface CabinetStreamHandle {
  close(): void;
  refetched: ReturnType<typeof signal<boolean>>;
  connected: ReturnType<typeof signal<boolean>>;
}

export interface OpenCabinetStreamOpts {
  meetingId: number;
  onEvent: (evt: CabinetEvent, seq: number) => void;
  /** Called on 410 Gone — caller should refetch /transcripts and reset its state. */
  onRefetchHint?: (hint: string) => void;
  /** Called on terminal error (auth fail, repeated reconnect failure). */
  onError?: (err: Error) => void;
}

/** Open an SSE stream + return a handle. Caller invokes .close() on unmount. */
export function openCabinetStream(opts: OpenCabinetStreamOpts): CabinetStreamHandle {
  const refetched = signal(false);
  const connected = signal(false);
  let closed = false;
  let es: EventSource | null = null;

  function open(): void {
    if (closed) return;
    const path = `/api/cabinet/stream?meetingId=${opts.meetingId}`;
    es = new EventSource(tokenizedSseUrl(path));
    es.onopen = () => { connected.value = true; };
    es.onerror = () => {
      connected.value = false;
      // Browser EventSource auto-reconnects with Last-Event-ID header on
      // most error cases. We listen for fetch failures too — on 410, the
      // browser sends the Last-Event-ID and the server responds with the
      // X-Refetch-Hint header; the browser doesn't expose that to us,
      // so we double-check via a tiny fetch().
      void check410();
    };
    es.addEventListener('message', (ev: MessageEvent) => {
      let parsed: unknown;
      try { parsed = JSON.parse(ev.data); } catch { return; }
      if (!parsed || typeof parsed !== 'object') return;
      const obj = parsed as { seq?: number; event?: CabinetEvent };
      const seq = typeof obj.seq === 'number' ? obj.seq : 0;
      const event = obj.event;
      if (!event || typeof event !== 'object') return;
      // Q4 outbound translation (dashboard-owner GAP 1 fix): SSE byte-streaming
      // through Hono moves the second authoritative Q4 site here. Every persona-
      // id-bearing field on the CabinetEvent payload gets default→main mapped
      // before the UI consumes it.
      const translated = translateCabinetEventOutbound(event as Record<string, unknown>) as CabinetEvent;
      opts.onEvent(translated, seq);
    });
    es.addEventListener('ping', () => { /* keepalive — ignore */ });
  }

  async function check410(): Promise<void> {
    if (closed) return;
    if (!dashboardToken) return;
    try {
      const res = await fetch(`/api/cabinet/stream?meetingId=${opts.meetingId}`, {
        method: 'GET',
        headers: { Authorization: `Bearer ${dashboardToken}` },
      });
      if (res.status === 410) {
        const hint = res.headers.get('X-Refetch-Hint') ?? '';
        refetched.value = true;
        opts.onRefetchHint?.(hint || `GET /api/cabinet/transcripts?meetingId=${opts.meetingId}`);
      }
    } catch (err) {
      opts.onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  }

  function close(): void {
    closed = true;
    if (es) { es.close(); es = null; }
    connected.value = false;
  }

  open();
  return { close, refetched, connected };
}

/** Fetch a page of transcripts for the cabinet (used by 410-fallback). */
export async function fetchCabinetTranscripts(
  meetingId: number,
  beforeId?: number,
): Promise<{ transcript: Array<{ id: number; speaker: string; text: string; created_at: number }> }> {
  const qs = new URLSearchParams();
  qs.set('meetingId', String(meetingId));
  if (beforeId !== undefined) qs.set('beforeId', String(beforeId));
  const res = await fetch(`/api/cabinet/transcripts?${qs.toString()}`, {
    method: 'GET',
    headers: dashboardToken ? { Authorization: `Bearer ${dashboardToken}` } : {},
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body, `transcripts fetch failed: ${res.status}`);
  }
  return res.json();
}
