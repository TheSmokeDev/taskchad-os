import { useFetch } from '@/lib/useFetch';
import { Empty } from './Empty';
import { Spinner } from './Spinner';

interface HiveMindEvent {
  id: string;
  personaId: string;
  type: string;
  timestamp: number;
  details?: string;
}

interface HiveMindResponse {
  events: HiveMindEvent[];
}

/** Lightweight 2D fallback for HiveMind data — used when WebGL is
 *  unavailable. Donor port simplified to a list. */
export function BrainGraph({ personaId, limit = 50 }: { personaId?: string; limit?: number }) {
  const params = new URLSearchParams();
  if (personaId) params.set('persona_id', personaId);
  params.set('limit', String(limit));
  params.set('window_minutes', '60');
  const { data, loading, error } = useFetch<HiveMindResponse>(`/api/hive-mind/recent?${params.toString()}`, 5_000);

  if (loading) return <div class="flex items-center justify-center h-full"><Spinner size={20} /></div>;
  if (error) return <Empty title="Hive mind unavailable" description={error} />;
  if (!data?.events?.length) return <Empty title="No recent activity" description="Hive mind activity will surface here as agents run." />;

  return (
    <ul class="divide-y divide-[var(--color-border)]">
      {data.events.map((ev) => (
        <li key={ev.id} class="px-4 py-2 text-[12px]">
          <span class="text-[var(--color-text-faint)] uppercase tracking-wider text-[10px] mr-2">{ev.personaId}</span>
          <span class="text-[var(--color-text)]">{ev.type}</span>
          {ev.details && <span class="text-[var(--color-text-muted)] ml-2">{ev.details}</span>}
        </li>
      ))}
    </ul>
  );
}
