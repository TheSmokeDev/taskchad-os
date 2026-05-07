import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'wouter-preact';
import { Search } from 'lucide-preact';
import { commandPaletteOpen, buildActions, filterActions } from '@/lib/command-palette';

export function CommandPalette() {
  const open = commandPaletteOpen.value;
  const [, navigate] = useLocation();
  const [query, setQuery] = useState('');
  const [highlight, setHighlight] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const isMod = e.metaKey || e.ctrlKey;
      if (isMod && e.key === 'k') {
        e.preventDefault();
        commandPaletteOpen.value = true;
      } else if (e.key === 'Escape' && commandPaletteOpen.value) {
        commandPaletteOpen.value = false;
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (open) {
      setQuery('');
      setHighlight(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  if (!open) return null;

  const actions = buildActions();
  const filtered = filterActions(query, actions);

  function run(idx: number) {
    const action = filtered[idx];
    if (!action) return;
    action.run({ navigate });
    commandPaletteOpen.value = false;
  }

  return (
    <div
      class="fixed inset-0 z-[55] flex items-start justify-center pt-[10vh] bg-black/50 backdrop-blur-sm p-4"
      onClick={() => { commandPaletteOpen.value = false; }}
    >
      <div
        class="bg-[var(--color-card)] border border-[var(--color-border)] rounded-lg shadow-2xl w-full max-w-lg flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div class="flex items-center gap-2 px-4 py-3 border-b border-[var(--color-border)]">
          <Search size={14} class="text-[var(--color-text-muted)]" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onInput={(e) => { setQuery((e.target as HTMLInputElement).value); setHighlight(0); }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') { e.preventDefault(); setHighlight((h) => Math.min(filtered.length - 1, h + 1)); }
              if (e.key === 'ArrowUp') { e.preventDefault(); setHighlight((h) => Math.max(0, h - 1)); }
              if (e.key === 'Enter') { e.preventDefault(); run(highlight); }
            }}
            placeholder="Jump to a page or run an action..."
            class="flex-1 bg-transparent border-none outline-none text-[14px] text-[var(--color-text)] placeholder:text-[var(--color-text-muted)]"
          />
        </div>
        <div class="max-h-80 overflow-y-auto p-1">
          {filtered.length === 0 && (
            <div class="px-4 py-6 text-[12px] text-[var(--color-text-muted)] text-center">
              No matches
            </div>
          )}
          {filtered.map((action, idx) => {
            const isActive = idx === highlight;
            return (
              <button
                key={action.id}
                type="button"
                onClick={() => run(idx)}
                onMouseEnter={() => setHighlight(idx)}
                class={[
                  'w-full text-left flex items-center gap-2 px-3 py-2 rounded text-[13px]',
                  isActive
                    ? 'bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
                    : 'text-[var(--color-text-muted)]',
                ].join(' ')}
              >
                <span class="flex-1">{action.label}</span>
                <span class="text-[10px] uppercase tracking-wider opacity-60">{action.group}</span>
                {action.hint && (
                  <span class="text-[10px] text-[var(--color-text-faint)]">{action.hint}</span>
                )}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
