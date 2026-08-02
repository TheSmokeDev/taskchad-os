/**
 * Runs.tsx — every voice-deployed worker in one place.
 *
 * The Talk page's RunSidebar made Archon runs visible, but only while the
 * operator sat on /talk; skill/agent/look runs had no surface outside the
 * page that started them. This page is the standalone execution view:
 *
 *   - Left: the unified talk-run registry (skill | agent | archon | look)
 *     via `GET /api/talk/runs?limit=50&history=true` — merged with the
 *     persisted JSONL tail, so runs survive an API restart. History-only
 *     rows carry `fromHistory`; a `running` row from a dead API process is
 *     reported as `lost` (for archon that means the WATCHER died — the
 *     detached workflow's real status lives in the Archon ledger, right
 *     column).
 *   - Right: the existing RunSidebar (Archon ledger + SSE), reused as-is.
 *
 * Poll cadence 5s — same order as the Talk page's own run poller.
 */

import { TopBar } from '@/components/TopBar';
import { RunSidebar } from '@/components/RunSidebar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';

const POLL_MS = 5_000;

interface TalkRun {
  runId: number;
  kind: string;
  label: string;
  status: 'running' | 'done' | 'failed' | 'lost';
  output: string;
  ts: number | null;
  updated: number | null;
  fromHistory?: boolean;
}

interface RunsResponse {
  ok: boolean;
  runs: TalkRun[];
}

const STATUS_STYLE: Record<TalkRun['status'], string> = {
  running: 'text-[var(--color-accent)] border-[var(--color-accent)]',
  done: 'text-emerald-400 border-emerald-700',
  failed: 'text-red-400 border-red-700',
  lost: 'text-amber-400 border-amber-700',
};

function formatWhen(epochSeconds: number | null): string {
  if (!epochSeconds) return '—';
  const d = new Date(epochSeconds * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay ? d.toLocaleTimeString() : d.toLocaleString();
}

function RunRow({ run }: { run: TalkRun }) {
  const style = STATUS_STYLE[run.status] ?? STATUS_STYLE.lost;
  return (
    <div class="border-b border-[var(--color-border)] px-4 py-3">
      <div class="flex items-center gap-3">
        <span class="text-[11px] text-[var(--color-text-dim)] w-10 shrink-0">#{run.runId}</span>
        <span class="text-[11px] uppercase tracking-wide text-[var(--color-text-dim)] w-14 shrink-0">
          {run.kind}
        </span>
        <span class="text-[13px] text-[var(--color-text)] truncate flex-1" title={run.label}>
          {run.label || '(unlabeled)'}
        </span>
        <span class={`text-[11px] border rounded-full px-2 py-0.5 shrink-0 ${style}`}>
          {run.status}
        </span>
        <span class="text-[11px] text-[var(--color-text-dim)] shrink-0">
          {formatWhen(run.updated ?? run.ts)}
        </span>
      </div>
      {run.output && (
        <details class="mt-1 ml-10">
          <summary class="text-[11px] text-[var(--color-text-dim)] cursor-pointer select-none">
            output
          </summary>
          <pre class="mt-1 text-[11px] text-[var(--color-text-dim)] whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
            {run.output}
          </pre>
        </details>
      )}
      {run.status === 'lost' && (
        <div class="mt-1 ml-10 text-[11px] text-[var(--color-text-dim)]">
          Watcher died with a previous API process
          {run.kind === 'archon' ? ' — the detached workflow itself is judged by the Archon ledger (right)' : ''}
          .
        </div>
      )}
    </div>
  );
}

export function Runs() {
  const { data, loading, error } = useFetch<RunsResponse>(
    '/api/talk/runs?limit=50&history=true',
    POLL_MS,
  );
  const runs = data?.runs ?? [];
  const runningCount = runs.filter((r) => r.status === 'running').length;

  return (
    <div class="flex flex-col h-full min-h-0">
      <TopBar
        title="Runs"
        subtitle={`${runningCount} running / ${runs.length} recent`}
      />
      <div class="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[1fr_360px]">
        <div class="overflow-y-auto min-h-0">
          {loading && !data && (
            <div class="flex items-center justify-center h-full">
              <Spinner />
            </div>
          )}
          {error && <Empty title="Failed to load runs" description={error} />}
          {!loading && !error && runs.length === 0 && (
            <Empty
              title="No runs yet"
              description="Voice-deployed work (skills, background agents, Archon workflows, screen looks) lands here the moment it starts."
            />
          )}
          {runs.map((run) => (
            <RunRow key={run.runId} run={run} />
          ))}
        </div>
        <div class="border-t lg:border-t-0 lg:border-l border-[var(--color-border)] overflow-y-auto min-h-0">
          <RunSidebar />
        </div>
      </div>
    </div>
  );
}
