/**
 * archon-runs.ts — browser client + pure reducer for the Archon live-run
 * telemetry bridge (epic #252 / ticket #257, architecture F9).
 *
 * Two sources, two jobs — this split is forced by the #254 wire contract, not
 * a preference:
 *
 *   - `GET /api/archon/events` (REST) is the IDENTITY source. It is the only
 *     surface that returns ledger `runs[]` rows, so `workflowName` and the
 *     authoritative run `status` exist nowhere else. It also backfills the
 *     event history the tail deliberately never replays (the cursor starts at
 *     the API process's boot), and it is the target named by the 410
 *     `X-Refetch-Hint`.
 *   - `GET /api/archon/stream` (SSE) is the DELTA source. Its snapshot frame
 *     carries `events` only — no `runs` — so a stream-only sidebar could never
 *     name a workflow.
 *
 * Consumer contract honored here, verbatim from `dashboard_api.py:7526-7534`:
 *   - only ring-backed entries carry an `id:` line, so the browser's
 *     `Last-Event-ID` cursor always names a real seq (we never fabricate one);
 *   - an `events_dropped` frame means live frames were shed — refetch REST;
 *   - `run_ended` closes the stream (per-run scopes only; the sidebar runs
 *     unscoped, but the frame is classified so a scoped caller works too).
 *
 * The reducer is IDEMPOTENT per ledger event id. That is load-bearing: a REST
 * reseed and the SSE replay window overlap by construction, so every event is
 * routinely applied twice. Status/node/approval are last-write-wins; tool
 * calls dedupe on the ledger row id (`event.id`), which is the only field that
 * makes a repeated append detectable.
 *
 * Event `data` is hostile input — LLM-authored `tool_input` and node output.
 * Python already redacts and caps it; every value is re-coerced to a bounded
 * string here before it reaches a DOM node.
 *
 * NOTE — no `new Map(` / `new Set(` at module scope: the Rule 2 grep in
 * `__tests__/anti-patterns.test.tsx:117-135` bans that shape under `src/lib/`.
 * These lookup tables are frozen literals and readonly arrays instead.
 */

import { ApiError, apiGet, dashboardToken, tokenizedSseUrl } from './api';

/** Newest ledger events pulled per REST refresh (server caps at 500). */
export const ARCHON_SNAPSHOT_LIMIT = 200;

/** Hard bound on the REST snapshot so a hung upstream cannot spin forever. */
export const ARCHON_SNAPSHOT_TIMEOUT_MS = 12_000;
/** Tool calls kept per run card — the sidebar shows a tail, not a log. */
export const MAX_TOOL_CALLS = 4;
/** Run cards kept. The unscoped tail sees every run forever; this bounds it. */
export const MAX_RUN_CARDS = 8;
/** Display cap for any string lifted out of a hostile `data` blob. */
const MAX_TEXT_CHARS = 120;

/** Event type -> the run status it proves. Mirrors `TERMINAL_EVENT_TYPES`. */
const TERMINAL_EVENT_STATUS: Readonly<Record<string, string>> = {
  workflow_completed: 'completed',
  workflow_failed: 'failed',
  workflow_cancelled: 'cancelled',
  workflow_abandoned: 'abandoned',
};

/** Ledger statuses that mean the run is over (`archon_events.py:95-102`). */
const TERMINAL_RUN_STATUSES: readonly string[] = [
  'completed',
  'failed',
  'cancelled',
  'canceled',
  'abandoned',
  'error',
];

/** `data` keys Archon may use to correlate a tool_completed to its tool_called. */
const TOOL_CORRELATION_KEYS: readonly string[] = [
  'tool_call_id',
  'toolCallId',
  'call_id',
  'callId',
];

/** One normalized ledger event (`archon_events.normalize_event_row`). */
export interface ArchonEvent {
  id: string;
  runId: string;
  type: string;
  stepIndex: number | null;
  stepName: string | null;
  createdAt: string;
  data: Record<string, unknown>;
}

/** One run-ledger row (`archon_events.read_run_rows`). */
export interface ArchonRunRow {
  runId: string;
  conversationId: string;
  workflowName: string;
  status: string;
  startedAt: string | null;
  completedAt: string | null;
  lastActivityAt: string | null;
  workingPath: string | null;
}

