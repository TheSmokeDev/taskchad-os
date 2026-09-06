import type { ChatEvent, ChatEventType, HistoryTurn } from '@/types';

export const CHAT_EVENT_NAMES = [
  'user_message',
  'assistant_message',
  'processing',
  'progress',
  'error',
] as const satisfies readonly ChatEventType[];

export function eventFromHistory(turn: HistoryTurn): ChatEvent {
  const parsed = turn.created_at ? Date.parse(turn.created_at) / 1000 : Number.NaN;
  const fallback = Number.isFinite(parsed) ? parsed : Date.now() / 1000;
  return {
    id: `history-${turn.id}`,
    type: turn.role === 'user' ? 'user_message' : 'assistant_message',
    text: typeof turn.content === 'string' ? turn.content : '',
    timestamp: Number.isFinite(turn.timestamp) ? Number(turn.timestamp) : fallback,
    components: [],
  };
}

export function eventFromStream(eventName: string, payload: unknown): ChatEvent | null {
  if (!CHAT_EVENT_NAMES.includes(eventName as ChatEventType)) return null;
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const data = payload as Record<string, unknown>;
  const eventId = data.event_id ?? data.last_event_id ?? `${Date.now()}`;
  const replaces = data.replaces_event_id;
  const components = Array.isArray(data.components)
    ? data.components.filter(
        (value): value is ChatEvent['components'][number] =>
          Boolean(
            value &&
              typeof value === 'object' &&
              typeof (value as Record<string, unknown>).label === 'string' &&
              typeof (value as Record<string, unknown>).custom_id === 'string',
          ),
      )
    : [];
  const rawTimestamp = Number(data.timestamp);
  return {
    id: String(eventId),
    type: eventName as ChatEventType,
    text:
      typeof data.text === 'string'
        ? data.text
        : typeof data.content === 'string'
          ? data.content
          : '',
    timestamp: Number.isFinite(rawTimestamp) ? rawTimestamp : Date.now() / 1000,
    components,
    ...(replaces !== undefined ? { replacesEventId: String(replaces) } : {}),
  };
}

export function mergeChatEvent(previous: ChatEvent[], incoming: ChatEvent): ChatEvent[] {
  const targetId = incoming.replacesEventId ?? incoming.id;
  const index = previous.findIndex((event) => event.id === targetId);
  if (index < 0) return [...previous, { ...incoming, id: targetId }];
  const next = [...previous];
  next[index] = { ...incoming, id: targetId };
  return next;
}

export function personaMatches(streamPersona: unknown, browserPersona: string): boolean {
  if (typeof streamPersona !== 'string' || !streamPersona) return true;
  if (browserPersona === 'main') return streamPersona === 'main' || streamPersona === 'default';
  return streamPersona === browserPersona;
}
