import type { ComponentChildren } from 'preact';

interface EmptyProps {
  title: string;
  description?: string;
  action?: ComponentChildren;
}

export function Empty({ title, description, action }: EmptyProps) {
  return (
    <div class="flex flex-col items-center justify-center py-12 px-6 text-center">
      <div class="text-[var(--color-text)] text-[14px] font-medium mb-1">{title}</div>
      {description && (
        <div class="text-[var(--color-text-muted)] text-[12px] max-w-sm mb-4">{description}</div>
      )}
      {action}
    </div>
  );
}
