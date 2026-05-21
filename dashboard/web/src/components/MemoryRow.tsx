import { renderMarkdown } from '@/lib/markdown';
import { formatRelativeTime } from '@/lib/format';

export interface MemoryRecord {
  id: string | number;
  personaId?: string;
  persona_id?: string;
  text?: string;
  chunk_text?: string;
  sourcePath?: string;
  source_path?: string;
  tags?: string[];
  createdAt?: number;
  created_at?: number | string;
  kind?: string;
}

export function MemoryRow({ memory }: { memory: MemoryRecord }) {
  const text = memory.text ?? memory.chunk_text ?? '';
  const personaId = memory.personaId ?? memory.persona_id ?? 'vault';
  const tags = Array.isArray(memory.tags) ? memory.tags : [];
  const createdAt = normalizeTimestamp(memory.createdAt ?? memory.created_at);
  const sourcePath = memory.sourcePath ?? memory.source_path;
  // Markdown body always goes through DOMPurify-wrapped renderer.
  const html = renderMarkdown(text);
  return (
    <div class="px-4 py-3 border-b border-[var(--color-border)]">
      <div class="flex items-center gap-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1.5">
        <span>{personaId}</span>
        <span>·</span>
        <span>{formatRelativeTime(createdAt)}</span>
        {tags.length > 0 && (
          <>
            <span>·</span>
            <span class="lowercase normal-case text-[10px]">
              {tags.map((t) => `#${t}`).join(' ')}
            </span>
          </>
        )}
      </div>
      {sourcePath && (
        <div class="text-[10px] text-[var(--color-text-faint)] mb-1 truncate">
          {sourcePath}
        </div>
      )}
      <div
        class="text-[13px] text-[var(--color-text)] prose-sm leading-relaxed"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}

function normalizeTimestamp(value: number | string | undefined): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return numeric;
    }
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) {
      return parsed / 1000;
    }
  }
  return Date.now() / 1000;
}
