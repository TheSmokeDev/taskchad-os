/**
 * Cabinet page — multi-persona meeting room (PRD-8 Phase 5a / WS4.1).
 *
 * HOMIE-NATIVE — built on the Phase 3 SSE pattern
 * (`.claude/scripts/dashboard_api.py:1825-1920`). NOT a port of
 * WarRoom.tsx (which only lists meetings + redirects). WarRoom.tsx
 * provides at most layout inspiration for the meeting-list pane.
 *
 * Renders:
 *   - meeting-list pane (left): /api/cabinet/list
 *   - active meeting pane (right): transcript + composer + tool-call
 *     disclosure with EventSource consumption + Last-Event-ID resume +
 *     410+X-Refetch-Hint fallback to /api/cabinet/transcripts.
 */

import { useEffect, useState } from 'preact/hooks';
import { apiGet, apiPost } from '@/lib/api';
import { CabinetComposer } from '@/components/CabinetComposer';
import { CabinetTranscript } from '@/components/CabinetTranscript';
import {
  fetchCabinetTranscripts,
  openCabinetStream,
  type CabinetEvent,
} from '@/lib/cabinet-stream';

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
  pinnedAgent: string | null;
  status: 'open' | 'ended';
}

interface TranscriptRow {
  id: number;
  speaker: string;
  text: string;
  created_at: number;
}

export function Cabinet() {
  const [meetings, setMeetings] = useState<CabinetMeetingRow[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [details, setDetails] = useState<MeetingDetails | null>(null);
  const [baseline, setBaseline] = useState<TranscriptRow[]>([]);
  const [liveEvents, setLiveEvents] = useState<Array<{ seq: number; event: CabinetEvent }>>([]);
  const [loading, setLoading] = useState(false);

  async function refreshList() {
    try {
      const res = await apiGet<{ meetings: CabinetMeetingRow[] }>('/api/cabinet/list?limit=20');
      setMeetings(res.meetings);
    } catch (err) {
      console.error('cabinet list failed', err);
    }
  }

  useEffect(() => {
    void refreshList();
    // Pre-warm SDK path so the first send feels snappy.
    void apiPost('/api/cabinet/warmup').catch(() => {});
  }, []);

  // When activeId changes, load details + baseline transcript and open SSE.
  useEffect(() => {
    if (activeId === null) {
      setDetails(null);
      setBaseline([]);
      setLiveEvents([]);
      return;
    }
    setLoading(true);
    let cancelled = false;
    let stream: ReturnType<typeof openCabinetStream> | null = null;

    (async () => {
      try {
        const det = await apiGet<MeetingDetails>(`/api/cabinet/details?meetingId=${activeId}`);
        if (cancelled) return;
        setDetails(det);
        const trx = await fetchCabinetTranscripts(activeId);
        if (cancelled) return;
        setBaseline(trx.transcript);
        setLiveEvents([]);
        stream = openCabinetStream({
          meetingId: activeId,
          onEvent: (event, seq) => {
            setLiveEvents((prev) => [...prev, { seq, event }]);
          },
          onRefetchHint: () => {
            // 410 — refetch baseline.
            void fetchCabinetTranscripts(activeId).then((t) => setBaseline(t.transcript));
          },
        });
      } catch (err) {
        console.error('cabinet open meeting failed', err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      if (stream) stream.close();
    };
  }, [activeId]);

  async function newMeeting() {
    try {
      const res = await apiPost<{ meetingId: number }>('/api/cabinet/new', {});
      await refreshList();
      setActiveId(res.meetingId);
    } catch (err) {
      console.error('cabinet new failed', err);
    }
  }

  async function endMeeting() {
    if (activeId === null) return;
    await apiPost('/api/cabinet/end', { meetingId: activeId });
    await refreshList();
    setActiveId(null);
  }

  return (
    <div class="flex flex-1 min-h-0">
      <div class="w-72 border-r border-[var(--color-border)] flex flex-col">
        <div class="p-3 border-b border-[var(--color-border)]">
          <button
            type="button"
            onClick={() => void newMeeting()}
            class="w-full px-3 py-2 bg-[var(--color-primary)] text-white rounded-md text-sm font-medium"
          >
            + New Meeting
          </button>
        </div>
        <div class="flex-1 overflow-y-auto">
          {meetings.length === 0 && (
            <div class="px-3 py-4 text-xs text-[var(--color-text-muted)]">
              No meetings yet — click "+ New Meeting" to begin.
            </div>
          )}
          {meetings.map((m) => (
            <button
              key={m.id}
              type="button"
              onClick={() => setActiveId(m.id)}
              class={`block w-full text-left px-3 py-2 border-b border-[var(--color-border)] hover:bg-[var(--color-hover)] text-sm ${
                activeId === m.id ? 'bg-[var(--color-hover)]' : ''
              }`}
            >
              <div class="font-medium truncate">{m.title || `Meeting #${m.id}`}</div>
              <div class="text-xs text-[var(--color-text-muted)]">
                {m.entry_count} entries • {m.ended_at ? 'ended' : 'open'}
              </div>
            </button>
          ))}
        </div>
      </div>

      <div class="flex-1 flex flex-col min-w-0">
        {activeId === null ? (
          <div class="flex-1 flex items-center justify-center text-[var(--color-text-muted)] text-sm">
            Select a meeting from the left or start a new one.
          </div>
        ) : (
          <>
            <div class="border-b border-[var(--color-border)] px-4 py-2 flex items-center justify-between">
              <div>
                <div class="text-sm font-medium">
                  {details?.meeting?.title || `Meeting #${activeId}`}
                </div>
                <div class="text-xs text-[var(--color-text-muted)]">
                  {details?.roster?.length ?? 0} agents
                  {details?.pinnedAgent ? ` • pinned: @${details.pinnedAgent}` : ''}
                </div>
              </div>
              <button
                type="button"
                onClick={() => void endMeeting()}
                class="px-3 py-1 bg-red-500/20 text-red-500 rounded-md text-xs font-medium hover:bg-red-500/30"
              >
                End
              </button>
            </div>
            {loading ? (
              <div class="flex-1 flex items-center justify-center text-[var(--color-text-muted)]">
                Loading…
              </div>
            ) : (
              <CabinetTranscript baselineRows={baseline} liveEvents={liveEvents} />
            )}
            <CabinetComposer
              meetingId={activeId}
              roster={details?.roster ?? []}
              disabled={details?.status === 'ended'}
            />
          </>
        )}
      </div>
    </div>
  );
}
