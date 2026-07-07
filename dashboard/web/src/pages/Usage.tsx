import { useState } from 'preact/hooks';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { LaneStatusPill } from '@/components/LaneStatusPill';
import { useFetch } from '@/lib/useFetch';
import { formatCost } from '@/lib/format';

interface TimelineEntry {
  date: string;
  claude_native: { turns: number; messages: number };
  generic: {
    by_provider: Record<string, { cost_usd: number; messages: number; model: string }>;
    total_cost_usd: number;
  };
}
interface TokensResponse {
  timeline: TimelineEntry[];
  summary: {
    claude_native: { turns_today: number; messages_today: number; plan_quota_estimate_pct: number };
    generic: { by_provider: Record<string, any>; total_cost_usd: number };
  };
}

interface InsightsResponse {
  days: number;
  totals?: { sessions?: number; messages?: number };
  sessions_by_surface?: { surface: string; sessions: number; messages: number }[];
  messages_per_day?: { date: string; messages: number }[];
  most_active_sessions?: { session_id: string; surface: string; messages: number; last_activity: string | null }[];
  top_commands?: { command: string; count: number }[];
}

/**
 * Lane-aware usage page. CRITICAL: claude_native (turns + plan-quota %)
 * and generic (provider × $) are SEPARATE displays. We never sum them
 * into a single cost number — claude_native turns are not USD-priced
 * (Max plan), and a single number would lie about either lane.
 */
