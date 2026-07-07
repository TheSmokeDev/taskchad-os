import { useEffect, useState } from 'preact/hooks';
import { ArrowLeft, MessageSquare, Search } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';
import { useDebouncedValue } from '@/lib/useDebounce';
import { renderMarkdown } from '@/lib/markdown';
import { pushToast } from '@/lib/toasts';

// Session ids are OPAQUE framework keys (`web:{cid}:{cid}`, `telegram:…`) —
// never browser persona aliases — so no main↔default translation applies
// (same contract as dashboard/server/src/routes/sessions.ts).

interface SessionSummary {
  session_id: string;
  platform: string;
  source: string;
  persona_id: string | null;
  message_count: number;
  updated_at: string;
  preview: string;
}

interface SearchHit {
  message_id: number;
  session_id: string;
  role: string;
  snippet: string;
  created_at: string | null;
}

interface TranscriptTurn {
  id: number;
  session_id: string;
  role: string;
  content: string;
  created_at: string | null;
  tool_calls_json: string;
}

interface SessionsResponse { sessions: SessionSummary[]; }
interface SearchResponse { hits: SearchHit[]; }
interface MessagesResponse { messages: TranscriptTurn[]; }

/** Read-only bubble tones — a light local copy of Chat.tsx's messageTone
 *  (unexported there; only the user/assistant tones apply to persisted
 *  transcripts). Markdown itself is the SHARED lib/markdown renderer. */
function bubbleTone(role: string): string {
  if (role === 'user') return 'bg-[var(--color-accent-soft)] text-[var(--color-accent)]';
  return 'border border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-text)]';
}

function bubbleActor(role: string): string {
  return role === 'user' ? 'you' : 'homie';
}

function formatStamp(iso: string | null | undefined): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  return new Date(t).toLocaleString();
}

/** Client-side term highlighting — pure JSX splitting (no innerHTML), so
 *  snippet content can never inject markup. */
function Highlight({ text, term }: { text: string; term: string }) {
  const t = term.trim();
  if (!t) return <>{text}</>;
  const lower = text.toLowerCase();
  const needle = t.toLowerCase();
  const parts: any[] = [];
  let i = 0;
  for (;;) {
    const at = lower.indexOf(needle, i);
    if (at === -1) {
      parts.push(text.slice(i));
      break;
    }
    if (at > i) parts.push(text.slice(i, at));
    parts.push(
      <mark class="rounded-sm bg-[var(--color-accent-soft)] px-0.5 text-[var(--color-accent)]">
        {text.slice(at, at + needle.length)}
      </mark>,
    );
    i = at + needle.length;
  }
  return <>{parts}</>;
}

