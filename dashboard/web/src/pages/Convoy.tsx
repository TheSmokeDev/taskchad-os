import { useMemo, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import {
  CheckCircle2,
  GitBranch,
  Mail,
  Play,
  Plus,
  RefreshCw,
  Send,
  XCircle,
} from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { Modal } from '@/components/Modal';
import { useFetch } from '@/lib/useFetch';
import { apiPost } from '@/lib/api';
import { pushToast } from '@/lib/toasts';

type ConvoyStatus = 'draft' | 'active' | 'paused' | 'completed' | 'failed' | 'cancelled';
type SubtaskStatus = 'pending' | 'ready' | 'dispatched' | 'running' | 'stalled' | 'completed' | 'failed' | 'cancelled';
type DetailTab = 'graph' | 'subtasks' | 'mailbox';

interface ConvoySummary {
  id: number;
  title: string;
  description?: string | null;
  status: ConvoyStatus;
  decomposition_mode?: string;
  created_by?: string;
  base_branch?: string;
  repo_path?: string | null;
  merge_strategy?: string;
  total_subtasks: number;
  completed_subtasks: number;
  failed_subtasks: number;
  started_at?: number | null;
  completed_at?: number | null;
  created_at?: number;
  updated_at?: number;
}

interface ConvoySubtask {
  id: number;
  convoy_id: number;
  title: string;
  description?: string | null;
  status: SubtaskStatus;
  assigned_agent_id?: string | null;
  assigned_agent_name?: string | null;
  remaining_dependencies: number;
  error_message?: string | null;
  dispatched_at?: number | null;
  started_at?: number | null;
  completed_at?: number | null;
  seq: number;
  updated_at?: number;
}

interface DependencyEdge {
  id: number;
  from_subtask_id: number;
  to_subtask_id: number;
}

interface ConvoyDetail {
  convoy: ConvoySummary;
  subtasks: ConvoySubtask[];
  edges: DependencyEdge[];
}

interface TeamSession {
  id: number;
  team_name: string;
  status: string;
  convoy_id?: number | null;
  backend_type?: string;
}

interface AgentMessage {
  id: number;
  from_agent: string;
  message_type?: string;
  subject?: string | null;
  body: string;
  created_at: number;
}

interface MessageWithDeliveries {
  message: AgentMessage;
  deliveries?: { id: number; recipient_agent: string; status: string }[];
}

const STATUS_TONE: Record<string, string> = {
  draft: 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]',
  active: 'bg-sky-500/10 text-sky-300',
  paused: 'bg-amber-500/10 text-amber-300',
  completed: 'bg-emerald-500/10 text-emerald-300',
  failed: 'bg-red-500/10 text-red-300',
  cancelled: 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]',
  pending: 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]',
  ready: 'bg-blue-500/10 text-blue-300',
  dispatched: 'bg-indigo-500/10 text-indigo-300',
  running: 'bg-amber-500/10 text-amber-300',
  stalled: 'bg-orange-500/10 text-orange-300',
};

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function formatTime(value?: number | null): string {
  if (!value) return 'never';
  return new Date(value * 1000).toLocaleString();
}

function progressPct(convoy: ConvoySummary): number {
  if (!convoy.total_subtasks) return 0;
  return Math.round((convoy.completed_subtasks / convoy.total_subtasks) * 100);
}

