import type { ComponentChildren } from 'preact';

interface TopBarProps {
  title: string;
  subtitle?: string;
  actions?: ComponentChildren;
}

export function TopBar({ title, subtitle, actions }: TopBarProps) {
  return (
    <div class="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)] bg-[var(--color-bg)]">
      <div class="min-w-0">
        <div class="text-[15px] font-semibold text-[var(--color-text)] truncate">{title}</div>
        {subtitle && (
          <div class="text-[12px] text-[var(--color-text-muted)] mt-0.5 truncate">{subtitle}</div>
        )}
      </div>
      {actions && <div class="flex items-center gap-2 flex-shrink-0">{actions}</div>}
    </div>
  );
}
