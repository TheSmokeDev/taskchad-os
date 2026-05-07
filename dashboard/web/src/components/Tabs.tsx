import type { ComponentChildren } from 'preact';

export interface TabSpec {
  id: string;
  label: string;
  badge?: string | number;
}

interface TabsProps {
  tabs: TabSpec[];
  active: string;
  onChange: (id: string) => void;
  children?: ComponentChildren;
}

export function Tabs({ tabs, active, onChange, children }: TabsProps) {
  return (
    <div class="flex flex-col h-full">
      <div class="flex items-center gap-1 border-b border-[var(--color-border)] px-2">
        {tabs.map((t) => {
          const isActive = t.id === active;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => onChange(t.id)}
              class={[
                'px-3 py-2 text-[12px] border-b-2 transition-colors',
                isActive
                  ? 'border-[var(--color-accent)] text-[var(--color-text)]'
                  : 'border-transparent text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
              ].join(' ')}
            >
              {t.label}
              {t.badge !== undefined && (
                <span class="ml-1.5 inline-flex items-center justify-center min-w-[16px] px-1 rounded text-[10px] bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                  {t.badge}
                </span>
              )}
            </button>
          );
        })}
      </div>
      <div class="flex-1 overflow-hidden">{children}</div>
    </div>
  );
}
