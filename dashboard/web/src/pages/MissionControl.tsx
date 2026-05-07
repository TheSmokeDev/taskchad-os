import { TopBar } from '@/components/TopBar';
import { AgentRow } from '@/components/AgentRow';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';
import type { Agent } from '@/components/AgentCard';

interface AgentsResponse { agents: Agent[]; }

export function MissionControl() {
  const { data, loading, error } = useFetch<AgentsResponse>('/api/agents', 30_000);
  const agents = data?.agents ?? [];

  return (
    <div class="flex flex-col h-full">
      <TopBar title="Mission Control" subtitle={`${agents.filter((a) => a.running).length} running · ${agents.length} total`} />
      <div class="flex-1 overflow-y-auto">
        {loading && !data && <div class="flex items-center justify-center h-full"><Spinner /></div>}
        {error && <Empty title="Failed to load" description={error} />}
        {!loading && !error && agents.length === 0 && (
          <Empty title="No agents" description="Create one in the Agents page." />
        )}
        {agents.map((a) => <AgentRow key={a.id} agent={a} />)}
      </div>
    </div>
  );
}
