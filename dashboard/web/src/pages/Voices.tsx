import { useEffect, useMemo, useState } from 'preact/hooks';
import { ExternalLink, MessageSquare, Mic, RefreshCw } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { apiPost, chatId as dashboardChatId } from '@/lib/api';
import { cabinetVoiceUrl } from '@/lib/cabinet-voice-url';

export function Voices() {
  const cabinetChatId = dashboardChatId || 'cabinet-browser';
  const [room, setRoom] = useState<OpenRoomResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const roster = room?.roster ?? room?.agents ?? [];
  const voiceUrl = useMemo(
    () => room ? cabinetVoiceUrl(room.meetingId, cabinetChatId) : '',
    [room, cabinetChatId],
  );

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

  useEffect(() => {
    void openRoom();
  }, []);

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

      <div class="flex-1 min-h-0 overflow-y-auto p-4 md:p-6">
        <div class="max-w-5xl mx-auto grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
          <section class="border border-[var(--color-border)] rounded-md bg-[var(--color-card)]">
            <div class="p-4 border-b border-[var(--color-border)] flex items-center justify-between gap-3">
              <div class="min-w-0">
                <div class="text-sm font-semibold">Current Cabinet Voice Room</div>
                <div class="text-xs text-[var(--color-text-muted)] truncate">
                  {loading ? 'Opening room...' : room ? `${roster.length} participants / ${room.status}` : 'No active room'}
                </div>
              </div>
              <div class="inline-flex items-center gap-2">
                <a
                  href={voiceUrl || undefined}
                  target="_blank"
                  rel="noreferrer"
                  class={`h-8 px-3 inline-flex items-center gap-2 rounded-md text-sm font-medium ${
                    voiceUrl
                      ? 'bg-[var(--color-primary)] text-white hover:opacity-90'
                      : 'bg-[var(--color-hover)] text-[var(--color-text-muted)] pointer-events-none'
                  }`}
                  aria-disabled={!voiceUrl}
                >
                  <Mic size={15} />
                  Open Voice
                </a>
                <a
                  href="/cabinet"
                  class="h-8 px-3 inline-flex items-center gap-2 rounded-md border border-[var(--color-border)] text-sm hover:bg-[var(--color-hover)]"
                >
                  <MessageSquare size={15} />
                  Cabinet
                </a>
              </div>
            </div>

            {error ? (
              <div class="p-4 text-sm text-red-500">{error}</div>
            ) : (
              <div class="p-4 grid gap-3">
                <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <Metric label="Meeting" value={room ? `#${room.meetingId}` : '...'} />
                  <Metric label="Chat" value={cabinetChatId} />
                  <Metric label="Lifecycle" value="Manual" />
                  <Metric label="Adapter" value="Python" />
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
                href={voiceUrl}
                target="_blank"
                rel="noreferrer"
                class="mt-3 h-8 w-full inline-flex items-center justify-center gap-2 rounded-md border border-[var(--color-border)] text-sm hover:bg-[var(--color-hover)]"
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

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div class="rounded-md border border-[var(--color-border)] p-3 min-w-0">
      <div class="text-xs text-[var(--color-text-muted)] mb-1">{label}</div>
      <div class="text-sm font-medium truncate">{value}</div>
    </div>
  );
}
