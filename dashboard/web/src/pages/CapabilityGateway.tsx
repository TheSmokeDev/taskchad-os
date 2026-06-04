import { RefreshCw, ShieldCheck } from 'lucide-preact';
import type { ComponentChildren } from 'preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';

type JsonRecord = Record<string, unknown>;

interface CapabilityGatewayStatus {
  status?: string;
  timestamp?: string;
  runtime?: JsonRecord;
  capabilities?: {
    total_count?: number;
    enabled_count?: number;
    sources?: Record<string, number>;
    items?: Array<Record<string, unknown>>;
    error?: string;
  };
  toolsets?: Array<Record<string, unknown>>;
  integrations?: {
    total_count?: number;
    enabled_count?: number;
    items?: Array<Record<string, unknown>>;
    error?: string;
  };
  browserops?: JsonRecord;
  outbound_messaging?: JsonRecord;
  approval_policy?: JsonRecord;
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {};
}

function asArray<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? value as T[] : [];
}

function text(value: unknown, fallback = 'unknown'): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function count(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '0';
}

function toneClass(value: unknown): string {
  const normalized = text(value).toLowerCase();
  if (['ok', 'ready', 'connected', 'true', 'available', 'enabled'].includes(normalized)) {
    return 'border-[color-mix(in_srgb,var(--color-status-done)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-done)_14%,transparent)] text-[var(--color-status-done)]';
  }
  if (['policy_gated', 'attention', 'degraded', 'false', 'unknown', 'none_declared'].includes(normalized)) {
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
    <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
      <div class="flex min-w-0 items-center justify-between gap-3">
        <div class="min-w-0 truncate text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{label}</div>
        {status !== undefined && <StatusPill value={status} />}
      </div>
      <div class="mt-4 truncate text-[20px] font-semibold leading-tight text-[var(--color-text)]">{text(value, '-')}</div>
    </div>
  );
}

