import { useMemo, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import {
  AlertTriangle,
  CheckCircle2,
  Play,
  Plus,
  RefreshCw,
  Send,
  UserRound,
  XCircle,
} from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { Modal } from '@/components/Modal';
import { useFetch } from '@/lib/useFetch';
import { apiPatch, apiPost } from '@/lib/api';
import { pushToast } from '@/lib/toasts';

type WorkStatus =
  | 'pending'
  | 'ready'
  | 'dispatched'
  | 'running'
  | 'stalled'
  | 'completed'
  | 'failed'
  | 'cancelled';

interface WorkColumn {
  id: WorkStatus;
  label: string;
}

interface WorkTask {
  id: number;
  task_id: number;
  convoy_id: number;
  convoy_title?: string | null;
  title: string;
  description?: string | null;
  status: WorkStatus;
  assigned_agent_id?: string | null;
  assigned_agent_name?: string | null;
  paperclip_issue_id?: string | null;
  remaining_dependencies: number;
  priority: string;
  tags: string[];
  target_session?: string | null;
  error_message?: string | null;
  dispatched_at?: number | null;
  started_at?: number | null;
  completed_at?: number | null;
  updated_at?: number | null;
}

interface WorkTasksResponse {
  tasks: WorkTask[];
  columns: WorkColumn[];
  summary: Record<string, number>;
}

interface WorkTaskMutationResponse {
  task: WorkTask;
}

const DEFAULT_COLUMNS: WorkColumn[] = [
  { id: 'pending', label: 'Pending' },
  { id: 'ready', label: 'Ready' },
  { id: 'dispatched', label: 'Dispatched' },
  { id: 'running', label: 'Running' },
  { id: 'stalled', label: 'Stalled' },
  { id: 'completed', label: 'Completed' },
  { id: 'failed', label: 'Failed' },
  { id: 'cancelled', label: 'Cancelled' },
];

const STATUS_IDS = new Set(DEFAULT_COLUMNS.map((c) => c.id));

const STATUS_TONE: Record<WorkStatus, string> = {
  pending: 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]',
  ready: 'bg-emerald-500/10 text-emerald-300',
  dispatched: 'bg-sky-500/10 text-sky-300',
  running: 'bg-amber-500/10 text-amber-300',
  stalled: 'bg-orange-500/10 text-orange-300',
  completed: 'bg-emerald-500/10 text-emerald-300',
  failed: 'bg-red-500/10 text-red-300',
  cancelled: 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]',
};

const PRIORITY_TONE: Record<string, string> = {
  low: 'border-[var(--color-border)] text-[var(--color-text-muted)]',
  medium: 'border-sky-500/30 text-sky-300',
  high: 'border-amber-500/40 text-amber-300',
  urgent: 'border-red-500/40 text-red-300',
};

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function isWorkStatus(value: string): value is WorkStatus {
  return STATUS_IDS.has(value as WorkStatus);
}

function formatTime(value?: number | null): string {
  if (!value) return 'never';
  return new Date(value * 1000).toLocaleString();
}

