import { Send, Loader2 } from 'lucide-preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { renderMarkdown } from '@/lib/markdown';
import { subscribeChatStream, startChatStream, chatStreamConnected, resetUnread } from '@/lib/chat-stream';
import {
  apiGet,
  apiPost,
  DASHBOARD_CHAT_CONVERSATION_ID,
  DASHBOARD_CHAT_PERSONA_ID,
  dashboardChatReadOnly,
  chatId,
  describeApiError,
} from '@/lib/api';
import { formatRelativeTime } from '@/lib/format';
import { pushToast } from '@/lib/toasts';
import { outboundPersonaId } from '@/lib/translate-personas';

interface ChatComponent {
  label: string;
  custom_id: string;
  style?: string;
  disabled?: boolean;
}

interface ChatEvent {
  id: string;
  type: 'user_message' | 'assistant_message' | 'processing' | 'progress' | 'error';
  text?: string;
  timestamp: number;
  components?: ChatComponent[];
  replacesEventId?: string;
}

interface HistoryTurn {
  id: number;
  role: 'user' | 'assistant' | string;
  content: string;
  timestamp?: number;
  created_at?: string;
}

function eventFromHistory(turn: HistoryTurn): ChatEvent {
  const fallback = turn.created_at ? Date.parse(turn.created_at) / 1000 : Date.now() / 1000;
  return {
    id: `history-${turn.id}`,
    type: turn.role === 'user' ? 'user_message' : 'assistant_message',
    text: turn.content,
    timestamp: Number.isFinite(turn.timestamp) ? Number(turn.timestamp) : fallback,
    components: [],
  };
}

function eventFromStream(eventName: string, data: any): ChatEvent {
  const replacesEventId = data?.replaces_event_id ? String(data.replaces_event_id) : undefined;
  const streamEventId = data?.event_id ?? data?.last_event_id;
  return {
    id: String(streamEventId ?? `${Date.now()}-${Math.random()}`),
    type: eventName as ChatEvent['type'],
    text: data?.text || data?.content || '',
    timestamp: data?.timestamp ?? Date.now() / 1000,
    components: Array.isArray(data?.components) ? data.components : [],
    replacesEventId,
  };
}

function mergeChatEvent(prev: ChatEvent[], ev: ChatEvent): ChatEvent[] {
  const replacementId = ev.replacesEventId;
  if (replacementId) {
    const targetIndex = prev.findIndex((item) => item.id === replacementId);
    if (targetIndex >= 0) {
      const next = [...prev];
      next[targetIndex] = { ...ev, id: replacementId };
      return next;
    }
    return [...prev, { ...ev, id: replacementId }];
  }

  const existing = prev.findIndex((item) => item.id === ev.id);
  if (existing >= 0) {
    const next = [...prev];
    next[existing] = ev;
    return next;
  }
  return [...prev, ev];
}

function messageTone(type: ChatEvent['type']): string {
  if (type === 'user_message') return 'bg-[var(--color-accent-soft)] text-[var(--color-accent)]';
  if (type === 'error') {
    return 'border border-[color-mix(in_srgb,var(--color-status-failed)_50%,transparent)] bg-[color-mix(in_srgb,var(--color-status-failed)_12%,transparent)] text-[var(--color-text)]';
  }
  if (type === 'processing' || type === 'progress') {
    return 'border border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)]';
  }
  return 'border border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-text)]';
}

function actorLabel(type: ChatEvent['type']): string {
  if (type === 'user_message') return 'you';
  if (type === 'error') return 'error';
  if (type === 'processing' || type === 'progress') return 'status';
  return 'homie';
}

function streamPersonaMatches(streamPersonaId: unknown, browserPersonaId: string): boolean {
  if (!streamPersonaId) return true;
  return outboundPersonaId(String(streamPersonaId)) === browserPersonaId;
}

