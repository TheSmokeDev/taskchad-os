import { Trash2 } from 'lucide-preact';
import { describeCron } from '@/lib/cron';

export interface ScheduledTask {
  taskId: string;
  personaId: string;
  cron: string;
  prompt: string;
  enabled: boolean;
  lastRunAt?: number;
}

interface ScheduledRowProps {
  task: ScheduledTask;
  onDelete: (taskId: string) => void;
  onToggle: (taskId: string, enabled: boolean) => void;
}

export function ScheduledRow({ task, onDelete, onToggle }: ScheduledRowProps) {
  const cronText = describeCron(task.cron);
  return (
    <div class="flex items-center gap-3 px-4 py-3 border-b border-[var(--color-border)] hover:bg-[var(--color-elevated)] transition-colors">
      <input
        type="checkbox"
        checked={task.enabled}
        onChange={(e) => onToggle(task.taskId, (e.target as HTMLInputElement).checked)}
        class="w-4 h-4 cursor-pointer"
        aria-label={`Toggle ${task.taskId}`}
      />
      <div class="flex-1 min-w-0">
        <div class="text-[13px] text-[var(--color-text)] truncate">{task.prompt}</div>
        <div class="text-[11px] text-[var(--color-text-muted)] mt-0.5">
          <span class="text-[var(--color-text-faint)]">{task.personaId}</span>
          {' · '}
          {cronText.ok ? cronText.text : <span class="text-[var(--color-status-failed)]">{cronText.text}</span>}
          {task.lastRunAt && ` · last run ${new Date(task.lastRunAt * 1000).toLocaleString()}`}
        </div>
      </div>
      <button
        type="button"
        onClick={() => onDelete(task.taskId)}
        class="p-1.5 rounded text-[var(--color-text-muted)] hover:text-[var(--color-status-failed)] hover:bg-[var(--color-elevated)] transition-colors"
        aria-label="Delete task"
      >
        <Trash2 size={12} />
      </button>
    </div>
  );
}
