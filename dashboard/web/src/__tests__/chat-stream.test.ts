import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

type EventHandler = ((event?: unknown) => void | Promise<void>) | null;

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  onopen: EventHandler = null;
  onerror: EventHandler = null;
  readonly listeners = new Map<string, (event: MessageEvent) => void>();
  readonly url: string;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(eventName: string, handler: (event: MessageEvent) => void): void {
    this.listeners.set(eventName, handler);
  }

  close(): void {
    this.closed = true;
  }
}

describe('chat-stream', () => {
  const originalPushState = history.pushState;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.resetModules();
    FakeEventSource.instances = [];
    history.pushState = originalPushState;
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  afterEach(() => {
    history.pushState = originalPushState;
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  test('410 stream probe emits refetch hint and reopens without page refresh', async () => {
    const fetchMock = vi.fn(async () => new Response('', {
      status: 410,
      headers: { 'X-Refetch-Hint': 'buffer-rotated' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    const events: Array<{ eventName: string; data: any }> = [];
    const { startChatStream, subscribeChatStream } = await import('@/lib/chat-stream');
    const unsubscribe = subscribeChatStream((eventName, data) => {
      events.push({ eventName, data });
    });

    startChatStream('main', 'dashboard-main');
    expect(FakeEventSource.instances).toHaveLength(1);
    const firstSource = FakeEventSource.instances[0];

    await firstSource.onerror?.({ type: 'error' });
    await firstSource.onerror?.({ type: 'error' });
    await firstSource.onerror?.({ type: 'error' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(events).toContainEqual({
      eventName: 'refetch_hint',
      data: { reason: 'buffer-rotated' },
    });
    expect(firstSource.closed).toBe(true);

    await vi.runOnlyPendingTimersAsync();

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toContain('/api/conversation/main/stream');
    unsubscribe();
  });
});