export function Chat() {
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [pendingActions, setPendingActions] = useState<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);

  const readOnly = dashboardChatReadOnly;
  const personaId = readOnly ? chatId : DASHBOARD_CHAT_PERSONA_ID;
  const conversationId = readOnly ? 'default' : DASHBOARD_CHAT_CONVERSATION_ID;

  const historyPath = useMemo(() => {
    const params = new URLSearchParams({ conversation_id: conversationId });
    return `/api/conversation/${encodeURIComponent(personaId)}/history?${params.toString()}`;
  }, [conversationId, personaId]);

  useEffect(() => {
    if (!personaId) return;
    startChatStream(personaId, conversationId);
    resetUnread();

    let cancelled = false;
    apiGet<{ turns: HistoryTurn[] }>(historyPath)
      .then((history) => {
        if (cancelled || !Array.isArray(history.turns)) return;
        setEvents(history.turns.map(eventFromHistory));
      })
      .catch(() => {
        if (!cancelled) setEvents([]);
      });

    const unsub = subscribeChatStream((eventName, data) => {
      if (eventName === 'refetch_hint') {
        apiGet<{ turns: HistoryTurn[] }>(historyPath)
          .then((history) => {
            if (Array.isArray(history.turns)) setEvents(history.turns.map(eventFromHistory));
          })
          .catch(() => {});
        return;
      }
      if (!['user_message', 'assistant_message', 'processing', 'progress', 'error'].includes(eventName)) return;
      if (!streamPersonaMatches(data?.persona_id, personaId)) return;
      if (data?.conversation_id && data.conversation_id !== conversationId) return;
      if ((eventName === 'processing' || eventName === 'progress') && !(data?.text || data?.content)) return;
      const ev = eventFromStream(eventName, data);
      setEvents((prev) => mergeChatEvent(prev, ev));
    });

    return () => {
      cancelled = true;
      unsub();
    };
  }, [conversationId, historyPath, personaId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [events.length]);

  async function submitMessage() {
    const text = draft.trim();
    if (!text || sending || readOnly) return;
    setSending(true);
    try {
      await apiPost(`/api/conversation/${encodeURIComponent(DASHBOARD_CHAT_PERSONA_ID)}/send`, {
        text,
        conversation_id: conversationId,
        client_message_id: `dash-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
        user_id: 'dashboard-user',
        display_name: 'Dashboard',
        source: 'interactive',
      });
      setDraft('');
    } catch (err) {
      pushToast({ tone: 'error', title: 'Message failed', description: describeApiError(err) });
    } finally {
      setSending(false);
    }
  }

  async function submitAction(customId: string) {
    if (!customId || pendingActions.has(customId) || readOnly) return;
    setPendingActions((prev) => new Set(prev).add(customId));
    try {
      await apiPost(`/api/conversation/${encodeURIComponent(DASHBOARD_CHAT_PERSONA_ID)}/send`, {
        conversation_id: conversationId,
        client_message_id: `dash-action-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
        user_id: 'dashboard-user',
        display_name: 'Dashboard',
        button_custom_id: customId,
        source: 'interactive',
      });
    } catch (err) {
      pushToast({ tone: 'error', title: 'Action failed', description: describeApiError(err) });
      setPendingActions((prev) => {
        const next = new Set(prev);
        next.delete(customId);
        return next;
      });
    }
  }

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Chat"
        subtitle={readOnly ? 'linked stream · read-only' : (chatStreamConnected.value ? 'dashboard chat · live' : 'dashboard chat · reconnecting')}
      />

      <div ref={scrollRef} class="flex-1 overflow-y-auto p-4 md:p-6">
        <div class="mx-auto flex max-w-4xl flex-col gap-3">
          {events.length === 0 && (
            <Empty
              title={readOnly ? 'No linked messages' : 'No messages yet'}
              description={readOnly ? 'Open dashboard chat directly for the writeable surface.' : 'Start a dashboard conversation with Homie.'}
            />
          )}
          {events.map((ev) => (
            <div key={ev.id} class={ev.type === 'user_message' ? 'flex justify-end' : 'flex justify-start'}>
              <div class={`max-w-[min(720px,86%)] rounded-lg px-3 py-2 ${messageTone(ev.type)}`}>
                <div class="mb-1 text-[10px] uppercase tracking-wider opacity-60">
                  {actorLabel(ev.type)}
                  {' · '}
                  {formatRelativeTime(ev.timestamp)}
                </div>
                {ev.text && (
                  <div
                    class="text-[13px] leading-relaxed prose-sm"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(ev.text) }}
                  />
                )}
                {ev.components && ev.components.length > 0 && (
                  <div class="mt-3 flex flex-wrap gap-2">
                    {ev.components.map((component) => (
                      <button
                        key={component.custom_id}
                        type="button"
                        disabled={readOnly || component.disabled || pendingActions.has(component.custom_id)}
                        onClick={() => submitAction(component.custom_id)}
                        class={`inline-flex h-8 items-center rounded-md border px-3 text-[12px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                          component.style === 'primary'
                            ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)] hover:bg-[color-mix(in_srgb,var(--color-accent)_18%,transparent)]'
                            : 'border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]'
                        }`}
                      >
                        {pendingActions.has(component.custom_id) ? 'Sent' : component.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      <form
        class="border-t border-[var(--color-border)] bg-[var(--color-bg)] p-3 md:p-4"
        onSubmit={(event) => {
          event.preventDefault();
          submitMessage();
        }}
      >
        <div class="mx-auto flex max-w-4xl items-end gap-2">
          <textarea
            value={draft}
            disabled={readOnly}
            onInput={(event) => setDraft((event.currentTarget as HTMLTextAreaElement).value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                submitMessage();
              }
            }}
            rows={1}
            placeholder={readOnly ? 'Linked stream is read-only' : 'Message Homie or type /provider'}
            class="min-h-10 max-h-36 flex-1 resize-none rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none transition-colors placeholder:text-[var(--color-text-faint)] focus:border-[var(--color-accent)] disabled:opacity-60"
          />
          <button
            type="submit"
            disabled={readOnly || sending || !draft.trim()}
            class="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-[var(--color-accent)] text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-45"
            title="Send"
          >
            {sending ? <Loader2 size={16} class="animate-spin" /> : <Send size={16} />}
          </button>
        </div>
      </form>
    </div>
  );
}
