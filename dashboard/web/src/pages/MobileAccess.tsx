import { Copy, ExternalLink, Network, RefreshCw, ShieldCheck, Smartphone, Terminal, Wifi, WifiOff } from 'lucide-preact';
import type { ComponentChildren } from 'preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';
import { pushToast } from '@/lib/toasts';

interface MobileAccessUrls {
  root: string | null;
  browser: string | null;
  teams: string | null;
  mobile: string | null;
}

interface MobileAccessStatus {
  status: string;
  mode: 'read_only';
  tailscale: {
    available: boolean;
    backend_state: string | null;
    hostname: string | null;
    dns_name: string | null;
    ips: string[];
    primary_ip: string | null;
    error: string | null;
  };
  dashboard: {
    web_port: number;
    request_host: string | null;
    urls: MobileAccessUrls;
    bind_hint: string;
  };
  serve: {
    available: boolean;
    enabled: boolean;
    http: boolean;
    https: boolean;
    hosts: string[];
    ports: Array<{ port: string; http: boolean; https: boolean }>;
    error: string | null;
  };
  controls: {
    mutates_tailscale: false;
    mutates_browser: false;
  };
}

function text(value: unknown, fallback = '-'): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function currentOriginUrl(pathname: string): string | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(window.location.href);
  url.pathname = pathname;
  url.search = '';
  url.hash = '';
  return url.toString();
}

async function writeClipboardText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Fall through to the local DOM fallback below for non-secure tailnet origins.
    }
  }

  const field = document.createElement('textarea');
  field.value = value;
  field.setAttribute('readonly', 'true');
  field.style.position = 'fixed';
  field.style.left = '-9999px';
  field.style.top = '0';
  document.body.appendChild(field);
  field.focus();
  field.select();
  const copied = document.execCommand('copy');
  document.body.removeChild(field);
  if (!copied) throw new Error('Clipboard unavailable');
}

function toneClass(value: unknown): string {
  const normalized = text(value).toLowerCase();
  if (['ready', 'running', 'true', 'enabled', 'read_only'].includes(normalized)) {
    return 'border-[color-mix(in_srgb,var(--color-status-done)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-done)_14%,transparent)] text-[var(--color-status-done)]';
  }
  if (['unavailable', 'false', 'not found', 'offline'].includes(normalized)) {
    return 'border-[color-mix(in_srgb,var(--color-status-failed)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-failed)_14%,transparent)] text-[var(--color-status-failed)]';
  }
  return 'border-[color-mix(in_srgb,var(--color-status-warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-warn)_14%,transparent)] text-[var(--color-status-warn)]';
}

function Pill({ value }: { value: unknown }) {
  return (
    <span class={`inline-flex max-w-full items-center rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${toneClass(value)}`}>
      <span class="truncate">{text(value, 'unknown')}</span>
    </span>
  );
}

function Metric({ label, value, status }: { label: string; value: unknown; status?: unknown }) {
  return (
    <div class="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
      <div class="flex min-w-0 items-center justify-between gap-3">
        <div class="truncate text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{label}</div>
        {status !== undefined && <Pill value={status} />}
      </div>
      <div class="mt-4 truncate text-[18px] font-semibold leading-tight text-[var(--color-text)]">{text(value)}</div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: unknown }) {
  return (
    <div class="flex min-w-0 items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
      <span class="shrink-0 text-[12px] text-[var(--color-text-muted)]">{label}</span>
      <span class="min-w-0 truncate text-right font-mono text-[12px] text-[var(--color-text)]">{text(value)}</span>
    </div>
  );
}

function Card({ title, icon, children }: { title: string; icon: ComponentChildren; children: ComponentChildren }) {
  return (
    <section class="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
      <div class="mb-4 flex items-center gap-2 text-[13px] font-semibold text-[var(--color-text)]">
        {icon}
        <span>{title}</span>
      </div>
      {children}
    </section>
  );
}

