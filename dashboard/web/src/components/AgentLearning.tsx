import { useRef, useState } from 'preact/hooks';
import { apiPost, describeApiError } from '@/lib/api';
import { useFetch } from '@/lib/useFetch';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { Modal } from '@/components/Modal';
import { LearningLifecyclePanel } from '@/components/LearningLifecycle';
import type { LearningPage, LearningRecord, LearningReport, LearningSummary } from '@/types/learning';

const buttonClass = 'px-3 py-2 rounded border border-[var(--color-border)] text-[12px] disabled:opacity-50 hover:bg-[var(--color-elevated)]';
const filters = [
  ['understanding', 'Understanding'], ['investigation', 'Investigations'], ['cognitive_cycle', 'Cognitive cycles'],
  ['', 'All activity'], ['experience', 'Experiences'], ['observation', 'Outcomes'],
  ['candidate', 'Proposed changes'], ['evaluation', 'Evaluations'],
  ['activation', 'Methods'], ['failure', 'Failures'],
  ['synthesis_cycle', 'Reflection and dream cycles'], ['synthesis_request', 'Synthesis admission receipts'],
  ['change_proposal', 'Automatic change proposals'], ['tuning_case', 'Validated recall cases'],
  ['tuning_corpus', 'Recall corpora'], ['tuning_run', 'Recall tuning runs'],
  ['tuning_evaluation', 'Recall comparisons'], ['tuning_policy', 'Recall policies'],
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
  const [status, setStatus] = useState('');
  const [cursors, setCursors] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [rollback, setRollback] = useState<LearningRecord | null>(null);
  const [tuningRollback, setTuningRollback] = useState(false);
  const [actionReceipt, setActionReceipt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [days, setDays] = useState(7);
  const [period, setPeriod] = useState(() => reportPeriod(7));
  const [explanation, setExplanation] = useState<LearningReport | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  const reportUrl = `${base}/report?${new URLSearchParams(period)}`;
  const currentReportUrl = useRef(reportUrl);
  currentReportUrl.current = reportUrl;
  const report = useFetch<LearningReport>(reportUrl);
  const summary = useFetch<LearningSummary>(base, 15000);
  const query = new URLSearchParams({ limit: '30' });
  if (kind) query.set('kind', kind);
  if (status) query.set('status', status);
  if (cursors.length) query.set('cursor', cursors[cursors.length - 1]);
  const history = useFetch<LearningPage>(`${base}/records?${query}`, 15000);
  const detail = useFetch<LearningRecord>(selected ? `${base}/records/${encodeURIComponent(selected)}` : null);

  async function mutate(path: string) {
    setBusy(true);
    setActionError(null);
    try {
      const receipt = await apiPost<{ status?: string; reason?: string }>(`${base}/${path}`);
      setActionReceipt(receipt.status ? `${receipt.status.replaceAll('_', ' ')}${receipt.reason ? ` · ${receipt.reason}` : ''}` : null);
      setRollback(null);
      setTuningRollback(false);
      summary.refresh();
      history.refresh();
      detail.refresh();
    } catch (error) {
      setActionError(describeApiError(error));
    } finally {
      setBusy(false);
    }
  }

  async function explain() {
    setReportBusy(true);
    setActionError(null);
    try {
      const result = await apiPost<LearningReport>(reportUrl);
      if (currentReportUrl.current === reportUrl) setExplanation(result);
    } catch (error) {
      setActionError(describeApiError(error));
    } finally {
      setReportBusy(false);
    }
  }

  return (
    <div class="space-y-5 max-w-5xl text-[var(--color-text)]">
      <div class="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 class="text-[16px] font-medium">Learning</h2>
          <p class="text-[12px] text-[var(--color-text-muted)] mt-1">What this Homie understands, what it is investigating, and what changed through experience.</p>
        </div>
        <div class="flex gap-2">
          <button type="button" class={buttonClass} onClick={() => { summary.refresh(); history.refresh(); setPeriod(reportPeriod(days)); setExplanation(null); }}>Refresh</button>
          {summary.data && <button type="button" class={buttonClass} disabled={busy || !summary.data.enabled}
            onClick={() => void mutate(summary.data!.paused ? 'resume' : 'pause')}>
            {summary.data.paused ? 'Resume learning' : 'Pause learning'}
          </button>}
        </div>
        {summary.data?.cognition && <section aria-label="Cognitive lifecycle" class="border border-[var(--color-border)] rounded p-3 space-y-2 text-[12px]">
          <h3 class="font-medium">Cognitive lifecycle</h3>
          <p>Dispatcher: {summary.data.cognition.dispatcher.state.replaceAll('_', ' ')}
             · Last successful check: {dispatcherCheckTime(summary.data.cognition.dispatcher.last_success_at)}</p>
          {summary.data.cognition.dispatcher.error_type && <p role="status" class="text-amber-400">{summary.data.cognition.dispatcher.error_type}</p>}
          <p>Cycles: {statusCounts(summary.data.cognition.cycles)} · Investigations: {statusCounts(summary.data.cognition.investigations)}</p>
          {summary.data.cognition.execution_modes && <p>Execution modes: {statusCounts(summary.data.cognition.execution_modes)} · Recorded model calls: {summary.data.cognition.recorded_model_calls ?? 0}</p>}
          <p>{summary.data.cognition.delivered_contexts} executed requests received retained understanding or investigations.</p>
          <details><summary class="cursor-pointer">Hook coverage</summary><pre class="whitespace-pre-wrap break-words mt-2">{JSON.stringify(summary.data.cognition.dispatcher.adapter_coverage ?? {}, null, 2)}</pre></details>
        </section>}
      </div>

      {actionError && <p role="alert" class="text-[12px] text-red-400">{actionError}</p>}
      {actionReceipt && <p role="status" class="text-[12px]">{actionReceipt}</p>}
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

      <LearningLifecyclePanel lifecycle={summary.data?.lifecycle} tuning={summary.data?.tuning}
        onSelect={setSelected} disabled={busy || !summary.data?.enabled || !!summary.data?.paused}
        onTune={() => void mutate('tuning/run')} onRollback={() => setTuningRollback(true)} />

      <section aria-label="Learning report" class="space-y-3 border border-[var(--color-border)] rounded p-4">
        <div class="flex justify-between items-center gap-3 flex-wrap">
          <h3 class="text-[13px] font-medium">What changed</h3>
          <select aria-label="Learning report period" value={days} class="text-[12px] border border-[var(--color-border)] bg-[var(--color-card)] rounded p-2"
            onChange={(event) => { const value = Number(event.currentTarget.value); setDays(value); setPeriod(reportPeriod(value)); setExplanation(null); }}>
            <option value={1}>Last 24 hours</option><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option>
          </select>
        </div>
        {report.error ? <p role="alert" class="text-[12px] text-red-400">Report unavailable: {report.error}</p>
          : report.loading ? <Spinner /> : report.data?.counts && <>
            <dl class="grid grid-cols-2 md:grid-cols-4 gap-3 text-[12px]">
              {[
                ['Distinct conclusions', 'distinct_conclusions'], ['Understanding changes', 'understanding_changes'],
                ['Investigations opened', 'investigations_opened'], ['Completed thinking cycles', 'completed_cycles'],
                ['Observations', 'observations'], ['Qualification attempts', 'qualification_attempts'],
                ['Methods adopted', 'methods_adopted'], ['Later context inclusion', 'delivered_contexts'],
                ['Reflection cycles', 'reflection_cycles'], ['Dream cycles', 'dream_cycles'],
                ['Synthesis skips', 'synthesis_skips'], ['Recall tuning runs', 'recall_tuning_runs'],
                ['Recall comparisons', 'recall_tuning_evaluations'], ['Recall policy activations', 'recall_policy_activations'],
                ['Context-only cycles', 'context_only_cycles'], ['Recorded model calls', 'recorded_model_calls'],
              ].map(([label, key]) => <div key={key}><dt class="text-[var(--color-text-muted)]">{label}</dt><dd class="text-[20px] tabular-nums">{report.data!.counts[key] ?? 0}</dd></div>)}
            </dl>
            <p class="text-[11px] text-[var(--color-text-muted)]">Counts come from distinct recorded changes. Evaluation trials are not counted as learned ideas; context inclusion does not by itself prove better results.</p>
            {!report.data.has_activity && <p class="text-[12px]">No learning changes were recorded in this period.</p>}
            <button type="button" class={buttonClass} disabled={reportBusy || !report.data.has_activity || summary.data?.paused || !summary.data?.enabled} onClick={() => void explain()}>
              {reportBusy ? 'Explaining recorded changes…' : 'Explain these changes'}
            </button>
            {(explanation?.narrative || report.data.narrative) && <p class="text-[12px] whitespace-pre-wrap break-words">{explanation?.narrative || report.data.narrative}</p>}
            {!!report.data.open_investigations?.length && <div class="space-y-2">
              <h4 class="text-[12px] font-medium">Open investigations</h4>
              {report.data.open_investigations.map((item) => <button type="button" key={item.id} class="block text-left text-[12px] hover:underline" onClick={() => setSelected(item.id)}>
                {item.question || item.id} · {item.status}{item.reason && ` · ${item.reason}`}
              </button>)}
            </div>}
            {!!report.data.records?.length && <nav aria-label="Reported learning records" class="flex flex-wrap gap-2">
              {report.data.records.map((item) => <button type="button" key={item.id} class={buttonClass} onClick={() => setSelected(item.id)}>{item.title || item.question || item.kind} · {item.id.slice(0, 8)}</button>)}
            </nav>}
            {report.data.records_truncated && <p class="text-[11px] text-[var(--color-text-muted)]">Showing the latest 60 records; counts cover the full selected period. Use History to inspect older records.</p>}
          </>}
      </section>

      <section class="space-y-3" aria-label="Learning history">
        <div class="flex justify-between items-center gap-3 flex-wrap">
          <h3 class="text-[13px] font-medium">History</h3>
          <select aria-label="Filter learning history" value={kind} class="text-[12px] border border-[var(--color-border)] bg-[var(--color-card)] rounded p-2"
            onChange={(event) => { setKind(event.currentTarget.value); setStatus(''); setCursors([]); }}>
            {filters.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <select aria-label="Filter learning status" value={status} class="text-[12px] border border-[var(--color-border)] bg-[var(--color-card)] rounded p-2"
            onChange={(event) => { setStatus(event.currentTarget.value); setCursors([]); }}>
            <option value="">All statuses</option>
            {['open', 'due', 'pending', 'queued', 'coalesced', 'no_signal', 'no_change', 'deferred', 'blocked', 'retained', 'completed', 'tentative', 'supported', 'superseded', 'needs_reassessment', 'failed', 'rolled_back'].map((value) => <option key={value} value={value}>{value.replaceAll('_', ' ')}</option>)}
          </select>
        </div>
        {history.error ? <p role="alert" class="text-[12px] text-red-400">History unavailable: {history.error}</p>
          : history.loading ? <Spinner />
            : !history.data?.records.length ? <Empty title="No learning records yet" description="Experience, outcomes, and evaluated changes will appear here as this Homie works." />
              : <ul class="divide-y divide-[var(--color-border)]">
                {history.data.records.map((record) => <li key={record.id} class="py-3">
                  <button type="button" class="text-left w-full hover:bg-[var(--color-elevated)] rounded p-2" onClick={() => setSelected(record.id)}>
                    <span class="text-[10px] uppercase text-[var(--color-text-faint)]">{record.kind.replaceAll('_', ' ')}</span>
                    <span class="block text-[12px] break-words">{textField(record, 'title', 'question', 'conclusion', 'summary', 'lesson', 'error', 'status')}</span>
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
              ['content', 'Understanding or method'], ['question', 'Investigation question'], ['why', 'Why it matters'],
              ['conclusion', 'Conclusion'], ['trigger', 'Next observation'], ['next_check_at', 'Next check'],
              ['scope', 'Scope'], ['applicability', 'Applies when'],
              ['claim', 'Expected result'], ['resolution_rule', 'How it is checked'],
              ['evidence', 'Observed evidence'], ['uncertainty', 'Uncertainty'],
              ['reason', 'Reason'], ['error', 'Problem'],
              ['execution_kind', 'Execution mode'], ['input_manifest', 'Exact input provenance'],
              ['omitted_manifest', 'Omitted input ranges'], ['derived_input_ids', 'Derived context'],
              ['result_ids', 'Output records'], ['comparison', 'Retrieval comparison'],
              ['predecessor_id', 'Predecessor policy or record'],
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
      <Modal open={tuningRollback} onClose={() => { if (!busy) setTuningRollback(false); }} title="Roll back recall policy?" footer={<>
        <button type="button" class={buttonClass} disabled={busy} onClick={() => setTuningRollback(false)}>Cancel</button>
        <button type="button" class={buttonClass} disabled={busy} onClick={() => void mutate('tuning/rollback')}>Confirm recall rollback</button>
      </>}>
        <p class="text-[12px]">Restore the predecessor retrieval policy for future recall. The corpus, comparison, and policy history stay available.</p>
        {actionError && <p role="alert" class="text-red-400 mt-3 text-[12px]">{actionError}</p>}
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

function reportPeriod(days: number) {
  const end = new Date();
  return { since: new Date(end.getTime() - days * 86400000).toISOString(), until: end.toISOString() };
}

function statusCounts(counts: Record<string, number>): string {
  return Object.entries(counts).map(([status, count]) => `${count} ${status.replaceAll('_', ' ')}`).join(', ') || 'none yet';
}

function dispatcherCheckTime(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 'Unknown';
  // The Python dispatcher records Unix seconds; JavaScript Date takes milliseconds.
  const date = new Date(value * 1000);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Unknown';
}