/** `GET /api/archon/events` body, normalized against a hostile/partial shape. */
export interface ArchonSnapshot {
  status: string;
  runStatus: string;
  events: ArchonEvent[];
  runs: ArchonRunRow[];
  latestSeq: number;
}

export interface ArchonToolCall {
  /** Ledger row id of the event that created this entry — the dedupe key. */
  key: string;
  name: string;
  /** null while the call is still in flight (no `tool_completed` yet). */
  durationMs: number | null;
  /** Correlation id when Archon supplied one; '' when it did not. */
  correlationId: string;
  /**
   * Ledger row id of the `tool_completed` that closed this call, or ''.
   *
   * Load-bearing for idempotency: closing a call UPDATES an entry rather than
   * appending one, so the completion's own id never reaches `key`. Without a
   * record of it, a replayed completion (the REST seed and the SSE
   * subscribe-time snapshot overlap the same rows) finds no still-open name
   * match and appends itself as a SECOND call — the real Archon ledger
   * carries no correlation ids on tool rows, so the name fallback is the
   * live path, not an edge case.
   */
  completedBy: string;
}

export interface ArchonRunCard {
  runId: string;
  workflowName: string;
  status: string;
  currentNode: string | null;
  nodeState: 'running' | 'completed' | null;
  toolCalls: ArchonToolCall[];
  approvalPending: boolean;
  approvalNote: string | null;
  /** naive-UTC 'YYYY-MM-DD HH:MM:SS' — fixed width, so string compare sorts. */
  lastActivityAt: string;
  ended: boolean;
}

/** Cards keyed by run id. A plain object, so spreading stays immutable. */
export type ArchonRunCards = Readonly<Record<string, ArchonRunCard>>;

/** The four frame kinds the #254 stream can deliver. */
export type ArchonStreamFrame =
  | { kind: 'snapshot'; status: string; events: ArchonEvent[] }
  | { kind: 'event'; event: ArchonEvent }
  | { kind: 'ended'; runId: string }
  | { kind: 'dropped' };

// ─────────────────────────────────────────────────────────────────────────────
// Hostile-input coercion
// ─────────────────────────────────────────────────────────────────────────────
function text(value: unknown, max: number): string {
  if (typeof value === 'string') return value.trim().slice(0, max);
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}

function finiteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  return null;
}

