import { useState, useEffect } from 'preact/hooks';
import { Power, Copy, Check } from 'lucide-preact';
import { Modal } from './Modal';
import { useFetch } from '@/lib/useFetch';
import { useDebouncedValue } from '@/lib/useDebounce';
import { apiPost } from '@/lib/api';

interface CreateAgentWizardProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}

interface Template { id: string; name: string; description: string; }

/**
 * Three-step wizard: basics → bot token → activate.
 *
 * Contract surface (Phase 3 canonical, NOT donor-shaped):
 *   - validate-id: POST body `{persona_id}` → `{valid, reason}`
 *   - validate-token: POST body `{bot_token}` → `{valid, display_name, username, error}`
 *   - create: POST `/api/agents` body `{persona_id, display_name, bot_token_env, model}`
 *             → `{persona_id, path, status}`
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
  const [model, setModel] = useState('claude-sonnet-4-6');
  const [template, setTemplate] = useState('');
  const [botToken, setBotToken] = useState('');
  const [createdId, setCreatedId] = useState<string | null>(null);
  const [createdSummary, setCreatedSummary] = useState<{ path?: string; status?: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const [activating, setActivating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const debouncedId = useDebouncedValue(id, 350);
  const debouncedToken = useDebouncedValue(botToken, 600);

  // Reset on close.
  function close() {
    setStep(1); setId(''); setName(''); setNameTouched(false); setDescription('');
    setModel('claude-sonnet-4-6'); setTemplate(''); setBotToken('');
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

  // Live token validation. Phase 3 contract: POST body `{bot_token}` → `{valid, display_name, username, error}`.
  const [tokenStatus, setTokenStatus] = useState<{ valid?: boolean; error?: string | null; username?: string } | null>(null);
  useEffect(() => {
    if (!debouncedToken || !debouncedToken.includes(':')) { setTokenStatus(null); return; }
    let cancelled = false;
    apiPost<{ valid: boolean; error: string | null; username: string; display_name: string }>('/api/agents/validate-token', { bot_token: debouncedToken })
      .then((r) => { if (!cancelled) setTokenStatus({ valid: r.valid, error: r.error, username: r.username }); })
      .catch((e) => { if (!cancelled) setTokenStatus({ valid: false, error: e?.message || String(e) }); });
    return () => { cancelled = true; };
  }, [debouncedToken]);

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
  const tokenValid = tokenStatus?.valid === true;
  const suggestedBotName = `Homie ${name || 'Agent'}`;
  const suggestedBotUsername = `homie_${id || 'agent'}_bot`;

  async function create() {
    setCreating(true); setError(null);
    try {
      // CANONICAL ROUTE — never /api/agents/create (donor alias dropped).
      // Phase 3 contract: body `{persona_id, display_name, bot_token_env, model}`,
      // response `{persona_id, path, status}`.
      // bot_token_env is the env-var NAME that holds the token, not the token value
      // itself — Python framework dereferences via os.environ at activation time.
      const botTokenEnv = `HOMIE_TG_TOKEN_${id.toUpperCase().replace(/-/g, '_')}`;
      const res = await apiPost<{ persona_id: string; path: string; status: string }>(
        '/api/agents',
        {
          persona_id: id,
          display_name: name,
          bot_token_env: botTokenEnv,
          model,
        },
      );
      setCreatedId(res.persona_id);
      setCreatedSummary({ path: res.path, status: res.status });
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
                Next: Bot token
              </button>
            </>
          )}
          {step === 2 && (
            <>
              <button type="button" onClick={() => setStep(1)} class="px-3 py-1.5 rounded text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors">Back</button>
              <button
                type="button"
                onClick={create}
                disabled={!tokenValid || creating}
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
              {n === 1 ? 'Basics' : n === 2 ? 'Bot token' : 'Activate'}
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
                <option value="claude-sonnet-4-6">Sonnet 4.6</option>
                <option value="claude-haiku-4-5">Haiku 4.5</option>
              </select>
            </Field>
            <Field label="Template">
              <select
                value={template}
                onChange={(e) => setTemplate((e.target as HTMLSelectElement).value)}
                class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              >
                <option value="">Blank</option>
                {templates.data?.templates?.map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </select>
            </Field>
          </div>
        </div>
      )}

      {step === 2 && (
        <div class="space-y-3">
          <div class="bg-[var(--color-elevated)] border border-[var(--color-border)] rounded p-3 text-[12px] leading-relaxed">
            <div class="font-semibold text-[var(--color-text)] mb-2">Create the bot in Telegram</div>
            <ol class="list-decimal list-inside space-y-1 text-[var(--color-text-muted)]">
              <li>Open <span class="font-mono text-[var(--color-accent)]">@BotFather</span> in Telegram</li>
              <li>Send <span class="font-mono text-[var(--color-accent)]">/newbot</span></li>
              <li>Name it: <CopyButton text={suggestedBotName} /></li>
              <li>Username: <CopyButton text={suggestedBotUsername} /></li>
              <li>Copy the token BotFather returns</li>
            </ol>
          </div>

          <Field label="Paste bot token">
            <input
              type="text"
              value={botToken}
              onInput={(e) => setBotToken((e.target as HTMLInputElement).value.trim())}
              placeholder="123456789:ABC..."
              class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] font-mono text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
            />
            {tokenStatus?.error && (
              <div class="text-[var(--color-status-failed)] text-[11px] mt-1">{tokenStatus.error}</div>
            )}
            {tokenStatus?.valid && tokenStatus.username && (
              <div class="text-[var(--color-status-done)] text-[11px] mt-1">✓ Verified: @{tokenStatus.username}</div>
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

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async (e) => {
        e.preventDefault();
        try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch {}
      }}
      class="inline-flex items-center gap-1 font-mono text-[var(--color-accent)] hover:text-[var(--color-accent-hover)] transition-colors"
    >
      <span>{text}</span>
      {copied ? <Check size={11} /> : <Copy size={11} />}
    </button>
  );
}
