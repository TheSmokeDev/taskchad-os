import { AlertTriangle } from 'lucide-preact';
import { useFetch } from '@/lib/useFetch';

interface Health {
  ok: boolean;
  killSwitches?: Record<string, boolean>;
}

/** Reads /api/health (the only unauthenticated endpoint) and shows a
 *  banner when any kill switch is OFF. WS3 health stub returns
 *  `killSwitches: {}` for Phase 3, so this banner is dormant until later
 *  phases populate the field. The component renders nothing when healthy. */
export function KillSwitchBanner() {
  const { data } = useFetch<Health>('/api/health', 30_000);
  const switches = data?.killSwitches || {};
  const off = Object.entries(switches).filter(([, on]) => !on);
  if (off.length === 0) return null;

  return (
    <div class="bg-[color-mix(in_srgb,var(--color-status-failed)_18%,transparent)] border-b border-[var(--color-status-failed)] px-4 py-2 flex items-center gap-2 text-[12px] text-[var(--color-status-failed)]">
      <AlertTriangle size={14} />
      <span>
        {off.length} kill switch{off.length === 1 ? '' : 'es'} OFF: {off.map(([k]) => k).join(', ')}
      </span>
    </div>
  );
}
