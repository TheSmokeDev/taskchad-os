/**
 * archon-runs.test.ts — the #257 sidebar's client + reducer (epic #252, F9).
 *
 * One test per distinct code path, exercising the real module (no
 * SimpleNamespace-style stand-ins): normalization of hostile wire payloads,
 * frame classification, every branch of the event reducer including the
 * idempotency that a REST-reseed/SSE-replay overlap depends on, ordering,
 * pruning, the honest-status text, and the transport (tokenized SSE URL,
 * frame delivery, 410 replay-gap probe, close()).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  applyArchonEvent,
  applyRunEnded,
  cardFromRunRow,
  classifyFrame,
  describeKillSwitch,
  describeLedgerStatus,
  fetchArchonSnapshot,
  formatToolDuration,
  isTerminalStatus,
  MAX_TOOL_CALLS,
  normalizeEvent,
  openArchonStream,
  pruneRunCards,
  seedRunCards,
  sortRunCards,
  type ArchonEvent,
  type ArchonRunCards,
  type ArchonRunRow,
} from '@/lib/archon-runs';
import { ApiError } from '@/lib/api';

type EventHandler = ((event?: unknown) => void | Promise<void>) | null;

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  onopen: EventHandler = null;
  onerror: EventHandler = null;
  readonly listeners = new Map<string, (event: MessageEvent) => void>();
  readonly url: string;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, handler: (event: MessageEvent) => void): void {
    this.listeners.set(name, handler);
  }

  close(): void {
    this.closed = true;
  }

  emit(payload: unknown): void {
    this.listeners.get('message')?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

/** A real wire event, shaped exactly like `normalize_event_row` emits. */
function event(overrides: Partial<ArchonEvent> & { type: string }): ArchonEvent {
  return {
    id: overrides.id ?? `evt-${overrides.type}`,
    runId: overrides.runId ?? 'run-1',
    type: overrides.type,
    stepIndex: overrides.stepIndex ?? null,
    stepName: overrides.stepName ?? null,
    createdAt: overrides.createdAt ?? '2026-07-27 20:00:00',
    data: overrides.data ?? {},
  };
}

function runRow(overrides: Partial<ArchonRunRow> = {}): ArchonRunRow {
  return {
    runId: 'run-1',
    conversationId: 'conv-1',
    workflowName: 'archon-piv-loop',
    status: 'running',
    startedAt: '2026-07-27 19:59:00',
    completedAt: null,
    lastActivityAt: '2026-07-27 20:00:00',
    workingPath: null,
    ...overrides,
  };
}

describe('normalizeEvent — hostile wire payloads', () => {
  it('drops an event with no run id', () => {
    expect(normalizeEvent({ id: 'x', type: 'node_started' })).toBeNull();
    expect(normalizeEvent(null)).toBeNull();
    expect(normalizeEvent('not-an-object')).toBeNull();
  });

  it('coerces non-string / non-object fields instead of trusting them', () => {
    const normalized = normalizeEvent({
      id: 42,
      runId: 'run-9',
      type: { evil: true },
      stepIndex: Number.NaN,
      stepName: ['array'],
      createdAt: '2026-07-27 20:00:00',
      data: ['not', 'a', 'record'],
    });
    expect(normalized).not.toBeNull();
    expect(normalized!.id).toBe('42');
    expect(normalized!.type).toBe('');
    expect(normalized!.stepIndex).toBeNull();
    expect(normalized!.stepName).toBeNull();
    expect(normalized!.data).toEqual({});
  });

  it('caps a long stepName so a hostile blob cannot blow up the card', () => {
    const normalized = normalizeEvent({
      runId: 'run-9',
      type: 'node_started',
      stepName: 'x'.repeat(500),
    });
    expect(normalized!.stepName!.length).toBe(120);
  });
});

