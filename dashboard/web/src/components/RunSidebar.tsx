/**
 * RunSidebar.tsx — live Archon run visibility (epic #252 / ticket #257, F9).
 *
 * The first visibility surface in the execution spine: the operator dispatches
 * work by voice on the Talk page and watches it run in the gutter that page
 * was already wasting above `lg`.
 *
 * Two health signals are tracked SEPARATELY, because collapsing them is how a
 * telemetry panel starts lying:
 *   - `streamUp`   — is the SSE socket delivering?
 *   - `ledgerStatus` — what did the read-only ledger read itself say? #254
 *     returns 200 with `db_missing` / `db_unreadable` rather than 500ing on a
 *     cold machine, so a non-`ok` value is a real answer, not a transport
 *     failure.
 *
 * Four honest phases, and `loaded` flips true on the first attempt whether it
 * succeeded or not — there is no path that leaves a spinner up forever:
 *   loading -> live (stream delivering)
 *           -> degraded (stream down, REST snapshot still answering)
 *           -> unavailable (ledger unreadable, or REST itself failing)
 *           -> disabled (operator kill-switch, 503)
 *
 * Refresh cadence is one interval, not a timer per state: each tick decides
 * whether a REST refresh is due. A live stream only needs a slow refresh (the
 * ledger row is the sole source of `workflowName` + authoritative status); a
 * dead stream polls fast; an event naming an unknown run asks for one
 * immediately, floored so a burst cannot become a refetch storm.
 */

import { useEffect, useRef, useState } from 'preact/hooks';
import { Activity, RefreshCw } from 'lucide-preact';
import { ApiError, describeApiError } from '@/lib/api';
import {
  applyArchonEvent,
  applyRunEnded,
  describeKillSwitch,
  describeLedgerStatus,
  fetchArchonSnapshot,
  formatToolDuration,
  openArchonStream,
  pruneRunCards,
  seedRunCards,
  sortRunCards,
  type ArchonRunCard,
  type ArchonRunCards,
  type ArchonStreamHandle,
} from '@/lib/archon-runs';

/** Interval that decides whether a refresh is due. */
const TICK_MS = 2_000;
/** Floor between REST refreshes, including event-driven ones. */
const MIN_REFRESH_MS = 2_000;
/** Stream down — REST is the only source, so poll it. */
const DEGRADED_REFRESH_MS = 5_000;
/** Stream up — REST only needs to top up run names + ledger status. */
const LIVE_REFRESH_MS = 30_000;

type SidebarPhase = 'loading' | 'live' | 'degraded' | 'unavailable' | 'disabled';

interface FatalState {
  phase: 'unavailable' | 'disabled';
  message: string;
}

