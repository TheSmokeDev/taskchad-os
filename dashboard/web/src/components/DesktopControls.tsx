import { useEffect, useMemo, useState } from 'preact/hooks';
import { Play, RefreshCw, Square } from 'lucide-preact';
import { pushToast } from '@/lib/toasts';

interface DesktopService {
  name: string;
  running: boolean;
  pid: number | null;
}

interface DesktopLogLine {
  timestamp: string;
  source: string;
  message: string;
}

interface DesktopStatus {
  running: boolean;
  targetUrl: string;
  config: {
    apiPort: number;
    dashboardPort: number;
    bind: string;
    startPath: string;
    autoStart: boolean;
  };
  services: DesktopService[];
  logs: DesktopLogLine[];
}

interface DesktopBridge {
  status: () => Promise<DesktopStatus>;
  startStack: () => Promise<DesktopStatus>;
  stopStack: () => Promise<DesktopStatus>;
  onStackEvent: (callback: (event: { type: string; status?: DesktopStatus }) => void) => () => void;
}

declare global {
  interface Window {
    homieDesktop?: DesktopBridge;
  }
}

function statusTone(running: boolean) {
  return running
    ? 'border-[color-mix(in_srgb,var(--color-status-done)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-done)_12%,transparent)] text-[var(--color-status-done)]'
    : 'border-[color-mix(in_srgb,var(--color-text-faint)_45%,transparent)] bg-[var(--color-elevated)] text-[var(--color-text-muted)]';
}

export function DesktopControls() {
  const bridge = typeof window !== 'undefined' ? window.homieDesktop : undefined;
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [busy, setBusy] = useState<'start' | 'stop' | 'refresh' | null>(null);
  const [expanded, setExpanded] = useState(false);

  const latestLogs = useMemo(() => status?.logs.slice(-3) ?? [], [status]);

  async function refresh() {
    if (!bridge) return;
    setBusy('refresh');
    try {
      setStatus(await bridge.status());
    } catch (error) {
      pushToast({
        tone: 'error',
        title: 'Desktop status unavailable',
        description: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy(null);
    }
  }

  async function run(action: 'start' | 'stop') {
    if (!bridge) return;
    setBusy(action);
    try {
      setStatus(action === 'start' ? await bridge.startStack() : await bridge.stopStack());
    } catch (error) {
      pushToast({
        tone: 'error',
        title: action === 'start' ? 'Start failed' : 'Stop failed',
        description: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => {
    if (!bridge) return;
    void refresh();
    return bridge.onStackEvent((event) => {
      if (event.type === 'status' && event.status) {
        setStatus(event.status);
      } else {
        void refresh();
      }
    });
  }, [bridge]);

  if (!bridge || !status) return null;

  return (
    <section class="border-b border-[var(--color-border)] bg-[var(--color-card)] px-4 py-2">
      <div class="flex min-w-0 flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          class={`inline-flex items-center gap-2 rounded border px-2.5 py-1 text-[11px] font-semibold uppercase ${statusTone(status.running)}`}
        >
          <span class="h-1.5 w-1.5 rounded-full bg-current" />
          Desktop Stack
        </button>
        <span class="truncate font-mono text-[11px] text-[var(--color-text-muted)]">{status.targetUrl}</span>
        <div class="ml-auto flex flex-wrap items-center gap-1.5">
          {status.services.map((service) => (
            <span
              key={service.name}
              class={`rounded border px-2 py-0.5 text-[10px] ${statusTone(service.running)}`}
              title={service.running && service.pid ? `${service.name} PID ${service.pid}` : service.name}
            >
              {service.name}
            </span>
          ))}
          <button
            type="button"
            onClick={refresh}
            disabled={busy !== null}
            class="inline-flex h-7 w-7 items-center justify-center rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-50"
            title="Refresh desktop stack status"
          >
            <RefreshCw size={13} />
          </button>
          <button
            type="button"
            onClick={() => run('start')}
            disabled={busy !== null || status.running}
            class="inline-flex h-7 w-7 items-center justify-center rounded border border-[var(--color-border)] text-[var(--color-status-done)] hover:bg-[var(--color-elevated)] disabled:opacity-40"
            title="Start local desktop stack"
          >
            <Play size={13} />
          </button>
          <button
            type="button"
            onClick={() => run('stop')}
            disabled={busy !== null || !status.running}
            class="inline-flex h-7 w-7 items-center justify-center rounded border border-[var(--color-border)] text-[var(--color-status-failed)] hover:bg-[var(--color-elevated)] disabled:opacity-40"
            title="Stop local desktop stack"
          >
            <Square size={13} />
          </button>
        </div>
      </div>
      {expanded && (
        <div class="mt-2 grid gap-1 rounded bg-[var(--color-bg)] p-2 font-mono text-[10.5px] text-[var(--color-text-muted)]">
          <div>api={status.config.apiPort} dashboard={status.config.dashboardPort} bind={status.config.bind}</div>
          {latestLogs.length === 0 && <div>No logs yet.</div>}
          {latestLogs.map((line) => (
            <div key={`${line.timestamp}-${line.source}-${line.message}`} class="truncate">
              [{line.timestamp}] {line.source}: {line.message}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
