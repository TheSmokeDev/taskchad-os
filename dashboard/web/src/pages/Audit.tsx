import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';

interface AuditRow {
  id?: unknown;
  created_at?: unknown;
  operator_id?: unknown;
  persona_id?: unknown;
  action?: unknown;
  outcome?: unknown;
  blocked?: unknown;
  detail?: unknown;
  target_persona_id?: unknown;
}

interface AuditLogResponse {
  rows?: unknown;
  next_before_id?: unknown;
}

function text(value: unknown, fallback = '—'): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'string' && value.trim()) return value;
  return fallback;
}

function outcomeText(row: AuditRow): string {
  if (row.outcome !== undefined && row.outcome !== null && row.outcome !== '') {
    return text(row.outcome);
  }
  return row.blocked === 1 || row.blocked === true ? 'blocked' : '—';
}

/**
 * Append-only audit-log viewer over Python's GET /api/audit-log
 * (PRD-8 Phase 7a). Python owns the admin gate: the endpoint is exempt
 * from the orchestration-token middleware and DASHBOARD_ADMIN_TOKEN
 * bearer is the SOLE auth path — fail-closed 503 while unset. Hono
 * proxies the operator bearer verbatim; this page renders whatever
 * Python returns, shaped defensively.
 */
export function Audit() {
  const { data, loading, error } = useFetch<AuditLogResponse>('/api/audit-log');

  if (loading) return <div class="flex items-center justify-center h-full"><Spinner /></div>;

  if (error) {
    // Fail-closed state: Python returns 503 until DASHBOARD_ADMIN_TOKEN
    // is set on the orchestration API process.
    if (/\b503\b/.test(error) || error.includes('DASHBOARD_ADMIN_TOKEN')) {
      return (
        <div class="flex flex-col h-full">
          <TopBar title="Audit" subtitle="Kill-switch refusals + hard-delete events" />
          <Empty
            title="Audit requires DASHBOARD_ADMIN_TOKEN"
            description="The audit-log endpoint is fail-closed: Python returns 503 until DASHBOARD_ADMIN_TOKEN is set on the orchestration API process. Set it, restart the API, then refresh."
          />
        </div>
      );
    }
    return (
      <div class="flex flex-col h-full">
        <TopBar title="Audit" subtitle="Kill-switch refusals + hard-delete events" />
        <Empty title="Failed to load audit log" description={error} />
      </div>
    );
  }

  const rows: AuditRow[] = Array.isArray(data?.rows) ? (data!.rows as AuditRow[]) : [];

  return (
    <div class="flex flex-col h-full">
      <TopBar title="Audit" subtitle="Kill-switch refusals + hard-delete events · newest first" />
      <div class="flex-1 overflow-y-auto p-6 max-w-4xl">
        {rows.length === 0 ? (
          <Empty
            title="No audit events"
            description="No rows yet. Hard-delete events and kill-switch refusals write audit rows server-side; they appear here once written."
          />
        ) : (
          <table class="w-full text-[12px]">
            <thead>
              <tr class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
                <th class="text-left py-1">Timestamp</th>
                <th class="text-left py-1">Actor</th>
                <th class="text-left py-1">Action</th>
                <th class="text-left py-1">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={text(row.id, String(index))} class="border-t border-[var(--color-border)]">
                  <td class="py-1.5 text-[var(--color-text-muted)] whitespace-nowrap">{text(row.created_at)}</td>
                  <td class="py-1.5 text-[var(--color-text)]">{text(row.operator_id, text(row.persona_id))}</td>
                  <td class="py-1.5 text-[var(--color-text)]">{text(row.action)}</td>
                  <td class="py-1.5 text-[var(--color-text-muted)]">{outcomeText(row)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