export function Usage() {
  const { data, loading, error } = useFetch<TokensResponse>('/api/tokens?range=30d&interval=1d', 60_000);

  if (loading) return <div class="flex items-center justify-center h-full"><Spinner /></div>;
  if (error) return <Empty title="Failed to load usage" description={error} />;
  if (!data) return <Empty title="No usage data" />;

  const summary = data.summary;
  const providers = Object.entries(summary.generic.by_provider);

  return (
    <div class="flex flex-col h-full">
      <TopBar title="Usage" subtitle="Lane-aware: Claude Max turns + Generic provider cost" />
      <div class="flex-1 overflow-y-auto p-6 space-y-8 max-w-4xl">
        <section class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Card title="Claude Max (subscription)">
            <div class="space-y-2">
              <div class="flex items-center gap-2">
                <LaneStatusPill
                  lane="claude_native"
                  value={summary.claude_native.turns_today}
                  quotaPct={summary.claude_native.plan_quota_estimate_pct}
                />
                <span class="text-[11px] text-[var(--color-text-muted)]">today</span>
              </div>
              <div class="text-[11px] text-[var(--color-text-faint)]">
                {summary.claude_native.messages_today} message{summary.claude_native.messages_today === 1 ? '' : 's'} today.
                Quota cycles weekly.
              </div>
            </div>
          </Card>
          <Card title="Generic providers (API-billed)">
            <div class="space-y-2">
              <LaneStatusPill lane="generic" value={summary.generic.total_cost_usd} />
              <div class="text-[11px] text-[var(--color-text-faint)]">
                {providers.length} provider{providers.length === 1 ? '' : 's'} active.
              </div>
            </div>
          </Card>
        </section>

        {providers.length > 0 && (
          <section>
            <h3 class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">Provider breakdown — today</h3>
            <div class="space-y-1">
              {providers.map(([name, p]: [string, any]) => (
                <div key={name} class="flex items-center justify-between px-3 py-2 bg-[var(--color-card)] border border-[var(--color-border)] rounded">
                  <div>
                    <span class="text-[12px] text-[var(--color-text)]">{name}</span>
                    {p.model && <span class="text-[10px] text-[var(--color-text-faint)] ml-2">{p.model}</span>}
                  </div>
                  <div class="flex items-center gap-3 text-[11px] text-[var(--color-text-muted)] tabular-nums">
                    <span>{p.messages ?? 0} msgs</span>
                    <span>{formatCost(p.cost_usd ?? 0)}</span>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        <section>
          <h3 class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">30-day timeline</h3>
          <table class="w-full text-[12px]">
            <thead>
              <tr class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
                <th class="text-left py-1">Date</th>
                <th class="text-right py-1">Claude turns</th>
                <th class="text-right py-1">Generic $</th>
              </tr>
            </thead>
            <tbody>
              {data.timeline.map((row) => (
                <tr key={row.date} class="border-t border-[var(--color-border)]">
                  <td class="py-1.5 text-[var(--color-text-muted)]">{row.date}</td>
                  <td class="py-1.5 text-right tabular-nums text-[var(--color-text)]">{row.claude_native.turns}</td>
                  <td class="py-1.5 text-right tabular-nums text-[var(--color-text)]">{formatCost(row.generic.total_cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <InsightsSection />
      </div>
    </div>
  );
}

const INSIGHT_RANGES = [7, 30, 90] as const;

/**
 * Conversation insights (Phase 3) — pure aggregation from /api/insights.
 * The endpoint is fail-open (zeroed shape on any backend error), and this
 * section is defensive about missing fields, so it can never take down
 * the token charts above it. Simple styled tiles — no chart library.
 */
function InsightsSection() {
  const [days, setDays] = useState<number>(7);
  const { data, loading, error } = useFetch<InsightsResponse>(`/api/insights?days=${days}`, 60_000);

  const surfaces = data?.sessions_by_surface ?? [];
  const perDay = data?.messages_per_day ?? [];
  const commands = data?.top_commands ?? [];
  const active = data?.most_active_sessions ?? [];
  const totalSessions = data?.totals?.sessions ?? 0;
  const totalMessages = data?.totals?.messages ?? 0;
  const busiest = perDay.reduce<{ date: string; messages: number } | null>(
    (acc, d) => (acc && acc.messages >= d.messages ? acc : d),
    null,
  );

  return (
    <section>
      <div class="flex items-center gap-3 mb-3">
        <h3 class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)]">Conversation insights</h3>
        <div class="inline-flex items-center rounded border border-[var(--color-border)] bg-[var(--color-elevated)] overflow-hidden">
          {INSIGHT_RANGES.map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => setDays(n)}
              aria-label={`Insights window ${n} days`}
              class={
                days === n
                  ? 'px-2 py-1 text-[11px] text-[var(--color-accent)] bg-[var(--color-accent-soft)]'
                  : 'px-2 py-1 text-[11px] text-[var(--color-text-muted)] hover:text-[var(--color-text)]'
              }
            >
              {n}d
            </button>
          ))}
        </div>
      </div>

      {loading && !data && <div class="py-4"><Spinner size={14} /></div>}
      {error && (
        <div class="text-[12px] text-[var(--color-text-muted)] py-2">
          Insights unavailable: {error}
        </div>
      )}

      {data && (
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Card title={`Sessions by surface — ${days}d`}>
            <div class="space-y-1.5">
              <div class="text-[12px] text-[var(--color-text-muted)]">
                {totalSessions} session{totalSessions === 1 ? '' : 's'} · {totalMessages} message{totalMessages === 1 ? '' : 's'}
              </div>
              {surfaces.length === 0 && (
                <div class="text-[11px] text-[var(--color-text-faint)]">No activity in window.</div>
              )}
              {surfaces.map((s) => (
                <div key={s.surface} class="flex items-center justify-between text-[12px]">
                  <span class="text-[var(--color-text)]">{s.surface}</span>
                  <span class="tabular-nums text-[var(--color-text-muted)]">{s.sessions} · {s.messages} msgs</span>
                </div>
              ))}
            </div>
          </Card>

          <Card title="Busiest day">
            {busiest ? (
              <div class="space-y-1">
                <div class="text-[18px] tabular-nums text-[var(--color-text)]">{busiest.messages}</div>
                <div class="text-[11px] text-[var(--color-text-muted)]">messages on {busiest.date}</div>
                <div class="text-[11px] text-[var(--color-text-faint)]">
                  {perDay.length} active day{perDay.length === 1 ? '' : 's'} in window
                </div>
              </div>
            ) : (
              <div class="text-[11px] text-[var(--color-text-faint)]">No activity in window.</div>
            )}
          </Card>

          <Card title="Top commands">
            <div class="space-y-1.5">
              {commands.length === 0 && (
                <div class="text-[11px] text-[var(--color-text-faint)]">No slash commands in window.</div>
              )}
              {commands.slice(0, 6).map((c) => (
                <div key={c.command} class="flex items-center justify-between text-[12px]">
                  <span class="font-mono text-[var(--color-text)]">{c.command}</span>
                  <span class="tabular-nums text-[var(--color-text-muted)]">{c.count}×</span>
                </div>
              ))}
            </div>
          </Card>

          {active.length > 0 && (
            <div class="md:col-span-3">
              <Card title="Most active sessions">
                <div class="space-y-1">
                  {active.map((s) => (
                    <div key={s.session_id} class="flex items-center gap-3 text-[12px]">
                      <span class="text-[10.5px] uppercase tracking-wider text-[var(--color-text-faint)] w-16 shrink-0">{s.surface}</span>
                      <span class="font-mono text-[11px] text-[var(--color-text-muted)] truncate">{s.session_id}</span>
                      <span class="ml-auto tabular-nums text-[var(--color-text-muted)] shrink-0">{s.messages} msgs</span>
                    </div>
                  ))}
                </div>
              </Card>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function Card({ title, children }: { title: string; children: any }) {
  return (
    <div class="bg-[var(--color-card)] border border-[var(--color-border)] rounded-lg p-4">
      <div class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">{title}</div>
      {children}
    </div>
  );
}
