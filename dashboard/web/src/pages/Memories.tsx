import { useState } from 'preact/hooks';
import { Brain, GitBranch, List, Network, Users, Boxes, MessageSquare } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { MemoryRow, type MemoryRecord } from '@/components/MemoryRow';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { BrainGraph2D } from '@/components/BrainGraph2D';
import { useFetch } from '@/lib/useFetch';
import { usePagedBrainGraph } from '@/lib/usePagedBrainGraph';

interface MemoriesResponse { memories: MemoryRecord[]; }

type ViewMode = 'graph' | 'list';
type ScopePreset = 'all' | 'global' | 'main' | 'persona' | 'agent' | 'team' | 'room';

export function Memories() {
  const [view, setView] = useState<ViewMode>('graph');
  const [scopePreset, setScopePreset] = useState<ScopePreset>('all');
  const [scopeId, setScopeId] = useState<string>('research');
  const [personaId, setPersonaId] = useState<string>('');
  const listParams = new URLSearchParams();
  if (personaId) listParams.set('persona_id', personaId);
  listParams.set('limit', '50');
  const graphParams = buildGraphParams(scopePreset, scopeId);
  const graph = usePagedBrainGraph(graphParams, view === 'graph');
  const list = useFetch<MemoriesResponse>(view === 'list' ? `/api/memories?${listParams.toString()}` : null, 30_000);
  const graphPage = graph.data?.stats?.memory?.page ?? graph.data?.stats?.page;
  const matchingChunks = graphPage?.matching_chunks;
  const loadedChunks = graphPage?.loaded_chunks ?? graphPage?.returned_chunks;
  const subtitle = view === 'graph'
    ? `${graph.data?.nodes?.length ?? 0} loaded nodes / ${graph.data?.edges?.length ?? 0} links${
        typeof matchingChunks === 'number' ? ` / ${loadedChunks ?? 0}/${matchingChunks} chunks loaded` : ''
      }`
    : list.data?.memories ? `${list.data.memories.length} entries` : '';

  return (
    <div class="flex flex-col h-full min-h-0">
      <TopBar
        title="Memories"
        subtitle={subtitle}
        actions={<ViewSwitch view={view} onChange={setView} />}
      />

      {view === 'graph' && (
        <div class="px-6 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg)] flex flex-wrap items-center gap-2">
          <ScopeButton label="All" icon={Network} active={scopePreset === 'all'} onClick={() => setScopePreset('all')} />
          <ScopeButton label="Global" icon={Brain} active={scopePreset === 'global'} onClick={() => setScopePreset('global')} />
          <ScopeButton label="Main" icon={Users} active={scopePreset === 'main'} onClick={() => setScopePreset('main')} />
          <ScopeButton label="Persona" icon={Users} active={scopePreset === 'persona'} onClick={() => setScopePreset('persona')} />
          <ScopeButton label="Agent" icon={Boxes} active={scopePreset === 'agent'} onClick={() => setScopePreset('agent')} />
          <ScopeButton label="Team" icon={GitBranch} active={scopePreset === 'team'} onClick={() => setScopePreset('team')} />
          <ScopeButton label="Room" icon={MessageSquare} active={scopePreset === 'room'} onClick={() => setScopePreset('room')} />
          {scopePreset !== 'all' && scopePreset !== 'global' && scopePreset !== 'main' && (
            <input
              type="text"
              value={scopeId}
              onInput={(e) => setScopeId((e.target as HTMLInputElement).value)}
              aria-label="Scope id"
              class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2 py-1 text-[12px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
            />
          )}
          {graph.hasMore && (
            <button
              type="button"
              onClick={graph.loadMore}
              disabled={graph.loadingMore}
              class="ml-auto inline-flex items-center px-2.5 py-1.5 rounded border border-[var(--color-border)] bg-[var(--color-elevated)] text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-50 disabled:cursor-wait transition-colors"
            >
              {graph.loadingMore ? 'Loading graph...' : 'Load more graph'}
            </button>
          )}
        </div>
      )}

      {view === 'list' && (
        <div class="px-6 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg)]">
          <input
            type="text"
            value={personaId}
            onInput={(e) => setPersonaId((e.target as HTMLInputElement).value)}
            placeholder="filter by persona id..."
            class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2 py-1 text-[12px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
          />
        </div>
      )}

      <div class="flex-1 overflow-hidden min-h-0">
        {view === 'graph' && graph.loading && !graph.data && (
          <div class="flex items-center justify-center h-full"><Spinner /></div>
        )}
        {view === 'graph' && graph.error && <Empty title="Failed to load memory graph" description={graph.error} />}
        {view === 'graph' && !graph.loading && !graph.error && (!graph.data?.nodes?.length) && (
          <Empty title="No graph nodes" description="Memory graph nodes will appear after the vault index has readable chunks." />
        )}
        {view === 'graph' && graph.data?.nodes?.length ? (
          <BrainGraph2D
            data={graph.data}
            mode="memory"
            showActivity={false}
            allowActivityToggle={false}
          />
        ) : null}

        {view === 'list' && list.loading && !list.data && (
          <div class="flex items-center justify-center h-full"><Spinner /></div>
        )}
        {view === 'list' && list.error && <Empty title="Failed to load memories" description={list.error} />}
        {view === 'list' && !list.loading && !list.error && (!list.data?.memories?.length) && (
          <Empty title="No memories" description="Memories accumulate as conversations and reflections run." />
        )}
        {view === 'list' && (
          <div class="h-full overflow-y-auto">
            {list.data?.memories?.map((m) => <MemoryRow key={m.id} memory={m} />)}
          </div>
        )}
      </div>
    </div>
  );
}

function ViewSwitch({ view, onChange }: { view: ViewMode; onChange: (view: ViewMode) => void }) {
  return (
    <div class="inline-flex items-center rounded border border-[var(--color-border)] bg-[var(--color-elevated)] overflow-hidden">
      <button
        type="button"
        title="Memory graph"
        aria-label="Memory graph"
        onClick={() => onChange('graph')}
        class={view === 'graph' ? switchActive : switchIdle}
      >
        <Network size={15} />
      </button>
      <button
        type="button"
        title="Memory list"
        aria-label="Memory list"
        onClick={() => onChange('list')}
        class={view === 'list' ? switchActive : switchIdle}
      >
        <List size={15} />
      </button>
    </div>
  );
}

const switchActive = 'px-2.5 py-1.5 text-[var(--color-accent)] bg-[var(--color-accent-soft)]';
const switchIdle = 'px-2.5 py-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)]';

function ScopeButton({
  label,
  icon: Icon,
  active,
  onClick,
}: {
  label: string;
  icon: typeof Brain;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={`Scope ${label}`}
      onClick={onClick}
      class={[
        'inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded border text-[12px] transition-colors',
        active
          ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
          : 'border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
      ].join(' ')}
    >
      <Icon size={13} />
      <span>{label}</span>
    </button>
  );
}

function buildGraphParams(scopePreset: ScopePreset, scopeId: string): URLSearchParams {
  const params = new URLSearchParams();
  params.set('limit', '140');
  params.set('activity_window_minutes', '60');
  if (scopePreset === 'all') {
    params.set('scope', 'all');
    return params;
  }
  if (scopePreset === 'global') {
    params.set('scope', 'global');
    return params;
  }
  if (scopePreset === 'main') {
    params.set('scope', 'persona');
    params.set('scope_id', 'main');
    return params;
  }
  params.set('scope', scopePreset);
  if (scopeId.trim()) {
    params.set('scope_id', scopeId.trim());
  }
  return params;
}