function Badge({ children, className = '' }: { children: ComponentChildren; className?: string }) {
  return (
    <span class={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] leading-4 ${className}`}>
      {children}
    </span>
  );
}

function statusTone(status: string): string {
  return STATUS_TONE[status] ?? 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]';
}

function DependencyGraph({
  subtasks,
  edges,
  onOpen,
}: {
  subtasks: ConvoySubtask[];
  edges: DependencyEdge[];
  onOpen: (task: ConvoySubtask) => void;
}) {
  const ordered = useMemo(
    () => [...subtasks].sort((a, b) => a.seq - b.seq || a.id - b.id),
    [subtasks],
  );
  const positions = useMemo(() => {
    const columns = ordered.length <= 2 ? 2 : 3;
    const xByColumn = columns === 2 ? [28, 72] : [16, 50, 84];
    return ordered.map((task, index) => ({
      task,
      x: xByColumn[index % columns],
      y: 28 + Math.floor(index / columns) * 124,
    }));
  }, [ordered]);
  const byId = useMemo(() => {
    const map = new Map<number, { x: number; y: number }>();
    positions.forEach((p) => map.set(p.task.id, { x: p.x, y: p.y }));
    return map;
  }, [positions]);
  const height = Math.max(280, 88 + Math.ceil(Math.max(1, positions.length) / 3) * 124);

  if (ordered.length === 0) {
    return <Empty title="No subtasks" description="Create or decompose work before the dependency graph can render." />;
  }

  return (
    <div
      class="relative overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-card)]"
      style={{ height: `${height}px` }}
      aria-label="Convoy dependency graph"
    >
      <svg class="absolute inset-0 h-full w-full" viewBox={`0 0 100 ${height}`} preserveAspectRatio="none">
        <defs>
          <marker id="convoy-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 Z" fill="var(--color-text-muted)" opacity="0.65" />
          </marker>
        </defs>
        {edges.map((edge) => {
          const from = byId.get(edge.from_subtask_id);
          const to = byId.get(edge.to_subtask_id);
          if (!from || !to) return null;
          return (
            <line
              key={edge.id}
              x1={from.x}
              y1={from.y + 34}
              x2={to.x}
              y2={to.y + 34}
              stroke="var(--color-text-muted)"
              stroke-width="0.35"
              opacity="0.65"
              marker-end="url(#convoy-arrow)"
            />
          );
        })}
      </svg>
      {positions.map(({ task, x, y }) => (
        <button
          key={task.id}
          type="button"
          onClick={() => onOpen(task)}
          class="absolute w-[158px] rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] p-2 text-left shadow-sm hover:border-[var(--color-accent)] transition-colors"
          style={{ left: `${x}%`, top: `${y}px`, transform: 'translateX(-50%)' }}
        >
          <div class="flex items-center justify-between gap-2">
            <span class="truncate text-[12px] font-medium text-[var(--color-text)]">{task.title}</span>
            <Badge className={statusTone(task.status)}>{task.status}</Badge>
          </div>
          <div class="mt-1 truncate text-[11px] text-[var(--color-text-muted)]">
            {task.assigned_agent_name || task.assigned_agent_id || 'Unassigned'}
          </div>
          {task.remaining_dependencies > 0 && (
            <div class="mt-1 text-[11px] text-amber-300">
              {task.remaining_dependencies} blocked
            </div>
          )}
        </button>
      ))}
    </div>
  );
}

function MailboxPane({ convoyId }: { convoyId: number }) {
  const { data, loading, error } = useFetch<MessageWithDeliveries[]>(`/api/mailbox/convoy/${convoyId}`, 15_000);
  const messages = data ?? [];
  if (loading && !data) return <div class="flex items-center justify-center py-10"><Spinner size={18} /></div>;
  if (error) return <Empty title="Failed to load mailbox" description={error} />;
  if (!messages.length) return <Empty title="No convoy mail" description="Mailbox events for this convoy will appear here." />;
  return (
    <div class="grid gap-3">
      {messages.map((item) => (
        <div key={item.message.id} class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="truncate text-[13px] font-medium text-[var(--color-text)]">
                {item.message.subject || item.message.message_type || 'Message'}
              </div>
              <div class="mt-1 text-[11px] text-[var(--color-text-muted)]">
                From {item.message.from_agent} · {formatTime(item.message.created_at)}
              </div>
            </div>
            <Badge className="border-[var(--color-border)] text-[var(--color-text-muted)]">
              <Mail size={11} class="mr-1" />
              {item.deliveries?.length ?? 0}
            </Badge>
          </div>
          <p class="mt-2 whitespace-pre-wrap text-[12px] leading-5 text-[var(--color-text-muted)]">{item.message.body}</p>
        </div>
      ))}
    </div>
  );
}

export function Convoy() {
  const convoysFetch = useFetch<ConvoySummary[]>('/api/convoy', 10_000);
  const teamsFetch = useFetch<TeamSession[]>('/api/team', 15_000);
  const convoys = convoysFetch.data ?? [];
  const teams = teamsFetch.data ?? [];
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<DetailTab>('graph');
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dispatchTeamId, setDispatchTeamId] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [newCreatedBy, setNewCreatedBy] = useState('dashboard');
  const [newBaseBranch, setNewBaseBranch] = useState('main');
  const [newSubtasks, setNewSubtasks] = useState('');

  const activeId = selectedId ?? convoys[0]?.id ?? null;
  const detailFetch = useFetch<ConvoyDetail>(activeId ? `/api/convoy/${activeId}` : null, 10_000);
  const detail = detailFetch.data;
  const convoy = detail?.convoy ?? convoys.find((c) => c.id === activeId) ?? null;
  const subtasks = detail?.subtasks ?? [];
  const edges = detail?.edges ?? [];
  const readyCount = subtasks.filter((task) => task.status === 'ready').length;

  async function refreshAll() {
    convoysFetch.refresh();
    detailFetch.refresh();
  }

  async function createConvoy(event: Event) {
    event.preventDefault();
    if (!newTitle.trim()) {
      pushToast({ tone: 'error', title: 'Title required' });
      return;
    }
    setBusy(true);
    try {
      const result = await apiPost<ConvoyDetail>('/api/convoy', {
        title: newTitle.trim(),
        description: newDescription.trim() || null,
        created_by: newCreatedBy.trim() || 'dashboard',
        base_branch: newBaseBranch.trim() || 'main',
        decomposition_mode: 'manual',
        subtasks: newSubtasks
          .split('\n')
          .map((line) => line.trim())
          .filter(Boolean)
          .map((title) => ({ title })),
      });
      setCreateOpen(false);
      setSelectedId(result.convoy.id);
      setNewTitle('');
      setNewDescription('');
      setNewCreatedBy('dashboard');
      setNewBaseBranch('main');
      setNewSubtasks('');
      pushToast({ tone: 'success', title: 'Convoy created' });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Create failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function setConvoyStatus(status: ConvoyStatus) {
    if (!convoy) return;
    setBusy(true);
    try {
      await apiPost(`/api/convoy/${convoy.id}/status`, { status });
      pushToast({ tone: 'success', title: `Convoy marked ${status}` });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Status update failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function dispatchSubtask(task: ConvoySubtask) {
    setBusy(true);
    try {
      const body = dispatchTeamId ? { team_id: Number(dispatchTeamId) } : {};
      await apiPost(`/api/convoy/${task.convoy_id}/subtask/${task.id}/dispatch`, body);
      pushToast({ tone: 'success', title: 'Subtask dispatched' });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Dispatch failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function completeSubtask(task: ConvoySubtask) {
    setBusy(true);
    try {
      await apiPost(`/api/convoy/${task.convoy_id}/subtask/${task.id}/complete`, {});
      pushToast({ tone: 'success', title: 'Subtask completed' });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Complete failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function failSubtask(task: ConvoySubtask) {
    setBusy(true);
    try {
      await apiPost(`/api/convoy/${task.convoy_id}/subtask/${task.id}/fail`, { error_message: 'Marked failed from dashboard' });
      pushToast({ tone: 'success', title: 'Subtask failed' });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Fail failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Convoy"
        subtitle={`${convoys.length} convoys · ${readyCount} ready subtasks`}
        actions={
          <>
            <button
              type="button"
              onClick={refreshAll}
              class="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)]"
            >
              <RefreshCw size={14} /> Refresh
            </button>
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              class="inline-flex items-center gap-1.5 rounded-md bg-[var(--color-accent)] px-2.5 py-1.5 text-[12px] font-medium text-white hover:bg-[var(--color-accent-hover)]"
            >
              <Plus size={14} /> New Convoy
            </button>
          </>
        }
      />

      <div class="grid min-h-0 flex-1 gap-4 overflow-hidden p-4 lg:grid-cols-[340px_1fr]">
        <aside class="min-h-0 overflow-y-auto rounded-md border border-[var(--color-border)] bg-[var(--color-card)]">
          <div class="sticky top-0 border-b border-[var(--color-border)] bg-[var(--color-card)] p-3 text-[12px] font-medium text-[var(--color-text)]">
            Active Convoys
          </div>
          {convoysFetch.error && <Empty title="Failed to load convoys" description={convoysFetch.error} />}
          {convoysFetch.loading && !convoysFetch.data && <div class="flex justify-center py-10"><Spinner size={18} /></div>}
          {!convoysFetch.loading && !convoysFetch.error && convoys.length === 0 && (
            <Empty title="No convoys" description="Create a convoy to see its dependency graph." />
          )}
          <div class="grid gap-2 p-3">
            {convoys.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  setSelectedId(item.id);
                  setActiveTab('graph');
                }}
                class={`rounded-md border p-3 text-left transition-colors ${
                  activeId === item.id
                    ? 'border-[var(--color-accent)] bg-[var(--color-elevated)]'
                    : 'border-[var(--color-border)] hover:border-[var(--color-accent)]'
                }`}
              >
                <div class="flex items-start justify-between gap-2">
                  <div class="min-w-0">
                    <div class="truncate text-[13px] font-medium text-[var(--color-text)]">{item.title}</div>
                    <div class="mt-1 text-[11px] text-[var(--color-text-muted)]">#{item.id} · {item.decomposition_mode || 'manual'}</div>
                  </div>
                  <Badge className={statusTone(item.status)}>{item.status}</Badge>
                </div>
                <div class="mt-3 h-1.5 rounded-full bg-[var(--color-elevated)]">
                  <div class="h-1.5 rounded-full bg-[var(--color-accent)]" style={{ width: `${progressPct(item)}%` }} />
                </div>
                <div class="mt-2 text-[11px] text-[var(--color-text-muted)]">
                  {item.completed_subtasks}/{item.total_subtasks} done · {item.failed_subtasks} failed
                </div>
              </button>
            ))}
          </div>
        </aside>

        <section class="min-h-0 overflow-y-auto">
          {!convoy && !detailFetch.loading && (
            <Empty title="Select a convoy" description="Convoy dependency, subtask, and mailbox detail will appear here." />
          )}
          {detailFetch.loading && !detail && <div class="flex justify-center py-16"><Spinner size={20} /></div>}
          {detailFetch.error && <Empty title="Failed to load convoy" description={detailFetch.error} />}
          {convoy && (
            <div class="grid gap-4">
              <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
                <div class="flex flex-wrap items-start justify-between gap-3">
                  <div class="min-w-0">
                    <div class="flex flex-wrap items-center gap-2">
                      <h2 class="truncate text-[18px] font-semibold text-[var(--color-text)]">{convoy.title}</h2>
                      <Badge className={statusTone(convoy.status)}>{convoy.status}</Badge>
                    </div>
                    {convoy.description && (
                      <p class="mt-2 max-w-3xl text-[13px] leading-5 text-[var(--color-text-muted)]">{convoy.description}</p>
                    )}
                    <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-[var(--color-text-muted)]">
                      <span>Base {convoy.base_branch || 'main'}</span>
                      <span>Merge {convoy.merge_strategy || 'squash'}</span>
                      <span>Updated {formatTime(convoy.updated_at)}</span>
                    </div>
                  </div>
                  <div class="flex flex-wrap items-center gap-2">
                    <select
                      value={dispatchTeamId}
                      onChange={(event) => setDispatchTeamId((event.target as HTMLSelectElement).value)}
                      class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[12px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                      aria-label="Dispatch team"
                    >
                      <option value="">Dispatch direct</option>
                      {teams.map((team) => (
                        <option key={team.id} value={team.id}>
                          Team #{team.id} · {team.team_name}
                        </option>
                      ))}
                    </select>
                    {convoy.status !== 'active' && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setConvoyStatus('active')}
                        class="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
                      >
                        <Play size={13} /> Activate
                      </button>
                    )}
                    {convoy.status === 'active' && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setConvoyStatus('paused')}
                        class="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
                      >
                        Pause
                      </button>
                    )}
                  </div>
                </div>
              </div>

              <div class="flex flex-wrap items-center gap-2">
                {(['graph', 'subtasks', 'mailbox'] as DetailTab[]).map((tab) => (
                  <button
                    key={tab}
                    type="button"
                    onClick={() => setActiveTab(tab)}
                    class={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-[12px] ${
                      activeTab === tab
                        ? 'border-[var(--color-accent)] bg-[var(--color-elevated)] text-[var(--color-text)]'
                        : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)]'
                    }`}
                  >
                    {tab === 'graph' && <GitBranch size={13} />}
                    {tab === 'subtasks' && <CheckCircle2 size={13} />}
                    {tab === 'mailbox' && <Mail size={13} />}
                    {tab === 'graph' ? 'Dependency Graph' : tab === 'subtasks' ? `Subtasks (${subtasks.length})` : 'Mailbox'}
                  </button>
                ))}
              </div>

              {activeTab === 'graph' && (
                <DependencyGraph subtasks={subtasks} edges={edges} onOpen={() => setActiveTab('subtasks')} />
              )}

              {activeTab === 'subtasks' && (
                <div class="grid gap-3">
                  {subtasks.length === 0 && <Empty title="No subtasks" description="Add subtasks through the orchestration API or create a convoy with initial lines." />}
                  {subtasks.map((task) => (
                    <div key={task.id} class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3">
                      <div class="flex flex-wrap items-start justify-between gap-3">
                        <div class="min-w-0">
                          <div class="flex flex-wrap items-center gap-2">
                            <span class="text-[13px] font-medium text-[var(--color-text)]">{task.title}</span>
                            <Badge className={statusTone(task.status)}>{task.status}</Badge>
                          </div>
                          {task.description && (
                            <p class="mt-2 text-[12px] leading-5 text-[var(--color-text-muted)]">{task.description}</p>
                          )}
                          <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-[var(--color-text-muted)]">
                            <span>Agent {task.assigned_agent_name || task.assigned_agent_id || 'unassigned'}</span>
                            <span>Dependencies {task.remaining_dependencies}</span>
                            <span>Updated {formatTime(task.updated_at)}</span>
                          </div>
                          {task.error_message && <div class="mt-2 text-[12px] text-red-300">{task.error_message}</div>}
                        </div>
                        <div class="flex flex-wrap items-center gap-2">
                          {task.status === 'ready' && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => dispatchSubtask(task)}
                              class="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] px-2 py-1 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
                            >
                              <Send size={13} /> Dispatch
                            </button>
                          )}
                          {(['dispatched', 'running', 'stalled'] as SubtaskStatus[]).includes(task.status) && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => completeSubtask(task)}
                              class="inline-flex items-center gap-1 rounded-md border border-emerald-500/30 px-2 py-1 text-[12px] text-emerald-300 hover:border-emerald-400 disabled:opacity-60"
                            >
                              <CheckCircle2 size={13} /> Done
                            </button>
                          )}
                          {(['pending', 'ready', 'dispatched', 'running', 'stalled'] as SubtaskStatus[]).includes(task.status) && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => failSubtask(task)}
                              class="inline-flex items-center gap-1 rounded-md border border-red-500/30 px-2 py-1 text-[12px] text-red-300 hover:border-red-400 disabled:opacity-60"
                            >
                              <XCircle size={13} /> Fail
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {activeTab === 'mailbox' && <MailboxPane convoyId={convoy.id} />}
            </div>
          )}
        </section>
      </div>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="New Convoy">
        <form class="grid gap-3" onSubmit={createConvoy}>
          <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
            Title
            <input
              value={newTitle}
              onInput={(event) => setNewTitle((event.target as HTMLInputElement).value)}
              class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              placeholder="Dashboard DAG slice"
            />
          </label>
          <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
            Description
            <textarea
              value={newDescription}
              onInput={(event) => setNewDescription((event.target as HTMLTextAreaElement).value)}
              class="min-h-[72px] rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              placeholder="What this convoy coordinates"
            />
          </label>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Created by
              <input
                value={newCreatedBy}
                onInput={(event) => setNewCreatedBy((event.target as HTMLInputElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              />
            </label>
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Base branch
              <input
                value={newBaseBranch}
                onInput={(event) => setNewBaseBranch((event.target as HTMLInputElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              />
            </label>
          </div>
          <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
            Initial subtasks
            <textarea
              value={newSubtasks}
              onInput={(event) => setNewSubtasks((event.target as HTMLTextAreaElement).value)}
              class="min-h-[96px] rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              placeholder="One subtask title per line"
            />
          </label>
          <div class="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => setCreateOpen(false)}
              class="rounded border border-[var(--color-border)] px-3 py-2 text-[12px] text-[var(--color-text)]"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              class="rounded bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white disabled:opacity-60"
            >
              Create
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
