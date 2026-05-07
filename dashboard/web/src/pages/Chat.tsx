import { useEffect, useRef, useState } from 'preact/hooks';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { renderMarkdown } from '@/lib/markdown';
import { subscribeChatStream, startChatStream, chatStreamConnected, resetUnread } from '@/lib/chat-stream';
import { chatId } from '@/lib/api';
import { formatRelativeTime } from '@/lib/format';

interface ChatEvent {
  id: string;
  type: 'user_message' | 'assistant_message' | 'processing' | 'progress' | 'error';
  text?: string;
  timestamp: number;
}

/**
 * Read-only chat overlay — Phase 3 Operational Question 3 deferral. The
 * dashboard surfaces the conversation stream live via SSE but does NOT
 * provide a send-message affordance. Users continue to send via Telegram
 * (the primary surface). Dashboard is a window into the conversation.
 */
export function Chat() {
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!chatId) return;
    startChatStream(chatId);
    resetUnread();

    const unsub = subscribeChatStream((eventName, data) => {
      if (eventName === 'refetch_hint') {
        // 410 Gone — full refetch goes here in a later phase. For now
        // we just log and keep going; new events will append after the
        // stream reopens.
        return;
      }
      const ev: ChatEvent = {
        id: `${Date.now()}-${Math.random()}`,
        type: eventName as ChatEvent['type'],
        text: data?.text || data?.content || '',
        timestamp: data?.timestamp ?? Date.now() / 1000,
      };
      setEvents((prev) => [...prev, ev]);
    });

    return unsub;
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [events.length]);

  if (!chatId) {
    return (
      <div class="flex flex-col h-full">
        <TopBar title="Chat" />
        <Empty title="No chat session" description="Open the dashboard from a Telegram deep link or set ?chatId=... in the URL." />
      </div>
    );
  }

  return (
    <div class="flex flex-col h-full">
      <TopBar
        title="Chat"
        subtitle={chatStreamConnected.value ? 'live · read-only' : 'reconnecting...'}
      />
      <div ref={scrollRef} class="flex-1 overflow-y-auto p-6 space-y-3">
        {events.length === 0 && (
          <div class="text-[12px] text-[var(--color-text-muted)] text-center py-8">
            Send a message in Telegram and it will stream here.
          </div>
        )}
        {events.map((ev) => (
          <div key={ev.id} class={ev.type === 'user_message' ? 'flex justify-end' : 'flex justify-start'}>
            <div class={[
              'max-w-[80%] rounded-lg px-3 py-2',
              ev.type === 'user_message'
                ? 'bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
                : 'bg-[var(--color-card)] border border-[var(--color-border)] text-[var(--color-text)]',
            ].join(' ')}>
              <div class="text-[10px] uppercase tracking-wider opacity-60 mb-1">
                {ev.type === 'user_message' ? 'you' : 'homie'}
                {' · '}
                {formatRelativeTime(ev.timestamp)}
              </div>
              {/* All chat HTML routed through DOMPurify-wrapped renderer. */}
              <div
                class="text-[13px] prose-sm leading-relaxed"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(ev.text || '') }}
              />
            </div>
          </div>
        ))}
      </div>
      <div class="px-6 py-3 border-t border-[var(--color-border)] text-[11px] text-[var(--color-text-muted)] text-center">
        Send messages in Telegram. This dashboard is a read-only stream.
      </div>
    </div>
  );
}
