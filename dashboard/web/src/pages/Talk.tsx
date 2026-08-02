/**
 * Talk.tsx — Talk mode page: a live voice conversation with the Homie over
 * OpenAI Realtime (WebRTC).
 *
 * Flow: GET /api/talk/status (readiness) → POST /api/talk/session (ephemeral
 * client secret minted by Python) → browser-direct SDP offer POST to OpenAI
 * (`offerUrl`, Bearer = clientSecret) → `oai-events` data channel drives the
 * status line + transcript.
 *
 * The WebRTC handshake is a subset port of the reference transport at
 * `.tmp/openclaw-pr100671/openclaw-talk-webrtc.ts` (class
 * `WebRtcSdpRealtimeTalkTransport`) — no camera/video, no consult. Function
 * calls (tools minted into the session server-side) are relayed to the
 * Python-owned `POST /api/talk/tool` endpoint and their outputs fed back as
 * conversation items.
 *
 * The minted clientSecret is kept in memory only (inside the transport
 * instance) and is never logged or persisted.
 *
 * Above `lg` the page is a two-column grid (`1fr 360px`) whose right cell is
 * the Archon run sidebar — epic #252 / ticket #257, architecture F9. Below
 * `lg` it collapses back to the original single centered column with the
 * sidebar stacked underneath.
 */

import { useEffect, useRef, useState } from 'preact/hooks';
import { Mic, RefreshCw, Square } from 'lucide-preact';
import { RunSidebar } from '@/components/RunSidebar';
import { TopBar } from '@/components/TopBar';
import { ApiError, apiGet, apiPost, dashboardToken, describeApiError } from '@/lib/api';

const OFFER_TIMEOUT_MS = 30_000;
/** Per-row and total caps for the session-end flush payload. `keepalive`
 * fetches cap the body at 64KB, and the Python side truncates to its own
 * 15k-char tail anyway — send the freshest rows that fit. */
const FLUSH_ROW_CHARS = 2_000;
const FLUSH_TOTAL_CHARS = 12_000;
const RUN_POLL_MS = 5_000;
const DEFAULT_RUN_POLL_CAP_MS = 600_000;
/** How long to watch each run kind before letting go (the work continues). */
const RUN_POLL_CAPS_MS: Record<string, number> = {
  skill: 600_000,
  agent: 2_700_000,
  archon: 10_800_000,
  look: 180_000,
};
/** A tool (or an injected run result) announcing async work to poll. */
const WORK_STARTED_RE = /WORK_STARTED #(\d+) kind=(\w+)/;

interface TalkStatusResponse {
  ok: boolean;
  configured: boolean;
  source: 'configured' | 'env' | 'codex-oauth' | null;
  detail: string;
  model: string;
  voice: string;
  voices: string[];
  killSwitchVoiceDisabled: boolean;
}

interface TalkSessionResponse {
  ok: boolean;
  clientSecret: string;
  expiresAt: number | null;
  offerUrl: string;
  model: string;
  voice: string;
  authSource: 'configured' | 'env' | 'codex-oauth';
}

interface TranscriptRow {
  id: number;
  role: 'user' | 'assistant';
  text: string;
  final: boolean;
}

type RealtimeServerEvent = {
  type?: string;
  transcript?: string;
  delta?: string;
  call_id?: string;
  name?: string;
  arguments?: string;
  error?: unknown;
};

interface TalkToolResponse {
  ok: boolean;
  output?: string;
}

interface TalkRunResponse {
  ok: boolean;
  status?: 'running' | 'done' | 'failed';
  output?: string;
  kind?: string;
  label?: string;
}

interface TalkTransportCallbacks {
  onStatus: (status: string) => void;
  onTranscript: (role: 'user' | 'assistant', text: string, final: boolean) => void;
  onError: (message: string) => void;
}

function describeAuthSource(source: TalkStatusResponse['source']): string {
  switch (source) {
    case 'configured':
      return 'configured API key';
    case 'env':
      return 'OPENAI_API_KEY';
    case 'codex-oauth':
      return 'Codex OAuth (ChatGPT sign-in)';
    default:
      return 'not configured';
  }
}

