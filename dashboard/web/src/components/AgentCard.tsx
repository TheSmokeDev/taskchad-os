import { useState } from 'preact/hooks';
import { Power, RotateCcw, Trash2, FileText } from 'lucide-preact';
import { Link } from 'wouter-preact';
import { apiPost, apiPatch, apiDelete, ApiError } from '@/lib/api';
import { formatCost } from '@/lib/format';
import { showCosts } from '@/lib/theme';
import { pushToast } from '@/lib/toasts';
import { LaneStatusPill } from './LaneStatusPill';
import { DEFAULT_PERSONA_ID_UI } from '@/lib/routes';

export interface Agent {
  id: string;
  name: string;
  description: string;
  model: string;
  running: boolean;
  todayTurns?: number;
  todayCost?: number;
  lane?: 'claude_native' | 'generic';
  planQuotaPct?: number;
}

interface AgentCardProps {
  agent: Agent;
  onChange: () => void;
  onOpen?: () => void;
}

export function AgentCard({ agent, onChange, onOpen }: AgentCardProps) {
  const [busy, setBusy] = useState<string | null>(null);

  async function run(action: 'restart' | 'stop' | 'start' | 'delete') {
    if (action === 'delete') {
      const ok = confirm(`Delete agent "${agent.id}"? This unloads the service, removes its config, deletes its bot token from .env, and removes log files.`);
      if (!ok) return;
    }
    setBusy(action);
    try {
      if (action === 'restart') await apiPost(`/api/agents/${agent.id}/restart`);
      if (action === 'stop') await apiPost(`/api/agents/${agent.id}/deactivate`);
      if (action === 'start') await apiPost(`/api/agents/${agent.id}/activate`);
      if (action === 'delete') {
        // Hard-delete requires confirmation + expected_persona_id query params
        // per WS3's full-delete contract.
        await apiDelete(`/api/agents/${agent.id}/full?confirm=true&expected_persona_id=${encodeURIComponent(agent.id)}`);
      }
      setTimeout(onChange, action === 'delete' ? 200 : 1500);
    } catch (err: any) {
      // Surface the BACKEND error detail when available — apiDelete/apiPost
      // throw ApiError with the parsed JSON response body. The body shape for
      // dashboard endpoints is { error, hint?, expected?, actual? }. Falling
      // back to err.message hides the actual reason (confirmation required,
      // expected_persona_id mismatch, default profile) behind the generic
      // "DELETE <path> failed: <status>" string built by the api helper.
      let description: string = err?.message || String(err);
      if (err instanceof ApiError && err.body && typeof err.body === 'object') {
        const body = err.body as Record<string, unknown>;
        if (typeof body.error === 'string') {
          let detail = body.error;
          if (typeof body.hint === 'string' && body.hint) {
            detail += ' — ' + body.hint;
          }
          if (typeof body.actual === 'string' && body.actual) {
            detail += ' (actual=' + body.actual + ')';
          }
          description = detail;
        }
      }
      pushToast({ tone: 'error', title: action + ' failed', description });
    } finally {
      setBusy(null);
    }
  }

  async function setModel(model: string) {
    setBusy('model');
    try {
      const res = await apiPatch<{ ok: boolean; restartRequired: boolean }>(`/api/agents/${agent.id}/model`, { model });
      if (res.restartRequired) {
        pushToast({
          tone: 'warn',
          title: agent.id + ' needs a restart',
          description: `Model is now ${model}, but the running process is still on the old one.`,
          durationMs: 0,
          action: {
            label: 'Restart now',
            run: async () => {
              await apiPost(`/api/agents/${agent.id}/restart`);
              pushToast({ tone: 'success', title: agent.id + ' restarting', description: 'Should be live again in a few seconds.' });
              setTimeout(onChange, 2500);
            },
          },
        });
      } else {
        pushToast({ tone: 'success', title: 'Model set to ' + model, description: 'Takes effect on the next message.' });
      }
      onChange();
    } catch (err: any) {
      pushToast({ tone: 'error', title: 'Model change failed', description: err?.message || String(err), durationMs: 6000 });
    } finally { setBusy(null); }
  }

  // The default persona is locked from destructive actions — its ID
  // displays as 'main' (Q4 lock; Hono translates to 'default' for python).
  const isDefault = agent.id === DEFAULT_PERSONA_ID_UI;

  return (
    <div
      class="bg-[var(--color-card)] border border-[var(--color-border)] rounded-lg p-4 hover:border-[var(--color-border-strong)] transition-colors"
      onClick={onOpen}
    >
      <div class="flex items-start gap-3 mb-3">
        <div class="w-9 h-9 rounded-full bg-[var(--color-elevated)] flex items-center justify-center text-[12px] font-medium text-[var(--color-text-muted)]">
          {(agent.name || agent.id).slice(0, 2).toUpperCase()}
        </div>
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-1.5 mb-0.5">
            <span class={`w-1.5 h-1.5 rounded-full ${agent.running ? 'bg-[var(--color-status-done)]' : 'bg-[var(--color-text-faint)]'}`} />
            <span class="text-[13px] font-medium text-[var(--color-text)] truncate">
              {agent.name || agent.id}
            </span>
          </div>
          <div class="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider">
            {agent.id}
          </div>
        </div>
      </div>

      {agent.description && (
        <div class="text-[12px] text-[var(--color-text-muted)] leading-snug mb-3 line-clamp-2 min-h-[2.4em]">
          {agent.description}
        </div>
      )}

      {/* Lane-aware metrics row — split, never summed across lanes. */}
      <div class="flex items-center gap-2 mb-3 flex-wrap" onClick={(e) => e.stopPropagation()}>
        <select
          value={agent.model}
          onChange={(e) => setModel((e.target as HTMLSelectElement).value)}
          disabled={busy === 'model'}
          class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2 py-1 text-[11px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
        >
          <option value="claude-opus-4-7">Opus 4.7</option>
          <option value="claude-sonnet-4-6">Sonnet 4.6</option>
          <option value="claude-haiku-4-5">Haiku 4.5</option>
        </select>
        <LaneStatusPill
          lane={agent.lane === 'generic' ? 'generic' : 'claude_native'}
          value={agent.lane === 'generic' ? (agent.todayCost ?? 0) : (agent.todayTurns ?? 0)}
          quotaPct={agent.lane === 'generic' ? undefined : agent.planQuotaPct}
        />
      </div>

      {/* Cost cell only when costs are visible AND lane is generic. */}
      {showCosts.value && agent.lane === 'generic' && (
        <div class="text-[10px] text-[var(--color-text-faint)] uppercase tracking-wider mb-3">
          Cost today: <span class="text-[var(--color-text-muted)] tabular-nums normal-case">{formatCost(agent.todayCost ?? 0)}</span>
        </div>
      )}

      <div class="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
        {agent.running ? (
          <button
            type="button"
            onClick={() => run('stop')}
            disabled={busy !== null || isDefault}
            class="flex-1 inline-flex items-center justify-center gap-1 px-2 py-1.5 rounded text-[11px] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-card)] border border-[var(--color-border)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            title={isDefault ? 'Default agent cannot be stopped from the dashboard' : 'Stop this agent'}
          >
            <Power size={11} /> {busy === 'stop' ? 'Stopping...' : 'Stop'}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => run('start')}
            disabled={busy !== null || isDefault}
            class="flex-1 inline-flex items-center justify-center gap-1 px-2 py-1.5 rounded text-[11px] bg-[var(--color-accent-soft)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white border border-[var(--color-accent-soft)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Power size={11} /> {busy === 'start' ? 'Starting...' : 'Start'}
          </button>
        )}
        <Link
          href={`/agents/${agent.id}/files`}
          class="inline-flex items-center justify-center px-2 py-1.5 rounded text-[11px] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] border border-[var(--color-border)] transition-colors"
          title="Edit persona + config"
        >
          <FileText size={11} />
        </Link>
        <button
          type="button"
          onClick={() => run('restart')}
          disabled={busy !== null || isDefault}
          class="inline-flex items-center justify-center px-2 py-1.5 rounded text-[11px] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] border border-[var(--color-border)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          title="Restart"
        >
          <RotateCcw size={11} class={busy === 'restart' ? 'animate-spin' : ''} />
        </button>
        <button
          type="button"
          onClick={() => run('delete')}
          disabled={busy !== null || isDefault}
          class="inline-flex items-center justify-center px-2 py-1.5 rounded text-[11px] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-status-failed)] border border-[var(--color-border)] hover:border-[var(--color-status-failed)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          title="Delete"
        >
          <Trash2 size={11} />
        </button>
      </div>
    </div>
  );
}
