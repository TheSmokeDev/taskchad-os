import { signal } from '@preact/signals';
import { tokenizedSseUrl, dashboardToken, chatId } from './api';

// SSE consumer for /api/conversation/<id>/stream.
//
// Browser-native EventSource handles Last-Event-ID resume automatically:
// on reconnect, it sets the `Last-Event-ID` header to the id of the last
// `event_id`-stamped message it saw. The Hono server (per WS3 spec)
// preserves Python's monotonic int event_id, emits `: keepalive\n\n` every
// 20s, and returns 410 Gone with `X-Refetch-Hint` on stale ids.
//
// 410 Gone handling: EventSource has no native "got 410" hook — the
// connection just errors. We detect 410 via the `error` event AND a
// preceding fetch HEAD probe; on confirmed 410 we full-refetch the
// conversation history then re-open the EventSource (it will start
// without a Last-Event-ID since the old one was rejected).

export const chatUnread = signal(0);
export const chatStreamConnected = signal(false);

type Listener = (eventName: string, data: any) => void;
const listeners = new Set<Listener>();

export function subscribeChatStream(fn: Listener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function resetUnread() { chatUnread.value = 0; }

let started = false;
let currentConversationId: string | null = null;

/** Start the global chat SSE for the lifetime of the page. Idempotent. */
export function startChatStream(conversationId?: string): void {
  // No token → can't auth → don't open SSE. Pages just won't get live
  // updates; manual refresh still works.
  if (!dashboardToken) return;

  // Resolve conversation id: explicit param > query chatId > skip.
  const targetId = conversationId || chatId;
  if (!targetId) return;

  // Already started for this conversation — no-op.
  if (started && currentConversationId === targetId) return;

  started = true;
  currentConversationId = targetId;

  let es: EventSource | null = null;
  let activeRoute = window.location.pathname;
  let lastReceivedId: string | null = null;
  let reconnectAttempt = 0;

  function reactToRoute() { activeRoute = window.location.pathname; }
  window.addEventListener('popstate', reactToRoute);

  // Wouter pushes via history.pushState; patch to fire popstate.
  const origPush = history.pushState;
  history.pushState = function (...args: any[]) {
    const ret = origPush.apply(this as any, args as any);
    reactToRoute();
    return ret;
  };

  function open() {
    if (es) return;

    const sseUrl = tokenizedSseUrl(`/api/conversation/${encodeURIComponent(targetId)}/stream`);
    es = new EventSource(sseUrl);

    es.onopen = () => {
      chatStreamConnected.value = true;
      reconnectAttempt = 0;
    };

    es.onerror = async () => {
      chatStreamConnected.value = false;

      // EventSource auto-reconnects natively. We use this hook only to
      // detect 410 Gone (last-event-id stale → buffer rotated past us).
      // We probe the same URL with a HEAD; a real 410 means full refetch.
      reconnectAttempt += 1;
      if (reconnectAttempt >= 3) {
        const probe = await probeFor410(sseUrl);
        if (probe?.gone) {
          // Force a full refetch by closing + reopening the EventSource.
          // The new connection has no Last-Event-ID so it starts fresh.
          // Pages that need history can listen for `refetch_hint`.
          for (const l of listeners) {
            try { l('refetch_hint', { reason: probe.hint }); } catch {}
          }
          es?.close();
          es = null;
          lastReceivedId = null;
          reconnectAttempt = 0;
          // Reopen on next tick.
          setTimeout(open, 100);
        }
      }
    };

    const dispatch = (eventName: string) => (ev: MessageEvent) => {
      if (ev.lastEventId) lastReceivedId = ev.lastEventId;
      let data: any;
      try { data = JSON.parse(ev.data); } catch { return; }
      // Bump unread when an assistant message arrives and we're not on /chat.
      if (eventName === 'assistant_message' && !activeRoute.startsWith('/chat')) {
        chatUnread.value = chatUnread.value + 1;
      }
      for (const l of listeners) {
        try { l(eventName, data); } catch (err) { console.error('chat listener', err); }
      }
    };

    es.addEventListener('user_message', dispatch('user_message'));
    es.addEventListener('assistant_message', dispatch('assistant_message'));
    es.addEventListener('assistant_photo', dispatch('assistant_photo'));
    es.addEventListener('processing', dispatch('processing'));
    es.addEventListener('progress', dispatch('progress'));
    es.addEventListener('error', dispatch('error') as any);
  }

  open();
}

/** HEAD-probe the SSE URL to check for 410 Gone. Used after repeated
 *  reconnect failures to distinguish "server is down" from "buffer
 *  rotated past our Last-Event-ID." */
async function probeFor410(url: string): Promise<{ gone: boolean; hint?: string } | null> {
  try {
    // Use GET (HEAD on SSE often gets 405). Abort immediately.
    const ctrl = new AbortController();
    const res = await fetch(url, { method: 'GET', signal: ctrl.signal });
    ctrl.abort();
    if (res.status === 410) {
      return {
        gone: true,
        hint: res.headers.get('X-Refetch-Hint') || 'buffer-rotated',
      };
    }
    return { gone: false };
  } catch {
    return null;
  }
}
