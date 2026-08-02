import { useState, useEffect } from 'preact/hooks';
import { Power } from 'lucide-preact';
import { Modal } from './Modal';
import { useFetch } from '@/lib/useFetch';
import { useDebouncedValue } from '@/lib/useDebounce';
import { apiPost } from '@/lib/api';

interface CreateAgentWizardProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}

interface Template {
  id: string;
  name: string;
  description: string;
  default_role: string;
  default_model: string;
  domain: string;
}

interface CreationPreview {
  preview_hash: string;
  state_hash: string;
}

interface CreationResponse {
  persona_id: string;
  path: string;
  status: string;
  preview_hash: string;
  receipt: { transaction_id: string; outcome: string };
}

/**
 * Three-step wizard: blueprint → channel intent → activate.
 *
 * Contract surface (Phase 3 canonical, NOT donor-shaped):
 *   - validate-id: POST body `{persona_id}` → `{valid, reason}`
 *   - preview: POST `/api/agents/preview` with the complete blueprint intent
 *   - create: POST `/api/agents` with that same intent plus preview/state hashes
 *             → `{persona_id, path, status, preview_hash, receipt}`
 *   - activate: POST `/api/agents/{persona_id}/activate`
 *
 * Donor used `apiPost('/api/agents/create', ...)` and donor-shaped fields
 * (`id`, `name`, `bot_token`, `agentId`, `envKey`, `agentDir`). Both the URL
 * AND the field shape are INTENTIONALLY DROPPED — see INTENTIONAL_DEVIATIONS.md.
 * The donor-route-manifest test enforces no `/api/agents/create` literal anywhere.
 */
