import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost, tokenizedConversationStream } from '@/lib/api';
import {
  CHAT_EVENT_NAMES,
  eventFromHistory,
  eventFromStream,
  mergeChatEvent,
  personaMatches,
} from '@/lib/conversation';
import type { ChatEvent, HistoryTurn } from '@/types';

function clientMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `web-next-${crypto.randomUUID()}`;
  }
  return `web-next-${Date.now().toString(36)}`;
}

export function useConversation(personaId: string | null, live = true) {
  const conversationId = useMemo(
    () => (personaId ? `dashboard-coworker-${personaId}` : 'dashboard-coworker-none'),
    [personaId],
  );
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const historyPath = useMemo(() => {
    if (!personaId) return null;
    const query = new URLSearchParams({ conversation_id: conversationId });
    return `/api/conversation/${encodeURIComponent(personaId)}/history?${query}`;
  }, [conversationId, personaId]);

  const loadHistory = useCallback(
    async (signal?: AbortSignal) => {
      if (!historyPath) return;
      const history = await apiGet<{ turns: HistoryTurn[] }>(historyPath, signal);
      setEvents(Array.isArray(history.turns) ? history.turns.map(eventFromHistory) : []);
    },
    [historyPath],
  );

  useEffect(() => {
    setEvents([]);
    setConnected(false);
    setError(null);
    if (!personaId || !historyPath) return;

    const controller = new AbortController();
    void loadHistory(controller.signal).catch((reason) => {
      if (controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : String(reason));
    });

    if (!live) return () => controller.abort();

    const query = new URLSearchParams({ conversation_id: conversationId });
    const source = new EventSource(
      tokenizedConversationStream(
        `/api/conversation/${encodeURIComponent(personaId)}/stream?${query}`,
      ),
    );
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    const handlers = CHAT_EVENT_NAMES.map((eventName) => {
      const handler = (message: MessageEvent) => {
        let payload: unknown;
        try {
          payload = JSON.parse(String(message.data));
        } catch {
          return;
        }
        if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
          const data = payload as Record<string, unknown>;
          if (!personaMatches(data.persona_id, personaId)) return;
          if (data.conversation_id && data.conversation_id !== conversationId) return;
          if (message.lastEventId && data.event_id === undefined) data.event_id = message.lastEventId;
        }
        const event = eventFromStream(eventName, payload);
        if (event) setEvents((previous) => mergeChatEvent(previous, event));
      };
      source.addEventListener(eventName, handler);
      return { eventName, handler };
    });

    return () => {
      controller.abort();
      for (const { eventName, handler } of handlers) source.removeEventListener(eventName, handler);
      source.close();
    };
  }, [conversationId, historyPath, live, loadHistory, personaId]);

  const submit = useCallback(
    async (text: string, buttonCustomId?: string) => {
      if (!personaId || sending) return;
      setSending(true);
      setError(null);
      try {
        await apiPost(`/api/conversation/${encodeURIComponent(personaId)}/send`, {
          ...(buttonCustomId ? { button_custom_id: buttonCustomId } : { text }),
          conversation_id: conversationId,
          client_message_id: clientMessageId(),
          user_id: 'dashboard-user',
          display_name: 'Dashboard',
          source: 'interactive',
        });
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
        throw reason;
      } finally {
        setSending(false);
      }
    },
    [conversationId, personaId, sending],
  );

  return { events, connected, sending, error, submit, loadHistory };
}
