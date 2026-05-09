/**
 * CabinetComposer — input box with @mention autocomplete from roster.
 *
 * PRD-8 Phase 5a / WS4.3. Body shape mirrors upstream
 * dashboard.ts:1016-1105 send body verbatim:
 *   { meetingId, text, clientMsgId, chatId? }
 *
 * Note: the @mention autocomplete popup is a UX nicety; the server already
 * parses `@<id>` from the body text via `extract_all_at_mentions()`, so
 * the popup only helps the operator type the right id.
 */

import { useState, useRef, useEffect } from 'preact/hooks';
import { apiPost } from '@/lib/api';

interface RosterAgent {
  id: string;
  name: string;
  description: string;
}

interface Props {
  meetingId: number;
  roster: RosterAgent[];
  disabled?: boolean;
}

export function CabinetComposer({ meetingId, roster, disabled }: Props) {
  const [text, setText] = useState('');
  const [showPicker, setShowPicker] = useState(false);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    // Show the @-picker when the cursor is right after a `@` token at end.
    const m = text.match(/@(\w*)$/);
    setShowPicker(!!m && roster.length > 0);
  }, [text, roster.length]);

  function insertMention(id: string) {
    const next = text.replace(/@\w*$/, `@${id} `);
    setText(next);
    setShowPicker(false);
    inputRef.current?.focus();
  }

  async function send() {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    try {
      const clientMsgId = `c_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
      await apiPost('/api/cabinet/send', {
        meetingId,
        text: t,
        clientMsgId,
      });
      setText('');
    } catch (err) {
      // Error surfaces via SSE error event; UI shows it from the transcript.
      console.error('cabinet send failed', err);
    } finally {
      setBusy(false);
    }
  }

  function onKey(ev: KeyboardEvent) {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      void send();
    }
  }

  const lastTokenMatch = text.match(/@(\w*)$/);
  const filterText = (lastTokenMatch?.[1] ?? '').toLowerCase();
  const filteredRoster = roster.filter((a) => a.id.toLowerCase().startsWith(filterText)).slice(0, 6);

  return (
    <div class="border-t border-[var(--color-border)] p-3 relative">
      {showPicker && filteredRoster.length > 0 && (
        <div class="absolute bottom-full left-3 mb-2 bg-[var(--color-card)] border border-[var(--color-border)] rounded-md shadow-lg max-w-md">
          {filteredRoster.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => insertMention(a.id)}
              class="block w-full text-left px-3 py-2 hover:bg-[var(--color-hover)] text-sm"
            >
              <span class="font-mono text-blue-500">@{a.id}</span> — {a.name}
            </button>
          ))}
        </div>
      )}
      <textarea
        ref={inputRef}
        value={text}
        onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
        onKeyDown={onKey}
        rows={2}
        disabled={disabled || busy}
        placeholder="Type @ for agent suggestions, Enter to send…"
        class="w-full bg-[var(--color-input)] text-[var(--color-text)] border border-[var(--color-border)] rounded-md p-2 text-sm resize-y"
      />
      <div class="flex justify-between mt-2 text-xs text-[var(--color-text-muted)]">
        <span>{busy ? 'Sending…' : 'Cmd/Ctrl+Enter to send'}</span>
        <button
          type="button"
          onClick={() => void send()}
          disabled={disabled || busy || !text.trim()}
          class="px-3 py-1 bg-[var(--color-primary)] text-white rounded-md text-xs font-medium disabled:opacity-50"
        >
          Send
        </button>
      </div>
    </div>
  );
}
