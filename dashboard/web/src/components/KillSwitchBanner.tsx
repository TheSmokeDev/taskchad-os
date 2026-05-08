import { AlertTriangle } from 'lucide-preact';
import { useFetch } from '@/lib/useFetch';

/**
 * PRD-8 Phase 7a (WS7 R3 NB3) — rich snapshot consumer.
 *
 * /api/health.killSwitches went from `{}` (Phase 3 stub) to:
 *
 *     {
 *       counters: {<switch_name>: <int_refusal_count>, ...},
 *       audit_write_failures: {<switch_name>: <int_failure_count>, ...},
 *       process_started_at: <unix_timestamp_float | null>
 *     }
 *
 * The banner renders nonzero refusal counters (LLM was disabled, recall
 * was disabled, etc.) AND nonzero audit-write failures (silent
 * persistence loss — operator should investigate). `process_started_at`
 * is kept in the type for future tooltip use; it's a debug field, NOT a
 * banner signal.
 */
interface KillSwitchSnapshot {
  counters: Record<string, number>;
  audit_write_failures: Record<string, number>;
  process_started_at: number | null;
}

interface Health {
  ok?: boolean;
  killSwitches?: KillSwitchSnapshot;
}

export function KillSwitchBanner() {
  const { data } = useFetch<Health>('/api/health', 30_000);
  const snapshot = data?.killSwitches;
  const refusals = Object.entries(snapshot?.counters || {}).filter(([, n]) => n > 0);
  const auditFailures = Object.entries(snapshot?.audit_write_failures || {}).filter(([, n]) => n > 0);
  if (refusals.length === 0 && auditFailures.length === 0) return null;

  return (
    <div class="bg-[color-mix(in_srgb,var(--color-status-failed)_18%,transparent)] border-b border-[var(--color-status-failed)] px-4 py-2 flex items-center gap-2 text-[12px] text-[var(--color-status-failed)]">
      <AlertTriangle size={14} />
      <span>
        {refusals.length > 0 && (
          <>Kill-switch refusals: {refusals.map(([name, n]) => `${name}=${n}`).join(', ')}</>
        )}
        {refusals.length > 0 && auditFailures.length > 0 && ' · '}
        {auditFailures.length > 0 && (
          <>Audit-write failures: {auditFailures.map(([name, n]) => `${name}=${n}`).join(', ')}</>
        )}
      </span>
    </div>
  );
}
