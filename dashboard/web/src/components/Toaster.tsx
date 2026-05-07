import { X, CheckCircle2, AlertTriangle, Info, AlertCircle } from 'lucide-preact';
import { useState } from 'preact/hooks';
import { toasts, dismissToast, type Toast } from '@/lib/toasts';

const ICONS = {
  info: Info,
  success: CheckCircle2,
  warn: AlertTriangle,
  error: AlertCircle,
};

const TONES = {
  info: 'var(--color-text-muted)',
  success: 'var(--color-status-done)',
  warn: 'var(--color-status-warn)',
  error: 'var(--color-status-failed)',
};

export function Toaster() {
  const list = toasts.value;
  if (list.length === 0) return null;
  return (
    <div class="fixed top-4 right-4 z-[60] flex flex-col gap-2 max-w-sm">
      {list.map((t) => <ToastItem key={t.id} toast={t} />)}
    </div>
  );
}

function ToastItem({ toast }: { toast: Toast }) {
  const Icon = ICONS[toast.tone];
  const [running, setRunning] = useState(false);

  async function runAction() {
    if (!toast.action) return;
    setRunning(true);
    try { await toast.action.run(); } finally {
      setRunning(false);
      dismissToast(toast.id);
    }
  }

  return (
    <div
      class="bg-[var(--color-card)] border border-[var(--color-border)] rounded-lg shadow-lg p-3 flex gap-3"
      role="status"
    >
      <Icon size={16} style={{ color: TONES[toast.tone], flexShrink: 0, marginTop: 2 }} />
      <div class="flex-1 min-w-0">
        <div class="text-[13px] font-medium text-[var(--color-text)]">{toast.title}</div>
        {toast.description && (
          <div class="text-[12px] text-[var(--color-text-muted)] mt-0.5">{toast.description}</div>
        )}
        {toast.action && (
          <button
            type="button"
            onClick={runAction}
            disabled={running}
            class="mt-2 px-2 py-1 rounded text-[11px] bg-[var(--color-accent-soft)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white transition-colors disabled:opacity-40"
          >
            {running ? '...' : toast.action.label}
          </button>
        )}
      </div>
      <button
        type="button"
        onClick={() => dismissToast(toast.id)}
        class="p-1 rounded text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-elevated)] transition-colors"
        aria-label="Dismiss"
      >
        <X size={12} />
      </button>
    </div>
  );
}
