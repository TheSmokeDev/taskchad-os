import { Send, Loader2, Square } from 'lucide-preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { useSearch } from 'wouter-preact';
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

interface SlashCommand {
  name: string;
  description: string;
  category?: string;
}

interface RuntimeStatus {
  available: boolean;
  lane: string | null;
  provider: string | null;
  provider_display: string | null;
  model: string | null;
  selection: string | null;
  choice: string | null;
  fallback_chain: string[];
  warnings: string[];
}

interface ModelCatalogEntry {
  model: string;
  alias: string;
}

interface ModelCatalog {
  claude_native?: ModelCatalogEntry[];
  generic_runtime?: Record<string, ModelCatalogEntry[]>;
}

interface SwitchTarget {
  token: string;
  label: string;
  group: 'lane' | 'model';
}

// Lane/provider aliases from the /model command description — the switch
// itself rides the EXISTING gated chat command (`/model <target>`), never
// a new mutation HTTP route.
const LANE_SWITCH_TARGETS: SwitchTarget[] = [
  { token: 'auto', label: 'Auto lane/provider routing', group: 'lane' },
  { token: 'claude', label: 'Claude — native lane', group: 'lane' },
  { token: 'codex', label: 'Codex — generic lane', group: 'lane' },
  { token: 'gemini', label: 'Gemini — generic lane', group: 'lane' },
  { token: 'openrouter', label: 'OpenRouter — generic lane', group: 'lane' },
  { token: 'openai', label: 'OpenAI — generic lane', group: 'lane' },
];

/** Merge the /api/agents/model catalog into provider:model switch tokens
 *  (`/model claude:claude-opus-4-7`, `/model gemini:gemini-2.5-pro`). The
 *  gated chat command validates targets server-side — unknown ones get a
 *  friendly in-stream reply, never a silent switch. */
function targetsFromCatalog(catalog: ModelCatalog | null | undefined): SwitchTarget[] {
  if (!catalog) return [];
  const out: SwitchTarget[] = [];
  for (const entry of catalog.claude_native ?? []) {
    if (!entry?.model) continue;
    out.push({ token: `claude:${entry.model}`, label: `Claude · ${entry.alias || entry.model}`, group: 'model' });
  }
  for (const [provider, entries] of Object.entries(catalog.generic_runtime ?? {})) {
    for (const entry of entries ?? []) {
      if (!entry?.model) continue;
      out.push({ token: `${provider}:${entry.model}`, label: `${provider} · ${entry.alias || entry.model}`, group: 'model' });
    }
  }
  return out;
}

