import { useEffect, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import { Bot, Inbox, MessageSquare, Play, Plus, RefreshCw, Send, ShieldAlert, Trash2, UserPlus } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { Modal } from '@/components/Modal';
import { useFetch } from '@/lib/useFetch';
import { apiDelete, apiPost } from '@/lib/api';
import { pushToast } from '@/lib/toasts';

interface TeamSession {
  id: number;
  team_name: string;
  lead_agent_id: string;
  lead_agent_name?: string | null;
  convoy_id?: number | null;
  status: string;
  backend_type?: string;
  last_activity_at?: number | null;
  shutdown_requested_at?: number | null;
  closed_at?: number | null;
  created_at?: number;
  updated_at?: number;
}

interface TeamMember {
  id: number;
  team_session_id: number;
  agent_id: string;
  agent_name?: string | null;
  role: string;
  subtask_id?: number | null;
  status: string;
  joined_at: number;
  last_activity_at?: number | null;
}

interface TeamDetail {
  session: TeamSession;
  members: TeamMember[];
}

interface AgentMessage {
  id: number;
  from_agent: string;
  message_type: string;
  msg_type?: string | null;
  subject?: string | null;
  body: string;
  convoy_id?: number | null;
  created_at: number;
}

interface AgentDelivery {
  id: number;
  recipient_agent: string;
  status: string;
  claim_token?: string | null;
}

interface MailboxEntry {
  message: AgentMessage;
  deliveries: AgentDelivery[];
}

interface TeamLoopStepResponse {
  agent_id: string;
  subtask_id: number;
  claimed_count: number;
  action: string;
  completed: boolean;
  convoy_completed: boolean;
  subtask_after?: { status: string } | null;
  reply?: AgentMessage | null;
  runtime?: {
    runtime_lane?: string | null;
    provider?: string | null;
    model?: string | null;
    session_id?: string | null;
    tool_call_count?: number;
  } | null;
}

interface TeamTickResponse {
  team_id: number;
  selected_action: string;
  reason: string;
  agent_id?: string | null;
  convoy_id?: number | null;
  subtask_id?: number | null;
  step?: TeamLoopStepResponse | null;
  waited: boolean;
  error?: string | null;
}

const STATUS_TONE: Record<string, string> = {
  active: 'bg-emerald-500/10 text-emerald-300',
  idle: 'bg-sky-500/10 text-sky-300',
  shutdown_requested: 'bg-amber-500/10 text-amber-300',
  closed: 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]',
  failed: 'bg-red-500/10 text-red-300',
};

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function formatTime(value?: number | null): string {
  if (!value) return 'never';
  return new Date(value * 1000).toLocaleString();
}