/** Session-mint errors carry the FastAPI shape `{detail: {error, switch?}}`. */
function talkSessionError(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: { error?: unknown } } | null)?.detail;
    if (detail && typeof detail.error === 'string' && detail.error.trim()) {
      return detail.error.trim();
    }
  }
  return describeApiError(err);
}

function realtimeEventError(error: unknown): string {
  if (!error || typeof error !== 'object') {
    return 'Realtime provider error';
  }
  const record = error as Record<string, unknown>;
  const message = typeof record.message === 'string' ? record.message.trim() : '';
  const code = typeof record.code === 'string' ? record.code.trim() : '';
  const type = typeof record.type === 'string' ? record.type.trim() : '';
  return message || code || type || 'Realtime provider error';
}

/**
 * WebRTC SDP realtime transport — subset port of the reference
 * `WebRtcSdpRealtimeTalkTransport` (setup :79-177, offer POST :256-295,
 * event handling :397-491). Audio-in/audio-out only.
 */
class TalkRealtimeTransport {
  private peer: RTCPeerConnection | null = null;
  private channel: RTCDataChannel | null = null;
  private media: MediaStream | null = null;
  private audio: HTMLAudioElement | null = null;
  private offerAbort: AbortController | null = null;
  private closed = false;

  constructor(
    private readonly session: TalkSessionResponse,
    private readonly callbacks: TalkTransportCallbacks,
  ) {}

  async start(): Promise<void> {
    if (typeof RTCPeerConnection === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      throw new Error('Talk mode needs a browser with WebRTC and microphone access.');
    }
    this.closed = false;
    const peer = new RTCPeerConnection();
    this.peer = peer;
    this.audio = document.createElement('audio');
    this.audio.autoplay = true;
    this.audio.style.display = 'none';
    document.body.append(this.audio);
    peer.addEventListener('track', (event) => {
      const stream = event.streams[0];
      if (this.audio && stream) {
        this.audio.srcObject = stream;
      }
    });

    const media = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (this.closed) {
      media.getTracks().forEach((track) => track.stop());
      return;
    }
    this.media = media;
    for (const track of media.getAudioTracks()) {
      peer.addTrack(track, media);
    }

    const channel = peer.createDataChannel('oai-events');
    this.channel = channel;
    channel.addEventListener('open', () => this.callbacks.onStatus('Listening…'));
    channel.addEventListener('message', (event) => this.handleRealtimeEvent(event.data));
    peer.addEventListener('connectionstatechange', () => {
      if (this.closed) {
        return;
      }
      if (peer.connectionState === 'failed' || peer.connectionState === 'closed') {
        this.callbacks.onError('Realtime connection closed');
        this.stop();
      }
    });

    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    const answerSdp = await this.postOffer(offer);
    if (this.closed) {
      return;
    }
    await peer.setRemoteDescription({ type: 'answer', sdp: answerSdp });
  }

  private async postOffer(offer: RTCSessionDescriptionInit): Promise<string> {
    const controller = new AbortController();
    this.offerAbort = controller;
    const timeout = window.setTimeout(() => controller.abort(), OFFER_TIMEOUT_MS);
    try {
      const res = await fetch(this.session.offerUrl, {
        method: 'POST',
        body: offer.sdp,
        headers: {
          Authorization: `Bearer ${this.session.clientSecret}`,
          'Content-Type': 'application/sdp',
        },
        signal: controller.signal,
      });
      if (!res.ok) {
        throw new Error(`Realtime WebRTC setup failed (${res.status})`);
      }
      return await res.text();
    } finally {
      window.clearTimeout(timeout);
      if (this.offerAbort === controller) {
        this.offerAbort = null;
      }
    }
  }

  stop(): void {
    this.closed = true;
    this.offerAbort?.abort();
    this.offerAbort = null;
    this.channel?.close();
    this.channel = null;
    this.peer?.close();
    this.peer = null;
    this.media?.getTracks().forEach((track) => track.stop());
    this.media = null;
    this.audio?.remove();
    this.audio = null;
  }

