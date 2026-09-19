import type { LearningLifecycle, LearningTuning } from '@/types/learning';

const buttonClass = 'px-3 py-2 rounded border border-[var(--color-border)] text-[12px] disabled:opacity-50 hover:bg-[var(--color-elevated)]';

export function LearningLifecyclePanel({ lifecycle, tuning, onSelect, onTune, onRollback, disabled }: {
  lifecycle?: LearningLifecycle;
  tuning?: LearningTuning;
  onSelect: (id: string) => void;
  onTune: () => void;
  onRollback: () => void;
  disabled: boolean;
}) {
  return <>
    {lifecycle && <section aria-label="Reflection and dreaming" class="space-y-3 border border-[var(--color-border)] rounded p-4 text-[12px]">
      <h3 class="text-[13px] font-medium">Reflection and dreaming</h3>
      <p class="text-[var(--color-text-muted)]">{lifecycle.pending_scope}</p>
      <div class="grid md:grid-cols-2 gap-3">
        {Object.entries(lifecycle.synthesis).map(([kind, stage]) => <div key={kind} class="border border-[var(--color-border)] rounded p-3 space-y-1">
          <h4 class="capitalize font-medium">{kind}</h4>
          <p>{stage.pending_cycles} pending · {stage.completed_cycles} completed</p>
          <p>{stage.consumed_sources} consumed source versions · {stage.consumed_characters} characters</p>
          {stage.latest_request && <p>Latest admission: {stage.latest_request.status?.replaceAll('_', ' ')}{stage.latest_request.reason && ` · ${stage.latest_request.reason}`}</p>}
          {stage.pending_consumers?.map((item) => <button type="button" key={item.cycle_id} class="block hover:underline" onClick={() => onSelect(item.cycle_id)}>
            Pending consumer · {item.input_count} inputs · {item.status}
          </button>)}
        </div>)}
      </div>
      {!!lifecycle.pending_stages.length && <div class="space-y-1">
        <h4 class="font-medium">Pending stages and deferrals</h4>
        {lifecycle.pending_stages.map((job) => <div key={job.id}>
          {job.record_id ? <button type="button" class="hover:underline" onClick={() => onSelect(job.record_id!)}>{job.kind} · {job.stage.replaceAll('_', ' ')} · {job.status}</button>
            : <span>{job.kind} · {job.stage.replaceAll('_', ' ')} · {job.status}</span>}
          {job.reason && <p class="text-amber-400 break-words">{job.reason}</p>}
        </div>)}
        {lifecycle.pending_stages_truncated && <p>Showing 60 pending stages. Use History to inspect additional records.</p>}
      </div>}
      <p>Admission receipts: {Object.entries(lifecycle.request_statuses).map(([status, count]) => `${count} ${status.replaceAll('_', ' ')}`).join(', ') || 'none yet'}</p>
      {lifecycle.recent_cycles.map((cycle) => <article key={cycle.id} class="border-t border-[var(--color-border)] pt-3 space-y-2">
        <button type="button" class="font-medium hover:underline" onClick={() => onSelect(cycle.id)}>{cycle.synthesis_kind} · {cycle.status} · {cycle.id.slice(0, 8)}</button>
        {cycle.conclusion && <p class="whitespace-pre-wrap break-words">{cycle.conclusion}</p>}
        <p>{cycle.input_manifest.length} inputs · {cycle.consumption_status} · {cycle.partial_inputs} partial inputs · {cycle.omitted_manifest.length} omitted inputs · projection {cycle.projection_status}</p>
        <p>Recorded model calls: {cycle.model_calls.length}{cycle.model_calls.length > 0 && ` · ${cycle.model_calls.map((call) => `${call.provider || 'unknown provider'} / ${call.model || 'unknown model'} (${call.status || (call.success ? 'succeeded' : 'unknown')})`).join('; ')}`}</p>
        {!!cycle.result_ids?.length && <nav aria-label="Synthesis outputs" class="flex flex-wrap gap-2">{cycle.result_ids.map((id) => <button type="button" key={id} class={buttonClass} onClick={() => onSelect(id)}>Output · {id.slice(0, 8)}</button>)}</nav>}
        <details><summary class="cursor-pointer">Exact source versions and excerpts</summary>
          <p class="text-[var(--color-text-muted)] mt-1">Only successful consumption receipts advance these ranges. Partial and omitted material remains eligible.</p>
          <ul class="space-y-1 mt-2">{cycle.input_manifest.map((source, index) => <li key={`${source.ref}:${index}`} class="break-words">{source.kind} · {source.ref} · revision {source.revision} · [{source.start}, {source.end}) · {source.complete ? 'complete' : 'partial'}</li>)}</ul>
        </details>
      </article>)}
      {lifecycle.cycles_truncated && <p>Showing the latest 20 synthesis cycles. Use History for earlier cycles.</p>}
    </section>}
    {tuning && <section aria-label="Recall tuning" class="space-y-3 border border-[var(--color-border)] rounded p-4 text-[12px]">
      <h3 class="text-[13px] font-medium">Recall tuning</h3>
      <p>{tuning.readiness.replaceAll('_', ' ')}{tuning.reason && ` · ${tuning.reason}`}</p>
      <dl class="grid grid-cols-3 gap-3">
        {[['Validated cases', `${tuning.validated_cases} / ${tuning.min_cases}`], ['Development', tuning.development_cases], ['Held out', tuning.heldout_cases]].map(([label, value]) => <div key={label}><dt class="text-[var(--color-text-muted)]">{label}</dt><dd class="text-[20px] tabular-nums">{value}</dd></div>)}
      </dl>
      <p class="text-[var(--color-text-muted)]">Source families stay in one split. A run can return no change. Tuning evaluations and retrieval policies do not count as learned ideas or adopted methods.</p>
      <div class="flex flex-wrap gap-2">
        <button type="button" class={buttonClass} disabled={disabled} onClick={onTune}>Request tuning run</button>
        <button type="button" class={buttonClass} disabled={disabled || !tuning.active_policy} onClick={onRollback}>Roll back recall policy</button>
      </div>
      {([['Latest run', tuning.latest_run], ['Latest comparison', tuning.latest_evaluation], ['Active policy', tuning.active_policy]] as const).map(([label, row]) => row && <details key={label}>
        <summary class="cursor-pointer">{label}{typeof row.status === 'string' && ` · ${row.status}`}</summary>
        {typeof row.id === 'string' && <button type="button" class={`${buttonClass} my-2`} onClick={() => onSelect(row.id as string)}>Inspect {row.id.slice(0, 8)}</button>}
        <pre class="whitespace-pre-wrap break-words bg-[var(--color-elevated)] rounded p-3">{JSON.stringify(row, null, 2)}</pre>
      </details>)}
      {!!tuning.policies?.length && <nav aria-label="Recall policy history" class="flex flex-wrap gap-2">{tuning.policies.map((policy) => typeof policy.id === 'string' && <button type="button" key={policy.id} class={buttonClass} onClick={() => onSelect(policy.id as string)}>Policy · {policy.id.slice(0, 8)}</button>)}</nav>}
    </section>}
  </>;
}
