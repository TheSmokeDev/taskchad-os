import { RefreshCw } from 'lucide-preact';
import type { ComponentChildren } from 'preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';

type JsonRecord = Record<string, unknown>;

interface JarvisStatusResponse {
  status?: string;
  timestamp?: string;
  runtime?: JsonRecord;
  autonomy?: JsonRecord;
  memory?: JsonRecord;
  capabilities?: JsonRecord;
  channels?: JsonRecord;
  observability?: JsonRecord;
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown, fallback = 'unknown'): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function numberText(value: unknown, fallback = '0'): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : fallback;
}

function joined(value: unknown): string {
  return asArray(value).map((item) => text(item, '')).filter(Boolean).join(' -> ') || '-';
}

function toneClass(value: unknown): string {
  const normalized = text(value).toLowerCase();
  if (['ok', 'live', 'on', 'ready', 'connected'].includes(normalized)) {
    return 'border-[color-mix(in_srgb,var(--color-status-done)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-done)_14%,transparent)] text-[var(--color-status-done)]';
  }
  if (['degraded', 'partial', 'unknown', 'missing', 'mismatch'].includes(normalized)) {
    return 'border-[color-mix(in_srgb,var(--color-status-warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-warn)_14%,transparent)] text-[var(--color-status-warn)]';
  }
  return 'border-[color-mix(in_srgb,var(--color-status-failed)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-failed)_14%,transparent)] text-[var(--color-status-failed)]';
}

function StatusPill({ value }: { value: unknown }) {
  return (
    <span class={`inline-flex max-w-full items-center rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${toneClass(value)}`}>
      <span class="truncate">{text(value)}</span>
    </span>
  );
}

function Metric({ label, value, status }: { label: string; value: unknown; status?: unknown }) {
  return (
    <div class="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
      <div class="flex min-w-0 items-center justify-between gap-3">
        <div class="min-w-0 truncate text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{label}</div>
        {status !== undefined && <StatusPill value={status} />}
      </div>
      <div class="mt-4 truncate text-[20px] font-semibold leading-tight text-[var(--color-text)]">{text(value, '-')}</div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: unknown }) {
  return (
    <div class="min-w-0">
      <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{label}</div>
      <div class="mt-1 truncate text-[13px] font-medium text-[var(--color-text)]">{text(value, '-')}</div>
    </div>
  );
}

function Panel({ title, status, children }: { title: string; status?: unknown; children: ComponentChildren }) {
  return (
    <section class="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
      <div class="mb-4 flex min-w-0 items-center justify-between gap-3">
        <h2 class="truncate text-[13px] font-semibold text-[var(--color-text)]">{title}</h2>
        {status !== undefined && <StatusPill value={status} />}
      </div>
      {children}
    </section>
  );
}

function CodeValue({ value }: { value: unknown }) {
  return (
    <span class="block max-w-full truncate rounded bg-[var(--color-elevated)] px-2 py-1 font-mono text-[12px] text-[var(--color-text)]">
      {text(value, 'not available')}
    </span>
  );
}

