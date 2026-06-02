import { useEffect, useMemo, useState } from 'preact/hooks';
import { ExternalLink, MessageSquare, Mic, Play, RefreshCw, RotateCw, Square } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { apiGet, apiPost, chatId as dashboardChatId } from '@/lib/api';
import { cabinetVoiceUrl } from '@/lib/cabinet-voice-url';

const VOICE_ACTION_ENDPOINTS = {
  start: '/api/cabinet/voice/start',
  stop: '/api/cabinet/voice/stop',
  restart: '/api/cabinet/voice/restart',
} as const;

export function Voices() {
  const cabinetChatId = dashboardChatId || 'cabinet-browser';
  const [room, setRoom] = useState<OpenRoomResponse | null>(null);
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [voiceLoading, setVoiceLoading] = useState(false);
  const [voiceAction, setVoiceAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const roster = room?.roster ?? room?.agents ?? [];
  const voiceUrl = useMemo(
    () => room ? cabinetVoiceUrl(room.meetingId, cabinetChatId) : '',
    [room, cabinetChatId],
  );
  const voiceMatchesRoom = voiceStatus?.matchesMeeting !== false;
  const voiceReady = voiceStatus?.status === 'ready' && voiceMatchesRoom;
  const voiceActive = Boolean(voiceStatus?.active && voiceMatchesRoom);
  const voiceConflict = Boolean(voiceStatus?.active && !voiceMatchesRoom);

  async function openRoom() {
    setLoading(true);
    setError(null);
    try {
      const res = await apiPost<OpenRoomResponse>('/api/cabinet/open', { chatId: cabinetChatId });
      setRoom(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Cabinet room unavailable');
    } finally {
      setLoading(false);
    }
  }

  async function refreshVoiceStatus(targetRoom = room) {
    if (!targetRoom) return;
    setVoiceLoading(true);
    try {
      const qs = new URLSearchParams({
        meetingId: String(targetRoom.meetingId),
        chatId: cabinetChatId,
      });
      const res = await apiGet<VoiceStatusResponse>(`/api/cabinet/voice/status?${qs.toString()}`);
      setVoiceStatus(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Cabinet voice status unavailable');
    } finally {
      setVoiceLoading(false);
    }
  }

  async function runVoiceAction(action: 'start' | 'stop' | 'restart') {
    if (!room) return;
    setVoiceAction(action);
    setError(null);
    try {
      const res = await apiPost<VoiceStatusResponse>(VOICE_ACTION_ENDPOINTS[action], {
        meetingId: room.meetingId,
        chatId: cabinetChatId,
      });
      setVoiceStatus(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Cabinet voice ${action} failed`);
      await refreshVoiceStatus(room);
    } finally {
      setVoiceAction(null);
    }
  }

  useEffect(() => {
    void openRoom();
  }, []);

  useEffect(() => {
    if (!room) return;
    void refreshVoiceStatus(room);
    const timer = window.setInterval(() => {
      void refreshVoiceStatus(room);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [room?.meetingId, cabinetChatId]);

  return (
    <div class="flex flex-col h-full min-h-0">
      <TopBar
        title="Voices"
        subtitle={room ? `Cabinet room #${room.meetingId}` : 'Cabinet voice launcher'}
        actions={(
          <button
            type="button"
            onClick={() => void openRoom()}
            class="w-9 h-8 inline-flex items-center justify-center rounded-md border border-[var(--color-border)] hover:bg-[var(--color-hover)]"
            title="Refresh"
            disabled={loading}
          >
            <RefreshCw size={15} />
          </button>
        )}
      />

      <div class="flex-1 min-h-0 overflow-y-auto p-3 sm:p-4 md:p-6">
        <div class="max-w-5xl mx-auto grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
          <section class="border border-[var(--color-border)] rounded-md bg-[var(--color-card)]">
            <div class="p-3 sm:p-4 border-b border-[var(--color-border)] flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
              <div class="min-w-0 w-full sm:w-auto">
                <div class="text-sm font-semibold">Current Cabinet Voice Room</div>
                <div class="text-xs text-[var(--color-text-muted)] truncate">
                  {loading ? 'Opening room...' : room ? `${roster.length} participants / ${room.status}` : 'No active room'}
                </div>
              </div>
              <div class="grid grid-cols-2 gap-2 w-full sm:w-auto">
                <a
                  href={voiceReady ? voiceUrl : undefined}
                  target="_blank"
                  rel="noreferrer"
                  class={`min-h-11 sm:min-h-8 px-3 inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium ${
                    voiceReady
                      ? 'bg-[var(--color-primary)] text-white hover:opacity-90'
                      : 'bg-[var(--color-hover)] text-[var(--color-text-muted)] pointer-events-none'
                  }`}
                  aria-disabled={!voiceReady}
                >
                  <Mic size={15} />
                  Open Voice
                </a>
                <a
                  href="/cabinet"
                  class="min-h-11 sm:min-h-8 px-3 inline-flex items-center justify-center gap-2 rounded-md border border-[var(--color-border)] text-sm hover:bg-[var(--color-hover)]"
                >
                  <MessageSquare size={15} />
                  Cabinet
                </a>
              </div>
            </div>

            {error ? (
              <div class="p-4 text-sm text-red-500">{error}</div>
            ) : (
              <div class="p-3 sm:p-4 grid gap-3">
                <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-3">
                  <Metric label="Meeting" value={room ? `#${room.meetingId}` : '...'} />
                  <Metric label="Chat" value={cabinetChatId} />
                  <Metric label="Lifecycle" value={voiceStatus?.status ?? (voiceLoading ? 'checking' : 'stopped')} />
                  <Metric label="Transport" value={voiceStatus?.port ? `${voiceStatus.bind}:${voiceStatus.port}` : 'Python'} />
                </div>

                <div class="border border-[var(--color-border)] rounded-md p-3">
                  <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-3 mb-3">
                    <div class="min-w-0">
                      <div class="text-sm font-semibold">Voice Subprocess</div>
                      <div class="text-xs text-[var(--color-text-muted)] break-words">
                        {voiceConflict
                          ? `Active on room #${voiceStatus?.meetingId}`
                          : voiceStatus?.pid
                            ? `PID ${voiceStatus.pid} / ${voiceStatus.wsUrl ?? 'waiting for socket'}`
                            : 'No active local voice subprocess'}
                      </div>
                    </div>
                    <div class="grid grid-cols-3 gap-2 w-full md:w-auto">
                      <button
                        type="button"
                        onClick={() => void runVoiceAction('start')}
                        disabled={!room || voiceActive || voiceConflict || voiceAction !== null}
                        class="min-h-11 md:min-h-8 px-2 md:px-3 inline-flex items-center justify-center gap-1.5 rounded-md border border-[var(--color-border)] text-sm hover:bg-[var(--color-hover)] disabled:opacity-50 disabled:pointer-events-none"
                      >
                        <Play size={14} />
                        Start
                      </button>
                      <button
                        type="button"
                        onClick={() => void runVoiceAction('stop')}
                        disabled={!voiceActive || voiceAction !== null}
                        class="min-h-11 md:min-h-8 px-2 md:px-3 inline-flex items-center justify-center gap-1.5 rounded-md border border-[var(--color-border)] text-sm hover:bg-[var(--color-hover)] disabled:opacity-50 disabled:pointer-events-none"
                      >
                        <Square size={14} />
                        Stop
                      </button>
                      <button
                        type="button"
                        onClick={() => void runVoiceAction('restart')}
                        disabled={!room || voiceConflict || voiceAction !== null}
                        class="min-h-11 md:min-h-8 px-2 md:px-3 inline-flex items-center justify-center gap-1.5 rounded-md border border-[var(--color-border)] text-sm hover:bg-[var(--color-hover)] disabled:opacity-50 disabled:pointer-events-none"
                      >
                        <RotateCw size={14} />
                        Restart
                      </button>
                    </div>
                  </div>
                  <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-2 text-xs">
                    <Metric label="Ready" value={voiceReady ? 'yes' : 'no'} />
                    <Metric label="Uptime" value={voiceStatus?.uptimeS != null ? `${voiceStatus.uptimeS}s` : '-'} />
                    <Metric label="STT/TTS" value={`${voiceStatus?.capabilities?.stt ? 'STT' : 'no STT'} / ${voiceStatus?.capabilities?.tts ? 'TTS' : 'no TTS'}`} />
                    <Metric label="Audio Runtime" value={`${voiceStatus?.capabilities?.pipecat ? 'Pipecat' : 'no Pipecat'} / ${voiceStatus?.capabilities?.ffmpeg ? 'ffmpeg' : 'no ffmpeg'}`} />
                  </div>
                  {voiceStatus?.lastError && (
                    <div class="mt-3 text-xs text-red-500 break-words">{voiceStatus.lastError}</div>
                  )}
                  {voiceStatus?.logPath && (
                    <div class="mt-3 text-xs text-[var(--color-text-muted)] break-all">{voiceStatus.logPath}</div>
                  )}
                </div>

                <div class="border border-[var(--color-border)] rounded-md overflow-hidden">
                  <div class="px-3 py-2 text-xs uppercase tracking-wide text-[var(--color-text-muted)] border-b border-[var(--color-border)]">
                    Roster Snapshot
                  </div>
                  <div class="divide-y divide-[var(--color-border)]">
                    {roster.length === 0 && (
                      <div class="px-3 py-3 text-sm text-[var(--color-text-muted)]">No participants loaded.</div>
                    )}
                    {roster.map((agent) => (
                      <div key={agent.id} class="px-3 py-3 flex items-center justify-between gap-3">
                        <div class="min-w-0">
                          <div class="text-sm font-medium truncate">{agent.name || agent.id}</div>
                          <div class="text-xs text-[var(--color-text-muted)] truncate">@{agent.id}</div>
                        </div>
                        {room?.pinnedAgent === agent.id && (
                          <span class="text-xs px-2 py-1 rounded-md border border-[var(--color-border)]">Pinned</span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </section>

          <aside class="border border-[var(--color-border)] rounded-md bg-[var(--color-card)] p-4">
            <div class="text-sm font-semibold mb-3">Launch URL</div>
            <div class="min-h-[96px] rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-3 text-xs break-all text-[var(--color-text-muted)]">
              {voiceUrl || 'Waiting for Cabinet room...'}
            </div>
            {voiceUrl && (
              <a
                href={voiceReady ? voiceUrl : undefined}
                target="_blank"
                rel="noreferrer"
                class={`mt-3 min-h-11 sm:min-h-8 w-full inline-flex items-center justify-center gap-2 rounded-md border border-[var(--color-border)] text-sm ${
                  voiceReady ? 'hover:bg-[var(--color-hover)]' : 'opacity-50 pointer-events-none'
                }`}
                aria-disabled={!voiceReady}
              >
                <ExternalLink size={15} />
                Open URL
              </a>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}

interface RosterAgent {
  id: string;
  name: string;
  description?: string;
}

interface MeetingRow {
  id: number;
  ended_at: number | null;
  title: string | null;
}

interface OpenRoomResponse {
  meetingId: number;
  created: boolean;
  meeting: MeetingRow;
  roster: RosterAgent[];
  agents?: RosterAgent[];
  broadcastOrder?: string[];
  pinnedAgent: string | null;
  status: 'open' | 'ended';
}

interface VoiceStatusResponse {
  ok?: boolean;
  status: 'stopped' | 'starting' | 'ready' | 'crashed' | 'stale';
  active?: boolean;
  meetingId: number | null;
  requestedMeetingId?: number | null;
  matchesMeeting?: boolean;
  chatId: string;
  pid: number | null;
  port: number;
  bind: string;
  wsUrl: string | null;
  startedAt: number | null;
  readyAt: number | null;
  uptimeS: number | null;
  lastError: string | null;
  logPath: string | null;
  action?: string;
  capabilities: {
    pipecat: boolean;
    ffmpeg: boolean;
    stt: boolean;
    tts: boolean;
  };
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div class="rounded-md border border-[var(--color-border)] p-3 min-w-0">
      <div class="text-xs text-[var(--color-text-muted)] mb-1">{label}</div>
      <div class="text-sm font-medium break-words">{value}</div>
    </div>
  );
}
