import { formatCost } from '@/lib/format';

interface LaneStatusPillProps {
  lane: 'claude_native' | 'generic';
  /** For claude_native: turns count. For generic: cost in USD. */
  value: number;
  /** For claude_native: plan quota %. */
  quotaPct?: number;
}

/** Lane-aware status pill. claude_native displays turns + plan-quota %;
 *  generic displays summed cost across providers. NEVER mix the two. */
export function LaneStatusPill({ lane, value, quotaPct }: LaneStatusPillProps) {
  if (lane === 'claude_native') {
    return (
      <span
        class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] bg-[var(--color-accent-soft)] text-[var(--color-accent)] tabular-nums"
        title="Claude Max — turns today (no per-token cost)"
      >
        <span class="font-medium">{value}</span> turns
        {quotaPct !== undefined && (
          <span class="text-[10px] opacity-70">· {Math.round(quotaPct)}% quota</span>
        )}
      </span>
    );
  }
  return (
    <span
      class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] bg-[var(--color-elevated)] text-[var(--color-text-muted)] tabular-nums"
      title="Generic provider — cost-billed"
    >
      <span class="font-medium">{formatCost(value)}</span>
    </span>
  );
}