export function AgentCreateWizard({ open, onClose, onCreated }: CreateAgentWizardProps) {
  const [step, setStep] = useState(1);
  const [id, setId] = useState('');
  const [name, setName] = useState('');
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState('');
  const [model, setModel] = useState('claude-sonnet-4-7');
  const [template, setTemplate] = useState('general-specialist');
  const [channelId, setChannelId] = useState('');
  const [createdId, setCreatedId] = useState<string | null>(null);
  const [createdSummary, setCreatedSummary] = useState<{
    path?: string;
    status?: string;
    previewHash?: string;
    transactionId?: string;
  } | null>(null);
  const [creating, setCreating] = useState(false);
  const [activating, setActivating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const debouncedId = useDebouncedValue(id, 350);

  // Reset on close.
  function close() {
    setStep(1); setId(''); setName(''); setNameTouched(false); setDescription('');
    setModel('claude-sonnet-4-7'); setTemplate('general-specialist'); setChannelId('');
    setCreatedId(null); setCreatedSummary(null); setError(null);
    onClose();
  }

  // Live ID validation. Phase 3 contract: POST body `{persona_id}` → `{valid, reason}`.
  const [idCheck, setIdCheck] = useState<{ valid?: boolean; reason?: string | null } | null>(null);
  useEffect(() => {
    if (!debouncedId) { setIdCheck(null); return; }
    let cancelled = false;
    apiPost<{ valid: boolean; reason: string | null }>('/api/agents/validate-id', { persona_id: debouncedId })
      .then((r) => { if (!cancelled) setIdCheck({ valid: r.valid, reason: r.reason }); })
      .catch((e) => { if (!cancelled) setIdCheck({ valid: false, reason: e?.message || String(e) }); });
    return () => { cancelled = true; };
  }, [debouncedId]);

  // Templates list.
  const templates = useFetch<{ templates: Template[] }>('/api/agents/templates');

  // Auto name from id when user hasn't touched it.
  useEffect(() => {
    if (!nameTouched && id && !name) {
      const auto = id.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
      setName(auto);
    }
  }, [id, nameTouched]);

  const idValid = !!debouncedId && idCheck?.valid === true;
  const channelValid = !channelId || /^[0-9]+$/.test(channelId);
  const selectedTemplate = templates.data?.templates?.find((item) => item.id === template);

  async function create() {
    setCreating(true); setError(null);
    try {
      const intent = {
        persona_id: id,
        display_name: name,
        template,
        role: description,
        model,
        domain: selectedTemplate?.domain,
        channel_intent: channelId
          ? { kind: 'discord', channel_id: channelId, name: id }
          : undefined,
        operator_exec: false,
      };
      const preview = await apiPost<CreationPreview>('/api/agents/preview', intent);
      const res = await apiPost<CreationResponse>(
        '/api/agents',
        {
          ...intent,
          expected_preview_hash: preview.preview_hash,
          expected_state_hash: preview.state_hash,
        },
      );
      setCreatedId(res.persona_id);
      setCreatedSummary({
        path: res.path,
        status: res.status,
        previewHash: res.preview_hash,
        transactionId: res.receipt.transaction_id,
      });
      setStep(3);
    } catch (err: any) {
      setError(err?.message || String(err));
    } finally { setCreating(false); }
  }

  async function activate() {
    if (!createdId) return;
    setActivating(true); setError(null);
    try {
      const res = await apiPost<{ ok?: boolean; error?: string }>(`/api/agents/${createdId}/activate`);
      if (res.ok === false) throw new Error(res.error || 'Activation failed');
      onCreated();
      setTimeout(close, 800);
    } catch (err: any) {
      setError(err?.message || String(err));
    } finally { setActivating(false); }
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="New Agent"
      width={520}
      footer={
        <>
          {step === 1 && (
            <>
              <button type="button" onClick={close} class="px-3 py-1.5 rounded text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors">Cancel</button>
              <button
                type="button"
                onClick={() => { if (idValid && name && description) setStep(2); }}
                disabled={!idValid || !name || !description}
                class="ml-auto px-3 py-1.5 rounded text-[12px] font-medium bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Next: Channel
              </button>
            </>
          )}
          {step === 2 && (
            <>
              <button type="button" onClick={() => setStep(1)} class="px-3 py-1.5 rounded text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors">Back</button>
              <button
                type="button"
                onClick={create}
                disabled={!channelValid || creating}
                class="ml-auto px-3 py-1.5 rounded text-[12px] font-medium bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {creating ? 'Creating...' : 'Create Agent'}
              </button>
            </>
          )}
          {step === 3 && (
            <>
              <button type="button" onClick={close} class="px-3 py-1.5 rounded text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors">Done</button>
              <button
                type="button"
                onClick={activate}
                disabled={activating}
                class="ml-auto inline-flex items-center gap-1 px-3 py-1.5 rounded text-[12px] font-medium bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] transition-colors disabled:opacity-40"
              >
                <Power size={12} /> {activating ? 'Activating...' : 'Activate'}
              </button>
            </>
          )}
        </>
      }
    >
      <div class="flex items-center gap-2 mb-4 text-[10px] uppercase tracking-wider">
        {[1, 2, 3].map((n) => (
          <div key={n} class="flex items-center gap-2">
            <div
              class="w-5 h-5 rounded-full flex items-center justify-center font-semibold"
              style={{
                backgroundColor: step >= n ? 'var(--color-accent-soft)' : 'var(--color-elevated)',
                color: step >= n ? 'var(--color-accent)' : 'var(--color-text-faint)',
                fontSize: '10px',
              }}
            >
              {step > n ? '✓' : n}
            </div>
            <span class={step === n ? 'text-[var(--color-text)]' : 'text-[var(--color-text-faint)]'}>
              {n === 1 ? 'Blueprint' : n === 2 ? 'Channel' : 'Activate'}
            </span>
            {n < 3 && <span class="text-[var(--color-border)]">·</span>}
          </div>
        ))}
      </div>

      {step === 1 && (
        <div class="space-y-3">
          <Field label="Agent ID" hint="Lowercase letters, numbers, dash/underscore. 30 chars max.">
            <input
              type="text"
              value={id}
              onInput={(e) => setId((e.target as HTMLInputElement).value.toLowerCase().replace(/[^a-z0-9_-]/g, ''))}
              placeholder="research"
              autoFocus
              class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
            />
            {debouncedId && idCheck && idCheck.valid === false && (
              <div class="text-[var(--color-status-failed)] text-[11px] mt-1">{idCheck.reason}</div>
            )}
            {debouncedId && idCheck?.valid && (
              <div class="text-[var(--color-status-done)] text-[11px] mt-1">✓ Available</div>
            )}
          </Field>

          <Field label="Display name">
            <input
              type="text"
              value={name}
              onInput={(e) => { setNameTouched(true); setName((e.target as HTMLInputElement).value); }}
              placeholder="Research"
              class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
            />
          </Field>

          <Field label="Description" hint="What this agent is responsible for.">
            <textarea
              value={description}
              onInput={(e) => setDescription((e.target as HTMLTextAreaElement).value)}
              rows={3}
              placeholder="Deep web research, competitive intel, trend research"
              class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)] resize-none"
            />
          </Field>

          <div class="grid grid-cols-2 gap-3">
            <Field label="Model">
              <select
                value={model}
                onChange={(e) => setModel((e.target as HTMLSelectElement).value)}
                class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              >
                <option value="claude-opus-4-7">Opus 4.7</option>
                <option value="claude-sonnet-4-7">Sonnet 4.7</option>
                <option value="claude-sonnet-4-6">Sonnet 4.6</option>
                <option value="claude-haiku-4-5">Haiku 4.5</option>
              </select>
            </Field>
            <Field label="Template">
              <select
                value={template}
                onChange={(e) => {
                  const nextId = (e.target as HTMLSelectElement).value;
                  const next = templates.data?.templates?.find((item) => item.id === nextId);
                  setTemplate(nextId);
                  if (next) {
                    if (!nameTouched) setName(next.name);
                    if (!description) setDescription(next.default_role);
                    setModel(next.default_model);
                  }
                }}
                class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              >
                {templates.data?.templates?.map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </select>
            </Field>
          </div>
          {selectedTemplate?.description && (
            <div class="text-[10.5px] text-[var(--color-text-faint)]">
              {selectedTemplate.description}
            </div>
          )}
        </div>
      )}

      {step === 2 && (
        <div class="space-y-3">
          <div class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded p-3 text-[12px] leading-relaxed">
            <div class="font-semibold text-[var(--color-text)] mb-2">Optional Discord binding</div>
            <p class="text-[var(--color-text-muted)]">
              Add a Discord channel id to compile a disabled-by-default ingress binding.
              Enabling shared ingress remains a separate operator action.
            </p>
          </div>

          <Field label="Discord channel ID" hint="Digits only. Creation never enables shared ingress.">
            <input
              type="text"
              value={channelId}
              onInput={(e) => setChannelId((e.target as HTMLInputElement).value.trim())}
              placeholder="123456789012345678"
              class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] font-mono text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
            />
            {!channelValid && (
              <div class="text-[var(--color-status-failed)] text-[11px] mt-1">
                Discord channel ids contain digits only.
              </div>
            )}
          </Field>

          {error && <div class="text-[var(--color-status-failed)] text-[11px]">{error}</div>}
        </div>
      )}

      {step === 3 && createdId && (
        <div class="space-y-3 text-[12.5px]">
          <div class="text-[var(--color-status-done)] text-[14px] font-medium">✓ Agent created</div>
          <div class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded p-3 space-y-1.5 font-mono text-[11px]">
            <div><span class="text-[var(--color-text-faint)]">id:</span> {createdId}</div>
            {createdSummary?.path && <div><span class="text-[var(--color-text-faint)]">path:</span> {createdSummary.path}</div>}
            {createdSummary?.status && <div><span class="text-[var(--color-text-faint)]">status:</span> {createdSummary.status}</div>}
            {createdSummary?.previewHash && <div><span class="text-[var(--color-text-faint)]">preview:</span> {createdSummary.previewHash}</div>}
            {createdSummary?.transactionId && <div><span class="text-[var(--color-text-faint)]">transaction:</span> {createdSummary.transactionId}</div>}
          </div>
          {error && <div class="text-[var(--color-status-failed)] text-[11px]">{error}</div>}
        </div>
      )}
    </Modal>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: any }) {
  return (
    <div>
      <label class="block text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">{label}</label>
      {children}
      {hint && <div class="text-[10.5px] text-[var(--color-text-faint)] mt-1">{hint}</div>}
    </div>
  );
}