describe('classifyFrame — the four documented stream frames', () => {
  it('classifies the archon_snapshot frame and normalizes its events', () => {
    const frame = classifyFrame({
      type: 'archon_snapshot',
      status: 'ok',
      events: [{ runId: 'run-1', type: 'node_started' }, { type: 'no-run-id' }],
    });
    expect(frame).toEqual({
      kind: 'snapshot',
      status: 'ok',
      events: [
        {
          id: '',
          runId: 'run-1',
          type: 'node_started',
          stepIndex: null,
          stepName: null,
          createdAt: '',
          data: {},
        },
      ],
    });
  });

  it('classifies events_dropped and run_ended', () => {
    expect(classifyFrame({ type: 'events_dropped', refetch: 'GET /api/archon/events' })).toEqual({
      kind: 'dropped',
    });
    expect(classifyFrame({ type: 'run_ended', runId: 'run-2', reason: 'terminal' })).toEqual({
      kind: 'ended',
      runId: 'run-2',
    });
  });

  it('classifies a ledger event, and returns null for an unusable frame', () => {
    const frame = classifyFrame({ id: 'e1', runId: 'run-1', type: 'tool_called' });
    expect(frame?.kind).toBe('event');
    expect(classifyFrame({ type: 'run_ended' })).toBeNull();
    expect(classifyFrame({ type: 'something_new' })).toBeNull();
  });
});

describe('cardFromRunRow — the ledger row is the identity source', () => {
  it('carries workflow name + status and marks a terminal row ended', () => {
    const card = cardFromRunRow(runRow({ status: 'completed', completedAt: '2026-07-27 20:05:00' }));
    expect(card.workflowName).toBe('archon-piv-loop');
    expect(card.status).toBe('completed');
    expect(card.ended).toBe(true);
  });

  it('falls back to the run id when the ledger has no workflow name', () => {
    const card = cardFromRunRow(runRow({ runId: '23c6c29ad89b', workflowName: '' }));
    expect(card.workflowName).toBe('run 23c6c29a');
    expect(card.ended).toBe(false);
  });

  it('isTerminalStatus covers both cancelled spellings the ledger uses', () => {
    expect(isTerminalStatus('cancelled')).toBe(true);
    expect(isTerminalStatus('canceled')).toBe(true);
    expect(isTerminalStatus('RUNNING')).toBe(false);
  });
});

