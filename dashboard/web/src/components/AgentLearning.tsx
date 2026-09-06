import { useState } from 'preact/hooks';
import { apiPost, describeApiError } from '@/lib/api';
import { useFetch } from '@/lib/useFetch';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { Modal } from '@/components/Modal';
import type { LearningPage, LearningRecord, LearningSummary } from '@/types/learning';

const buttonClass = 'px-3 py-2 rounded border border-[var(--color-border)] text-[12px] disabled:opacity-50 hover:bg-[var(--color-elevated)]';
const filters = [
  ['', 'All activity'], ['experience', 'Experiences'], ['observation', 'Outcomes'],
  ['candidate', 'Proposed changes'], ['evaluation', 'Evaluations'],
  ['activation', 'Methods'], ['failure', 'Failures'],
];

function textField(record: LearningRecord, ...keys: string[]): string {
  for (const key of keys) {
    const value = record.payload[key];
    if (typeof value === 'string' && value) return value;
  }
  return record.kind.replaceAll('_', ' ');
}

/** Each mount belongs to exactly one persona; the parent keys this component. */
export function AgentLearning({ agentId }: { agentId: string }) {
  const base = `/api/agents/${encodeURIComponent(agentId)}/learning`;
  const [kind, setKind] = useState('');
  const [cursors, setCursors] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [rollback, setRollback] = useState<LearningRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const summary = useFetch<LearningSummary>(base, 15000);
  const query = new URLSearchParams({ limit: '30' });
  if (kind) query.set('kind', kind);
  if (cursors.length) query.set('cursor', cursors[cursors.length - 1]);
  const history = useFetch<LearningPage>(`${base}/records?${query}`, 15000);
  const detail = useFetch<LearningRecord>(selected ? `${base}/records/${encodeURIComponent(selected)}` : null);

  async function mutate(path: string) {
    setBusy(true);
    setActionError(null);
    try {
      await apiPost(`${base}/${path}`);
      setRollback(null);
      summary.refresh();
      history.refresh();
      detail.refresh();
    } catch (error) {
      setActionError(describeApiError(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="space-y-5 max-w-5xl text-[var(--color-text)]">
      <div class="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 class="text-[16px] font-medium">Learning</h2>
          <p class="text-[12px] text-[var(--color-text-muted)] mt-1">What changed, what happened, and which methods this Homie is using.</p>
        </div>
        <div class="flex gap-2">
          <button type="button" class={buttonClass} onClick={() => { summary.refresh(); history.refresh(); }}>Refresh</button>
          {summary.data && <button type="button" class={buttonClass} disabled={busy || !summary.data.enabled}
            onClick={() => void mutate(summary.data!.paused ? 'resume' : 'pause')}>
            {summary.data.paused ? 'Resume learning' : 'Pause learning'}
          </button>}
        </div>
      </div>

      {actionError && <p role="alert" class="text-[12px] text-red-400">{actionError}</p>}
      {summary.error && <p role="alert" class="text-[12px] text-red-400">Learning status unavailable: {summary.error}</p>}
      {summary.loading && !summary.data && <Spinner />}
      {summary.data && <>
        {!summary.data.enabled && <p role="status" class="text-[12px] text-amber-400">Learning is disabled in this persona's configuration. Existing history remains available.</p>}
        {summary.data.paused && <p role="status" class="text-[12px] text-amber-400">Learning is paused. Recorded experience and current methods are preserved.</p>}
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          {[
            ['Experiences', summary.data.counts.experience ?? 0],
            ['Waiting for outcomes', summary.data.pending_outcomes],
            ['Current methods', summary.data.active_methods.length],
            ['Recorded failures', summary.data.failures],
          ].map(([label, count]) => <div key={label} class="border border-[var(--color-border)] rounded p-3">
            <div class="text-[11px] text-[var(--color-text-muted)]">{label}</div>
            <div class="text-[22px] tabular-nums">{count}</div>
          </div>)}
        </div>
        {!!summary.data.queue?.jobs.length && <section aria-label="Background learning" class="space-y-2">
          <h3 class="text-[13px] font-medium">Background learning</h3>
          <p class="text-[11px] text-[var(--color-text-muted)]">{summary.data.queue.pending} items waiting or running</p>
          {summary.data.queue.jobs.map((job) => <div key={job.id} class="border border-[var(--color-border)] rounded p-3 text-[12px]">
            {job.record_id ? <button type="button" class="hover:underline" onClick={() => setSelected(job.record_id!)}>{job.stage.replaceAll('_', ' ')} · {job.status}</button>
              : <span>{job.stage.replaceAll('_', ' ')} · {job.status}</span>}
            {job.last_error && <p class="text-amber-400 mt-1 whitespace-pre-wrap break-words">{job.last_error}</p>}
          </div>)}
        </section>}
        {summary.data.active_methods.length > 0 && <section aria-label="Current methods" class="space-y-2">
          <h3 class="text-[13px] font-medium">Current methods</h3>
          <p class="text-[11px] text-[var(--color-text-muted)]">Provisional means the method passed practice evaluation; it does not establish live results.</p>
          {summary.data.active_methods.map((method) => <div key={method.id} class="border border-[var(--color-border)] rounded p-3 flex items-center justify-between gap-3">
            <button type="button" class="text-left text-[12px] hover:underline" onClick={() => setSelected(method.id)}>
              {textField(method, 'title', 'summary', 'candidate_id')} <span class="text-[var(--color-text-muted)]">({textField(method, 'status').replaceAll('_', ' ')})</span>
            </button>
            <button type="button" class={buttonClass} disabled={busy} onClick={() => setRollback(method)}>Roll back</button>
          </div>)}
        </section>}
      </>}

      <section class="space-y-3" aria-label="Learning history">
        <div class="flex justify-between items-center gap-3">
          <h3 class="text-[13px] font-medium">History</h3>
          <select aria-label="Filter learning history" value={kind} class="text-[12px] border border-[var(--color-border)] bg-[var(--color-card)] rounded p-2"
            onChange={(event) => { setKind(event.currentTarget.value); setCursors([]); }}>
            {filters.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        {history.error ? <p role="alert" class="text-[12px] text-red-400">History unavailable: {history.error}</p>
          : history.loading ? <Spinner />
            : !history.data?.records.length ? <Empty title="No learning records yet" description="Experience, outcomes, and evaluated changes will appear here as this Homie works." />
              : <ul class="divide-y divide-[var(--color-border)]">
                {history.data.records.map((record) => <li key={record.id} class="py-3">
                  <button type="button" class="text-left w-full hover:bg-[var(--color-elevated)] rounded p-2" onClick={() => setSelected(record.id)}>
                    <span class="text-[10px] uppercase text-[var(--color-text-faint)]">{record.kind.replaceAll('_', ' ')}</span>
                    <span class="block text-[12px] break-words">{textField(record, 'title', 'summary', 'lesson', 'error', 'status')}</span>
                    <span class="block text-[11px] text-[var(--color-text-muted)]">{['status', 'mode', 'quality'].map((field) => record.payload[field]).filter((value): value is string => typeof value === 'string').join(' · ').replaceAll('_', ' ')}</span>
                    <time class="text-[10px] text-[var(--color-text-muted)]">{new Date(record.created_at).toLocaleString()}</time>
                  </button>
                </li>)}
              </ul>}
        <div class="flex justify-between gap-2">
          <button type="button" class={buttonClass} disabled={!cursors.length || history.loading} onClick={() => setCursors(cursors.slice(0, -1))}>Newer</button>
          <button type="button" class={buttonClass} disabled={!history.data?.next_cursor || history.loading} onClick={() => setCursors([...cursors, history.data!.next_cursor!])}>Older</button>
        </div>
      </section>

      <Modal open={selected !== null} onClose={() => setSelected(null)} title="Learning record" width={760}>
        {detail.loading ? <Spinner /> : detail.error ? <p role="alert">{detail.error}</p> : detail.data && <>
          <h3 class="text-[13px] mb-2">{textField(detail.data, 'title', 'summary')}</h3>
          <p class="text-[11px] text-[var(--color-text-muted)] mb-3">{detail.data.kind} · {new Date(detail.data.created_at).toLocaleString()}</p>
          <dl class="space-y-3 mb-4 text-[12px]">
            {[
              ['content', 'Method or lesson'], ['applicability', 'Applies when'],
              ['claim', 'Expected result'], ['resolution_rule', 'How it is checked'],
              ['evidence', 'Observed evidence'], ['uncertainty', 'Uncertainty'],
              ['reason', 'Reason'], ['error', 'Problem'],
            ].map(([key, label]) => {
              const value = detail.data!.payload[key];
              if (value === undefined || value === null || value === '') return null;
              return <div key={key}><dt class="text-[var(--color-text-muted)]">{label}</dt><dd class="whitespace-pre-wrap break-words mt-1">{typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}</dd></div>;
            })}
          </dl>
          {!!detail.data.links?.length && <nav aria-label="Linked evidence" class="flex flex-wrap gap-2 mb-3">
            {detail.data.links.map((link) => <button type="button" key={`${link.id}:${link.label}`} class={buttonClass} onClick={() => setSelected(link.id)}>{link.label}</button>)}
          </nav>}
          <details><summary class="text-[12px] cursor-pointer mb-2">Full record and history</summary>
            <pre class="text-[11px] whitespace-pre-wrap break-words bg-[var(--color-elevated)] rounded p-3">{JSON.stringify(detail.data.payload, null, 2)}</pre>
          </details>
        </>}
      </Modal>
      <Modal open={rollback !== null} onClose={() => { if (!busy) setRollback(null); }} title="Roll back this method?" footer={<>
        <button type="button" class={buttonClass} disabled={busy} onClick={() => setRollback(null)}>Cancel</button>
        <button type="button" class={buttonClass} disabled={busy} onClick={() => rollback && void mutate(`activations/${encodeURIComponent(rollback.id)}/rollback`)}>Confirm rollback</button>
      </>}>
        <p class="text-[12px]">Restore the previous procedure for future work. Experience and evaluation history remain available. A conflict with newer changes will be reported.</p>
        {actionError && <p role="alert" class="text-red-400 mt-3 text-[12px]">{actionError}</p>}
      </Modal>
    </div>
  );
}