export function Jarvis() {
  const { data, loading, error, refresh } = useFetch<JarvisStatusResponse>('/api/jarvis/status', 30_000);

  if (loading && !data) return <div class="flex h-full items-center justify-center"><Spinner /></div>;
  if (error) return <Empty title="Failed to load Jarvis" description={error} />;
  if (!data) return <Empty title="No Jarvis status" />;

  const runtime = asRecord(data.runtime);
  const autonomy = asRecord(data.autonomy);
  const memory = asRecord(data.memory);
  const capabilities = asRecord(data.capabilities);
  const channels = asRecord(data.channels);
  const telegram = asRecord(channels.telegram);
  const relay = asRecord(channels.mission_control_relay);
  const alignment = asRecord(telegram.metadata_alignment);
  const observability = asRecord(data.observability);
  const providers = Object.entries(asRecord(runtime.providers));
  const toolsets = asArray(capabilities.toolsets).map((item) => text(item, '')).filter(Boolean);
  const enabledCapabilities = asArray(capabilities.enabled).map(asRecord);

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Jarvis"
        subtitle={`${text(runtime.selected_lane)} · ${text(runtime.selected_model)} · ${text(data.timestamp, 'no timestamp')}`}
        actions={(
          <button
            type="button"
            onClick={refresh}
            class="inline-flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[12px] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)]"
          >
            <RefreshCw size={14} />
            <span>Refresh</span>
          </button>
        )}
      />

      <div class="flex-1 overflow-y-auto p-4 md:p-6">
        <div class="mx-auto max-w-6xl space-y-4">
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <Metric label="Lane" value={runtime.selected_lane} status={runtime.selected_lane} />
            <Metric label="Model" value={runtime.selected_model} />
            <Metric label="Autonomy" value={autonomy.autonomy_overall} status={autonomy.autonomy_overall} />
            <Metric label="Memory Docs" value={numberText(memory.doc_count)} status={memory.embedding_status} />
          </div>

          <div class="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
            <Panel title="Runtime" status={runtime.selected_lane}>
              <div class="grid gap-3 md:grid-cols-2">
                <Field label="Generic Provider" value={runtime.selected_generic_provider} />
                <Field label="Text Route" value={joined(runtime.generic_text_route)} />
                <Field label="Tool Route" value={joined(runtime.generic_tool_route)} />
                <Field label="Configured Models" value={Object.keys(asRecord(runtime.configured_models)).length} />
              </div>
              <div class="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {providers.map(([name, value]) => (
                  <div key={name} class="flex min-w-0 items-center justify-between gap-2 rounded bg-[var(--color-elevated)] px-3 py-2">
                    <span class="truncate text-[12px] text-[var(--color-text-muted)]">{name}</span>
                    <StatusPill value={value} />
                  </div>
                ))}
              </div>
            </Panel>

            <Panel title="Channels" status={telegram.connected ? 'connected' : telegram.status}>
              <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
                <Field label="Telegram" value={telegram.connected ? 'connected' : telegram.status} />
                <Field label="Telegram Sessions" value={numberText(telegram.sessions_active)} />
                <Field label="Health Port" value={relay.health_check_port} />
                <Field label="Orchestration Port" value={relay.orchestration_api_port} />
              </div>
              <div class="mt-4 grid gap-2">
                <div class="flex items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
                  <span class="text-[12px] text-[var(--color-text-muted)]">Runtime metadata</span>
                  <StatusPill value={alignment.runtime_providers_populated ? 'live' : 'missing'} />
                </div>
                <div class="flex items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
                  <span class="text-[12px] text-[var(--color-text-muted)]">Memory parity</span>
                  <StatusPill value={alignment.memory_doc_count_matches_cli ? 'live' : 'mismatch'} />
                </div>
              </div>
            </Panel>
          </div>

          <div class="grid gap-4 xl:grid-cols-2">
            <Panel title="Autonomy" status={autonomy.autonomous_loop_overall}>
              <div class="grid gap-3 md:grid-cols-2">
                <Field label="Cognitive Loop" value={autonomy.cognitive_loop_overall} />
                <Field label="Source Wiring" value={autonomy.source_wiring_overall} />
                <Field label="Autonomy Gate" value={autonomy.autonomy_overall} />
                <Field label="Autonomous Loop" value={autonomy.autonomous_loop_overall} />
              </div>
            </Panel>

            <Panel title="Observability" status={observability.lookup_status}>
              <div class="grid gap-3">
                <div>
                  <div class="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Langfuse Trace</div>
                  <CodeValue value={observability.langfuse_trace_id} />
                </div>
                <div>
                  <div class="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Sentry Event</div>
                  <CodeValue value={observability.sentry_event_id} />
                </div>
                <div>
                  <div class="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Self Amendment</div>
                  <CodeValue value={observability.self_amendment_proposal_id} />
                </div>
              </div>
            </Panel>
          </div>

          <Panel title="Capabilities" status={`${numberText(capabilities.enabled_count)} / ${numberText(capabilities.total_count)}`}>
            <div class="mb-3 flex flex-wrap gap-2">
              {toolsets.map((name) => (
                <span key={name} class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1 text-[12px] text-[var(--color-text-muted)]">
                  {name}
                </span>
              ))}
            </div>
            <div class="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {enabledCapabilities.slice(0, 18).map((item) => (
                <div key={text(item.id)} class="min-w-0 rounded bg-[var(--color-elevated)] px-3 py-2">
                  <div class="truncate text-[12px] font-medium text-[var(--color-text)]">{text(item.display_name, text(item.id))}</div>
                  <div class="mt-0.5 truncate text-[11px] text-[var(--color-text-faint)]">{text(item.source)}</div>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
