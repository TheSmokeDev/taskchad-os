import { useState } from 'preact/hooks';
import { TopBar } from '@/components/TopBar';
import { MemoryRow, type MemoryRecord } from '@/components/MemoryRow';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';

interface MemoriesResponse { memories: MemoryRecord[]; }

export function Memories() {
  const [personaId, setPersonaId] = useState<string>('');
  const params = new URLSearchParams();
  if (personaId) params.set('persona_id', personaId);
  params.set('limit', '50');
  const { data, loading, error } = useFetch<MemoriesResponse>(`/api/memories?${params.toString()}`, 30_000);

  return (
    <div class="flex flex-col h-full">
      <TopBar
        title="Memories"
        subtitle={data?.memories ? `${data.memories.length} entries` : ''}
        actions={
          <input
            type="text"
            value={personaId}
            onInput={(e) => setPersonaId((e.target as HTMLInputElement).value)}
            placeholder="filter by persona id..."
            class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2 py-1 text-[12px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
          />
        }
      />
      <div class="flex-1 overflow-y-auto">
        {loading && !data && <div class="flex items-center justify-center h-full"><Spinner /></div>}
        {error && <Empty title="Failed to load memories" description={error} />}
        {!loading && !error && (!data?.memories?.length) && (
          <Empty title="No memories" description="Memories accumulate as conversations and reflections run." />
        )}
        {data?.memories?.map((m) => <MemoryRow key={m.id} memory={m} />)}
      </div>
    </div>
  );
}
