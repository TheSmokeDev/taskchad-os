import { useState } from 'preact/hooks';
import { Plus } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { AgentCard, type Agent } from '@/components/AgentCard';
import { AgentCreateWizard } from '@/components/AgentCreateWizard';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';
import { apiPatch, apiPost } from '@/lib/api';
import { pushToast } from '@/lib/toasts';

interface AgentsResponse { agents: Agent[]; }
interface ModelResponse { model?: string; }

export function Agents() {
  const { data, loading, error, refresh } = useFetch<AgentsResponse>('/api/agents', 30_000);
  const globalModel = useFetch<ModelResponse>('/api/agents/model');
  const [wizardOpen, setWizardOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);

  const agents = data?.agents ?? [];
  const live = agents.filter((a) => a.running).length;

  async function setAllModels(model: string) {
    setBulkBusy(true);
    try {
      const res = await apiPatch<{ ok: boolean; updated?: string[]; restartRequired?: string[] }>('/api/agents/model', { model });
      const restartCount = res.restartRequired?.length || 0;
      if (restartCount > 0) {
        pushToast({
          tone: 'warn',
          title: `${restartCount} agent${restartCount === 1 ? '' : 's'} need restart`,
          description: 'YAML updated, but running processes still use the old model.',
          durationMs: 0,
          action: {
            label: 'Restart all',
            run: async () => {
              await Promise.all((res.restartRequired ?? []).map((id) =>
                apiPost(`/api/agents/${id}/restart`).catch(() => null)
              ));
              pushToast({ tone: 'success', title: 'Restarting agents' });
              setTimeout(refresh, 3000);
            },
          },
        });
      } else {
        pushToast({ tone: 'success', title: 'Global model set', description: model });
      }
      refresh();
    } catch (err: any) {
      pushToast({ tone: 'error', title: 'Bulk model change failed', description: err?.message || String(err) });
    } finally {
      setBulkBusy(false);
    }
  }

  return (
    <div class="flex flex-col h-full">
      <TopBar
        title="Agents"
        subtitle={`${live} live · ${agents.length} total`}
        actions={
          <>
            <select
              value={globalModel.data?.model ?? ''}
              onChange={(e) => setAllModels((e.target as HTMLSelectElement).value)}
              disabled={bulkBusy}
              class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2 py-1 text-[12px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)] disabled:opacity-40"
            >
              <option value="">Global model...</option>
              <option value="claude-opus-4-7">Opus 4.7</option>
              <option value="claude-sonnet-4-6">Sonnet 4.6</option>
              <option value="claude-haiku-4-5">Haiku 4.5</option>
            </select>
            <button
              type="button"
              onClick={() => setWizardOpen(true)}
              class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] transition-colors"
            >
              <Plus size={14} /> New Agent
            </button>
          </>
        }
      />

      {error && <Empty title="Failed to load agents" description={error} />}
      {loading && !data && <div class="flex items-center justify-center h-full"><Spinner size={20} /></div>}
      {!loading && !error && agents.length === 0 && (
        <Empty title="No agents configured" description="Click New Agent to create your first one." />
      )}

      {agents.length > 0 && (
        <div class="flex-1 overflow-y-auto p-6">
          <div class="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))' }}>
            {agents.map((a) => (
              <AgentCard key={a.id} agent={a} onChange={refresh} />
            ))}
          </div>
        </div>
      )}

      <AgentCreateWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        onCreated={refresh}
      />
    </div>
  );
}