function Badge({ children, className = '' }: { children: ComponentChildren; className?: string }) {
  return (
    <span class={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] leading-4 ${className}`}>
      {children}
    </span>
  );
}

function statusTone(status: string): string {
  return STATUS_TONE[status] ?? 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]';
}

function agentLabel(agentId: string, members: TeamMember[]): string {
  const member = members.find((m) => m.agent_id === agentId);
  return member?.agent_name || agentId;
}

export function Teams() {
  const teamsFetch = useFetch<TeamSession[]>('/api/team', 10_000);
  const teams = teamsFetch.data ?? [];
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [memberOpen, setMemberOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [teamName, setTeamName] = useState('');
  const [leadAgentId, setLeadAgentId] = useState('');
  const [leadAgentName, setLeadAgentName] = useState('');
  const [convoyId, setConvoyId] = useState('');
  const [backendType, setBackendType] = useState('local');
  const [memberAgentId, setMemberAgentId] = useState('');
  const [memberAgentName, setMemberAgentName] = useState('');
  const [memberRole, setMemberRole] = useState('worker');
  const [memberSubtaskId, setMemberSubtaskId] = useState('');

  const activeId = selectedId ?? teams[0]?.id ?? null;
  const detailFetch = useFetch<TeamDetail>(activeId ? `/api/team/${activeId}` : null, 10_000);
  const session = detailFetch.data?.session ?? teams.find((t) => t.id === activeId) ?? null;
  const members = detailFetch.data?.members ?? [];
  const convoyMailboxFetch = useFetch<MailboxEntry[]>(
    session?.convoy_id !== null && session?.convoy_id !== undefined ? `/api/mailbox/convoy/${session.convoy_id}` : null,
    10_000,
  );
  const convoyMailbox = convoyMailboxFetch.data ?? [];
  const activeCount = teams.filter((team) => team.status === 'active' || team.status === 'idle').length;
  const agentOptions = members.length > 0
    ? members
    : session
      ? [{
          id: -1,
          team_session_id: session.id,
          agent_id: session.lead_agent_id,
          agent_name: session.lead_agent_name,
          role: 'lead',
          status: 'active',
          joined_at: session.created_at ?? Math.floor(Date.now() / 1000),
        }]
      : [];
  const [mailFrom, setMailFrom] = useState('');
  const [mailTo, setMailTo] = useState('');
  const [mailSubject, setMailSubject] = useState('Team handoff');
  const [mailBody, setMailBody] = useState('');
  const [claimAgent, setClaimAgent] = useState('');
  const [claimedMail, setClaimedMail] = useState<MailboxEntry[]>([]);
  const [loopAgent, setLoopAgent] = useState('');
  const [loopComplete, setLoopComplete] = useState(false);
  const [loopUseRuntime, setLoopUseRuntime] = useState(false);
  const [lastLoopStep, setLastLoopStep] = useState<TeamLoopStepResponse | null>(null);
  const [tickUseRuntime, setTickUseRuntime] = useState(false);
  const [tickCompleteRunning, setTickCompleteRunning] = useState(false);
  const [lastTeamTick, setLastTeamTick] = useState<TeamTickResponse | null>(null);

  useEffect(() => {
    const agentIds = agentOptions.map((member) => member.agent_id);
    const firstAgent = agentOptions[0]?.agent_id ?? '';
    const secondAgent = agentOptions.find((member) => member.agent_id !== firstAgent)?.agent_id ?? firstAgent;
    if (!agentIds.includes(mailFrom)) setMailFrom(firstAgent);
    if (!agentIds.includes(mailTo)) setMailTo(secondAgent);
    if (!agentIds.includes(claimAgent)) setClaimAgent(secondAgent);
    if (!agentIds.includes(loopAgent)) setLoopAgent(secondAgent);
  }, [agentOptions, mailFrom, mailTo, claimAgent, loopAgent]);

  function refreshAll() {
    teamsFetch.refresh();
    detailFetch.refresh();
    convoyMailboxFetch.refresh();
  }

  async function createTeam(event: Event) {
    event.preventDefault();
    if (!teamName.trim() || !leadAgentId.trim()) {
      pushToast({ tone: 'error', title: 'Team name and lead agent required' });
      return;
    }
    setBusy(true);
    try {
      const result = await apiPost<TeamDetail>('/api/team', {
        team_name: teamName.trim(),
        lead_agent_id: leadAgentId.trim(),
        lead_agent_name: leadAgentName.trim() || null,
        convoy_id: convoyId.trim() ? Number(convoyId) : null,
        backend_type: backendType,
      });
      setSelectedId(result.session.id);
      setCreateOpen(false);
      setTeamName('');
      setLeadAgentId('');
      setLeadAgentName('');
      setConvoyId('');
      setBackendType('local');
      pushToast({ tone: 'success', title: 'Team created' });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Create failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function addMember(event: Event) {
    event.preventDefault();
    if (!activeId || !memberAgentId.trim()) {
      pushToast({ tone: 'error', title: 'Agent id required' });
      return;
    }
    setBusy(true);
    try {
      await apiPost(`/api/team/${activeId}/members`, {
        agent_id: memberAgentId.trim(),
        agent_name: memberAgentName.trim() || null,
        role: memberRole,
        subtask_id: memberSubtaskId.trim() ? Number(memberSubtaskId) : null,
      });
      setMemberOpen(false);
      setMemberAgentId('');
      setMemberAgentName('');
      setMemberRole('worker');
      setMemberSubtaskId('');
      pushToast({ tone: 'success', title: 'Member added' });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Add member failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function shutdownTeam() {
    if (!session) return;
    setBusy(true);
    try {
      await apiPost(`/api/team/${session.id}/shutdown`, {});
      pushToast({ tone: 'success', title: 'Shutdown requested' });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Shutdown failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function closeTeam() {
    if (!session) return;
    setBusy(true);
    try {
      await apiDelete(`/api/team/${session.id}`);
      pushToast({ tone: 'success', title: 'Team closed' });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Close failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function sendTeamMessage(event: Event) {
    event.preventDefault();
    if (!session?.convoy_id) {
      pushToast({ tone: 'error', title: 'Convoy binding required' });
      return;
    }
    if (!mailFrom || !mailTo || !mailBody.trim()) {
      pushToast({ tone: 'error', title: 'From, recipient, and message required' });
      return;
    }
    setBusy(true);
    try {
      await apiPost('/api/mailbox/send', {
        from_agent: mailFrom,
        recipients: [mailTo],
        body: mailBody.trim(),
        convoy_id: session.convoy_id,
        subject: mailSubject.trim() || null,
        msg_type: 'team_message',
      });
      setMailBody('');
      pushToast({ tone: 'success', title: 'Message sent' });
      convoyMailboxFetch.refresh();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Message failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function claimInbox() {
    if (!claimAgent) {
      pushToast({ tone: 'error', title: 'Recipient required' });
      return;
    }
    setBusy(true);
    try {
      const query = session?.convoy_id ? `?convoy_id=${session.convoy_id}&limit=10` : '?limit=10';
      const result = await apiPost<MailboxEntry[]>(`/api/mailbox/claim/${encodeURIComponent(claimAgent)}${query}`, undefined);
      setClaimedMail(result);
      pushToast({ tone: 'success', title: result.length ? 'Inbox claimed' : 'No pending mail' });
      convoyMailboxFetch.refresh();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Claim failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function runLoopStep() {
    if (!session || !loopAgent) {
      pushToast({ tone: 'error', title: 'Loop agent required' });
      return;
    }
    setBusy(true);
    try {
      const result = await apiPost<TeamLoopStepResponse>(`/api/team/${session.id}/loop-step`, {
        agent_id: loopAgent,
        use_runtime: loopUseRuntime,
        complete: loopComplete,
      });
      setLastLoopStep(result);
      pushToast({ tone: 'success', title: 'Loop step ran', description: `${result.agent_id}: ${result.action}` });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Loop step failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function runTeamTick() {
    if (!session) {
      pushToast({ tone: 'error', title: 'Team required' });
      return;
    }
    setBusy(true);
    try {
      const result = await apiPost<TeamTickResponse>(`/api/team/${session.id}/tick`, {
        use_runtime: tickUseRuntime,
        complete_running: tickCompleteRunning,
      });
      setLastTeamTick(result);
      pushToast({ tone: result.error ? 'error' : 'success', title: 'Team tick ran', description: `${result.selected_action}: ${result.reason}` });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Team tick failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Teams"
        subtitle={`${activeCount} active · ${teams.length} total`}
        actions={
          <>
            <button
              type="button"
              onClick={refreshAll}
              class="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)]"
            >
              <RefreshCw size={14} /> Refresh
            </button>
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              class="inline-flex items-center gap-1.5 rounded-md bg-[var(--color-accent)] px-2.5 py-1.5 text-[12px] font-medium text-white hover:bg-[var(--color-accent-hover)]"
            >
              <Plus size={14} /> New Team
            </button>
          </>
        }
      />

      <div class="grid min-h-0 flex-1 gap-4 overflow-hidden p-4 lg:grid-cols-[minmax(280px,340px)_minmax(0,1fr)]">
        <aside class="min-h-0 overflow-y-auto rounded-md border border-[var(--color-border)] bg-[var(--color-card)]">
          <div class="sticky top-0 border-b border-[var(--color-border)] bg-[var(--color-card)] p-3 text-[12px] font-medium text-[var(--color-text)]">
            Team Sessions
          </div>
          {teamsFetch.error && <Empty title="Failed to load teams" description={teamsFetch.error} />}
          {teamsFetch.loading && !teamsFetch.data && <div class="flex justify-center py-10"><Spinner size={18} /></div>}
          {!teamsFetch.loading && !teamsFetch.error && teams.length === 0 && (
            <Empty title="No teams" description="Create a framework-owned team session or bind one to a convoy." />
          )}
          <div class="grid gap-2 p-3">
            {teams.map((team) => (
              <button
                key={team.id}
                type="button"
                onClick={() => setSelectedId(team.id)}
                class={`rounded-md border p-3 text-left transition-colors ${
                  activeId === team.id
                    ? 'border-[var(--color-accent)] bg-[var(--color-elevated)]'
                    : 'border-[var(--color-border)] hover:border-[var(--color-accent)]'
                }`}
              >
                <div class="flex items-start justify-between gap-2">
                  <div class="min-w-0">
                    <div class="truncate text-[13px] font-medium text-[var(--color-text)]">{team.team_name}</div>
                    <div class="mt-1 text-[11px] text-[var(--color-text-muted)]">
                      Lead {team.lead_agent_name || team.lead_agent_id}
                    </div>
                  </div>
                  <Badge className={statusTone(team.status)}>{team.status}</Badge>
                </div>
                <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-[var(--color-text-muted)]">
                  <span>#{team.id}</span>
                  <span>{team.backend_type || 'local'}</span>
                  {team.convoy_id !== null && team.convoy_id !== undefined && <span>Convoy #{team.convoy_id}</span>}
                </div>
              </button>
            ))}
          </div>
        </aside>

        <section class="min-w-0 min-h-0 overflow-y-auto">
          {!session && !detailFetch.loading && (
            <Empty title="Select a team" description="Team members, convoy binding, and lifecycle controls will appear here." />
          )}
          {detailFetch.loading && !detailFetch.data && <div class="flex justify-center py-16"><Spinner size={20} /></div>}
          {detailFetch.error && <Empty title="Failed to load team" description={detailFetch.error} />}
          {session && (
            <div class="grid gap-4">
              <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
                <div class="flex flex-wrap items-start justify-between gap-3">
                  <div class="min-w-0">
                    <div class="flex flex-wrap items-center gap-2">
                      <h2 class="truncate text-[18px] font-semibold text-[var(--color-text)]">{session.team_name}</h2>
                      <Badge className={statusTone(session.status)}>{session.status}</Badge>
                    </div>
                    <div class="mt-3 grid gap-1 text-[12px] text-[var(--color-text-muted)] sm:grid-cols-2">
                      <div>Lead: {session.lead_agent_name || session.lead_agent_id}</div>
                      <div>Backend: {session.backend_type || 'local'}</div>
                      <div>Convoy: {session.convoy_id !== null && session.convoy_id !== undefined ? `#${session.convoy_id}` : 'none'}</div>
                      <div>Last activity: {formatTime(session.last_activity_at)}</div>
                      <div>Shutdown requested: {formatTime(session.shutdown_requested_at)}</div>
                      <div>Updated: {formatTime(session.updated_at)}</div>
                    </div>
                  </div>
                  <div class="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setMemberOpen(true)}
                      class="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)]"
                    >
                      <UserPlus size={13} /> Add Member
                    </button>
                    {(session.status === 'active' || session.status === 'idle') && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={shutdownTeam}
                        class="inline-flex items-center gap-1 rounded-md border border-amber-500/30 px-2.5 py-1.5 text-[12px] text-amber-300 hover:border-amber-400 disabled:opacity-60"
                      >
                        <ShieldAlert size={13} /> Shutdown
                      </button>
                    )}
                    {session.status !== 'closed' && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={closeTeam}
                        class="inline-flex items-center gap-1 rounded-md border border-red-500/30 px-2.5 py-1.5 text-[12px] text-red-300 hover:border-red-400 disabled:opacity-60"
                      >
                        <Trash2 size={13} /> Close
                      </button>
                    )}
                  </div>
                </div>
              </div>

              <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]">
                <div class="border-b border-[var(--color-border)] px-4 py-3 text-[13px] font-medium text-[var(--color-text)]">
                  Members ({members.length})
                </div>
                {members.length === 0 ? (
                  <Empty title="No members" description="Add agents to make the team visible to the operator." />
                ) : (
                  <div class="grid gap-2 p-3">
                    {members.map((member) => (
                      <div key={member.id} class="rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="flex flex-wrap items-start justify-between gap-3">
                          <div class="min-w-0">
                            <div class="truncate text-[13px] font-medium text-[var(--color-text)]">
                              {member.agent_name || member.agent_id}
                            </div>
                            <div class="mt-1 text-[11px] text-[var(--color-text-muted)]">
                              {member.agent_id} · {member.role}
                              {member.subtask_id !== null && member.subtask_id !== undefined ? ` · subtask #${member.subtask_id}` : ''}
                            </div>
                          </div>
                          <Badge className={statusTone(member.status)}>{member.status}</Badge>
                        </div>
                        <div class="mt-2 text-[11px] text-[var(--color-text-muted)]">
                          Joined {formatTime(member.joined_at)} · Last activity {formatTime(member.last_activity_at)}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]">
                <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--color-border)] px-4 py-3">
                  <div class="text-[13px] font-medium text-[var(--color-text)]">Team Mailbox</div>
                  <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                    {session.convoy_id !== null && session.convoy_id !== undefined ? `Convoy #${session.convoy_id}` : 'no convoy'}
                  </Badge>
                </div>
                <div class="grid min-w-0 gap-4 p-4 2xl:grid-cols-[minmax(0,1fr)_340px]">
                  <form class="grid min-w-0 gap-3" onSubmit={sendTeamMessage}>
                    <div class="grid gap-3 sm:grid-cols-2">
                      <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                        From
                        <select
                          value={mailFrom}
                          onChange={(event) => setMailFrom((event.target as HTMLSelectElement).value)}
                          class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                        >
                          {agentOptions.map((member) => (
                            <option key={member.agent_id} value={member.agent_id}>{member.agent_name || member.agent_id}</option>
                          ))}
                        </select>
                      </label>
                      <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                        To
                        <select
                          value={mailTo}
                          onChange={(event) => setMailTo((event.target as HTMLSelectElement).value)}
                          class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                        >
                          {agentOptions.map((member) => (
                            <option key={member.agent_id} value={member.agent_id}>{member.agent_name || member.agent_id}</option>
                          ))}
                        </select>
                      </label>
                    </div>
                    <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                      Subject
                      <input
                        value={mailSubject}
                        onInput={(event) => setMailSubject((event.target as HTMLInputElement).value)}
                        class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                        placeholder="Campaign handoff"
                      />
                    </label>
                    <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                      Message
                      <textarea
                        value={mailBody}
                        onInput={(event) => setMailBody((event.target as HTMLTextAreaElement).value)}
                        class="min-h-[96px] resize-y rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                        placeholder="Ask another agent for the next handoff, blocker, or review."
                      />
                    </label>
                    <div class="flex justify-end">
                      <button
                        type="submit"
                        disabled={busy || !session.convoy_id}
                        class="inline-flex min-w-0 items-center gap-1.5 rounded bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white disabled:opacity-60"
                      >
                        <Send size={13} /> Send Message
                      </button>
                    </div>
                  </form>

                  <div class="grid min-w-0 content-start gap-3">
                    <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                      <div class="mb-3 text-[12px] font-medium text-[var(--color-text)]">Loop Step</div>
                      <div class="grid gap-3">
                        <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                          Run as
                          <select
                            value={loopAgent}
                            onChange={(event) => setLoopAgent((event.target as HTMLSelectElement).value)}
                            class="rounded border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                          >
                            {agentOptions.map((member) => (
                              <option key={member.agent_id} value={member.agent_id}>{member.agent_name || member.agent_id}</option>
                            ))}
                          </select>
                        </label>
                        <label class="flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                          <input
                            type="checkbox"
                            checked={loopUseRuntime}
                            onChange={(event) => setLoopUseRuntime((event.target as HTMLInputElement).checked)}
                          />
                          Runtime lane reply
                        </label>
                        <label class="flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                          <input
                            type="checkbox"
                            checked={loopComplete}
                            onChange={(event) => setLoopComplete((event.target as HTMLInputElement).checked)}
                          />
                          Complete running subtask
                        </label>
                        <button
                          type="button"
                          disabled={busy || !loopAgent}
                          onClick={runLoopStep}
                          class="inline-flex items-center justify-center gap-1.5 rounded-md bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white disabled:opacity-60"
                        >
                          <Play size={13} /> Run Loop Step
                        </button>
                        {lastLoopStep && (
                          <div class="rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
                            <div class="font-medium text-[var(--color-text)]">{lastLoopStep.action}</div>
                            <div class="break-words">{lastLoopStep.agent_id} · subtask #{lastLoopStep.subtask_id}</div>
                            <div>claimed {lastLoopStep.claimed_count} · status {lastLoopStep.subtask_after?.status || 'unknown'}</div>
                            {lastLoopStep.runtime && (
                              <div class="break-words">{lastLoopStep.runtime.runtime_lane || 'runtime'} · {lastLoopStep.runtime.provider || 'provider'}</div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                    <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                      <div class="mb-3 text-[12px] font-medium text-[var(--color-text)]">Auto Tick</div>
                      <div class="grid gap-3">
                        <label class="flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                          <input
                            type="checkbox"
                            checked={tickUseRuntime}
                            onChange={(event) => setTickUseRuntime((event.target as HTMLInputElement).checked)}
                          />
                          Runtime lane reply
                        </label>
                        <label class="flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                          <input
                            type="checkbox"
                            checked={tickCompleteRunning}
                            onChange={(event) => setTickCompleteRunning((event.target as HTMLInputElement).checked)}
                          />
                          Complete running subtask
                        </label>
                        <button
                          type="button"
                          disabled={busy || !session.convoy_id}
                          onClick={runTeamTick}
                          class="inline-flex items-center justify-center gap-1.5 rounded-md bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white disabled:opacity-60"
                        >
                          <Bot size={13} /> Run Auto Tick
                        </button>
                        {lastTeamTick && (
                          <div class="rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
                            <div class="font-medium text-[var(--color-text)]">{lastTeamTick.selected_action}</div>
                            <div class="break-words">{lastTeamTick.reason}</div>
                            {lastTeamTick.agent_id && (
                              <div class="break-words">{lastTeamTick.agent_id} · subtask #{lastTeamTick.subtask_id || 'none'}</div>
                            )}
                            {lastTeamTick.step && (
                              <div>
                                claimed {lastTeamTick.step.claimed_count} · status {lastTeamTick.step.subtask_after?.status || 'unknown'}
                              </div>
                            )}
                            {lastTeamTick.step?.runtime && (
                              <div class="break-words">{lastTeamTick.step.runtime.runtime_lane || 'runtime'} · {lastTeamTick.step.runtime.provider || 'provider'}</div>
                            )}
                            {lastTeamTick.waited && <div>waited</div>}
                            {lastTeamTick.error && <div class="break-words text-red-300">{lastTeamTick.error}</div>}
                          </div>
                        )}
                      </div>
                    </div>
                    <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                      Claim inbox for
                      <select
                        value={claimAgent}
                        onChange={(event) => setClaimAgent((event.target as HTMLSelectElement).value)}
                        class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                      >
                        {agentOptions.map((member) => (
                          <option key={member.agent_id} value={member.agent_id}>{member.agent_name || member.agent_id}</option>
                        ))}
                      </select>
                    </label>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={claimInbox}
                      class="inline-flex items-center justify-center gap-1.5 rounded-md border border-[var(--color-border)] px-3 py-2 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
                    >
                      <Inbox size={13} /> Claim Inbox
                    </button>
                    {claimedMail.length > 0 && (
                      <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="mb-2 text-[12px] font-medium text-[var(--color-text)]">Claimed ({claimedMail.length})</div>
                        <div class="grid gap-2">
                          {claimedMail.map((entry) => (
                            <div key={entry.message.id} class="break-words text-[12px] text-[var(--color-text-muted)]">
                              <span class="text-[var(--color-text)]">{entry.message.subject || 'Message'}</span>
                              {' from '}
                              {agentLabel(entry.message.from_agent, members)}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
                <div class="border-t border-[var(--color-border)] px-4 py-3">
                  <div class="mb-3 flex items-center gap-2 text-[12px] font-medium text-[var(--color-text)]">
                    <MessageSquare size={13} /> Convoy Timeline
                  </div>
                  {convoyMailboxFetch.error && <Empty title="Failed to load mailbox" description={convoyMailboxFetch.error} />}
                  {convoyMailboxFetch.loading && !convoyMailboxFetch.data && <div class="flex justify-center py-6"><Spinner size={16} /></div>}
                  {!convoyMailboxFetch.loading && !convoyMailboxFetch.error && convoyMailbox.length === 0 && (
                    <Empty title="No team messages" description="Send a convoy-scoped message to prove team handoff flow." />
                  )}
                  {convoyMailbox.length > 0 && (
                    <div class="grid gap-2">
                      {convoyMailbox.slice(-6).reverse().map((entry) => (
                        <div key={entry.message.id} class="min-w-0 rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                          <div class="flex flex-wrap items-center justify-between gap-2">
                            <div class="text-[12px] font-medium text-[var(--color-text)]">
                              {entry.message.subject || entry.message.msg_type || entry.message.message_type}
                            </div>
                            <div class="text-[11px] text-[var(--color-text-muted)]">{formatTime(entry.message.created_at)}</div>
                          </div>
                          <div class="mt-1 break-words text-[11px] text-[var(--color-text-muted)]">
                            {agentLabel(entry.message.from_agent, members)}
                            {' -> '}
                            {entry.deliveries.map((d) => agentLabel(d.recipient_agent, members)).join(', ')}
                          </div>
                          <div class="mt-2 whitespace-pre-wrap break-words text-[12px] text-[var(--color-text)]">{entry.message.body}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </section>
      </div>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="New Team">
        <form class="grid gap-3" onSubmit={createTeam}>
          <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
            Team name
            <input
              value={teamName}
              onInput={(event) => setTeamName((event.target as HTMLInputElement).value)}
              class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              placeholder="Dashboard DAG team"
            />
          </label>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Lead agent id
              <input
                value={leadAgentId}
                onInput={(event) => setLeadAgentId((event.target as HTMLInputElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                placeholder="codex"
              />
            </label>
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Lead display name
              <input
                value={leadAgentName}
                onInput={(event) => setLeadAgentName((event.target as HTMLInputElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                placeholder="Codex"
              />
            </label>
          </div>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Convoy id
              <input
                value={convoyId}
                onInput={(event) => setConvoyId((event.target as HTMLInputElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                inputMode="numeric"
                placeholder="optional"
              />
            </label>
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Backend
              <select
                value={backendType}
                onChange={(event) => setBackendType((event.target as HTMLSelectElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              >
                <option value="local">local</option>
                <option value="paperclip">paperclip</option>
              </select>
            </label>
          </div>
          <div class="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => setCreateOpen(false)}
              class="rounded border border-[var(--color-border)] px-3 py-2 text-[12px] text-[var(--color-text)]"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              class="rounded bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white disabled:opacity-60"
            >
              Create
            </button>
          </div>
        </form>
      </Modal>

      <Modal open={memberOpen} onClose={() => setMemberOpen(false)} title="Add Team Member">
        <form class="grid gap-3" onSubmit={addMember}>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Agent id
              <input
                value={memberAgentId}
                onInput={(event) => setMemberAgentId((event.target as HTMLInputElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                placeholder="codex-worker"
              />
            </label>
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Display name
              <input
                value={memberAgentName}
                onInput={(event) => setMemberAgentName((event.target as HTMLInputElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                placeholder="Codex Worker"
              />
            </label>
          </div>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Role
              <select
                value={memberRole}
                onChange={(event) => setMemberRole((event.target as HTMLSelectElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
              >
                <option value="lead">lead</option>
                <option value="worker">worker</option>
                <option value="reviewer">reviewer</option>
              </select>
            </label>
            <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
              Subtask id
              <input
                value={memberSubtaskId}
                onInput={(event) => setMemberSubtaskId((event.target as HTMLInputElement).value)}
                class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                inputMode="numeric"
                placeholder="optional"
              />
            </label>
          </div>
          <div class="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => setMemberOpen(false)}
              class="rounded border border-[var(--color-border)] px-3 py-2 text-[12px] text-[var(--color-text)]"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              class="rounded bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white disabled:opacity-60"
            >
              Add
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
