import { Link } from 'wouter-preact';
import { LaneStatusPill } from './LaneStatusPill';
import type { Agent } from './AgentCard';

interface AgentRowProps {
  agent: Agent;
}

export function AgentRow({ agent }: AgentRowProps) {
  return (
    <Link
      href={`/agents/${agent.id}/files`}
      class="flex items-center gap-3 px-4 py-2.5 border-b border-[var(--color-border)] hover:bg-[var(--color-elevated)] transition-colors"
    >
      <span class={`w-1.5 h-1.5 rounded-full ${agent.running ? 'bg-[var(--color-status-done)]' : 'bg-[var(--color-text-faint)]'}`} />
      <div class="flex-1 min-w-0">
        <div class="text-[13px] text-[var(--color-text)] truncate">{agent.name || agent.id}</div>
        <div class="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider">{agent.id}</div>
      </div>
      <LaneStatusPill
        lane={agent.lane === 'generic' ? 'generic' : 'claude_native'}
        value={agent.lane === 'generic' ? (agent.todayCost ?? 0) : (agent.todayTurns ?? 0)}
        quotaPct={agent.lane === 'generic' ? undefined : agent.planQuotaPct}
      />
    </Link>
  );
}