function Badge({ children, className = '' }: { children: ComponentChildren; className?: string }) {
  return (
    <span class={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] leading-4 ${className}`}>
      {children}
    </span>
  );
}

function WorkCard({
  task,
  onOpen,
  onDispatch,
  busy,
}: {
  task: WorkTask;
  onOpen: (task: WorkTask) => void;
  onDispatch: (task: WorkTask) => void;
  busy: boolean;
}) {
  const assignee = task.assigned_agent_name || task.assigned_agent_id || 'Unassigned';
  const priorityTone = PRIORITY_TONE[task.priority] ?? PRIORITY_TONE.medium;
  return (
    <button
      type="button"
      onClick={() => onOpen(task)}
      class="w-full text-left rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3 hover:border-[var(--color-accent)] transition-colors"
    >
      <div class="flex items-start justify-between gap-2">
        <div class="min-w-0">
          <div class="text-[13px] font-medium text-[var(--color-text)] break-words">{task.title}</div>
          {task.convoy_title && (
            <div class="mt-1 text-[11px] text-[var(--color-text-muted)] truncate">
              Convoy #{task.convoy_id} · {task.convoy_title}
            </div>
          )}
        </div>
        <Badge className={priorityTone}>{task.priority}</Badge>
      </div>
      {task.description && (
        <div class="mt-2 line-clamp-2 text-[12px] leading-5 text-[var(--color-text-muted)]">
          {task.description}
        </div>
      )}
      <div class="mt-3 flex flex-wrap items-center gap-1.5">
        <Badge className="border-[var(--color-border)] text-[var(--color-text-muted)]">
          <UserRound size={11} class="mr-1" />
          {assignee}
        </Badge>
        {task.tags.map((tag) => (
          <Badge key={tag} className="border-[var(--color-border)] text-[var(--color-text-muted)]">
            {tag}
          </Badge>
        ))}
      </div>
      <div class="mt-3 flex items-center justify-between gap-2">
        <span class="text-[11px] text-[var(--color-text-muted)]">Updated {formatTime(task.updated_at)}</span>
        {task.status === 'ready' && (
          <button
            type="button"
            disabled={busy}
            onClick={(event) => {
              event.stopPropagation();
              onDispatch(task);
            }}
            class="inline-flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-1 text-[11px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
          >
            <Send size={12} />
            Dispatch
          </button>
        )}
      </div>
    </button>
  );
}

export function WorkQueue() {
  const { data, loading, error, refresh } = useFetch<WorkTasksResponse>('/api/work/tasks', 15_000);
  const [statusFilter, setStatusFilter] = useState<WorkStatus | 'all'>('all');
  const [newOpen, setNewOpen] = useState(false);
  const [activeTask, setActiveTask] = useState<WorkTask | null>(null);
  const [busyTaskId, setBusyTaskId] = useState<number | null>(null);
  const [newTitle, setNewTitle] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [newAssigneeId, setNewAssigneeId] = useState('');
  const [newAssigneeName, setNewAssigneeName] = useState('');
  const [newPriority, setNewPriority] = useState('medium');
  const [newTags, setNewTags] = useState('');
  const [assignAgentId, setAssignAgentId] = useState('');
  const [assignAgentName, setAssignAgentName] = useState('');
  const [failureMessage, setFailureMessage] = useState('');

  const tasks = data?.tasks ?? [];
  const columns = useMemo(() => {
    const apiColumns = data?.columns?.filter((c) => isWorkStatus(c.id)) ?? [];
    return apiColumns.length ? apiColumns : DEFAULT_COLUMNS;
  }, [data?.columns]);
  const visibleTasks = useMemo(
    () => tasks.filter((task) => statusFilter === 'all' || task.status === statusFilter),
    [statusFilter, tasks],
  );
  const summary = data?.summary ?? { total: tasks.length };

  function openTask(task: WorkTask) {
    setActiveTask(task);
    setAssignAgentId(task.assigned_agent_id ?? '');
    setAssignAgentName(task.assigned_agent_name ?? '');
    setFailureMessage(task.error_message ?? '');
  }

  async function createTask(event: Event) {
    event.preventDefault();
    if (!newTitle.trim()) {
      pushToast({ tone: 'error', title: 'Title required' });
      return;
    }
    setBusyTaskId(-1);
    try {
      await apiPost<WorkTaskMutationResponse>('/api/work/tasks', {
        title: newTitle.trim(),
        description: newDescription.trim() || null,
        assigned_agent_id: newAssigneeId.trim() || null,
        assigned_agent_name: newAssigneeName.trim() || null,
        priority: newPriority,
        tags: newTags.split(',').map((tag) => tag.trim()).filter(Boolean),
      });
      setNewOpen(false);
      setNewTitle('');
      setNewDescription('');
      setNewAssigneeId('');
      setNewAssigneeName('');
      setNewPriority('medium');
      setNewTags('');
      pushToast({ tone: 'success', title: 'Work created' });
      refresh();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Create failed', description: errorMessage(err) });
    } finally {
      setBusyTaskId(null);
    }
  }

  async function updateTask(task: WorkTask, body: Record<string, unknown>, successTitle: string) {
    setBusyTaskId(task.id);
    try {
      await apiPatch<WorkTaskMutationResponse>(`/api/work/tasks/${task.id}`, body);
      pushToast({ tone: 'success', title: successTitle });
      setActiveTask(null);
      refresh();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Update failed', description: errorMessage(err) });
    } finally {
      setBusyTaskId(null);
    }
  }

  async function dispatchTask(task: WorkTask) {
    setBusyTaskId(task.id);
    try {
      await apiPost<WorkTaskMutationResponse>(`/api/work/tasks/${task.id}/dispatch`, {});
      pushToast({ tone: 'success', title: 'Work dispatched' });
      setActiveTask(null);
      refresh();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Dispatch failed', description: errorMessage(err) });
    } finally {
      setBusyTaskId(null);
    }
  }

  const activeBusy = activeTask ? busyTaskId === activeTask.id : false;

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Work Queue"
        subtitle={`${summary.total ?? tasks.length} item${(summary.total ?? tasks.length) === 1 ? '' : 's'} · ${summary.ready ?? 0} ready · ${summary.running ?? 0} running`}
        actions={
          <>
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter((event.target as HTMLSelectElement).value as WorkStatus | 'all')}
              class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1 text-[12px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              aria-label="Filter work status"
            >
              <option value="all">All statuses</option>
              {columns.map((column) => (
                <option key={column.id} value={column.id}>{column.label}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={refresh}
              class="inline-flex items-center gap-1.5 rounded border border-[var(--color-border)] px-2.5 py-1 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)]"
            >
              <RefreshCw size={13} />
              Refresh
            </button>
            <button
              type="button"
              onClick={() => setNewOpen(true)}
              class="inline-flex items-center gap-1.5 rounded bg-[var(--color-accent)] px-2.5 py-1 text-[12px] font-medium text-black hover:opacity-90"
            >
              <Plus size={13} />
              New Work
            </button>
          </>
        }
      />

      <div class="flex-1 overflow-hidden">
        {loading && !data && <div class="flex h-full items-center justify-center"><Spinner /></div>}
        {error && <Empty title="Failed to load work" description={error} />}
        {!loading && !error && tasks.length === 0 && (
          <Empty title="No work queued" description="Create dashboard-owned work to dispatch through Homie orchestration." />
        )}
        {!loading && !error && tasks.length > 0 && (
          <div class="h-full overflow-x-auto overflow-y-hidden">
            <div class="flex h-full min-w-max gap-3 p-4">
              {columns.map((column) => {
                const columnTasks = visibleTasks.filter((task) => task.status === column.id);
                return (
                  <section
                    key={column.id}
                    class="flex h-full w-[280px] flex-shrink-0 flex-col rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)]/40"
                  >
                    <div class="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-2">
                      <div class="flex items-center gap-2">
                        <span class={`h-2 w-2 rounded-full ${STATUS_TONE[column.id]}`} />
                        <span class="text-[12px] font-medium text-[var(--color-text)]">{column.label}</span>
                      </div>
                      <span class="rounded bg-[var(--color-card)] px-1.5 py-0.5 text-[10px] text-[var(--color-text-muted)]">
                        {columnTasks.length}
                      </span>
                    </div>
                    <div class="flex-1 space-y-2 overflow-y-auto p-2">
                      {columnTasks.map((task) => (
                        <WorkCard
                          key={task.id}
                          task={task}
                          onOpen={openTask}
                          onDispatch={dispatchTask}
                          busy={busyTaskId === task.id}
                        />
                      ))}
                    </div>
                  </section>
                );
              })}
            </div>
          </div>
        )}
      </div>

      <Modal open={newOpen} onClose={() => setNewOpen(false)} title="New Work" width={520}>
        <form class="space-y-3" onSubmit={createTask}>
          <label class="block">
            <span class="mb-1 block text-[11px] text-[var(--color-text-muted)]">Title</span>
            <input
              value={newTitle}
              onInput={(event) => setNewTitle((event.target as HTMLInputElement).value)}
              class="w-full rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              maxLength={180}
              autoFocus
            />
          </label>
          <label class="block">
            <span class="mb-1 block text-[11px] text-[var(--color-text-muted)]">Description</span>
            <textarea
              value={newDescription}
              onInput={(event) => setNewDescription((event.target as HTMLTextAreaElement).value)}
              rows={4}
              class="w-full resize-none rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
            />
          </label>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label class="block">
              <span class="mb-1 block text-[11px] text-[var(--color-text-muted)]">Agent ID</span>
              <input
                value={newAssigneeId}
                onInput={(event) => setNewAssigneeId((event.target as HTMLInputElement).value)}
                class="w-full rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                placeholder="codex"
              />
            </label>
            <label class="block">
              <span class="mb-1 block text-[11px] text-[var(--color-text-muted)]">Agent Name</span>
              <input
                value={newAssigneeName}
                onInput={(event) => setNewAssigneeName((event.target as HTMLInputElement).value)}
                class="w-full rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                placeholder="Codex"
              />
            </label>
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label class="block">
              <span class="mb-1 block text-[11px] text-[var(--color-text-muted)]">Priority</span>
              <select
                value={newPriority}
                onChange={(event) => setNewPriority((event.target as HTMLSelectElement).value)}
                class="w-full rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              >
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="urgent">Urgent</option>
              </select>
            </label>
            <label class="block">
              <span class="mb-1 block text-[11px] text-[var(--color-text-muted)]">Tags</span>
              <input
                value={newTags}
                onInput={(event) => setNewTags((event.target as HTMLInputElement).value)}
                class="w-full rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                placeholder="dashboard, runtime"
              />
            </label>
          </div>
          <div class="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => setNewOpen(false)}
              class="rounded border border-[var(--color-border)] px-3 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)]"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busyTaskId === -1}
              class="inline-flex items-center gap-1.5 rounded bg-[var(--color-accent)] px-3 py-1.5 text-[12px] font-medium text-black disabled:opacity-60"
            >
              <Plus size={13} />
              Create
            </button>
          </div>
        </form>
      </Modal>

      <Modal
        open={activeTask !== null}
        onClose={() => setActiveTask(null)}
        title={activeTask ? `Work #${activeTask.id}` : 'Work'}
        width={640}
      >
        {activeTask && (
          <div class="space-y-4">
            <div>
              <div class="flex flex-wrap items-center gap-2">
                <Badge className={STATUS_TONE[activeTask.status]}>{activeTask.status}</Badge>
                <Badge className={PRIORITY_TONE[activeTask.priority] ?? PRIORITY_TONE.medium}>{activeTask.priority}</Badge>
                <Badge className="border-[var(--color-border)] text-[var(--color-text-muted)]">
                  Convoy #{activeTask.convoy_id}
                </Badge>
              </div>
              <h2 class="mt-3 text-[16px] font-semibold text-[var(--color-text)] break-words">{activeTask.title}</h2>
              {activeTask.description && (
                <p class="mt-2 whitespace-pre-wrap text-[13px] leading-5 text-[var(--color-text-muted)]">
                  {activeTask.description}
                </p>
              )}
            </div>

            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div class="rounded-md border border-[var(--color-border)] p-3">
                <div class="text-[11px] uppercase tracking-wide text-[var(--color-text-muted)]">Dispatch</div>
                <div class="mt-2 space-y-1 text-[12px] text-[var(--color-text-muted)]">
                  <div>Dependencies: {activeTask.remaining_dependencies}</div>
                  <div>Paperclip ref: {activeTask.paperclip_issue_id || 'none'}</div>
                  <div>Target session: {activeTask.target_session || 'none'}</div>
                </div>
              </div>
              <div class="rounded-md border border-[var(--color-border)] p-3">
                <div class="text-[11px] uppercase tracking-wide text-[var(--color-text-muted)]">Timing</div>
                <div class="mt-2 space-y-1 text-[12px] text-[var(--color-text-muted)]">
                  <div>Dispatched: {formatTime(activeTask.dispatched_at)}</div>
                  <div>Started: {formatTime(activeTask.started_at)}</div>
                  <div>Completed: {formatTime(activeTask.completed_at)}</div>
                </div>
              </div>
            </div>

            <div class="rounded-md border border-[var(--color-border)] p-3">
              <div class="mb-3 text-[12px] font-medium text-[var(--color-text)]">Assignment</div>
              <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <input
                  value={assignAgentId}
                  onInput={(event) => setAssignAgentId((event.target as HTMLInputElement).value)}
                  class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                  placeholder="agent id"
                />
                <input
                  value={assignAgentName}
                  onInput={(event) => setAssignAgentName((event.target as HTMLInputElement).value)}
                  class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                  placeholder="agent name"
                />
              </div>
              <button
                type="button"
                disabled={activeBusy}
                onClick={() => updateTask(activeTask, {
                  assigned_agent_id: assignAgentId.trim() || null,
                  assigned_agent_name: assignAgentName.trim() || null,
                }, 'Assignment updated')}
                class="mt-3 inline-flex items-center gap-1.5 rounded border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
              >
                <UserRound size={13} />
                Save Assignment
              </button>
            </div>

            <div class="rounded-md border border-[var(--color-border)] p-3">
              <div class="mb-3 text-[12px] font-medium text-[var(--color-text)]">Actions</div>
              <div class="flex flex-wrap gap-2">
                {activeTask.status === 'ready' && (
                  <button
                    type="button"
                    disabled={activeBusy}
                    onClick={() => dispatchTask(activeTask)}
                    class="inline-flex items-center gap-1.5 rounded border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
                  >
                    <Send size={13} />
                    Dispatch
                  </button>
                )}
                {(activeTask.status === 'dispatched' || activeTask.status === 'stalled') && (
                  <button
                    type="button"
                    disabled={activeBusy}
                    onClick={() => updateTask(activeTask, { status: 'running' }, 'Marked running')}
                    class="inline-flex items-center gap-1.5 rounded border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
                  >
                    <Play size={13} />
                    Running
                  </button>
                )}
                {(['dispatched', 'running', 'stalled'] as WorkStatus[]).includes(activeTask.status) && (
                  <button
                    type="button"
                    disabled={activeBusy}
                    onClick={() => updateTask(activeTask, { status: 'completed' }, 'Marked completed')}
                    class="inline-flex items-center gap-1.5 rounded border border-emerald-500/40 px-2.5 py-1.5 text-[12px] text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-60"
                  >
                    <CheckCircle2 size={13} />
                    Complete
                  </button>
                )}
                {(['pending', 'ready', 'dispatched', 'running', 'stalled'] as WorkStatus[]).includes(activeTask.status) && (
                  <button
                    type="button"
                    disabled={activeBusy}
                    onClick={() => updateTask(activeTask, { status: 'cancelled' }, 'Marked cancelled')}
                    class="inline-flex items-center gap-1.5 rounded border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
                  >
                    <XCircle size={13} />
                    Cancel
                  </button>
                )}
              </div>
              {(['dispatched', 'running', 'stalled'] as WorkStatus[]).includes(activeTask.status) && (
                <div class="mt-3 space-y-2">
                  <textarea
                    value={failureMessage}
                    onInput={(event) => setFailureMessage((event.target as HTMLTextAreaElement).value)}
                    rows={2}
                    class="w-full resize-none rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                    placeholder="Failure reason"
                  />
                  <button
                    type="button"
                    disabled={activeBusy}
                    onClick={() => updateTask(activeTask, {
                      status: 'failed',
                      error_message: failureMessage.trim() || 'Failed from dashboard',
                    }, 'Marked failed')}
                    class="inline-flex items-center gap-1.5 rounded border border-red-500/40 px-2.5 py-1.5 text-[12px] text-red-300 hover:bg-red-500/10 disabled:opacity-60"
                  >
                    <AlertTriangle size={13} />
                    Fail
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
