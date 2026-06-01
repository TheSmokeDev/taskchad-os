/**
 * Cabinet page — Homie-native multi-persona room.
 *
 * NOT a port of WarRoom.tsx. Opens the current browser Cabinet room on mount, keeps the historical
 * meeting list available, streams live room events, and exposes participant
 * controls backed by the Python room state contract.
 */

import { useEffect, useMemo, useState } from 'preact/hooks';
import { Mic, Pin, PinOff, Plus, Trash2 } from 'lucide-preact';
import { apiGet, apiPost, chatId as dashboardChatId } from '@/lib/api';
import { CabinetComposer } from '@/components/CabinetComposer';
import { CabinetTranscript } from '@/components/CabinetTranscript';
import { cabinetVoiceUrl, openCabinetVoiceUrl } from '@/lib/cabinet-voice-url';
import {
  fetchCabinetTranscripts,
  openCabinetStream,
  type CabinetEvent,
} from '@/lib/cabinet-stream';

const CABINET_CHAT_ID = dashboardChatId || 'cabinet-browser';

interface CabinetMeetingRow {
  id: number;
  started_at: number;
  ended_at: number | null;
  pinned_persona: string | null;
  entry_count: number;
  title: string | null;
  chat_id: string;
}

interface RosterAgent {
  id: string;
  name: string;
  description: string;
}

interface MeetingDetails {
  meeting: CabinetMeetingRow;
  roster: RosterAgent[];
  agents?: RosterAgent[];
  broadcastOrder?: string[];
  pinnedAgent: string | null;
  status: 'open' | 'ended';
}

interface OpenRoomResponse extends MeetingDetails {
  meetingId: number;
  created: boolean;
}

interface TranscriptRow {
  id: number;
  speaker: string;
  text: string;
  created_at: number;
}

function mergeStateEvent(details: MeetingDetails, event: CabinetEvent): MeetingDetails {
  if (event.type === 'meeting_ended') {
    return { ...details, status: 'ended', meeting: { ...details.meeting, ended_at: Number(event.at ?? Date.now() / 1000) } };
  }
  if (event.type !== 'meeting_state' && event.type !== 'meeting_state_update') {
    return details;
  }
  const next: MeetingDetails = { ...details };
  if (Array.isArray(event.agents)) {
    next.roster = event.agents as RosterAgent[];
    next.agents = event.agents as RosterAgent[];
  }
  if (Array.isArray(event.broadcastOrder)) {
    next.broadcastOrder = event.broadcastOrder as string[];
  }
  if ('pinnedAgent' in event) {
    next.pinnedAgent = typeof event.pinnedAgent === 'string' ? event.pinnedAgent : null;
  }
  return next;
}

