import { X } from 'lucide-preact';
import type { ComponentChildren } from 'preact';
import { useEffect } from 'preact/hooks';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  width?: number;
  children: ComponentChildren;
  footer?: ComponentChildren;
}

export function Modal({ open, onClose, title, width = 480, children, footer }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        class="bg-[var(--color-card)] border border-[var(--color-border)] rounded-lg shadow-2xl flex flex-col max-h-[90vh]"
        style={{ width: `${width}px`, maxWidth: '95vw' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div class="flex items-center justify-between px-4 py-3 border-b border-[var(--color-border)]">
          <div class="text-[13px] font-medium text-[var(--color-text)]">{title}</div>
          <button
            type="button"
            onClick={onClose}
            class="p-1 rounded text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-elevated)] transition-colors"
            aria-label="Close"
          >
            <X size={14} />
          </button>
        </div>
        <div class="flex-1 overflow-y-auto p-4">{children}</div>
        {footer && (
          <div class="flex items-center gap-2 px-4 py-3 border-t border-[var(--color-border)]">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
