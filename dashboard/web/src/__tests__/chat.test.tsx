import { fireEvent, render, screen, waitFor, act } from '@testing-library/preact';
import { describe, expect, test, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Chat } from '@/pages/Chat';

const streamMock = vi.hoisted(() => ({
  listener: null as null | ((eventName: string, data: any) => void),
}));

vi.mock('@/lib/chat-stream', () => ({
  chatStreamConnected: { value: true },
  resetUnread: vi.fn(),
  startChatStream: vi.fn(),
  subscribeChatStream: vi.fn((fn: (eventName: string, data: any) => void) => {
    streamMock.listener = fn;
    return () => {};
  }),
}));

describe('dashboard chat', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    streamMock.listener = null;
    sessionStorage.clear();
    globalThis.fetch = vi.fn(async (input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/history')) {
        return new Response(JSON.stringify({ turns: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (url.includes('/send')) {
        return new Response(JSON.stringify({ ok: true, queued: true }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (url.includes('/api/runtime/status')) {
        return new Response(
          JSON.stringify({
            available: true,
            lane: 'claude_native',
            provider: 'claude',
            provider_display: 'Claude',
            model: 'claude-opus-4-8',
            selection: 'Claude native lane',
            choice: 'claude',
            fallback_chain: ['claude', 'openai_codex', 'gemini', 'openrouter', 'openai'],
            warnings: [],
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        );
      }
      if (url.includes('/api/agents/model')) {
        return new Response(
          JSON.stringify({
            claude_native: [{ model: 'claude-opus-4-7', alias: 'Opus 4.7' }],
            generic_runtime: { gemini: [{ model: 'gemini-2.5-pro', alias: 'Gemini 2.5 Pro' }] },
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        );
      }
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
    }) as any;
  });

  test('Chat page is no longer Telegram-only read-only copy', () => {
    const src = readFileSync(join(__dirname, '..', 'pages', 'Chat.tsx'), 'utf-8');
    expect(src).not.toContain('Send messages in Telegram');
    expect(src).toContain('/api/conversation/');
    expect(src).toContain('conversation_id');
  });

  test('composer posts dashboard messages to the conversation send route', async () => {
    render(<Chat />);

    const textarea = await screen.findByPlaceholderText('Message Homie or type /provider');
    fireEvent.input(textarea, { target: { value: '/provider' } });
    fireEvent.click(screen.getByTitle('Send'));

    await waitFor(() => {
      const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
      const sendCall = calls.find(([url]) => String(url).includes('/api/conversation/main/send'));
      expect(sendCall).toBeTruthy();
      const body = JSON.parse((sendCall?.[1] as RequestInit).body as string);
      expect(body.text).toBe('/provider');
      expect(body.conversation_id).toBe('dashboard-main');
    });
  });

  test('composer preserves LinkedIn draft slash command text', async () => {
    render(<Chat />);

    const textarea = await screen.findByPlaceholderText('Message Homie or type /provider');
    fireEvent.input(textarea, {
      target: { value: '/linkedin draft a post about AI operators. Do not post.' },
    });
    fireEvent.click(screen.getByTitle('Send'));

    await waitFor(() => {
      const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
      const sendCall = calls.find(([url]) => String(url).includes('/api/conversation/main/send'));
      expect(sendCall).toBeTruthy();
      const body = JSON.parse((sendCall?.[1] as RequestInit).body as string);
      expect(body.text).toBe('/linkedin draft a post about AI operators. Do not post.');
      expect(body.conversation_id).toBe('dashboard-main');
    });
  });

  test('video quick command sends /video through dashboard chat', async () => {
    render(<Chat />);

    fireEvent.click(await screen.findByText('/video'));
    fireEvent.click(screen.getByTitle('Send'));

    await waitFor(() => {
      const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
      const sendCall = calls.find(([url]) => String(url).includes('/api/conversation/main/send'));
      expect(sendCall).toBeTruthy();
      const body = JSON.parse((sendCall?.[1] as RequestInit).body as string);
      expect(body.text).toBe('/video');
      expect(body.conversation_id).toBe('dashboard-main');
    });
  });

  test('router action buttons post button_custom_id back through chat send', async () => {
    render(<Chat />);

    await waitFor(() => expect(streamMock.listener).toBeTruthy());
    act(() => {
      streamMock.listener?.('assistant_message', {
        event_id: 42,
        text: 'How should I apply this follow-up?',
        timestamp: Date.now() / 1000,
        components: [
          { label: 'Queue Next', custom_id: 'turn_queue:abc', style: 'secondary' },
          { label: 'Steer Current', custom_id: 'turn_steer:abc', style: 'primary' },
        ],
      });
    });

    fireEvent.click(await screen.findByText('Steer Current'));

    await waitFor(() => {
      const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
      const sendCall = calls.find(([, init]) => {
        if (!init?.body) return false;
        return JSON.parse(init.body as string).button_custom_id === 'turn_steer:abc';
      });
      expect(sendCall).toBeTruthy();
    });
  });

  test('progress updates replace one placeholder instead of stacking status bubbles', async () => {
    render(<Chat />);

    await waitFor(() => expect(streamMock.listener).toBeTruthy());
    act(() => {
      streamMock.listener?.('processing', {
        event_id: 10,
        text: 'Thinking...',
        timestamp: Date.now() / 1000,
      });
      streamMock.listener?.('progress', {
        event_id: 11,
        replaces_event_id: 10,
        text: 'Working... (12s)',
        timestamp: Date.now() / 1000,
      });
      streamMock.listener?.('progress', {
        event_id: 12,
        replaces_event_id: 10,
        text: 'Working... (24s)',
        timestamp: Date.now() / 1000,
      });
      streamMock.listener?.('assistant_message', {
        event_id: 13,
        replaces_event_id: 10,
        text: 'Done.',
        timestamp: Date.now() / 1000,
      });
    });

    expect(screen.queryByText('Thinking...')).toBeNull();
    expect(screen.queryByText('Working... (12s)')).toBeNull();
    expect(screen.queryByText('Working... (24s)')).toBeNull();
    expect(await screen.findByText('Done.')).toBeTruthy();
    expect(document.body.textContent?.match(/homie/g)).toHaveLength(1);
  });

  test('stream ignores other conversations and blank status events', async () => {
    render(<Chat />);

    await waitFor(() => expect(streamMock.listener).toBeTruthy());
    act(() => {
      streamMock.listener?.('processing', {
        event_id: 20,
        persona_id: 'main',
        conversation_id: 'dashboard-main',
        text: '',
        timestamp: Date.now() / 1000,
      });
      streamMock.listener?.('assistant_message', {
        event_id: 21,
        persona_id: 'other',
        conversation_id: 'dashboard-main',
        text: 'Wrong persona',
        timestamp: Date.now() / 1000,
      });
      streamMock.listener?.('assistant_message', {
        event_id: 22,
        persona_id: 'main',
        conversation_id: 'other-chat',
        text: 'Wrong conversation',
        timestamp: Date.now() / 1000,
      });
      streamMock.listener?.('assistant_message', {
        event_id: 23,
        persona_id: 'main',
        conversation_id: 'dashboard-main',
        text: 'Right conversation',
        timestamp: Date.now() / 1000,
      });
    });

    expect(screen.queryByText('Wrong persona')).toBeNull();
    expect(screen.queryByText('Wrong conversation')).toBeNull();
    expect(await screen.findByText('Right conversation')).toBeTruthy();
  });

  test('stream accepts Python canonical default persona for dashboard main chat', async () => {
    render(<Chat />);

    await waitFor(() => expect(streamMock.listener).toBeTruthy());
    act(() => {
      streamMock.listener?.('assistant_message', {
        event_id: 24,
        persona_id: 'default',
        conversation_id: 'dashboard-main',
        text: 'Live SSE reached dashboard main.',
        timestamp: Date.now() / 1000,
      });
    });

    expect(await screen.findByText('Live SSE reached dashboard main.')).toBeTruthy();
  });

  test('runtime pill renders identity (lane · provider · model) from /api/runtime/status', async () => {
    render(<Chat />);

    expect(await screen.findByText('claude native')).toBeTruthy();
    expect(await screen.findByText('Claude')).toBeTruthy();
    expect(await screen.findByText('claude-opus-4-8')).toBeTruthy();
    // Identity only — never-mix rule: no turns/quota/cost text in the pill.
    expect(document.body.textContent).not.toContain('turns');
    expect(document.body.textContent).not.toContain('quota');
  });

  test('choosing a switch target sends /model <target> through the chat send path', async () => {
    render(<Chat />);

    fireEvent.click(await screen.findByText('claude native'));
    fireEvent.click(await screen.findByText('Auto lane/provider routing'));

    await waitFor(() => {
      const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
      const sendCall = calls.find(([url, init]) => {
        if (!String(url).includes('/api/conversation/main/send') || !init?.body) return false;
        return JSON.parse(init.body as string).text === '/model auto';
      });
      expect(sendCall).toBeTruthy();
      const body = JSON.parse((sendCall?.[1] as RequestInit).body as string);
      expect(body.conversation_id).toBe('dashboard-main');
      expect(body.source).toBe('interactive');
    });
  });

  test('switch dropdown merges catalog targets with lane aliases', async () => {
    render(<Chat />);

    fireEvent.click(await screen.findByText('claude native'));
    expect(await screen.findByText('Claude · Opus 4.7')).toBeTruthy();
    expect(await screen.findByText('gemini · Gemini 2.5 Pro')).toBeTruthy();
    expect(screen.getByText('/model gemini:gemini-2.5-pro')).toBeTruthy();
  });

  test('read-only mode keeps the pill visible but disables switching (source contract)', () => {
    const src = readFileSync(join(__dirname, '..', 'pages', 'Chat.tsx'), 'utf-8');
    // The pill button is disabled in read-only mode…
    expect(src).toMatch(/disabled=\{readOnly \|\| !runtimeStatus\.available\}/);
    // …the dropdown never renders read-only…
    expect(src).toMatch(/modelPickerOpen && !readOnly && runtimeStatus\.available/);
    // …and the send function itself is gated (defense in depth).
    expect(src).toMatch(/async function sendModelCommand[\s\S]*?if \(readOnly \|\| switchPending\) return;/);
    // No new mutation HTTP route: the switch composes the /model chat command.
    expect(src).toContain('`/model ${token}`');
  });
});