describe('applyArchonEvent — one branch per event type', () => {
  const empty: ArchonRunCards = {};

  it('creates a placeholder card for a run with no ledger row yet', () => {
    const cards = applyArchonEvent(empty, event({ runId: 'abcdef1234', type: 'node_started', stepName: 'plan' }));
    expect(cards['abcdef1234'].workflowName).toBe('run abcdef12');
    expect(cards['abcdef1234'].status).toBe('running');
    expect(cards['abcdef1234'].currentNode).toBe('plan');
  });

  it('node_started sets the current node; node_completed marks it done', () => {
    let cards = applyArchonEvent(empty, event({ type: 'node_started', stepName: 'implement' }));
    expect(cards['run-1'].nodeState).toBe('running');
    cards = applyArchonEvent(cards, event({ type: 'node_completed', stepName: 'implement' }));
    expect(cards['run-1'].currentNode).toBe('implement');
    expect(cards['run-1'].nodeState).toBe('completed');
  });

  it('falls back to data.node_name when the row carries no stepName', () => {
    const cards = applyArchonEvent(empty, event({ type: 'node_started', data: { node_name: 'test-gate' } }));
    expect(cards['run-1'].currentNode).toBe('test-gate');
  });

  it('tool_called appends an open call; tool_completed fills its duration', () => {
    let cards = applyArchonEvent(
      empty,
      event({ id: 'e1', type: 'tool_called', data: { tool_name: 'Read', tool_call_id: 'c1' } }),
    );
    expect(cards['run-1'].toolCalls).toEqual([
      { key: 'e1', name: 'Read', durationMs: null, correlationId: 'c1', completedBy: '' },
    ]);
    cards = applyArchonEvent(
      cards,
      event({ id: 'e2', type: 'tool_completed', data: { tool_name: 'Read', tool_call_id: 'c1', duration_ms: 842 } }),
    );
    expect(cards['run-1'].toolCalls).toHaveLength(1);
    expect(cards['run-1'].toolCalls[0].durationMs).toBe(842);
  });

  it('matches on tool name when Archon sends no correlation id', () => {
    let cards = applyArchonEvent(empty, event({ id: 'e1', type: 'tool_called', data: { tool_name: 'Bash' } }));
    cards = applyArchonEvent(
      cards,
      event({ id: 'e2', type: 'tool_completed', data: { tool_name: 'Bash', duration_ms: 1500 } }),
    );
    expect(cards['run-1'].toolCalls).toHaveLength(1);
    expect(cards['run-1'].toolCalls[0].durationMs).toBe(1500);
  });

  it('records a tool_completed whose tool_called predates the window', () => {
    const cards = applyArchonEvent(
      empty,
      event({ id: 'e9', type: 'tool_completed', data: { tool_name: 'Grep', duration_ms: 12 } }),
    );
    expect(cards['run-1'].toolCalls).toEqual([
      { key: 'e9', name: 'Grep', durationMs: 12, correlationId: '', completedBy: 'e9' },
    ]);
  });

  it('is idempotent per ledger id — a replayed tool_called appends once', () => {
    const called = event({ id: 'e1', type: 'tool_called', data: { tool_name: 'Read' } });
    let cards = applyArchonEvent(empty, called);
    cards = applyArchonEvent(cards, called);
    cards = applyArchonEvent(cards, called);
    expect(cards['run-1'].toolCalls).toHaveLength(1);
  });

  it('keeps only the newest MAX_TOOL_CALLS entries', () => {
    let cards: ArchonRunCards = empty;
    for (let i = 0; i < MAX_TOOL_CALLS + 3; i += 1) {
      cards = applyArchonEvent(
        cards,
        event({ id: `e${i}`, type: 'tool_called', data: { tool_name: `tool-${i}` } }),
      );
    }
    const calls = cards['run-1'].toolCalls;
    expect(calls).toHaveLength(MAX_TOOL_CALLS);
    expect(calls[calls.length - 1].name).toBe(`tool-${MAX_TOOL_CALLS + 2}`);
  });

  it('approval_requested raises the flag with its note; approval_received clears it', () => {
    let cards = applyArchonEvent(
      empty,
      event({ type: 'approval_requested', data: { message: 'Approve the deploy?' } }),
    );
    expect(cards['run-1'].approvalPending).toBe(true);
    expect(cards['run-1'].approvalNote).toBe('Approve the deploy?');
    cards = applyArchonEvent(cards, event({ type: 'approval_received' }));
    expect(cards['run-1'].approvalPending).toBe(false);
    expect(cards['run-1'].approvalNote).toBeNull();
  });

  it('a terminal workflow event ends the run and drops a pending approval', () => {
    let cards = applyArchonEvent(empty, event({ type: 'approval_requested' }));
    cards = applyArchonEvent(cards, event({ type: 'node_started', stepName: 'ship' }));
    cards = applyArchonEvent(cards, event({ type: 'workflow_failed' }));
    expect(cards['run-1'].status).toBe('failed');
    expect(cards['run-1'].ended).toBe(true);
    expect(cards['run-1'].approvalPending).toBe(false);
    expect(cards['run-1'].nodeState).toBe('completed');
  });

  it('advances lastActivityAt forward only', () => {
    let cards = applyArchonEvent(empty, event({ type: 'node_started', createdAt: '2026-07-27 20:00:05' }));
    cards = applyArchonEvent(cards, event({ id: 'old', type: 'node_started', createdAt: '2026-07-27 19:00:00' }));
    expect(cards['run-1'].lastActivityAt).toBe('2026-07-27 20:00:05');
  });

  it('ignores an event with an empty run id', () => {
    const cards = applyArchonEvent(empty, { ...event({ type: 'node_started' }), runId: '' });
    expect(cards).toEqual(empty);
  });

  it('applyRunEnded marks a known run finished and no-ops on an unknown one', () => {
    const seeded = { 'run-1': cardFromRunRow(runRow()) };
    expect(applyRunEnded(seeded, 'run-1')['run-1'].ended).toBe(true);
    expect(applyRunEnded(seeded, 'nope')).toBe(seeded);
  });
});