function runtimeLaneLabel(lane: string | null): string {
  if (lane === 'claude_native') return 'claude native';
  if (lane === 'generic_runtime') return 'generic';
  return 'auto';
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

// Curated quick-chip candidates — rendered only when the fetched registry
// confirms the command exists (fallback: the legacy /video chip).
const QUICK_COMMAND_CANDIDATES = ['/video', '/status', '/help', '/clear'];
const QUICK_COMMANDS_FALLBACK = ['/video'];
const AUTOCOMPLETE_MAX_ROWS = 8;

function streamPersonaMatches(streamPersonaId: unknown, browserPersonaId: string): boolean {
  if (!streamPersonaId) return true;
  return outboundPersonaId(String(streamPersonaId)) === browserPersonaId;
}

/** Status text that reads like tool/step output (e.g. "Read(foo.py)",
 *  "$ npm test", "→ step 2/5") gets a monospace block treatment. Pure
 *  presentation — the SSE contract is untouched. */
function looksLikeToolLine(text: string): boolean {
  return /^[\w.-]+\([^)]*\)|^\$\s|^`|^(→|>|\||\[)/m.test(text.trim());
}

export function Chat() {
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [pendingActions, setPendingActions] = useState<Set<string>>(new Set());
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [switchPending, setSwitchPending] = useState(false);
  const [catalogTargets, setCatalogTargets] = useState<SwitchTarget[]>([]);
  const [acHighlight, setAcHighlight] = useState(0);
  const [acDismissed, setAcDismissed] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  const readOnly = dashboardChatReadOnly;
  const personaId = readOnly ? chatId : DASHBOARD_CHAT_PERSONA_ID;
  const conversationId = readOnly ? 'default' : DASHBOARD_CHAT_CONVERSATION_ID;

  const historyPath = useMemo(() => {
    const params = new URLSearchParams({ conversation_id: conversationId });
    return `/api/conversation/${encodeURIComponent(personaId)}/history?${params.toString()}`;
  }, [conversationId, personaId]);

  // Deep-link prefill (?draft=...) — e.g. the Skills page composes the
  // gated `/skills promote <name>` command here. Prefill ONLY: nothing is
  // sent until the operator hits Send, so the default-deny chat gate is
  // untouched. Ignored in read-only mode.
  const search = useSearch();
  useEffect(() => {
    if (readOnly) return;
    const prefillDraft = new URLSearchParams(search).get('draft');
    if (prefillDraft) {
      setDraft(prefillDraft);
      composerRef.current?.focus();
    }
  }, [search, readOnly]);

  // Slash-command registry — fetched once on mount, fail-open to [].
  useEffect(() => {
    let cancelled = false;
    apiGet<{ commands?: SlashCommand[] }>('/api/commands')
      .then((res) => {
        if (cancelled || !Array.isArray(res.commands)) return;
        setCommands(
          res.commands.filter(
            (cmd): cmd is SlashCommand =>
              !!cmd && typeof cmd.name === 'string' && cmd.name.startsWith('/'),
          ),
        );
      })
      .catch(() => {
        // Fail-open: autocomplete simply stays empty.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Runtime status pill — identity only (lane · provider · model). Never
  // shows turns/quota/cost here: that stays LaneStatusPill's job and the
  // two are never mixed. Fail-open: fetch failure hides the pill.
  async function refreshRuntimeStatus() {
    try {
      const status = await apiGet<RuntimeStatus>('/api/runtime/status');
      setRuntimeStatus(status && typeof status.available === 'boolean' ? status : null);
    } catch {
      setRuntimeStatus(null);
    }
  }

  useEffect(() => {
    void refreshRuntimeStatus();
    const timer = window.setInterval(() => {
      void refreshRuntimeStatus();
    }, 60_000);
    return () => window.clearInterval(timer);
  }, []);

  // Switch-target catalog — /api/agents/model merged with the /model lane
  // aliases. Fail-open to the lane aliases alone.
  useEffect(() => {
    apiGet<ModelCatalog>('/api/agents/model')
      .then((catalog) => setCatalogTargets(targetsFromCatalog(catalog)))
      .catch(() => {});
  }, []);

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

  // A turn is in-flight while the latest event is a status placeholder that
  // no terminal assistant_message/error has replaced yet.
  const turnInFlight = useMemo(() => {
    const last = events[events.length - 1];
    return !!last && (last.type === 'processing' || last.type === 'progress');
  }, [events]);

  const quickCommands = useMemo(() => {
    if (commands.length === 0) return QUICK_COMMANDS_FALLBACK;
    const known = new Set(commands.map((cmd) => cmd.name));
    const chips = QUICK_COMMAND_CANDIDATES.filter((name) => known.has(name));
    return chips.length > 0 ? chips : QUICK_COMMANDS_FALLBACK;
  }, [commands]);

  // Autocomplete: only while the draft is a bare "/prefix" (no whitespace yet).
  const autocompleteMatches = useMemo(() => {
    if (readOnly || acDismissed) return [];
    if (!draft.startsWith('/') || /\s/.test(draft)) return [];
    const prefix = draft.slice(1).toLowerCase();
    return commands
      .filter((cmd) => cmd.name.slice(1).toLowerCase().startsWith(prefix))
      .slice(0, AUTOCOMPLETE_MAX_ROWS);
  }, [acDismissed, commands, draft, readOnly]);

  const showAutocomplete = autocompleteMatches.length > 0;
  const highlightIndex = Math.min(acHighlight, Math.max(0, autocompleteMatches.length - 1));

  function acceptAutocomplete(cmd: SlashCommand | undefined) {
    if (!cmd || readOnly) return;
    setDraft(`${cmd.name} `);
    setAcDismissed(true);
    composerRef.current?.focus();
  }

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
      if (text.startsWith('/model')) {
        window.setTimeout(() => {
          void refreshRuntimeStatus();
        }, 1500);
      }
    } catch (err) {
      pushToast({ tone: 'error', title: 'Message failed', description: describeApiError(err) });
    } finally {
      setSending(false);
    }
  }

  async function stopTurn() {
    if (readOnly || stopping) return;
    setStopping(true);
    try {
      await apiPost(`/api/conversation/${encodeURIComponent(DASHBOARD_CHAT_PERSONA_ID)}/stop`, {
        conversation_id: conversationId,
      });
    } catch (err) {
      pushToast({ tone: 'error', title: 'Stop failed', description: describeApiError(err) });
    } finally {
      setStopping(false);
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

  async function sendModelCommand(token: string) {
    if (readOnly || switchPending) return;
    setModelPickerOpen(false);
    setSwitchPending(true);
    try {
      // The mutation IS the existing gated chat command — same send path as
      // submitMessage; the router handles /model instantly and the reply
      // (success or friendly rejection) appears in-stream.
      await apiPost(`/api/conversation/${encodeURIComponent(DASHBOARD_CHAT_PERSONA_ID)}/send`, {
        text: `/model ${token}`,
        conversation_id: conversationId,
        client_message_id: `dash-model-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
        user_id: 'dashboard-user',
        display_name: 'Dashboard',
        source: 'interactive',
      });
      window.setTimeout(() => {
        void refreshRuntimeStatus();
      }, 1500);
    } catch (err) {
      pushToast({ tone: 'error', title: 'Model switch failed', description: describeApiError(err) });
    } finally {
      setSwitchPending(false);
    }
  }

  const switchTargets = useMemo(
    () => [...LANE_SWITCH_TARGETS, ...catalogTargets],
    [catalogTargets],
  );

  function isActiveTarget(target: SwitchTarget): boolean {
    if (!runtimeStatus?.available) return false;
    if (target.group === 'lane') return runtimeStatus.choice === target.token;
    return !!runtimeStatus.model && target.token.endsWith(`:${runtimeStatus.model}`);
  }

  function insertQuickCommand(command: string) {
    if (readOnly) return;
    setDraft(command);
    composerRef.current?.focus();
  }

  return (
    <div class="flex h-full min-h-0 flex-col">
      <TopBar
        title="Chat"
        subtitle={readOnly ? 'linked stream · read-only' : (chatStreamConnected.value ? 'dashboard chat · live' : 'dashboard chat · reconnecting')}
        actions={runtimeStatus && (
          <div class="relative">
            {/* Identity-only pill: lane · provider · model. claude_native
                gets the accent tint like LaneStatusPill; no turns/quota/cost
                ever renders here (never-mix rule). Read-only mode keeps the
                status visible but disables switching. */}
            <button
              type="button"
              disabled={readOnly || !runtimeStatus.available}
              onClick={() => setModelPickerOpen((open) => !open)}
              title={
                runtimeStatus.available
                  ? `${runtimeStatus.selection ?? 'runtime selection'} — fallback: ${runtimeStatus.fallback_chain.join(' → ')}${readOnly ? ' (read-only)' : ' · click to switch'}`
                  : 'Runtime status unavailable'
              }
              class={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[11px] tabular-nums transition-opacity disabled:cursor-default ${
                runtimeStatus.available && runtimeStatus.lane === 'claude_native'
                  ? 'bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
                  : 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]'
              } ${readOnly ? 'opacity-70' : 'hover:opacity-85'}`}
            >
              {runtimeStatus.available ? (
                <>
                  <span class="font-medium">{runtimeLaneLabel(runtimeStatus.lane)}</span>
                  <span class="opacity-60">·</span>
                  <span>{runtimeStatus.provider_display ?? 'auto'}</span>
                  <span class="opacity-60">·</span>
                  <span class="font-mono">{runtimeStatus.model ?? 'auto'}</span>
                </>
              ) : (
                <span class="opacity-70">runtime: unknown</span>
              )}
            </button>
            {modelPickerOpen && !readOnly && runtimeStatus.available && (
              <div class="absolute right-0 top-full z-30 mt-2 max-h-80 w-72 overflow-y-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-1 shadow-2xl">
                <div class="px-3 py-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
                  Switch runtime — sends /model
                </div>
                {switchTargets.map((target) => {
                  const active = isActiveTarget(target);
                  return (
                    <button
                      key={target.token}
                      type="button"
                      disabled={switchPending}
                      onClick={() => sendModelCommand(target.token)}
                      class={[
                        'flex w-full items-baseline gap-2 rounded px-3 py-1.5 text-left text-[13px] disabled:opacity-50',
                        active
                          ? 'bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
                          : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
                      ].join(' ')}
                    >
                      <span class="min-w-0 flex-1 truncate">{target.label}</span>
                      <span class="shrink-0 font-mono text-[11px] opacity-70">/model {target.token}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        )}
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
                {ev.text && (ev.type === 'processing' || ev.type === 'progress') ? (
                  <div class="flex items-start gap-2">
                    <Loader2 size={12} class="mt-[3px] shrink-0 animate-spin opacity-70" />
                    <div
                      class={`min-w-0 flex-1 whitespace-pre-wrap font-mono text-[12px] leading-relaxed ${
                        looksLikeToolLine(ev.text)
                          ? 'rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1'
                          : ''
                      }`}
                    >
                      {ev.text}
                    </div>
                  </div>
                ) : ev.text ? (
                  <div
                    class="text-[13px] leading-relaxed prose-sm"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(ev.text) }}
                  />
                ) : null}
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
        class="border-t border-[var(--color-border)] bg-[var(--color-bg)] composer-safe"
        onSubmit={(event) => {
          event.preventDefault();
          submitMessage();
        }}
      >
        <div class="mx-auto flex max-w-4xl flex-col gap-2">
          {!readOnly && (
            <div class="flex flex-wrap gap-2">
              {quickCommands.map((command) => (
                <button
                  key={command}
                  type="button"
                  onClick={() => insertQuickCommand(command)}
                  class="inline-flex h-7 items-center rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-2.5 font-mono text-[12px] text-[var(--color-text-muted)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-text)]"
                >
                  {command}
                </button>
              ))}
            </div>
          )}
          <div class="relative flex items-end gap-2">
          {showAutocomplete && (
            <div class="absolute bottom-full left-0 right-0 z-20 mb-2 max-h-64 overflow-y-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-1 shadow-2xl">
              {autocompleteMatches.map((cmd, idx) => {
                const isActive = idx === highlightIndex;
                return (
                  <button
                    key={cmd.name}
                    type="button"
                    onClick={() => acceptAutocomplete(cmd)}
                    onMouseEnter={() => setAcHighlight(idx)}
                    class={[
                      'flex w-full items-baseline gap-2 rounded px-3 py-1.5 text-left text-[13px]',
                      isActive
                        ? 'bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
                        : 'text-[var(--color-text-muted)]',
                    ].join(' ')}
                  >
                    <span class="shrink-0 font-mono">{cmd.name}</span>
                    <span class="min-w-0 flex-1 truncate text-[11px] opacity-70">{cmd.description}</span>
                    {cmd.category && (
                      <span class="shrink-0 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
                        {cmd.category}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
          <textarea
            ref={composerRef}
            value={draft}
            disabled={readOnly}
            onInput={(event) => {
              setDraft((event.currentTarget as HTMLTextAreaElement).value);
              setAcDismissed(false);
              setAcHighlight(0);
            }}
            onKeyDown={(event) => {
              if (showAutocomplete) {
                if (event.key === 'ArrowDown') {
                  event.preventDefault();
                  setAcHighlight((h) => Math.min(autocompleteMatches.length - 1, h + 1));
                  return;
                }
                if (event.key === 'ArrowUp') {
                  event.preventDefault();
                  setAcHighlight((h) => Math.max(0, h - 1));
                  return;
                }
                if (event.key === 'Enter' || event.key === 'Tab') {
                  event.preventDefault();
                  acceptAutocomplete(autocompleteMatches[highlightIndex]);
                  return;
                }
                if (event.key === 'Escape') {
                  event.preventDefault();
                  setAcDismissed(true);
                  return;
                }
              }
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                submitMessage();
              }
            }}
            rows={1}
            placeholder={readOnly ? 'Linked stream is read-only' : 'Message Homie or type /provider'}
            class="min-h-10 max-h-36 flex-1 resize-none rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none transition-colors placeholder:text-[var(--color-text-faint)] focus:border-[var(--color-accent)] disabled:opacity-60"
          />
          {!readOnly && turnInFlight && (
            <button
              type="button"
              onClick={stopTurn}
              disabled={stopping}
              class="inline-flex h-10 shrink-0 items-center gap-1.5 rounded-md border border-[color-mix(in_srgb,var(--color-status-failed)_50%,transparent)] bg-[color-mix(in_srgb,var(--color-status-failed)_12%,transparent)] px-3 text-[12px] font-medium text-[var(--color-text)] transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-45"
              title="Stop the in-flight turn"
            >
              {stopping ? <Loader2 size={13} class="animate-spin" /> : <Square size={13} />}
              Stop
            </button>
          )}
          <button
            type="submit"
            disabled={readOnly || sending || !draft.trim()}
            class="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-[var(--color-accent)] text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-45"
            title="Send"
          >
            {sending ? <Loader2 size={16} class="animate-spin" /> : <Send size={16} />}
          </button>
          </div>
        </div>
      </form>
    </div>
  );
}
