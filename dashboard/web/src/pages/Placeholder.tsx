import { TopBar } from '@/components/TopBar';

interface PlaceholderProps {
  title: string;
  description?: string;
}

export function Placeholder({ title, description }: PlaceholderProps) {
  return (
    <div class="flex flex-col h-full">
      <TopBar title={title} />
      <div class="flex-1 flex items-center justify-center p-8">
        <div class="text-center max-w-md">
          <div class="text-[14px] text-[var(--color-text)] font-medium mb-2">Coming soon</div>
          <div class="text-[12px] text-[var(--color-text-muted)]">
            {description ?? `${title} ships in a later phase.`}
          </div>
        </div>
      </div>
    </div>
  );
}
