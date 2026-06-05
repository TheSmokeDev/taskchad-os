import type { ComponentChildren } from 'preact';
import { useMemo, useState } from 'preact/hooks';
import { Activity, Network, List as ListIcon } from 'lucide-preact';
import { BrainGraph2D, type BrainActivity } from '@/components/BrainGraph2D';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { TopBar } from '@/components/TopBar';
import { formatRelativeTime } from '@/lib/format';
import { useFetch } from '@/lib/useFetch';
import { usePagedBrainGraph } from '@/lib/usePagedBrainGraph';

interface HiveEntry {
  id: number;
  agent_id: string;
  chat_id: string;
  action: string;
  summary: string;
  artifacts: string | null;
  created_at: number;
}

const KNOWN_AGENTS = ['main', 'research', 'comms', 'content', 'ops'];
const VIEW_KEY = 'homie.knowledgeGraph.view';
type ViewMode = 'graph' | 'activity';

function loadView(): ViewMode {
  try {
    const value = localStorage.getItem(VIEW_KEY);
    if (value === 'graph' || value === 'activity') return value;
    if (value === 'brain' || value === 'brain2d' || value === 'brain3d') return 'graph';
  } catch {}
  return 'graph';
}

export function HiveMind() {
  const [filter, setFilter] = useState<string>('all');
  const [view, setView] = useState<ViewMode>(loadView());
  const [showActivity, setShowActivity] = useState(true);
  const [revealed, setRevealed] = useState<Set<number>>(new Set());

  const params = new URLSearchParams();
  params.set('activity_window_minutes', '60');
  if (filter !== 'all') {
    params.set('scope', 'persona');
    params.set('scope_id', filter);
  }

  const agentList = useFetch<{ agents?: { id: string }[] }>('/api/agents', 30_000);
  const { data, loading, loadingMore, error, hasMore, loadMore } = usePagedBrainGraph(params);
  const entries = useMemo(() => normalizeHiveEntries(data?.activity ?? []), [data?.activity]);
  const agentColors = useMemo(() => buildAgentColors(entries, agentList.data?.agents ?? []), [entries, agentList.data]);
  const allAgents = useMemo(() => {
    const ids = new Set<string>(KNOWN_AGENTS);
    for (const agent of agentList.data?.agents ?? []) {
      if (agent.id) ids.add(agent.id);
    }
    for (const entry of entries) {
      ids.add(entry.agent_id);
    }
    for (const scope of data?.stats?.scopes ?? []) {
      if (scope.scope_id) ids.add(scope.scope_id === 'default' ? 'main' : scope.scope_id);
    }
    return [...ids];
  }, [agentList.data, entries, data?.stats?.scopes]);

  const nodeCount = data?.nodes?.length ?? 0;
  const edgeCount = data?.edges?.length ?? 0;
  const graphPage = data?.stats?.memory?.page ?? data?.stats?.page;
  const loadedChunks = graphPage?.loaded_chunks ?? graphPage?.returned_chunks;
  const matchingChunks = graphPage?.matching_chunks;
  const graphProgress = typeof matchingChunks === 'number'
    ? ` / ${loadedChunks ?? nodeCount}/${matchingChunks} chunks loaded`
    : '';
  function setViewPersisted(next: ViewMode) {
    setView(next);
    try { localStorage.setItem(VIEW_KEY, next); } catch {}
  }

  function toggleRow(id: number) {
    const next = new Set(revealed);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setRevealed(next);
  }

  return (
    <div class="flex flex-col h-full min-h-0">
      <TopBar
        title="Knowledge Graph"
        subtitle={`${nodeCount} loaded memory ${nodeCount === 1 ? 'node' : 'nodes'} / ${edgeCount} links / ${entries.length} recent ${entries.length === 1 ? 'event' : 'events'}${graphProgress}`}
        actions={<ViewSwitcher view={view} onChange={setViewPersisted} />}
      />
      <div class="px-6 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg)] flex items-center gap-2 overflow-x-auto">
        <FilterTab label="All" active={filter === 'all'} onClick={() => setFilter('all')} />
        {allAgents.map((id) => (
          <FilterTab
            key={id}
            label={id}
            active={filter === id}
            color={agentColors[id]}
            onClick={() => setFilter(id)}
          />
        ))}
        {view === 'graph' && hasMore && (
          <button
            type="button"
            title="Load more memory graph"
            onClick={loadMore}
            disabled={loadingMore}
            class="ml-auto inline-flex items-center gap-1.5 px-2.5 py-1 rounded border border-[var(--color-border)] bg-[var(--color-elevated)] text-[11.5px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-50 disabled:cursor-wait shrink-0 transition-colors"
          >
            {loadingMore ? 'Loading graph...' : 'Load more graph'}
          </button>
        )}
        <button
          type="button"
          title="Activity overlay"
          aria-pressed={showActivity}
          onClick={() => setShowActivity((value) => !value)}
          class={[
            (view === 'graph' && hasMore ? '' : 'ml-auto ') + 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded border text-[11.5px] shrink-0 transition-colors',
            showActivity
              ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
              : 'border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
          ].join(' ')}
        >
          <Activity size={12} />
          Activity
        </button>
      </div>

      {error && <Empty title="Knowledge graph unavailable" description={error} />}
      {loading && !data && (
        <div class="flex-1 flex items-center justify-center">
          <Spinner size={22} />
        </div>
      )}
      {!loading && !error && nodeCount === 0 && entries.length === 0 && (
        <Empty
          title="No knowledge graph"
          description="Memory graph nodes and recent activity will appear after the backend has indexed data."
        />
      )}

      {data && (nodeCount > 0 || entries.length > 0) && view === 'graph' && (
        <BrainGraph2D
          data={data}
          mode="hive"
          agentFilter={filter}
          agentColors={agentColors}
          showActivity={showActivity}
          allowActivityToggle
          onShowActivityChange={setShowActivity}
          blurOn={false}
        />
      )}

      {entries.length > 0 && view === 'activity' && (
        <div class="flex-1 overflow-y-auto">
          <table class="w-full text-[12px]">
            <thead class="sticky top-0 bg-[var(--color-bg)] border-b border-[var(--color-border)]">
              <tr class="text-left">
                <th class="px-6 py-2 font-medium text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] w-[12%]">When</th>
                <th class="px-3 py-2 font-medium text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] w-[12%]">Agent</th>
                <th class="px-3 py-2 font-medium text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] w-[14%]">Action</th>
                <th class="px-3 py-2 font-medium text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Summary</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id} class="border-b border-[var(--color-border)] hover:bg-[var(--color-elevated)] transition-colors">
                  <td class="px-6 py-2 text-[var(--color-text-faint)] tabular-nums whitespace-nowrap">
                    {formatRelativeTime(entry.created_at)}
                  </td>
                  <td class="px-3 py-2">
                    <span class="inline-flex items-center gap-1.5" style={{ color: agentColors[entry.agent_id] || 'var(--color-text-muted)' }}>
                      <span class="inline-block w-1.5 h-1.5 rounded-full" style={{ backgroundColor: 'currentColor' }} />
                      {entry.agent_id}
                    </span>
                  </td>
                  <td class="px-3 py-2 font-mono text-[11px] text-[var(--color-text-muted)]">{entry.action}</td>
                  <td class="px-3 py-2 text-[var(--color-text)] truncate max-w-0">
                    <span
                      class={revealed.has(entry.id) ? 'revealed' : ''}
                      onClick={(ev) => { ev.stopPropagation(); toggleRow(entry.id); }}
                    >
                      {entry.summary}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function FilterTab({
  label,
  active,
  color,
  onClick,
}: {
  label: string;
  active: boolean;
  color?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      class={[
        'inline-flex items-center gap-1.5 px-2.5 py-1 rounded border text-[11.5px] shrink-0 transition-colors',
        active
          ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-text)]'
          : 'border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
      ].join(' ')}
    >
      {color && label !== 'All' && (
        <span class="inline-block w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />
      )}
      {label}
    </button>
  );
}

function ViewSwitcher({
  view,
  onChange,
}: {
  view: ViewMode;
  onChange: (value: ViewMode) => void;
}) {
  return (
    <div class="inline-flex bg-[var(--color-elevated)] border border-[var(--color-border)] rounded p-0.5">
      <ViewButton icon={<Network size={13} />} title="Graph" active={view === 'graph'} onClick={() => onChange('graph')} />
      <ViewButton icon={<ListIcon size={13} />} title="Activity table" active={view === 'activity'} onClick={() => onChange('activity')} />
    </div>
  );
}

function ViewButton({
  icon,
  title,
  active,
  onClick,
  disabled,
}: {
  icon: ComponentChildren;
  title: string;
  active: boolean;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      class={[
        'inline-flex items-center justify-center w-7 h-7 rounded transition-colors',
        active ? 'bg-[var(--color-accent)] text-white' : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
        disabled ? 'opacity-30 cursor-not-allowed' : '',
      ].join(' ')}
    >
      {icon}
    </button>
  );
}

function normalizeHiveEntries(activity: BrainActivity[]): HiveEntry[] {
  return activity.map((event, index) => {
    const agentId = normalizeAgentId(event.personaId ?? event.persona_id ?? event.agentId ?? event.agent_id);
    const action = normalizeAction(event);
    return {
      id: numericId(event.eventId ?? event.event_id ?? event.id, index),
      agent_id: agentId,
      chat_id: String(event.chatId ?? event.chat_id ?? event.sessionId ?? event.session_id ?? `agent:${agentId}`),
      action,
      summary: String(event.details ?? event.excerpt ?? event.summary ?? action),
      artifacts: normalizeArtifacts(event),
      created_at: normalizeTimestamp(event.timestamp ?? event.createdAt ?? event.created_at),
    };
  });
}

function normalizeAgentId(value: unknown): string {
  const text = String(value || 'main').trim();
  return text === 'default' ? 'main' : text || 'main';
}

function normalizeAction(event: BrainActivity): string {
  const base = String(event.action ?? event.type ?? event.event_type ?? 'chat_message');
  if (base === 'chat_message' && event.role) {
    return `${base}:${event.role}`;
  }
  return base;
}

function normalizeArtifacts(event: BrainActivity): string | null {
  if (event.artifacts) return String(event.artifacts);
  const bits = [event.provider, event.model].filter(Boolean).map(String);
  return bits.length ? bits.join(' / ') : null;
}

function normalizeTimestamp(value: number | string | undefined): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 10_000_000_000 ? value / 1000 : value;
  }
  if (typeof value === 'string' && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric > 10_000_000_000 ? numeric / 1000 : numeric;
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed / 1000;
  }
  return Date.now() / 1000;
}

function numericId(value: unknown, index: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric;
  return stableHash(String(value ?? `hive-${index}`));
}

function stableHash(value: string): number {
  let hash = 2166136261;
  for (const char of value) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function buildAgentColors(entries: HiveEntry[], agents: { id: string }[]): Record<string, string> {
  const colors: Record<string, string> = {
    main: 'var(--color-accent)',
    research: '#5eb6ff',
    comms: '#10b981',
    content: '#f59e0b',
    ops: '#a78bfa',
  };
  const ids = new Set<string>([...KNOWN_AGENTS, ...agents.map((agent) => agent.id), ...entries.map((entry) => entry.agent_id)]);
  for (const id of ids) {
    if (!colors[id]) colors[id] = paletteColor(id);
  }
  return colors;
}

function paletteColor(id: string): string {
  const palette = ['#5eb6ff', '#10b981', '#f59e0b', '#a78bfa', '#f87171', '#2dd4bf', '#e879f9', '#84cc16'];
  return palette[stableHash(id) % palette.length];
}
