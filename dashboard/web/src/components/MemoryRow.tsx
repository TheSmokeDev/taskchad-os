import { renderMarkdown } from '@/lib/markdown';
import { formatRelativeTime } from '@/lib/format';

export interface MemoryRecord {
  id: string;
  personaId: string;
  text: string;
  tags: string[];
  createdAt: number;
}

export function MemoryRow({ memory }: { memory: MemoryRecord }) {
  // Markdown body always goes through DOMPurify-wrapped renderer.
  const html = renderMarkdown(memory.text);
  return (
    <div class="px-4 py-3 border-b border-[var(--color-border)]">
      <div class="flex items-center gap-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">
        <span>{memory.personaId}</span>
        <span>·</span>
        <span>{formatRelativeTime(memory.createdAt)}</span>
        {memory.tags.length > 0 && (
          <>
            <span>·</span>
            <span class="lowercase normal-case text-[10px]">
              {memory.tags.map((t) => `#${t}`).join(' ')}
            </span>
          </>
        )}
      </div>
      <div
        class="text-[13px] text-[var(--color-text)] prose-sm leading-relaxed"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}
