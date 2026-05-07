import { useState } from 'preact/hooks';
import { useRoute } from 'wouter-preact';
import { TopBar } from '@/components/TopBar';
import { Tabs } from '@/components/Tabs';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { AgentActions } from '@/components/AgentActions';
import { AvatarUploader } from '@/components/AvatarUploader';
import { LaneStatusPill } from '@/components/LaneStatusPill';
import { useFetch } from '@/lib/useFetch';
import { formatCost } from '@/lib/format';
import { showCosts } from '@/lib/theme';
import type { Agent } from '@/components/AgentCard';

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

interface TaskRow {
  id: string;
  title: string;
  status: string;
  created_at: number;
}
interface TasksResponse { tasks: TaskRow[]; }

export function AgentDetail() {
  const [, params] = useRoute('/agents/:id');
  const agentId = params?.id ?? '';
  const [tab, setTab] = useState<'overview' | 'tokens' | 'tasks'>('overview');

  const agentFetch = useFetch<Agent>(agentId ? `/api/agents/${agentId}` : null);
  const tokensFetch = useFetch<TokensResponse>(
    agentId && tab === 'tokens' ? `/api/agents/${agentId}/tokens?range=7d&interval=1d` : null
  );
  const tasksFetch = useFetch<TasksResponse>(
    agentId && tab === 'tasks' ? `/api/agents/${agentId}/tasks` : null
  );

  if (!agentId) return <Empty title="No agent specified" />;
  if (agentFetch.loading) return <div class="flex items-center justify-center h-full"><Spinner /></div>;
  if (agentFetch.error || !agentFetch.data) return <Empty title="Agent not found" description={agentFetch.error ?? undefined} />;

  const agent = agentFetch.data;

  return (
    <div class="flex flex-col h-full">
      <TopBar
        title={agent.name || agent.id}
        subtitle={agent.description}
        actions={<AgentActions agentId={agent.id} running={agent.running} onChange={agentFetch.refresh} />}
      />
      <Tabs
        tabs={[
          { id: 'overview', label: 'Overview' },
          { id: 'tokens', label: 'Tokens' },
          { id: 'tasks', label: 'Tasks' },
        ]}
        active={tab}
        onChange={(t) => setTab(t as any)}
      >
        <div class="h-full overflow-y-auto p-6">
          {tab === 'overview' && (
            <div class="space-y-6 max-w-2xl">
              <section>
                <h3 class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-2">Avatar</h3>
                <AvatarUploader agentId={agent.id} onChange={agentFetch.refresh} />
              </section>
              <section class="grid grid-cols-2 gap-4">
                <Stat label="Status" value={agent.running ? 'running' : 'offline'} />
                <Stat label="Model" value={agent.model} />
                <Stat label="Today turns" value={String(agent.todayTurns ?? 0)} />
                {showCosts.value && (
                  <Stat label="Today cost" value={formatCost(agent.todayCost ?? 0)} />
                )}
              </section>
            </div>
          )}
          {tab === 'tokens' && <TokensTab fetchState={tokensFetch} />}
          {tab === 'tasks' && <TasksTab fetchState={tasksFetch} />}
        </div>
      </Tabs>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div class="bg-[var(--color-card)] border border-[var(--color-border)] rounded p-3">
      <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">{label}</div>
      <div class="text-[14px] text-[var(--color-text)] tabular-nums">{value}</div>
    </div>
  );
}

function TokensTab({ fetchState }: { fetchState: ReturnType<typeof useFetch<TokensResponse>> }) {
  const { data, loading, error } = fetchState;
  if (loading) return <Spinner />;
  if (error) return <Empty title="Failed to load tokens" description={error} />;
  if (!data) return <Empty title="No token data" />;

  const summary = data.summary;
  const providers = Object.entries(summary.generic.by_provider);

  return (
    <div class="space-y-6">
      <section>
        <h3 class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">Today (lane-aware)</h3>
        <div class="flex items-center gap-3 flex-wrap">
          <div>
            <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Claude Max</div>
            <LaneStatusPill
              lane="claude_native"
              value={summary.claude_native.turns_today}
              quotaPct={summary.claude_native.plan_quota_estimate_pct}
            />
          </div>
          <div>
            <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Generic providers</div>
            <LaneStatusPill lane="generic" value={summary.generic.total_cost_usd} />
          </div>
        </div>
      </section>

      {providers.length > 0 && (
        <section>
          <h3 class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">Provider breakdown</h3>
          <div class="space-y-1">
            {providers.map(([name, p]: [string, any]) => (
              <div key={name} class="flex items-center justify-between px-3 py-2 bg-[var(--color-card)] border border-[var(--color-border)] rounded">
                <span class="text-[12px] text-[var(--color-text)]">{name}</span>
                <span class="text-[11px] text-[var(--color-text-muted)] tabular-nums">{formatCost(p.cost_usd ?? 0)}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section>
        <h3 class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">7-day timeline</h3>
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
  );
}

function TasksTab({ fetchState }: { fetchState: ReturnType<typeof useFetch<TasksResponse>> }) {
  const { data, loading, error } = fetchState;
  if (loading) return <Spinner />;
  if (error) return <Empty title="Failed to load tasks" description={error} />;
  if (!data?.tasks?.length) return <Empty title="No tasks yet" />;
  return (
    <ul class="divide-y divide-[var(--color-border)]">
      {data.tasks.map((t) => (
        <li key={t.id} class="py-2 flex items-center gap-2">
          <span class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{t.status}</span>
          <span class="text-[12px] text-[var(--color-text)] flex-1">{t.title}</span>
          <span class="text-[10px] text-[var(--color-text-faint)]">{new Date(t.created_at * 1000).toLocaleString()}</span>
        </li>
      ))}
    </ul>
  );
}