function LinkRow({
  label,
  value,
  onCopy,
}: {
  label: string;
  value: string | null;
  onCopy: (label: string, value: string | null) => void;
}) {
  return (
    <div class="flex min-w-0 flex-col gap-2 rounded bg-[var(--color-elevated)] p-3 sm:flex-row sm:items-center sm:justify-between">
      <div class="min-w-0">
        <div class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)]">{label}</div>
        <div class="mt-1 truncate font-mono text-[12px] text-[var(--color-text)]">{text(value, 'unavailable')}</div>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <button
          type="button"
          onClick={() => onCopy(label, value)}
          disabled={!value}
          class="inline-flex h-8 w-8 items-center justify-center rounded-md border border-[var(--color-border)] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-card)] hover:text-[var(--color-text)] disabled:opacity-50"
          title={`Copy ${label}`}
        >
          <Copy size={14} />
        </button>
        <a
          href={value ?? '#'}
          target="_blank"
          rel="noreferrer"
          class={`inline-flex h-8 w-8 items-center justify-center rounded-md border border-[var(--color-border)] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-card)] hover:text-[var(--color-text)] ${value ? '' : 'pointer-events-none opacity-50'}`}
          title={`Open ${label}`}
        >
          <ExternalLink size={14} />
        </a>
      </div>
    </div>
  );
}

export function MobileAccess() {
  const { data, loading, error, refresh } = useFetch<MobileAccessStatus>('/api/dashboard/mobile-access', 30_000);

  function mobileUrl(path: keyof MobileAccessUrls): string | null {
    const direct = data?.dashboard.urls[path] ?? null;
    const current = currentOriginUrl(path === 'root' ? '/' : `/${path}`);
    return direct ?? current;
  }

  async function copyValue(label: string, value: string | null) {
    if (!value) return;
    try {
      await writeClipboardText(value);
      pushToast({ tone: 'success', title: `${label} copied` });
    } catch (err) {
      pushToast({ tone: 'error', title: 'Copy failed', description: err instanceof Error ? err.message : String(err) });
    }
  }

  if (loading && !data) return <div class="flex h-full items-center justify-center"><Spinner /></div>;

  const browserUrl = mobileUrl('browser');
  const teamsUrl = mobileUrl('teams');
  const mobilePageUrl = mobileUrl('mobile');
  const serveState = data?.serve.enabled ? 'enabled' : 'disabled';
  const subtitle = data
    ? `${text(data.mode)} · ${text(data.tailscale.primary_ip)} · ${text(data.tailscale.backend_state)}`
    : 'mobile access';

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Mobile Access"
        subtitle={subtitle}
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
        <div class="mx-auto max-w-7xl space-y-4">
          {error && <Empty title="Mobile access unavailable" description={error} />}

          <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Status" value={data?.status} status={data?.status} />
            <Metric label="Tailnet IP" value={data?.tailscale.primary_ip} status={data?.tailscale.available ? 'ready' : 'offline'} />
            <Metric label="DNS" value={data?.tailscale.dns_name} />
            <Metric label="Serve" value={serveState} status={serveState} />
          </div>

          <div class="grid gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(320px,0.85fr)]">
            <Card title="Phone Links" icon={<Smartphone size={15} />}>
              <div class="grid gap-3">
                <LinkRow label="Browser Viewer" value={browserUrl} onCopy={copyValue} />
                <LinkRow label="Teams" value={teamsUrl} onCopy={copyValue} />
                <LinkRow label="Mobile Access" value={mobilePageUrl} onCopy={copyValue} />
              </div>
            </Card>

            <Card title="Tailnet" icon={<Network size={15} />}>
              <div class="grid gap-2">
                <Field label="Hostname" value={data?.tailscale.hostname} />
                <Field label="Backend" value={data?.tailscale.backend_state} />
                <Field label="Request Host" value={data?.dashboard.request_host} />
                <Field label="IPs" value={data?.tailscale.ips.join(', ')} />
              </div>
            </Card>

            <Card title="Serve" icon={data?.serve.enabled ? <Wifi size={15} /> : <WifiOff size={15} />}>
              <div class="grid gap-2">
                <Field label="Available" value={data?.serve.available} />
                <Field label="HTTP" value={data?.serve.http} />
                <Field label="HTTPS" value={data?.serve.https} />
                <Field label="Hosts" value={data?.serve.hosts.join(', ')} />
                {data?.serve.error && <Field label="Error" value={data.serve.error} />}
              </div>
            </Card>

            <Card title="Runtime" icon={<Terminal size={15} />}>
              <div class="grid gap-2">
                <Field label="Web Port" value={data?.dashboard.web_port} />
                <Field label="Bind" value={data?.dashboard.bind_hint} />
              </div>
            </Card>

            <Card title="Controls" icon={<ShieldCheck size={15} />}>
              <div class="grid gap-2">
                <Field label="Tailscale Write" value={data?.controls.mutates_tailscale} />
                <Field label="Browser Write" value={data?.controls.mutates_browser} />
              </div>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}