  private handleRealtimeEvent(data: unknown): void {
    if (this.closed) {
      return;
    }
    let event: RealtimeServerEvent;
    try {
      event = JSON.parse(String(data)) as RealtimeServerEvent;
    } catch {
      return;
    }
    switch (event.type) {
      case 'conversation.item.input_audio_transcription.completed':
        if (event.transcript) {
          this.callbacks.onTranscript('user', event.transcript, true);
        }
        return;
      case 'response.output_audio_transcript.delta':
        if (event.delta) {
          this.callbacks.onTranscript('assistant', event.delta, false);
        }
        return;
      case 'response.output_audio_transcript.done':
        if (event.transcript) {
          this.callbacks.onTranscript('assistant', event.transcript, true);
        }
        return;
      case 'input_audio_buffer.speech_started':
        this.callbacks.onStatus('Listening…');
        return;
      case 'input_audio_buffer.speech_stopped':
        this.callbacks.onStatus('Processing…');
        return;
      case 'response.created':
        this.callbacks.onStatus('Homie is thinking…');
        return;
      case 'response.done':
        this.callbacks.onStatus('Listening…');
        return;
      case 'response.function_call_arguments.done':
        void this.handleFunctionCall(event);
        return;
      case 'error':
        this.callbacks.onError(realtimeEventError(event.error));
        return;
      default:
    }
  }

  /**
   * Relay a model function call to the Python-owned tool endpoint, then feed
   * the output back as a conversation item and let the model speak it. The
   * session stays alive on tool failure — the model hears the error text.
   */
  private async handleFunctionCall(event: RealtimeServerEvent): Promise<void> {
    const callId = typeof event.call_id === 'string' ? event.call_id : '';
    const name = typeof event.name === 'string' ? event.name : '';
    if (!callId || !name) {
      return;
    }
    let args: Record<string, unknown> = {};
    try {
      const parsed = JSON.parse(event.arguments || '{}') as unknown;
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        args = parsed as Record<string, unknown>;
      }
    } catch {
      // fall through — a malformed arguments blob executes with {}
    }
    this.callbacks.onStatus(`Using ${name}…`);
    let output: string;
    try {
      const res = await apiPost<TalkToolResponse>('/api/talk/tool', { name, arguments: args });
      output = typeof res.output === 'string' && res.output.trim() ? res.output : '(no output)';
    } catch (err) {
      output = `Tool ${name} failed: ${describeApiError(err)}`;
    }
    this.sendClientEvent({
      type: 'conversation.item.create',
      item: { type: 'function_call_output', call_id: callId, output },
    });
    this.sendClientEvent({ type: 'response.create' });
    this.watchForRun(output);
  }

  /** Start polling if this text announced async work. */
  private watchForRun(text: string): void {
    const started = WORK_STARTED_RE.exec(text);
    if (started) {
      this.pollRun(started[1], started[2]);
    }
  }

  /**
   * Async runs: the tool returned instantly with a started receipt — poll the
   * registry until the run terminates, then inject the result as a user-role
   * note so the model speaks it the moment it lands. Caps are per kind: an
   * Archon build gets hours, a screen look gets minutes.
   *
   * A finished run can itself announce a follow-on run (a skill that outgrew
   * its budget hands off to a background agent), so results are re-scanned.
   */
  private pollRun(runId: string, kind: string): void {
    const startedAt = Date.now();
    const cap = RUN_POLL_CAPS_MS[kind] ?? DEFAULT_RUN_POLL_CAP_MS;
    const tick = async (): Promise<void> => {
      if (this.closed || Date.now() - startedAt > cap) {
        return;
      }
      try {
        const run = await apiGet<TalkRunResponse>(`/api/talk/runs/${runId}`);
        if (run.status === 'running') {
          window.setTimeout(() => void tick(), RUN_POLL_MS);
          return;
        }
        const result = run.output || '(no output)';
        this.sendClientEvent({
          type: 'conversation.item.create',
          item: {
            type: 'message',
            role: 'user',
            content: [
              {
                type: 'input_text',
                text:
                  `Work run #${runId} (${run.kind ?? kind}) finished with status ` +
                  `'${run.status ?? 'done'}'. Result: ${result}\n\n` +
                  'Summarize this aloud for owner in one to three spoken sentences.',
              },
            ],
          },
        });
        this.sendClientEvent({ type: 'response.create' });
        this.watchForRun(result);
      } catch {
        // transient API failure — keep polling within the cap
        window.setTimeout(() => void tick(), RUN_POLL_MS);
      }
    };
    void tick();
  }

  private sendClientEvent(payload: Record<string, unknown>): void {
    if (this.channel && this.channel.readyState === 'open') {
      this.channel.send(JSON.stringify(payload));
    }
  }
}