function record(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

/** Coerce one wire event. Returns null when it carries no usable run id. */
export function normalizeEvent(raw: unknown): ArchonEvent | null {
  const obj = record(raw);
  const runId = text(obj.runId, 80);
  if (!runId) return null;
  const stepIndex = finiteNumber(obj.stepIndex);
  const stepName = text(obj.stepName, MAX_TEXT_CHARS);
  return {
    id: text(obj.id, 80),
    runId,
    type: text(obj.type, 60),
    stepIndex,
    stepName: stepName || null,
    createdAt: text(obj.createdAt, 40),
    data: record(obj.data),
  };
}

function normalizeRunRow(raw: unknown): ArchonRunRow | null {
  const obj = record(raw);
  const runId = text(obj.runId, 80);
  if (!runId) return null;
  return {
    runId,
    conversationId: text(obj.conversationId, 80),
    workflowName: text(obj.workflowName, MAX_TEXT_CHARS),
    status: text(obj.status, 40).toLowerCase(),
    startedAt: text(obj.startedAt, 40) || null,
    completedAt: text(obj.completedAt, 40) || null,
    lastActivityAt: text(obj.lastActivityAt, 40) || null,
    workingPath: text(obj.workingPath, MAX_TEXT_CHARS) || null,
  };
}

export function isTerminalStatus(status: string): boolean {
  return TERMINAL_RUN_STATUSES.includes(status.trim().toLowerCase());
}

// ─────────────────────────────────────────────────────────────────────────────
// Frame classification
// ─────────────────────────────────────────────────────────────────────────────
/**
 * Classify one `message` frame payload (`{seq, event}` already unwrapped to
 * the inner `event`). Unknown shapes return null rather than guessing.
 */
export function classifyFrame(raw: unknown): ArchonStreamFrame | null {
  const obj = record(raw);
  const type = text(obj.type, 60);
  if (type === 'archon_snapshot') {
    const events = Array.isArray(obj.events) ? obj.events : [];
    return {
      kind: 'snapshot',
      status: text(obj.status, 40) || 'ok',
      events: events.map(normalizeEvent).filter((e): e is ArchonEvent => e !== null),
    };
  }
  if (type === 'events_dropped') return { kind: 'dropped' };
  if (type === 'run_ended') {
    const runId = text(obj.runId, 80);
    return runId ? { kind: 'ended', runId } : null;
  }
  const event = normalizeEvent(obj);
  return event ? { kind: 'event', event } : null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Reducer — pure, idempotent per ledger event id
// ─────────────────────────────────────────────────────────────────────────────
function placeholderCard(runId: string): ArchonRunCard {
  return {
    runId,
    // Superseded by the ledger row on the next REST refresh, which an unknown
    // run id triggers. Until then the run id is the only honest name we have.
    workflowName: `run ${runId.slice(0, 8)}`,
    // An event just landed for this run and it was not a terminal one, so the
    // run is producing telemetry right now. The ledger row overwrites this.
    status: 'running',
    currentNode: null,
    nodeState: null,
    toolCalls: [],
    approvalPending: false,
    approvalNote: null,
    lastActivityAt: '',
    ended: false,
  };
}

export function cardFromRunRow(row: ArchonRunRow): ArchonRunCard {
  const status = row.status || 'unknown';
  return {
    runId: row.runId,
    workflowName: row.workflowName || `run ${row.runId.slice(0, 8)}`,
    status,
    currentNode: null,
    nodeState: null,
    toolCalls: [],
    approvalPending: false,
    approvalNote: null,
    lastActivityAt: row.lastActivityAt ?? row.completedAt ?? row.startedAt ?? '',
    ended: isTerminalStatus(status),
  };
}

function nodeLabel(event: ArchonEvent): string {
  const data = event.data;
  return (
    event.stepName ??
    (text(data.node_name, MAX_TEXT_CHARS) ||
      text(data.step_name, MAX_TEXT_CHARS) ||
      text(data.node, MAX_TEXT_CHARS))
  );
}

function toolLabel(event: ArchonEvent): string {
  return text(event.data.tool_name, MAX_TEXT_CHARS) || text(event.data.name, MAX_TEXT_CHARS);
}

function correlationId(event: ArchonEvent): string {
  for (const key of TOOL_CORRELATION_KEYS) {
    const value = text(event.data[key], 80);
    if (value) return value;
  }
  return '';
}

function appendToolCall(
  calls: readonly ArchonToolCall[],
  event: ArchonEvent,
  durationMs: number | null,
): ArchonToolCall[] {
  const name = toolLabel(event);
  if (!name) return calls as ArchonToolCall[];
  // Idempotency: the same ledger row arriving twice (REST reseed + SSE replay)
  // must not append twice.
  if (event.id && calls.some((call) => call.key === event.id)) {
    return calls as ArchonToolCall[];
  }
  const entry: ArchonToolCall = {
    key: event.id || `${event.runId}:${event.createdAt}:${name}`,
    name,
    durationMs,
    correlationId: correlationId(event),
    completedBy: durationMs === null ? '' : event.id,
  };
  return [...calls, entry].slice(-MAX_TOOL_CALLS);
}

/**
 * Fold a `tool_completed` into the entry its `tool_called` opened.
 *
 * Match order: Archon's own correlation id when the payload carries one,
 * otherwise the newest still-open entry with the same tool name. The name
 * fallback can mis-pair two concurrent calls to the same tool — it only ever
 * mislabels a duration, never invents or drops a row, which is the right
 * trade for a glanceable sidebar.
 */
function completeToolCall(
  calls: readonly ArchonToolCall[],
  event: ArchonEvent,
): ArchonToolCall[] {
  const duration = finiteNumber(event.data.duration_ms);
  const cid = correlationId(event);
  const name = toolLabel(event);

  // Already folded this exact completion — the REST seed and the SSE
  // subscribe-time snapshot overlap, so every completion in the overlap
  // arrives twice. Re-appending it is the duplicate-row defect.
  if (event.id && calls.some((call) => call.completedBy === event.id)) {
    return calls as ArchonToolCall[];
  }

  let index = -1;
  if (cid) {
    index = calls.findIndex((call) => call.correlationId === cid);
  }
  if (index === -1 && name) {
    for (let i = calls.length - 1; i >= 0; i -= 1) {
      if (calls[i].name === name && calls[i].durationMs === null) {
        index = i;
        break;
      }
    }
  }
  if (index === -1) {
    // No open call to close — the `tool_called` predates our window. Record
    // the completion itself so the operator still sees the tool ran.
    return appendToolCall(calls, event, duration);
  }
  const updated = [...calls];
  updated[index] = {
    ...updated[index],
    durationMs: duration,
    completedBy: event.id || updated[index].completedBy,
  };
  return updated;
}

/** Fold one ledger event into the card map. Pure; safe to re-apply. */
export function applyArchonEvent(cards: ArchonRunCards, event: ArchonEvent): ArchonRunCards {
  if (!event.runId) return cards;
  const prev = cards[event.runId] ?? placeholderCard(event.runId);
  const next: ArchonRunCard = { ...prev };
  if (event.createdAt && event.createdAt > next.lastActivityAt) {
    next.lastActivityAt = event.createdAt;
  }

  const terminal = TERMINAL_EVENT_STATUS[event.type];
  if (terminal) {
    next.status = terminal;
    next.ended = true;
    next.approvalPending = false;
    next.approvalNote = null;
    next.nodeState = next.currentNode ? 'completed' : null;
    return { ...cards, [event.runId]: next };
  }

  switch (event.type) {
    case 'workflow_started': {
      next.status = 'running';
      next.ended = false;
      break;
    }
    case 'node_started': {
      const label = nodeLabel(event);
      if (label) {
        next.currentNode = label;
        next.nodeState = 'running';
      }
      break;
    }
    case 'node_completed': {
      const label = nodeLabel(event);
      if (label) next.currentNode = label;
      next.nodeState = 'completed';
      break;
    }
    case 'tool_called': {
      next.toolCalls = appendToolCall(prev.toolCalls, event, null);
      break;
    }
    case 'tool_completed': {
      next.toolCalls = completeToolCall(prev.toolCalls, event);
      break;
    }
    case 'approval_requested': {
      next.approvalPending = true;
      next.approvalNote =
        text(event.data.message, MAX_TEXT_CHARS) ||
        text(event.data.prompt, MAX_TEXT_CHARS) ||
        text(event.data.reason, MAX_TEXT_CHARS) ||
        null;
      break;
    }
    case 'approval_received':
    case 'approval_resolved': {
      next.approvalPending = false;
      next.approvalNote = null;
      break;
    }
    default:
      break;
  }
  return { ...cards, [event.runId]: next };
}

/** Mark a run finished from a synthetic `run_ended` frame. */
export function applyRunEnded(cards: ArchonRunCards, runId: string): ArchonRunCards {
  const prev = cards[runId];
  if (!prev) return cards;
  return {
    ...cards,
    [runId]: { ...prev, ended: true, approvalPending: false, nodeState: prev.currentNode ? 'completed' : null },
  };
}

/**
 * Build the card map from a REST snapshot: ledger rows first (identity), then
 * the event history folded over them (live detail). Rows are the source of
 * truth for name + status; events only add node/tool/approval texture.
 */
export function seedRunCards(
  snapshot: ArchonSnapshot,
  previous?: ArchonRunCards,
): ArchonRunCards {
  let cards: ArchonRunCards = {};
  for (const row of snapshot.runs) {
    const fresh = cardFromRunRow(row);
    const prior = previous?.[row.runId];
    // This is a LIVE sidebar, not a run history. A run that was already
    // finished before we started watching never becomes a card — otherwise
    // the "no active runs" empty state is unreachable on any real ledger
    // (this one holds 923 completed, 314 failed, 237 cancelled rows). Runs
    // that FINISH while we watch keep their card, which is the useful case:
    // you see the thing you dispatched land.
    if (fresh.ended && !prior) continue;
    // A periodic reseed must not FORGET. The REST history is the newest N
    // events globally, so an approval raised by run A scrolls out of the
    // window as soon as another run is noisy — reseeding from that snapshot
    // alone silently cleared the amber "waiting on you" state for a run that
    // is still blocked. The ledger row is authoritative for status; the
    // event-derived detail below it survives until an event resolves it.
    cards = {
      ...cards,
      [row.runId]: prior
        ? {
            ...fresh,
            currentNode: prior.currentNode,
            nodeState: prior.nodeState,
            toolCalls: prior.toolCalls,
            // A finished run is never still waiting on the operator.
            approvalPending: fresh.ended ? false : prior.approvalPending,
            approvalNote: fresh.ended ? null : prior.approvalNote,
            workflowName: prior.workflowName.startsWith('run ')
              ? fresh.workflowName
              : prior.workflowName,
            lastActivityAt:
              fresh.lastActivityAt > prior.lastActivityAt
                ? fresh.lastActivityAt
                : prior.lastActivityAt,
          }
        : fresh,
    };
  }
  for (const event of snapshot.events) {
    cards = applyArchonEvent(cards, event);
  }
  // Events in the window can resurrect a run we deliberately skipped above
  // (the tail carries rows for long-finished runs). Drop anything that is
  // already over and that we were not already watching.
  let live: ArchonRunCards = {};
  for (const [runId, card] of Object.entries(cards)) {
    if (card.ended && !previous?.[runId]) continue;
    live = { ...live, [runId]: card };
  }
  return live;
}

/**
 * Render order: anything waiting on the operator first, then live runs, then
 * finished ones — each group newest-activity first. `lastActivityAt` is
 * archon.db's fixed-width naive-UTC format, so a string compare is ordinally
 * correct (`archon_events.py:40-42`).
 */
export function sortRunCards(cards: ArchonRunCards): ArchonRunCard[] {
  return Object.values(cards).sort((a, b) => {
    if (a.approvalPending !== b.approvalPending) return a.approvalPending ? -1 : 1;
    if (a.ended !== b.ended) return a.ended ? 1 : -1;
    return b.lastActivityAt.localeCompare(a.lastActivityAt);
  });
}

/** Bound the map so a long session cannot grow cards without limit. */
export function pruneRunCards(cards: ArchonRunCards, max?: number): ArchonRunCards {
  const limit = max ?? MAX_RUN_CARDS;
  const ordered = sortRunCards(cards);
  if (ordered.length <= limit) return cards;
  let out: ArchonRunCards = {};
  for (const card of ordered.slice(0, limit)) {
    out = { ...out, [card.runId]: card };
  }
  return out;
}

// ─────────────────────────────────────────────────────────────────────────────
// Honest status text
// ─────────────────────────────────────────────────────────────────────────────
/**
 * Turn the ledger `status` field into operator English. The #254 contract is
 * that this endpoint returns 200 with an honest status rather than 500ing on
 * a cold machine, so a non-`ok` value is a real answer, not an error.
 */
export function describeLedgerStatus(status: string): string | null {
  switch (status) {
    case 'ok':
      return null;
    case 'db_missing':
      return "Archon's run ledger isn't on this machine yet, so there is nothing to show.";
    case 'db_unreadable':
      return "Archon's run ledger is unreadable right now — telemetry is unavailable.";
    default:
      return `Archon telemetry is unavailable (${status}).`;
  }
}

/** Pull the operator-facing text out of a kill-switch 503 body. */
export function describeKillSwitch(err: ApiError): string {
  const body = record(err.body);
  const detail = record(body.detail);
  return (
    text(detail.error, 200) ||
    text(body.error, 200) ||
    'Archon event ingest is disabled by operator.'
  );
}

/** '842ms' / '3.4s' / '…' while the call is still open. */
export function formatToolDuration(durationMs: number | null): string {
  if (durationMs === null) return '…';
  if (durationMs < 1000) return `${Math.max(0, Math.round(durationMs))}ms`;
  return `${(durationMs / 1000).toFixed(1)}s`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Transport
// ─────────────────────────────────────────────────────────────────────────────
/** REST snapshot. Throws ApiError on a non-2xx (503 = kill-switch). */
export async function fetchArchonSnapshot(limit?: number): Promise<ArchonSnapshot> {
  const size = limit ?? ARCHON_SNAPSHOT_LIMIT;
  // A never-settling upstream is the forever-spinner: the sidebar only leaves
  // "Loading Archon telemetry…" once this settles, and an unbounded fetch may
  // never do so (Hono accepts the GET, its Python upstream hangs). Aborting
  // turns that into an ordinary failure the caller can render as
  // "telemetry unavailable". `AbortSignal.timeout` is guarded because jsdom
  // in older test envs does not implement it.
  const signal =
    typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function'
      ? AbortSignal.timeout(ARCHON_SNAPSHOT_TIMEOUT_MS)
      : undefined;
  const body = record(await apiGet<unknown>(`/api/archon/events?limit=${size}`, signal));
  const rawEvents = Array.isArray(body.events) ? body.events : [];
  const rawRuns = Array.isArray(body.runs) ? body.runs : [];
  return {
    status: text(body.status, 40) || 'ok',
    runStatus: text(body.runStatus, 40) || 'ok',
    events: rawEvents.map(normalizeEvent).filter((e): e is ArchonEvent => e !== null),
    runs: rawRuns.map(normalizeRunRow).filter((r): r is ArchonRunRow => r !== null),
    latestSeq: finiteNumber(body.latestSeq) ?? 0,
  };
}

export interface ArchonStreamHandle {
  close(): void;
}

export interface OpenArchonStreamOpts {
  /** Resume position — the `latestSeq` captured by the REST snapshot. */
  sinceSeq: number;
  onFrame: (frame: ArchonStreamFrame) => void;
  onOpen?: () => void;
  /** The socket dropped. EventSource reconnects itself; this is for the UI. */
  onDown?: () => void;
  /** Confirmed 410 replay-gap — the caller must refetch the REST snapshot. */
  onReplayGap?: (hint: string) => void;
}

/**
 * Open the unscoped Archon tail.
 *
 * The 410 probe follows `chat-stream.ts:155-171` rather than
 * `cabinet-stream.ts:105-123`: both re-request the URL because EventSource
 * hides the response status, but chat-stream aborts the body immediately, so
 * a non-410 probe does not leave a second stream fanning out server-side.
 */
export function openArchonStream(opts: OpenArchonStreamOpts): ArchonStreamHandle {
  let closed = false;
  const url = tokenizedSseUrl(`/api/archon/stream?sinceSeq=${opts.sinceSeq}`);
  const es = new EventSource(url);

  es.onopen = () => {
    if (closed) return;
    opts.onOpen?.();
  };

  es.onerror = () => {
    if (closed) return;
    opts.onDown?.();
    void probeReplayGap();
  };

  es.addEventListener('message', (ev: MessageEvent) => {
    if (closed) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(ev.data);
    } catch {
      return;
    }
    const envelope = record(parsed);
    const frame = classifyFrame(envelope.event);
    if (frame) opts.onFrame(frame);
  });

  // Keepalive — the server sends these with no id: line so the browser's
  // Last-Event-ID cursor keeps naming a real seq. Nothing to do.
  es.addEventListener('ping', () => {});

  async function probeReplayGap(): Promise<void> {
    if (closed || !opts.onReplayGap) return;
    try {
      const controller = new AbortController();
      const res = await fetch(url, {
        method: 'GET',
        headers: dashboardToken ? { Authorization: `Bearer ${dashboardToken}` } : {},
        signal: controller.signal,
      });
      controller.abort();
      if (res.status === 410) {
        opts.onReplayGap(res.headers.get('X-Refetch-Hint') || 'GET /api/archon/events');
      }
    } catch {
      // The stack is down, not the buffer. The degraded REST poll covers it.
    }
  }

  return {
    close(): void {
      closed = true;
      es.close();
    },
  };
}