function Panel({ title, status, children }: { title: string; status?: unknown; children: ComponentChildren }) {
  return (
    <section class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
      <div class="mb-4 flex min-w-0 items-center justify-between gap-3">
        <h2 class="truncate text-[13px] font-semibold text-[var(--color-text)]">{title}</h2>
        {status !== undefined && <StatusPill value={status} />}
      </div>
      {children}
    </section>
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

function TinyRow({ label, value, status }: { label: string; value: unknown; status?: unknown }) {
  return (
    <div class="flex min-w-0 items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
      <span class="min-w-0 truncate text-[12px] text-[var(--color-text-muted)]">{label}</span>
      {status !== undefined ? <StatusPill value={status} /> : <span class="truncate text-[12px] text-[var(--color-text)]">{text(value, '-')}</span>}
    </div>
  );
}

export function CapabilityGateway() {
  const { data, loading, error, refresh } = useFetch<CapabilityGatewayStatus>('/api/capabilities/status', 30_000);

  if (loading && !data) return <div class="flex h-full items-center justify-center"><Spinner /></div>;
  if (error) return <Empty title="Failed to load Capability Gateway" description={error} />;
  if (!data) return <Empty title="No Capability Gateway status" />;

  const runtime = asRecord(data.runtime);
  const capabilities = data.capabilities ?? {};
  const integrations = data.integrations ?? {};
  const browserops = asRecord(data.browserops);
  const outbound = asRecord(data.outbound_messaging);
  const approval = asRecord(data.approval_policy);
  const toolsets = asArray<Record<string, unknown>>(data.toolsets);
  const integrationItems = asArray<Record<string, unknown>>(integrations.items);
  const mutatingActions = asArray<Record<string, unknown>>(approval.model_exposed_mutating_actions);

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Capability Gateway"
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
            <Metric label="Toolsets" value={toolsets.length} />
            <Metric label="Default Deny" value={approval.default_deny} status={approval.default_deny} />
          </div>

          <div class="grid gap-4 xl:grid-cols-[1fr_0.9fr]">
            <Panel title="Runtime Lane" status={runtime.selected_lane}>
              <div class="grid gap-3 md:grid-cols-2">
                <Field label="Generic Provider" value={runtime.selected_generic_provider} />
                <Field label="Selected Model" value={runtime.selected_model} />
                <Field label="Text Route" value={asArray(runtime.generic_text_route).join(' -> ')} />
                <Field label="Tool Route" value={asArray(runtime.generic_tool_route).join(' -> ')} />
              </div>
            </Panel>

            <Panel title="Approval Policy" status={approval.dashboard_mode}>
              <div class="grid gap-2">
                <TinyRow label="Default deny" value={approval.default_deny} status={approval.default_deny} />
                <TinyRow label="Mutations require confirmation" value={approval.mutating_actions_require_operator_confirmation} status={approval.mutating_actions_require_operator_confirmation} />
                <TinyRow label="Dashboard mode" value={approval.dashboard_mode} status={approval.dashboard_mode} />
                <TinyRow label="Model-exposed mutations" value={mutatingActions.length} />
              </div>
            </Panel>
          </div>

          <div class="grid gap-4 xl:grid-cols-2">
            <Panel title="Toolsets" status={`${toolsets.length} available`}>
              <div class="grid gap-2">
                {toolsets.map((item) => (
                  <div key={text(item.name)} class="rounded bg-[var(--color-elevated)] px-3 py-2">
                    <div class="truncate text-[12px] font-medium text-[var(--color-text)]">{text(item.name)}</div>
                    <div class="mt-0.5 truncate text-[11px] text-[var(--color-text-faint)]">
                      {count(item.capability_count)} capabilities
                    </div>
                  </div>
                ))}
                {toolsets.length === 0 && <Empty title="No toolsets registered" />}
              </div>
            </Panel>

            <Panel title="Direct Integrations" status={`${count(integrations.enabled_count)} / ${count(integrations.total_count)}`}>
              <div class="grid gap-2">
                {integrationItems.slice(0, 12).map((item) => (
                  <TinyRow
                    key={text(item.id)}
                    label={text(item.display_name, text(item.id))}
                    value={`${count(item.action_count)} actions`}
                    status={item.enabled ? 'enabled' : 'disabled'}
                  />
                ))}
                {integrationItems.length === 0 && <Empty title="No integrations registered" description={integrations.error} />}
              </div>
            </Panel>
          </div>

          <div class="grid gap-4 xl:grid-cols-[0.85fr_1.15fr]">
            <Panel title="BrowserOps" status={browserops.status ?? browserops.enabled}>
              <div class="grid gap-3 md:grid-cols-2">
                <Field label="Enabled" value={browserops.enabled} />
                <Field label="Status" value={browserops.status} />
                <Field label="CDP Port" value={browserops.cdp_port} />
                <Field label="Reason" value={browserops.reason} />
              </div>
            </Panel>

            <Panel title="Outbound Messaging" status={outbound.status}>
              <div class="mb-3 flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                <ShieldCheck size={14} />
                <span>Operator confirmation: {text(outbound.requires_operator_confirmation)}</span>
              </div>
              <div class="grid gap-2 md:grid-cols-2">
                {asArray<Record<string, unknown>>(outbound.actions).slice(0, 8).map((action) => (
                  <div key={text(action.id)} class="rounded bg-[var(--color-elevated)] px-3 py-2">
                    <div class="truncate text-[12px] font-medium text-[var(--color-text)]">{text(action.id)}</div>
                    <div class="mt-0.5 truncate text-[11px] text-[var(--color-text-faint)]">{text(action.effect)}</div>
                  </div>
                ))}
                {asArray(outbound.actions).length === 0 && <Empty title="No outbound actions declared" />}
              </div>
            </Panel>
          </div>
        </div>
      </div>
    </div>
  );
}