export function Cabinet() {
  const [meetings, setMeetings] = useState<CabinetMeetingRow[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [details, setDetails] = useState<MeetingDetails | null>(null);
  const [baseline, setBaseline] = useState<TranscriptRow[]>([]);
  const [liveEvents, setLiveEvents] = useState<Array<{ seq: number; event: CabinetEvent }>>([]);
  const [available, setAvailable] = useState<RosterAgent[]>([]);
  const [selectedAgent, setSelectedAgent] = useState('');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const roster = details?.roster ?? [];
  const pinnedAgent = details?.pinnedAgent ?? null;
  const isEnded = details?.status === 'ended';
  const activeVoiceUrl = activeId !== null ? cabinetVoiceUrl(activeId, CABINET_CHAT_ID) : '';
  const selectedAvailable = useMemo(
    () => available.find((agent) => agent.id === selectedAgent) ?? available[0],
    [available, selectedAgent],
  );

  async function refreshList() {
    try {
      const res = await apiGet<{ meetings: CabinetMeetingRow[] }>(
        `/api/cabinet/list?limit=20&chatId=${encodeURIComponent(CABINET_CHAT_ID)}`,
      );
      setMeetings(res.meetings);
    } catch (err) {
      console.error('cabinet list failed', err);
    }
  }

  async function refreshAvailable(meetingId: number) {
    try {
      const qs = new URLSearchParams({ meetingId: String(meetingId), chatId: CABINET_CHAT_ID });
      const res = await apiGet<{ agents: RosterAgent[] }>(`/api/cabinet/participants/available?${qs.toString()}`);
      setAvailable(res.agents);
      setSelectedAgent((current) => current || res.agents[0]?.id || '');
    } catch (err) {
      console.error('cabinet available participants failed', err);
      setAvailable([]);
    }
  }

  async function openCurrentRoom() {
    setLoading(true);
    try {
      const res = await apiPost<OpenRoomResponse>('/api/cabinet/open', { chatId: CABINET_CHAT_ID });
      setActiveId(res.meetingId);
      setDetails(res);
      await refreshList();
      await refreshAvailable(res.meetingId);
    } catch (err) {
      console.error('cabinet open failed', err);
    } finally {
      setLoading(false);
    }
  }

  async function loadRoom(meetingId: number) {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ meetingId: String(meetingId), chatId: CABINET_CHAT_ID });
      const det = await apiGet<MeetingDetails>(`/api/cabinet/details?${qs.toString()}`);
      setDetails(det);
      await refreshAvailable(meetingId);
      const trx = await fetchCabinetTranscripts(meetingId, undefined, CABINET_CHAT_ID);
      setBaseline(trx.transcript);
      setLiveEvents([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void openCurrentRoom();
    void apiPost('/api/cabinet/warmup').catch(() => {});
  }, []);

  useEffect(() => {
    if (activeId === null) return;
    let cancelled = false;
    let stream: ReturnType<typeof openCabinetStream> | null = null;

    (async () => {
      try {
        const trx = await fetchCabinetTranscripts(activeId, undefined, CABINET_CHAT_ID);
        if (cancelled) return;
        setBaseline(trx.transcript);
        setLiveEvents([]);
        stream = openCabinetStream({
          meetingId: activeId,
          chatId: CABINET_CHAT_ID,
          onEvent: (event, seq) => {
            setLiveEvents((prev) => [...prev, { seq, event }]);
            setDetails((current) => current ? mergeStateEvent(current, event) : current);
            if (event.type === 'meeting_state_update') {
              void refreshAvailable(activeId);
            }
          },
          onRefetchHint: () => {
            void fetchCabinetTranscripts(activeId, undefined, CABINET_CHAT_ID).then((t) => setBaseline(t.transcript));
          },
        });
      } catch (err) {
        console.error('cabinet open meeting failed', err);
      }
    })();

    return () => {
      cancelled = true;
      if (stream) stream.close();
    };
  }, [activeId]);

  async function newMeeting() {
    setBusy(true);
    try {
      const res = await apiPost<{ meetingId: number }>('/api/cabinet/new', { chatId: CABINET_CHAT_ID });
      await refreshList();
      setActiveId(res.meetingId);
      await loadRoom(res.meetingId);
    } finally {
      setBusy(false);
    }
  }

  async function endMeeting() {
    if (activeId === null) return;
    setBusy(true);
    try {
      await apiPost('/api/cabinet/end', { meetingId: activeId, chatId: CABINET_CHAT_ID });
      await refreshList();
      setDetails((current) => current ? { ...current, status: 'ended' } : current);
    } finally {
      setBusy(false);
    }
  }

  async function addParticipant() {
    if (activeId === null || !selectedAvailable) return;
    setBusy(true);
    try {
      const res = await apiPost<MeetingDetails>('/api/cabinet/participants/add', {
        meetingId: activeId,
        agentId: selectedAvailable.id,
        chatId: CABINET_CHAT_ID,
      });
      setDetails((current) => current ? { ...current, roster: res.roster, agents: res.roster } : current);
      await refreshAvailable(activeId);
    } finally {
      setBusy(false);
    }
  }

  async function removeParticipant(agentId: string) {
    if (activeId === null) return;
    setBusy(true);
    try {
      const res = await apiPost<MeetingDetails>('/api/cabinet/participants/remove', {
        meetingId: activeId,
        agentId,
        chatId: CABINET_CHAT_ID,
      });
      setDetails((current) => current ? {
        ...current,
        roster: res.roster,
        agents: res.roster,
        pinnedAgent: res.pinnedAgent ?? null,
      } : current);
      await refreshAvailable(activeId);
    } finally {
      setBusy(false);
    }
  }

  async function pinParticipant(agentId: string | null) {
    if (activeId === null) return;
    setBusy(true);
    try {
      const path = agentId ? '/api/cabinet/pin' : '/api/cabinet/unpin';
      const body = agentId
        ? { meetingId: activeId, agentId, chatId: CABINET_CHAT_ID }
        : { meetingId: activeId, chatId: CABINET_CHAT_ID };
      const res = await apiPost<{ pinnedAgent: string | null }>(path, body);
      setDetails((current) => current ? { ...current, pinnedAgent: res.pinnedAgent } : current);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="flex flex-1 min-h-0">
      <div class="w-72 border-r border-[var(--color-border)] flex flex-col">
        <div class="p-3 border-b border-[var(--color-border)] flex gap-2">
          <button
            type="button"
            onClick={() => void openCurrentRoom()}
            class="flex-1 px-3 py-2 bg-[var(--color-primary)] text-white rounded-md text-sm font-medium"
            disabled={busy}
          >
            Current
          </button>
          <button
            type="button"
            onClick={() => void newMeeting()}
            class="w-10 inline-flex items-center justify-center bg-[var(--color-card)] border border-[var(--color-border)] rounded-md"
            disabled={busy}
            title="New room"
          >
            <Plus size={16} />
          </button>
        </div>
        <div class="flex-1 overflow-y-auto">
          {meetings.length === 0 && (
            <div class="px-3 py-4 text-xs text-[var(--color-text-muted)]">
              No meetings yet.
            </div>
          )}
          {meetings.map((m) => (
            <button
              key={m.id}
              type="button"
              onClick={() => {
                setActiveId(m.id);
                void loadRoom(m.id);
              }}
              class={`block w-full text-left px-3 py-2 border-b border-[var(--color-border)] hover:bg-[var(--color-hover)] text-sm ${
                activeId === m.id ? 'bg-[var(--color-hover)]' : ''
              }`}
            >
              <div class="font-medium truncate">{m.title || `Room #${m.id}`}</div>
              <div class="text-xs text-[var(--color-text-muted)]">
                {m.entry_count} entries / {m.ended_at ? 'ended' : 'open'}
              </div>
            </button>
          ))}
        </div>
      </div>

      <div class="flex-1 flex flex-col min-w-0">
        <div class="border-b border-[var(--color-border)] px-4 py-2">
          <div class="flex items-center justify-between gap-3">
            <div>
              <div class="text-sm font-medium">
                {details?.meeting?.title || (activeId ? `Room #${activeId}` : 'Cabinet')}
              </div>
              <div class="text-xs text-[var(--color-text-muted)]">
                {roster.length} homies{pinnedAgent ? ` / pinned @${pinnedAgent}` : ''}
              </div>
            </div>
            <div class="flex items-center gap-2">
              <button
                type="button"
                onClick={() => {
                  if (activeVoiceUrl) openCabinetVoiceUrl(activeVoiceUrl);
                }}
                class="w-9 h-8 inline-flex items-center justify-center rounded-md border border-[var(--color-border)]"
                title="Voice"
                disabled={!activeId || isEnded}
              >
                <Mic size={15} />
              </button>
              <button
                type="button"
                onClick={() => void endMeeting()}
                class="px-3 py-1 bg-red-500/20 text-red-500 rounded-md text-xs font-medium hover:bg-red-500/30"
                disabled={!activeId || isEnded || busy}
              >
                End
              </button>
            </div>
          </div>
          {activeId !== null && (
            <div class="mt-2 flex flex-wrap items-center gap-2">
              {roster.map((agent) => (
                <div
                  key={agent.id}
                  class="inline-flex items-center gap-1 border border-[var(--color-border)] rounded-md px-2 py-1 text-xs"
                >
                  <span class="font-mono">@{agent.id}</span>
                  <button
                    type="button"
                    title={pinnedAgent === agent.id ? 'Unpin' : 'Pin'}
                    onClick={() => void pinParticipant(pinnedAgent === agent.id ? null : agent.id)}
                    class="w-5 h-5 inline-flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                    disabled={busy || isEnded}
                  >
                    {pinnedAgent === agent.id ? <PinOff size={13} /> : <Pin size={13} />}
                  </button>
                  {agent.id !== 'main' && (
                    <button
                      type="button"
                      title="Remove"
                      onClick={() => void removeParticipant(agent.id)}
                      class="w-5 h-5 inline-flex items-center justify-center text-red-500"
                      disabled={busy || isEnded}
                    >
                      <Trash2 size={13} />
                    </button>
                  )}
                </div>
              ))}
              {available.length > 0 && (
                <div class="inline-flex items-center gap-1">
                  <select
                    value={selectedAvailable?.id ?? ''}
                    onChange={(ev) => setSelectedAgent((ev.target as HTMLSelectElement).value)}
                    class="h-7 bg-[var(--color-input)] border border-[var(--color-border)] rounded-md text-xs px-2"
                    disabled={busy || isEnded}
                  >
                    {available.map((agent) => (
                      <option key={agent.id} value={agent.id}>@{agent.id}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => void addParticipant()}
                    class="w-7 h-7 inline-flex items-center justify-center bg-[var(--color-primary)] text-white rounded-md"
                    title="Add"
                    disabled={busy || isEnded}
                  >
                    <Plus size={14} />
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
        {loading ? (
          <div class="flex-1 flex items-center justify-center text-[var(--color-text-muted)]">
            Loading...
          </div>
        ) : (
          <CabinetTranscript baselineRows={baseline} liveEvents={liveEvents} />
        )}
        {activeId !== null && (
          <CabinetComposer
            meetingId={activeId}
            roster={roster}
            chatId={CABINET_CHAT_ID}
            disabled={isEnded}
          />
        )}
      </div>
    </div>
  );
}
