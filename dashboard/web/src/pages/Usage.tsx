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
      </div>
    </div>
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