describe('seedRunCards / sortRunCards / pruneRunCards', () => {
  it('layers event texture over the ledger identity', () => {
    const cards = seedRunCards({
      status: 'ok',
      runStatus: 'ok',
      latestSeq: 7,
      runs: [runRow()],
      events: [
        event({ id: 'a', type: 'node_started', stepName: 'implement' }),
        event({ id: 'b', type: 'tool_called', data: { tool_name: 'Edit' } }),
      ],
    });
    expect(cards['run-1'].workflowName).toBe('archon-piv-loop');
    expect(cards['run-1'].currentNode).toBe('implement');
    expect(cards['run-1'].toolCalls[0].name).toBe('Edit');
  });

  it('orders approval-pending first, then live runs, then finished by recency', () => {
    const cards: ArchonRunCards = {
      done: cardFromRunRow(runRow({ runId: 'done', status: 'completed', lastActivityAt: '2026-07-27 21:00:00' })),
      live: cardFromRunRow(runRow({ runId: 'live', lastActivityAt: '2026-07-27 20:00:00' })),
      gated: {
        ...cardFromRunRow(runRow({ runId: 'gated', lastActivityAt: '2026-07-27 19:00:00' })),
        approvalPending: true,
      },
    };
    expect(sortRunCards(cards).map((c) => c.runId)).toEqual(['gated', 'live', 'done']);
  });

  it('prunes to the cap, keeping the highest-priority cards', () => {
    let cards: ArchonRunCards = {};
    for (let i = 0; i < 12; i += 1) {
      const id = `run-${String(i).padStart(2, '0')}`;
      cards = {
        ...cards,
        [id]: cardFromRunRow(
          runRow({ runId: id, status: 'completed', lastActivityAt: `2026-07-27 20:00:${String(i).padStart(2, '0')}` }),
        ),
      };
    }
    const pruned = pruneRunCards(cards, 3);
    expect(Object.keys(pruned).sort()).toEqual(['run-09', 'run-10', 'run-11']);
  });

  it('returns the same object when nothing needs pruning', () => {
    const cards = { 'run-1': cardFromRunRow(runRow()) };
    expect(pruneRunCards(cards, 8)).toBe(cards);
  });
});

describe('honest status text', () => {
  it('describeLedgerStatus stays silent on ok and names each failure', () => {
    expect(describeLedgerStatus('ok')).toBeNull();
    expect(describeLedgerStatus('db_missing')).toMatch(/isn't on this machine/);
    expect(describeLedgerStatus('db_unreadable')).toMatch(/unreadable/);
    expect(describeLedgerStatus('weird_new_status')).toBe(
      'Archon telemetry is unavailable (weird_new_status).',
    );
  });

  it('describeKillSwitch reads the FastAPI nested detail and the flat shape', () => {
    const nested = new ApiError(503, { detail: { error: 'archon event ingest is disabled by operator' } }, 'x');
    expect(describeKillSwitch(nested)).toBe('archon event ingest is disabled by operator');
    const flat = new ApiError(503, { error: 'switched off' }, 'x');
    expect(describeKillSwitch(flat)).toBe('switched off');
    expect(describeKillSwitch(new ApiError(503, null, 'x'))).toMatch(/disabled by operator/);
  });

  it('formatToolDuration marks an open call and scales past a second', () => {
    expect(formatToolDuration(null)).toBe('…');
    expect(formatToolDuration(842)).toBe('842ms');
    expect(formatToolDuration(3400)).toBe('3.4s');
  });
});

describe('fetchArchonSnapshot', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('normalizes a partial body instead of trusting the wire', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ ok: true, events: null, runs: [{ runId: 'r1' }, {}] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    );
    const snapshot = await fetchArchonSnapshot();
    expect(snapshot.status).toBe('ok');
    expect(snapshot.events).toEqual([]);
    expect(snapshot.runs).toHaveLength(1);
    expect(snapshot.latestSeq).toBe(0);
  });

  it('passes the ledger status through and raises ApiError on a 503', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toContain('/api/archon/events?limit=');
      return new Response(JSON.stringify({ detail: { error: 'disabled' } }), { status: 503 });
    });
    vi.stubGlobal('fetch', fetchMock);
    await expect(fetchArchonSnapshot()).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('reports the ledger status verbatim when the DB is missing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ status: 'db_missing', events: [], runs: [], latestSeq: 0 }), {
          status: 200,
        }),
      ),
    );
    expect((await fetchArchonSnapshot()).status).toBe('db_missing');
  });
});