export function Talk() {
  const [status, setStatus] = useState<TalkStatusResponse | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [selectedVoice, setSelectedVoice] = useState('');
  const [sessionState, setSessionState] = useState<'idle' | 'starting' | 'active'>('idle');
  const [liveStatus, setLiveStatus] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const transportRef = useRef<TalkRealtimeTransport | null>(null);
  const nextRowId = useRef(1);
  // Session-end vault debrief state. The transcript mirror exists because the
  // flush fires from unmount cleanup / pagehide, where reading React state is
  // stale; the flushed flag stops the stop-button + unmount + pagehide trio
  // from triple-firing one session.
  const transcriptRef = useRef<TranscriptRow[]>([]);
  const flushSessionIdRef = useRef('');
  const flushStartedAtRef = useRef('');
  const flushedRef = useRef(false);

  async function refreshStatus() {
    setStatusLoading(true);
    try {
      const res = await apiGet<TalkStatusResponse>('/api/talk/status');
      setStatus(res);
      setSelectedVoice((current) => current || res.voice || '');
      setError(null);
    } catch (err) {
      setStatus(null);
      setError(describeApiError(err));
    } finally {
      setStatusLoading(false);
    }
  }

  /**
   * Session-end vault debrief: post the finalized transcript so Python can
   * spawn the detached memory_flush (daily log + episode). `keepalive` lets
   * the request survive tab close/navigation, and a flush failure never
   * surfaces on the page — but it DOES re-arm the guard, so a later stop/
   * navigation/pagehide retries instead of silently losing the debrief
   * (the server's 60s dedup absorbs any double-delivery).
   */
  function flushSession() {
    if (flushedRef.current || !flushSessionIdRef.current) return;
    flushedRef.current = true;
    const rows: { role: string; text: string }[] = [];
    let budget = FLUSH_TOTAL_CHARS;
    // Walk newest-first so the freshest exchanges survive the budget cap,
    // then restore chronological order for the flush prompt.
    for (let i = transcriptRef.current.length - 1; i >= 0 && budget > 0; i--) {
      const row = transcriptRef.current[i];
      if (!row.final || !row.text.trim()) continue;
      const text = row.text.slice(0, FLUSH_ROW_CHARS).slice(0, budget);
      budget -= text.length;
      rows.push({ role: row.role, text });
    }
    if (rows.length === 0) return;
    rows.reverse();
    const headers: Record<string, string> = { 'content-type': 'application/json' };
    if (dashboardToken) headers['Authorization'] = `Bearer ${dashboardToken}`;
    // Capture the session this attempt belongs to: a late failure response
    // must not re-arm the guard after a NEW session has started.
    const attemptSessionId = flushSessionIdRef.current;
    const rearm = () => {
      if (flushSessionIdRef.current === attemptSessionId) flushedRef.current = false;
    };
    void fetch('/api/talk/flush', {
      method: 'POST',
      headers,
      keepalive: true,
      body: JSON.stringify({
        sessionId: attemptSessionId,
        startedAt: flushStartedAtRef.current || undefined,
        transcript: rows,
      }),
    })
      .then(async (res) => {
        if (!res.ok) {
          rearm();
          return;
        }
        // The endpoint returns HTTP 200 even for server-side failures
        // (teardown must never surface as a page error) — the receipt's
        // status carries the truth. `skipped` counts as delivered.
        const receipt = (await res.json().catch(() => null)) as { status?: string } | null;
        if (receipt?.status === 'error') rearm();
      })
      .catch(rearm);
  }

  useEffect(() => {
    void refreshStatus();
    const onPageHide = () => flushSession();
    window.addEventListener('pagehide', onPageHide);
    return () => {
      window.removeEventListener('pagehide', onPageHide);
      transportRef.current?.stop();
      transportRef.current = null;
      flushSession();
    };
  }, []);

  function appendTranscript(role: 'user' | 'assistant', text: string, final: boolean) {
    // The ref is the canonical accumulator, updated synchronously — the
    // session-end flush reads it from stop/pagehide/unmount where React
    // state would be one commit stale. State just mirrors it for render.
    const prev = transcriptRef.current;
    const last = prev[prev.length - 1];
    let next: TranscriptRow[];
    if (role === 'assistant') {
      if (!final) {
        // Coalesce streaming deltas into one growing assistant message.
        if (last && last.role === 'assistant' && !last.final) {
          next = [...prev.slice(0, -1), { ...last, text: last.text + text }];
        } else {
          next = [...prev, { id: nextRowId.current++, role, text, final: false }];
        }
      } else if (last && last.role === 'assistant' && !last.final) {
        // `.done` replaces the growing message with the final transcript.
        next = [...prev.slice(0, -1), { ...last, text, final: true }];
      } else {
        next = [...prev, { id: nextRowId.current++, role, text, final: true }];
      }
    } else {
      next = [...prev, { id: nextRowId.current++, role, text, final: true }];
    }
    transcriptRef.current = next;
    setTranscript(next);
  }

  async function startTalk() {
    setError(null);
    if (typeof RTCPeerConnection === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      setError('Talk mode needs a browser with WebRTC and microphone access. Open this page in a current Chrome or Edge build.');
      return;
    }
    setSessionState('starting');
    setLiveStatus(null);
    setTranscript([]);
    transcriptRef.current = [];
    flushSessionIdRef.current =
      typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}`;
    flushStartedAtRef.current = new Date().toISOString();
    flushedRef.current = false;
    try {
      const session = await apiPost<TalkSessionResponse>('/api/talk/session', {
        voice: selectedVoice || undefined,
      });
      const transport = new TalkRealtimeTransport(session, {
        onStatus: (next) => setLiveStatus(next),
        onTranscript: appendTranscript,
        onError: (message) => setError(message),
      });
      transportRef.current = transport;
      await transport.start();
      setSessionState('active');
    } catch (err) {
      transportRef.current?.stop();
      transportRef.current = null;
      setSessionState('idle');
      setLiveStatus(null);
      setError(talkSessionError(err));
    }
  }

  function stopTalk() {
    transportRef.current?.stop();
    transportRef.current = null;
    setSessionState('idle');
    setLiveStatus(null);
    flushSession();
  }

  const ready = Boolean(status?.configured) && !status?.killSwitchVoiceDisabled;
  const starting = sessionState === 'starting';
  const active = sessionState === 'active';

  return (
    <div class="flex flex-col h-full min-h-0">
      <TopBar
        title="Talk"
        subtitle={status?.configured ? `${status.model} / ${status.voice}` : 'OpenAI Realtime voice session'}
        actions={(
          <button
            type="button"
            onClick={() => void refreshStatus()}
            class="w-9 h-8 inline-flex items-center justify-center rounded-md border border-[var(--color-border)] hover:bg-[var(--color-hover)]"
            title="Refresh"
            disabled={statusLoading}
          >
            <RefreshCw size={15} />
          </button>
        )}
      />

      <div class="flex-1 min-h-0 overflow-y-auto p-3 sm:p-4 md:p-6">
        {/*
          F9 layout: below `lg` this is the original single `max-w-3xl` column
          with the run sidebar stacked underneath (one column, `min-w-0` on
          both cells, so nothing forces a horizontal scroll). At `lg` the page
          widens to exactly 768 + 16 + 360 and the sidebar claims the gutter —
          the conversation column keeps its original reading measure.
        */}
        <div class="mx-auto w-full max-w-3xl lg:max-w-[1144px] grid gap-4 lg:grid-cols-[1fr_360px] lg:items-start">
          <div class="grid gap-4 min-w-0">
            <section class="border border-[var(--color-border)] rounded-md bg-[var(--color-card)]">
              <div class="p-3 sm:p-4 border-b border-[var(--color-border)] flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                <div class="min-w-0">
                  <div class="text-sm font-semibold">Talk Mode</div>
                  <div class="text-xs text-[var(--color-text-muted)]">
                    {statusLoading
                      ? 'Checking readiness...'
                      : status?.configured
                        ? `Ready via ${describeAuthSource(status.source)}`
                        : 'Not configured'}
                  </div>
                </div>
                <div class="w-full sm:w-auto">
                  {active || starting ? (
                    <button
                      type="button"
                      onClick={stopTalk}
                      disabled={starting}
                      class="min-h-11 sm:min-h-8 w-full sm:w-auto px-3 inline-flex items-center justify-center gap-2 rounded-md border border-[var(--color-border)] text-sm hover:bg-[var(--color-hover)] disabled:opacity-50 disabled:pointer-events-none"
                    >
                      <Square size={15} />
                      {starting ? 'Connecting...' : 'Stop'}
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void startTalk()}
                      disabled={!ready || statusLoading}
                      class="min-h-11 sm:min-h-8 w-full sm:w-auto px-3 inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium bg-[var(--color-primary)] text-white hover:opacity-90 disabled:opacity-50 disabled:pointer-events-none"
                    >
                      <Mic size={15} />
                      Start
                    </button>
                  )}
                </div>
              </div>

              <div class="p-3 sm:p-4 grid gap-3">
                <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <Metric label="Auth" value={status?.configured ? describeAuthSource(status.source) : 'not configured'} />
                  <Metric label="Model" value={status?.model ?? '...'} />
                  <Metric label="Session" value={active ? 'live' : starting ? 'connecting' : 'idle'} />
                </div>

                <label class="grid gap-1 text-xs text-[var(--color-text-muted)]">
                  Voice
                  <select
                    value={selectedVoice}
                    onChange={(e) => setSelectedVoice((e.target as HTMLSelectElement).value)}
                    disabled={!ready || active || starting}
                    class="min-h-11 sm:min-h-8 px-3 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] text-sm text-[var(--color-text)] disabled:opacity-50"
                  >
                    {(status?.voices ?? []).map((voice) => (
                      <option key={voice} value={voice}>{voice}</option>
                    ))}
                  </select>
                </label>

                {status && !status.configured && (
                  <div class="border border-[var(--color-border)] rounded-md p-3 text-sm text-[var(--color-text-muted)]">
                    {status.detail || 'Talk mode is not configured.'}
                  </div>
                )}

                {status?.killSwitchVoiceDisabled && (
                  <div class="border border-amber-500/40 rounded-md p-3 text-sm text-amber-500">
                    Voice kill-switch is disabled — Talk sessions are blocked until the voice switch is enabled.
                  </div>
                )}

                {liveStatus && (
                  <div class="text-xs text-[var(--color-text-muted)]">{liveStatus}</div>
                )}

                {error && (
                  <div class="text-sm text-red-500 break-words">{error}</div>
                )}
              </div>
            </section>

            <section class="border border-[var(--color-border)] rounded-md bg-[var(--color-card)] overflow-hidden">
              <div class="px-3 py-2 text-xs uppercase tracking-wide text-[var(--color-text-muted)] border-b border-[var(--color-border)]">
                Transcript
              </div>
              <div class="divide-y divide-[var(--color-border)]">
                {transcript.length === 0 && (
                  <div class="px-3 py-3 text-sm text-[var(--color-text-muted)]">
                    No transcript yet. Start a session and speak.
                  </div>
                )}
                {transcript.map((row) => (
                  <div key={row.id} class="px-3 py-3">
                    <div class="text-xs uppercase tracking-wide text-[var(--color-text-muted)] mb-1">
                      {row.role === 'user' ? 'You' : 'Homie'}{row.final ? '' : ' …'}
                    </div>
                    <div class="text-sm whitespace-pre-wrap break-words">{row.text}</div>
                  </div>
                ))}
              </div>
            </section>
          </div>

          <RunSidebar />
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div class="rounded-md border border-[var(--color-border)] p-3 min-w-0">
      <div class="text-xs text-[var(--color-text-muted)] mb-1">{label}</div>
      <div class="text-sm font-medium break-words">{value}</div>
    </div>
  );
}
