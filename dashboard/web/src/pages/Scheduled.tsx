import { useState } from 'preact/hooks';
import { TopBar } from '@/components/TopBar';
import { ScheduledRow, type ScheduledTask } from '@/components/ScheduledRow';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';
import { apiPatch, apiDelete } from '@/lib/api';
import { pushToast } from '@/lib/toasts';

interface ScheduledResponse { tasks: ScheduledTask[]; }

export function Scheduled() {
  const [personaId, setPersonaId] = useState('');
  const params = new URLSearchParams();
  if (personaId) params.set('persona_id', personaId);
  const { data, loading, error, refresh } = useFetch<ScheduledResponse>(
    `/api/scheduled${personaId ? '?' + params.toString() : ''}`,
    30_000
  );

  async function deleteTask(taskId: string) {
    if (!confirm(`Delete scheduled task ${taskId}?`)) return;
    try {
      await apiDelete(`/api/scheduled/${taskId}`);
      pushToast({ tone: 'success', title: 'Task deleted' });
      refresh();
    } catch (err: any) {
      pushToast({ tone: 'error', title: 'Delete failed', description: err?.message || String(err) });
    }
  }

  async function toggleTask(taskId: string, enabled: boolean) {
    try {
      await apiPatch(`/api/scheduled/${taskId}`, { enabled });
      refresh();
    } catch (err: any) {
      pushToast({ tone: 'error', title: 'Toggle failed', description: err?.message || String(err) });
    }
  }

  const tasks = data?.tasks ?? [];

  return (
    <div class="flex flex-col h-full">
      <TopBar
        title="Scheduled"
        subtitle={`${tasks.length} task${tasks.length === 1 ? '' : 's'}`}
        actions={
          <input
            type="text"
            value={personaId}
            onInput={(e) => setPersonaId((e.target as HTMLInputElement).value)}
            placeholder="filter by persona..."
            class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2 py-1 text-[12px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
          />
        }
      />
      <div class="flex-1 overflow-y-auto">
        {loading && !data && <div class="flex items-center justify-center h-full"><Spinner /></div>}
        {error && <Empty title="Failed to load" description={error} />}
        {!loading && !error && tasks.length === 0 && (
          <Empty title="No scheduled tasks" description="Cron-driven prompts will surface here." />
        )}
        {tasks.map((t) => (
          <ScheduledRow key={t.taskId} task={t} onDelete={deleteTask} onToggle={toggleTask} />
        ))}
      </div>
    </div>
  );
}