export function Sessions() {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const debounced = useDebouncedValue(query, 300);
  const term = debounced.trim();
  const searching = term.length > 0;

  const recent = useFetch<SessionsResponse>(
    searching ? null : '/api/sessions?limit=60',
    30_000,
  );
  const search = useFetch<SearchResponse>(
    searching ? `/api/sessions/search?q=${encodeURIComponent(term)}&limit=50` : null,
  );
  const transcript = useFetch<MessagesResponse>(
    selected
      ? `/api/sessions/messages?session_id=${encodeURIComponent(selected)}&limit=300`
      : null,
  );

  // Fail-open UX: a search/transcript error surfaces as toast + empty state,
  // never a crash.
  useEffect(() => {
    if (search.error) {
      pushToast({ tone: 'error', title: 'Conversation search failed', description: search.error });
    }
  }, [search.error]);
  useEffect(() => {
    if (transcript.error) {
      pushToast({ tone: 'error', title: 'Failed to load transcript', description: transcript.error });
    }
  }, [transcript.error]);

  const sessions = recent.data?.sessions ?? [];
  const hits = search.data?.hits ?? [];
  const turns = transcript.data?.messages ?? [];
  const listLoading = searching ? search.loading : recent.loading && !recent.data;

  const subtitle = searching
    ? (search.data ? `${hits.length} match${hits.length === 1 ? '' : 'es'} for "${term}"` : '')
    : (recent.data ? `${sessions.length} recent session${sessions.length === 1 ? '' : 's'}` : '');

  return (
    <div class="flex flex-col h-full min-h-0">
      <TopBar title="History" subtitle={subtitle} />

      <div class="px-6 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg)] flex items-center gap-2">
        <Search size={14} class="text-[var(--color-text-faint)] shrink-0" />
        <input
          type="text"
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
          placeholder="search every conversation (FTS5)…"
          aria-label="Search conversations"
          class="flex-1 max-w-xl bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2 py-1.5 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)] placeholder:text-[var(--color-text-faint)]"
        />
      </div>

      <div class="flex flex-1 min-h-0">
        {/* Master list — hidden on mobile while a transcript is open. */}
        <div class={`${selected ? 'hidden md:flex' : 'flex'} w-full md:w-96 lg:w-[26rem] shrink-0 flex-col border-r border-[var(--color-border)] min-h-0`}>
          <div class="flex-1 overflow-y-auto scroll-safe-bottom">
            {listLoading && (
              <div class="flex items-center justify-center h-40"><Spinner /></div>
            )}
            {!searching && recent.error && (
              <Empty title="Failed to load sessions" description={recent.error} />
            )}
            {searching && search.error && (
              <Empty title="Search unavailable" description={search.error} />
            )}

            {!searching && !recent.error && !listLoading && sessions.length === 0 && (
              <Empty title="No sessions yet" description="Conversations appear here once the bot has persisted turns." />
            )}
            {searching && !search.error && !search.loading && hits.length === 0 && (
              <Empty title="No matches" description={`Nothing in any conversation matches "${term}".`} />
            )}

            {!searching && sessions.map((s) => (
              <button
                key={s.session_id}
                type="button"
                onClick={() => setSelected(s.session_id)}
                class={`w-full text-left px-4 py-3 border-b border-[var(--color-border)] transition-colors hover:bg-[var(--color-elevated)] ${
                  selected === s.session_id ? 'bg-[var(--color-elevated)]' : ''
                }`}
              >
                <div class="flex items-center gap-2 mb-1">
                  <MessageSquare size={13} class="text-[var(--color-accent)] shrink-0" />
                  <span class="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)]">{s.platform}</span>
                  {s.persona_id && (
                    <span class="text-[10.5px] rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-1 py-px text-[var(--color-text-faint)]">{s.persona_id}</span>
                  )}
                  <span class="ml-auto text-[10.5px] tabular-nums text-[var(--color-text-faint)]">
                    {s.message_count} msg{s.message_count === 1 ? '' : 's'}
                  </span>
                </div>
                <div class="font-mono text-[10.5px] text-[var(--color-text-faint)] truncate mb-1">{s.session_id}</div>
                <div class="text-[12px] text-[var(--color-text-muted)] line-clamp-2">{s.preview || 'No preview.'}</div>
                <div class="mt-1 text-[10.5px] text-[var(--color-text-faint)]">{formatStamp(s.updated_at)}</div>
              </button>
            ))}

            {searching && hits.map((h) => (
              <button
                key={h.message_id}
                type="button"
                onClick={() => setSelected(h.session_id)}
                class={`w-full text-left px-4 py-3 border-b border-[var(--color-border)] transition-colors hover:bg-[var(--color-elevated)] ${
                  selected === h.session_id ? 'bg-[var(--color-elevated)]' : ''
                }`}
              >
                <div class="flex items-center gap-2 mb-1">
                  <span class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{bubbleActor(h.role)}</span>
                  <span class="font-mono text-[10.5px] text-[var(--color-text-faint)] truncate">{h.session_id}</span>
                  <span class="ml-auto shrink-0 text-[10.5px] text-[var(--color-text-faint)]">{formatStamp(h.created_at)}</span>
                </div>
                <div class="text-[12.5px] leading-relaxed text-[var(--color-text)]">
                  <Highlight text={h.snippet} term={term} />
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* Detail — full-pane on mobile, inline panel on desktop. */}
        <div class={`${selected ? 'flex' : 'hidden md:flex'} flex-1 min-w-0 flex-col min-h-0`}>
          {!selected && (
            <div class="flex-1 flex items-center justify-center">
              <Empty title="Select a conversation" description="Pick a session or a search result to read its transcript." />
            </div>
          )}
          {selected && (
            <>
              <div class="flex items-center gap-2 px-4 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg)]">
                <button
                  type="button"
                  onClick={() => setSelected(null)}
                  aria-label="Back to sessions"
                  class="md:hidden inline-flex items-center gap-1 rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1 text-[12px] text-[var(--color-text-muted)]"
                >
                  <ArrowLeft size={13} />
                  Back
                </button>
                <span class="font-mono text-[11px] text-[var(--color-text-muted)] truncate">{selected}</span>
                <span class="ml-auto text-[10.5px] uppercase tracking-wider text-[var(--color-text-faint)]">read-only</span>
              </div>
              <div class="flex-1 overflow-y-auto p-4 md:p-6 scroll-safe-bottom">
                <div class="mx-auto flex max-w-4xl flex-col gap-3">
                  {transcript.loading && !transcript.data && (
                    <div class="flex items-center justify-center h-40"><Spinner /></div>
                  )}
                  {transcript.error && (
                    <Empty title="Failed to load transcript" description={transcript.error} />
                  )}
                  {!transcript.loading && !transcript.error && turns.length === 0 && (
                    <Empty title="Empty transcript" description="No persisted turns for this session." />
                  )}
                  {turns.map((turn) => (
                    <div key={turn.id} class={turn.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
                      <div class={`max-w-[min(720px,86%)] rounded-lg px-3 py-2 ${bubbleTone(turn.role)}`}>
                        <div class="mb-1 text-[10px] uppercase tracking-wider opacity-60">
                          {bubbleActor(turn.role)}
                          {turn.created_at ? ` · ${formatStamp(turn.created_at)}` : ''}
                        </div>
                        <div
                          class="text-[13px] leading-relaxed prose-sm"
                          dangerouslySetInnerHTML={{ __html: renderMarkdown(turn.content) }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