export function RunSidebar() {
  const [cards, setCards] = useState<ArchonRunCards>({});
  const [streamUp, setStreamUp] = useState(false);
  const [fatal, setFatal] = useState<FatalState | null>(null);
  const [loaded, setLoaded] = useState(false);

  const alive = useRef(true);
  const cardsRef = useRef<ArchonRunCards>({});
  const streamRef = useRef<ArchonStreamHandle | null>(null);
  const streamUpRef = useRef(false);
  const sinceSeq = useRef(0);
  const inFlight = useRef(false);
  const lastRefreshAt = useRef(0);
  const refreshPending = useRef(false);

  function commitCards(next: ArchonRunCards): void {
    cardsRef.current = next;
    setCards(next);
  }

  function markStream(up: boolean): void {
    streamUpRef.current = up;
    if (alive.current) setStreamUp(up);
  }

  function closeStream(): void {
    streamRef.current?.close();
    streamRef.current = null;
    markStream(false);
  }

  function openStream(): void {
    if (!alive.current || streamRef.current) return;
    // No EventSource in this runtime — stay on the REST poll rather than let
    // a transport gap surface as a ledger failure. `refresh()` calls this from
    // inside its try, so an unguarded throw here would be caught and reported
    // as "telemetry unavailable" when the ledger read actually succeeded.
    if (typeof EventSource === 'undefined') return;
    streamRef.current = openArchonStream({
      sinceSeq: sinceSeq.current,
      onOpen: () => markStream(true),
      onDown: () => markStream(false),
      // A confirmed replay gap means the ring rotated past us: the events are
      // still in the ledger, so a REST reseed is the whole recovery.
      onReplayGap: () => {
        refreshPending.current = true;
      },
      onFrame: (frame) => {
        if (!alive.current) return;
        switch (frame.kind) {
          case 'snapshot': {
            let next = cardsRef.current;
            for (const event of frame.events) {
              next = applyArchonEvent(next, event);
            }
            commitCards(pruneRunCards(next));
            break;
          }
          case 'event': {
            if (!cardsRef.current[frame.event.runId]) {
              // A run we have no ledger row for — only REST can name it.
              refreshPending.current = true;
            }
            commitCards(pruneRunCards(applyArchonEvent(cardsRef.current, frame.event)));
            break;
          }
          case 'ended': {
            commitCards(applyRunEnded(cardsRef.current, frame.runId));
            break;
          }
          case 'dropped': {
            // Live frames were shed. The ledger still holds them.
            refreshPending.current = true;
            break;
          }
        }
      },
    });
  }

  async function refresh(): Promise<void> {
    if (inFlight.current || !alive.current) return;
    inFlight.current = true;
    refreshPending.current = false;
    lastRefreshAt.current = Date.now();
    try {
      const snapshot = await fetchArchonSnapshot();
      if (!alive.current) return;
      sinceSeq.current = snapshot.latestSeq;
      // Reseed FROM the cards we already hold: the REST window is the newest
      // events globally, so live detail (an unresolved approval, the current
      // node, the tool list) must survive a snapshot that no longer contains
      // the events that produced it.
      commitCards(pruneRunCards(seedRunCards(snapshot, cardsRef.current)));
      const ledgerNote = describeLedgerStatus(snapshot.status);
      setFatal(ledgerNote ? { phase: 'unavailable', message: ledgerNote } : null);
      if (!ledgerNote) openStream();
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof ApiError && err.status === 503) {
        closeStream();
        setFatal({ phase: 'disabled', message: describeKillSwitch(err) });
        return;
      }
      setFatal({ phase: 'unavailable', message: describeApiError(err) });
    } finally {
      inFlight.current = false;
      if (alive.current) setLoaded(true);
    }
  }

  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = window.setInterval(() => {
      if (!alive.current) return;
      const due = refreshPending.current
        ? MIN_REFRESH_MS
        : streamUpRef.current
          ? LIVE_REFRESH_MS
          : DEGRADED_REFRESH_MS;
      if (Date.now() - lastRefreshAt.current >= due) void refresh();
    }, TICK_MS);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      closeStream();
    };
    // Mount-only: the sidebar watches the unscoped tail for the page's life.
  }, []);

  const phase: SidebarPhase = !loaded
    ? 'loading'
    : fatal
      ? fatal.phase
      : streamUp
        ? 'live'
        : 'degraded';
  const ordered = sortRunCards(cards);

  return (
    <aside class="min-w-0" data-testid="archon-run-sidebar">
      <section class="border border-[var(--color-border)] rounded-md bg-[var(--color-card)] overflow-hidden">
        <div class="px-3 py-2 border-b border-[var(--color-border)] flex items-center justify-between gap-2">
          <div class="text-xs uppercase tracking-wide text-[var(--color-text-muted)] flex items-center gap-1.5">
            <Activity size={13} />
            Runs
          </div>
          <div class="flex items-center gap-2">
            <PhasePill phase={phase} />
            <button
              type="button"
              onClick={() => void refresh()}
              class="w-7 h-7 inline-flex items-center justify-center rounded-md border border-[var(--color-border)] hover:bg-[var(--color-hover)]"
              title="Refresh runs"
              aria-label="Refresh runs"
            >
              <RefreshCw size={13} />
            </button>
          </div>
        </div>

        {phase === 'loading' && (
          <div class="px-3 py-3 text-sm text-[var(--color-text-muted)]">
            Loading Archon telemetry...
          </div>
        )}

        {fatal && loaded && (
          <div class="px-3 py-3 text-sm text-[var(--color-text-muted)] break-words">
            {fatal.message}
          </div>
        )}

        {loaded && !fatal && ordered.length === 0 && (
          <div class="px-3 py-3 text-sm text-[var(--color-text-muted)]">
            No Archon runs yet. Dispatch work and it shows up here.
          </div>
        )}

        {loaded && ordered.length > 0 && (
          <div class="divide-y divide-[var(--color-border)]">
            {ordered.map((card) => (
              <RunCard key={card.runId} card={card} />
            ))}
          </div>
        )}
      </section>
    </aside>
  );
}

const PHASE_LABEL: Readonly<Record<SidebarPhase, string>> = {
  loading: 'loading',
  live: 'live',
  degraded: 'polling',
  unavailable: 'unavailable',
  disabled: 'disabled',
};

function PhasePill({ phase }: { phase: SidebarPhase }) {
  const tone =
    phase === 'live'
      ? 'text-emerald-500 border-emerald-500/40'
      : phase === 'degraded'
        ? 'text-amber-500 border-amber-500/40'
        : phase === 'loading'
          ? 'text-[var(--color-text-muted)] border-[var(--color-border)]'
          : 'text-red-500 border-red-500/40';
  return (
    <span
      class={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border ${tone}`}
      data-testid="archon-phase"
    >
      {PHASE_LABEL[phase]}
    </span>
  );
}

function RunCard({ card }: { card: ArchonRunCard }) {
  return (
    <article
      class={`px-3 py-3 grid gap-1.5 min-w-0 ${card.approvalPending ? 'border-l-2 border-amber-500 bg-amber-500/5' : ''}`}
      data-testid={`archon-run-${card.runId}`}
    >
      <div class="flex items-start justify-between gap-2 min-w-0">
        <div class="text-sm font-medium break-words min-w-0">{card.workflowName}</div>
        <span class="text-[10px] uppercase tracking-wide text-[var(--color-text-muted)] shrink-0">
          {card.status}
        </span>
      </div>

      {card.approvalPending && (
        <div class="text-xs text-amber-500 break-words">
          Waiting on your approval{card.approvalNote ? ` — ${card.approvalNote}` : ''}
        </div>
      )}

      {card.currentNode && (
        <div class="text-xs text-[var(--color-text-muted)] break-words">
          Node: {card.currentNode}
          {card.nodeState === 'completed' ? ' (done)' : ''}
        </div>
      )}

      {card.toolCalls.length > 0 && (
        <ul class="grid gap-0.5">
          {card.toolCalls.map((call) => (
            <li key={call.key} class="flex items-baseline justify-between gap-2 text-xs min-w-0">
              <span class="text-[var(--color-text)] break-words min-w-0">{call.name}</span>
              <span class="text-[var(--color-text-muted)] tabular-nums shrink-0">
                {formatToolDuration(call.durationMs)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
