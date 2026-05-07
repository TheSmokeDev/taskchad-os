import { useState } from 'preact/hooks';
import { Power, RotateCcw, Trash2 } from 'lucide-preact';
import { apiPost, apiDelete } from '@/lib/api';
import { pushToast } from '@/lib/toasts';
import { DEFAULT_PERSONA_ID_UI } from '@/lib/routes';

interface AgentActionsProps {
  agentId: string;
  running: boolean;
  onChange: () => void;
}

/** Standalone action button group — used in AgentDetail.tsx side panel. */
export function AgentActions({ agentId, running, onChange }: AgentActionsProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const isDefault = agentId === DEFAULT_PERSONA_ID_UI;

  async function run(action: 'restart' | 'stop' | 'start' | 'delete') {
    if (action === 'delete') {
      const ok = confirm(`Delete agent "${agentId}"? Permanent.`);
      if (!ok) return;
    }
    setBusy(action);
    try {
      if (action === 'restart') await apiPost(`/api/agents/${agentId}/restart`);
      if (action === 'stop') await apiPost(`/api/agents/${agentId}/deactivate`);
      if (action === 'start') await apiPost(`/api/agents/${agentId}/activate`);
      if (action === 'delete') {
        await apiDelete(`/api/agents/${agentId}/full?confirm=true&expected_persona_id=${encodeURIComponent(agentId)}`);
      }
      onChange();
    } catch (err: any) {
      pushToast({ tone: 'error', title: action + ' failed', description: err?.message || String(err) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div class="flex items-center gap-2">
      {running ? (
        <button
          type="button"
          onClick={() => run('stop')}
          disabled={busy !== null || isDefault}
          class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-[12px] bg-[var(--color-elevated)] text-[var(--color-text)] border border-[var(--color-border)] hover:border-[var(--color-border-strong)] transition-colors disabled:opacity-40"
        >
          <Power size={12} /> {busy === 'stop' ? 'Stopping...' : 'Stop'}
        </button>
      ) : (
        <button
          type="button"
          onClick={() => run('start')}
          disabled={busy !== null || isDefault}
          class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-[12px] bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] transition-colors disabled:opacity-40"
        >
          <Power size={12} /> {busy === 'start' ? 'Starting...' : 'Start'}
        </button>
      )}
      <button
        type="button"
        onClick={() => run('restart')}
        disabled={busy !== null || isDefault}
        class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-[12px] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] border border-[var(--color-border)] transition-colors disabled:opacity-40"
      >
        <RotateCcw size={12} class={busy === 'restart' ? 'animate-spin' : ''} />
        Restart
      </button>
      <button
        type="button"
        onClick={() => run('delete')}
        disabled={busy !== null || isDefault}
        class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-[12px] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-status-failed)] border border-[var(--color-border)] hover:border-[var(--color-status-failed)] transition-colors disabled:opacity-40"
      >
        <Trash2 size={12} /> Delete
      </button>
    </div>
  );
}