describe('openArchonStream', () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('opens the unscoped tail at the snapshot resume position', () => {
    const handle = openArchonStream({ sinceSeq: 41, onFrame: () => {} });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toContain('/api/archon/stream?sinceSeq=41');
    handle.close();
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });

  it('unwraps the {seq, event} envelope and delivers a classified frame', () => {
    const frames: string[] = [];
    openArchonStream({ sinceSeq: 0, onFrame: (frame) => frames.push(frame.kind) });
    const es = FakeEventSource.instances[0];
    es.emit({ seq: 0, event: { type: 'archon_snapshot', status: 'ok', events: [] } });
    es.emit({ seq: 5, event: { id: 'e1', runId: 'run-1', type: 'tool_called' } });
    es.emit({ seq: 0, event: { type: 'events_dropped', refetch: 'GET /api/archon/events' } });
    expect(frames).toEqual(['snapshot', 'event', 'dropped']);
  });

  it('drops a malformed payload without throwing', () => {
    const frames: string[] = [];
    openArchonStream({ sinceSeq: 0, onFrame: (frame) => frames.push(frame.kind) });
    const es = FakeEventSource.instances[0];
    es.listeners.get('message')?.({ data: 'not json' } as MessageEvent);
    es.emit({ seq: 1, event: { type: 'ping-ish' } });
    expect(frames).toEqual([]);
  });

  it('reports the socket down and surfaces a confirmed 410 replay gap', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response('{"error":"replay_gap"}', {
          status: 410,
          headers: { 'X-Refetch-Hint': 'GET /api/archon/events' },
        }),
      ),
    );
    let down = 0;
    let hint = '';
    openArchonStream({
      sinceSeq: 3,
      onFrame: () => {},
      onDown: () => { down += 1; },
      onReplayGap: (value) => { hint = value; },
    });
    await FakeEventSource.instances[0].onerror?.({ type: 'error' });
    expect(down).toBe(1);
    expect(hint).toBe('GET /api/archon/events');
  });

  it('a plain outage is not reported as a replay gap', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    let hint: string | null = null;
    let down = 0;
    openArchonStream({
      sinceSeq: 3,
      onFrame: () => {},
      onDown: () => { down += 1; },
      onReplayGap: (value) => { hint = value; },
    });
    await FakeEventSource.instances[0].onerror?.({ type: 'error' });
    expect(down).toBe(1);
    expect(hint).toBeNull();
  });

  it('a closed handle delivers nothing further', () => {
    const frames: string[] = [];
    const handle = openArchonStream({ sinceSeq: 0, onFrame: (frame) => frames.push(frame.kind) });
    const es = FakeEventSource.instances[0];
    handle.close();
    es.emit({ seq: 9, event: { id: 'e1', runId: 'run-1', type: 'tool_called' } });
    expect(frames).toEqual([]);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Gate round-2 regressions — each of these reproduced against the real ledger
// shape (no correlation ids on tool rows) before the fix.
// ─────────────────────────────────────────────────────────────────────────────
describe('live-sidebar correctness (gate round 2)', () => {
  const empty: ArchonRunCards = {};
  const ev = (over: Parameters<typeof event>[0]) => event(over);

  it('a replayed correlation-free completion does not duplicate the call', () => {
    // The REST seed and the SSE subscribe-time snapshot overlap the same rows,
    // so this completion arrives twice. It closed the open call the first
    // time, which meant its own id was never recorded as a dedupe key.
    const called = ev({ id: 'e1', type: 'tool_called', data: { tool_name: 'Bash' } });
    const completed = ev({
      id: 'e2',
      type: 'tool_completed',
      data: { tool_name: 'Bash', duration_ms: 842 },
    });

    let cards = applyArchonEvent(empty, called);
    cards = applyArchonEvent(cards, completed);
    cards = applyArchonEvent(cards, completed);

    expect(cards['run-1'].toolCalls).toHaveLength(1);
    expect(cards['run-1'].toolCalls[0].durationMs).toBe(842);
  });

  it('a reseed keeps an unresolved approval the REST window has scrolled past', () => {
    const runId = 'run-1';
    const snapshot = {
      status: 'ok',
      runStatus: 'ok',
      latestSeq: 5,
      runs: [{ runId, workflowName: 'archon-clutch', status: 'running', startedAt: '2026-07-28 00:00:00', completedAt: '', lastActivityAt: '2026-07-28 00:00:05' }],
      events: [],
    } as never;

    // Approval raised earlier, its event no longer in the newest-N window.
    const previous = seedRunCards(snapshot);
    const blocked = {
      ...previous,
      [runId]: { ...previous[runId], approvalPending: true, approvalNote: 'APPROVE SPEND?' },
    };

    const reseeded = seedRunCards(snapshot, blocked);

    expect(reseeded[runId].approvalPending).toBe(true);
    expect(reseeded[runId].approvalNote).toBe('APPROVE SPEND?');
  });

  it('a run that finished before we started watching never becomes a card', () => {
    // The real ledger holds ~1500 terminal rows; seeding them made the
    // "no active runs" empty state unreachable.
    const snapshot = {
      status: 'ok',
      runStatus: 'ok',
      latestSeq: 9,
      runs: [
        { runId: 'old-1', workflowName: 'archon-clutch', status: 'completed', startedAt: '2026-07-01 00:00:00', completedAt: '2026-07-01 00:10:00', lastActivityAt: '2026-07-01 00:10:00' },
        { runId: 'old-2', workflowName: 'image-node-factory', status: 'failed', startedAt: '2026-07-02 00:00:00', completedAt: '2026-07-02 00:05:00', lastActivityAt: '2026-07-02 00:05:00' },
      ],
      events: [],
    } as never;

    expect(Object.keys(seedRunCards(snapshot))).toHaveLength(0);
  });

  it('a run that finishes WHILE we watch keeps its card', () => {
    const runId = 'run-1';
    const running = {
      status: 'ok', runStatus: 'ok', latestSeq: 1, events: [],
      runs: [{ runId, workflowName: 'archon-clutch', status: 'running', startedAt: '2026-07-28 00:00:00', completedAt: '', lastActivityAt: '2026-07-28 00:00:01' }],
    } as never;
    const finished = {
      status: 'ok', runStatus: 'ok', latestSeq: 2, events: [],
      runs: [{ runId, workflowName: 'archon-clutch', status: 'completed', startedAt: '2026-07-28 00:00:00', completedAt: '2026-07-28 00:02:00', lastActivityAt: '2026-07-28 00:02:00' }],
    } as never;

    const watched = seedRunCards(running);
    const after = seedRunCards(finished, watched);

    expect(after[runId]).toBeDefined();
    expect(after[runId].ended).toBe(true);
    // A finished run is not still waiting on the operator.
    expect(after[runId].approvalPending).toBe(false);
  });
});
