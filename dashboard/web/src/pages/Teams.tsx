import { useEffect, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import { Bot, ClipboardList, Inbox, MessageSquare, Play, Plus, RefreshCw, Send, ShieldAlert, Terminal, Trash2, UserPlus } from 'lucide-preact';
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
  executor?: TeamExecutorStepResponse | null;
  waited: boolean;
  error?: string | null;
}

interface TeamExecutorStepResponse {
  team_id: number;
  agent_id: string;
  convoy_id: number;
  subtask_id: number;
  command_key: string;
  argv: string[];
  cwd: string;
  success: boolean;
  exit_code?: number | null;
  timed_out: boolean;
  duration_ms: number;
  stdout: string;
  stderr: string;
  completed: boolean;
  convoy_completed: boolean;
}

interface TaskChadDrillTurnResponse {
  role: string;
  role_name: string;
  agent_id: string;
  subtask_id: number;
  action: string;
  status?: string | null;
  completed: boolean;
  reply?: AgentMessage | null;
}

interface TaskChadDrillResponse {
  target_url: string;
  team_id: number;
  convoy_id: number;
  initial_message_count: number;
  revision_message_count?: number;
  role_turns: TaskChadDrillTurnResponse[];
  reviewer_turn: TaskChadDrillTurnResponse;
  revision_turns?: TaskChadDrillTurnResponse[];
  final_turn: TaskChadDrillTurnResponse;
  final_plan: string;
}

interface TeamRoomRuntimeMetadata {
  runtime_lane?: string | null;
  provider?: string | null;
  model?: string | null;
  profile_key?: string | null;
  cost_usd?: number | null;
  execution_time_ms?: number | null;
  tool_call_count?: number | null;
  error?: string | null;
}

interface TeamRoomTurnResponse {
  phase: string;
  role: string;
  role_name: string;
  agent_id: string;
  subtask_id: number;
  action: string;
  status?: string | null;
  completed: boolean;
  reply?: AgentMessage | null;
  runtime?: TeamRoomRuntimeMetadata | null;
}

interface TeamRoomRuntimeSummary {
  enabled: boolean;
  turn_count: number;
  lanes: string[];
  providers: string[];
  models: string[];
  tool_call_count: number;
  cost_usd?: number | null;
  execution_time_ms?: number | null;
  errors: string[];
}

interface TeamRoomDiscussionRound {
  round_number: number;
  facilitator_message?: AgentMessage | null;
  facilitator_turn?: TeamRoomTurnResponse | null;
  crosstalk_messages?: AgentMessage[];
  crosstalk_turns: TeamRoomTurnResponse[];
}

interface TeamRoomDecisionLedger {
  decisions: string[];
  accepted_bets: string[];
  rejected_bets: string[];
  owner_actions: Array<{
    owner: string;
    action: string;
    validation_signal: string;
  }>;
  open_questions: string[];
  strongest_objection: string;
  next_meeting_trigger: string;
}

interface TeamRoomRunResponse {
  workflow_id: string;
  meeting_mode: string;
  max_rounds: number;
  goal: string;
  context_excerpt?: string | null;
  team_id: number;
  convoy_id: number;
  runtime: TeamRoomRuntimeSummary;
  progress: {
    completed: number;
    total: number;
    status: string;
  };
  lead_frame_excerpt?: string | null;
  message_counts?: Record<string, number>;
  turn_summary: string;
  discussion_rounds: TeamRoomDiscussionRound[];
  decision_ledger: TeamRoomDecisionLedger;
  phase_results: {
    facilitator: TeamRoomTurnResponse[];
    proposal: TeamRoomTurnResponse[];
    crosstalk: TeamRoomTurnResponse[];
    adversarial_review: TeamRoomTurnResponse;
    revision: TeamRoomTurnResponse[];
    synthesis: TeamRoomTurnResponse;
  };
  final_brief: string;
}

const STATUS_TONE: Record<string, string> = {
  active: 'bg-emerald-500/10 text-emerald-300',
  idle: 'bg-sky-500/10 text-sky-300',
  shutdown_requested: 'bg-amber-500/10 text-amber-300',
  closed: 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]',
  failed: 'bg-red-500/10 text-red-300',
};

const EXECUTOR_COMMANDS = [
  ['git_status', 'git status'],
  ['git_diff_stat', 'git diff stat'],
  ['npm_build', 'npm build'],
  ['npm_lint', 'npm lint'],
  ['npm_test', 'npm test'],
  ['pnpm_build', 'pnpm build'],
  ['pnpm_lint', 'pnpm lint'],
  ['pnpm_test', 'pnpm test'],
  ['uv_pytest', 'uv pytest'],
] as const;

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function formatTime(value?: number | null): string {
  if (!value) return 'never';
  return new Date(value * 1000).toLocaleString();
}

function clipText(value?: string | null, maxChars = 260): string {
  const text = (value ?? '').trim();
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars - 3).trimEnd()}...`;
}

function formatList(values?: string[]): string {
  return values && values.length > 0 ? values.join(', ') : 'none';
}

function formatCost(value?: number | null): string {
  return typeof value === 'number' ? `$${value.toFixed(6)}` : 'n/a';
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
  const [tickExecuteRunning, setTickExecuteRunning] = useState(false);
  const [tickExecutorCommand, setTickExecutorCommand] = useState('git_status');
  const [tickCompleteOnExecutorSuccess, setTickCompleteOnExecutorSuccess] = useState(false);
  const [lastTeamTick, setLastTeamTick] = useState<TeamTickResponse | null>(null);
  const [executorAgent, setExecutorAgent] = useState('');
  const [executorCommand, setExecutorCommand] = useState('git_status');
  const [executorCwd, setExecutorCwd] = useState('');
  const [executorCompleteOnSuccess, setExecutorCompleteOnSuccess] = useState(false);
  const [lastExecutorStep, setLastExecutorStep] = useState<TeamExecutorStepResponse | null>(null);
  const [lastTaskChadDrill, setLastTaskChadDrill] = useState<TaskChadDrillResponse | null>(null);
  const [drillUseRuntime, setDrillUseRuntime] = useState(false);
  const [lastTeamRoomRun, setLastTeamRoomRun] = useState<TeamRoomRunResponse | null>(null);
  const [teamRoomGoal, setTeamRoomGoal] = useState('How do we get TaskChad to one million dollars?');
  const [teamRoomContext, setTeamRoomContext] = useState('');
  const [teamRoomUseV2, setTeamRoomUseV2] = useState(true);
  const [teamRoomUseRuntime, setTeamRoomUseRuntime] = useState(false);
  const [teamRoomRuntimeLane, setTeamRoomRuntimeLane] = useState('generic_runtime');
  const [teamRoomMaxRounds, setTeamRoomMaxRounds] = useState('2');

  useEffect(() => {
    const agentIds = agentOptions.map((member) => member.agent_id);
    const firstAgent = agentOptions[0]?.agent_id ?? '';
    const secondAgent = agentOptions.find((member) => member.agent_id !== firstAgent)?.agent_id ?? firstAgent;
    if (!agentIds.includes(mailFrom)) setMailFrom(firstAgent);
    if (!agentIds.includes(mailTo)) setMailTo(secondAgent);
    if (!agentIds.includes(claimAgent)) setClaimAgent(secondAgent);
    if (!agentIds.includes(loopAgent)) setLoopAgent(secondAgent);
    if (!agentIds.includes(executorAgent)) setExecutorAgent(secondAgent);
  }, [agentOptions, mailFrom, mailTo, claimAgent, loopAgent, executorAgent]);

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
        execute_running: tickExecuteRunning,
        executor_command: tickExecutorCommand,
        complete_on_executor_success: tickCompleteOnExecutorSuccess,
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

  async function runExecutorStep() {
    if (!session || !executorAgent) {
      pushToast({ tone: 'error', title: 'Executor agent required' });
      return;
    }
    setBusy(true);
    try {
      const result = await apiPost<TeamExecutorStepResponse>(`/api/team/${session.id}/executor-step`, {
        agent_id: executorAgent,
        command_key: executorCommand,
        cwd: executorCwd.trim() || null,
        complete_on_success: executorCompleteOnSuccess,
      });
      setLastExecutorStep(result);
      pushToast({
        tone: result.success ? 'success' : 'error',
        title: 'Executor step ran',
        description: `${result.command_key}: exit ${result.exit_code ?? 'timeout'}`,
      });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Executor step failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function runTaskChadDrill() {
    setBusy(true);
    try {
      const result = await apiPost<TaskChadDrillResponse>('/api/team/taskchad-drill', {
        target_url: 'https://www.taskchad.com/',
        use_runtime: drillUseRuntime,
      });
      setLastTaskChadDrill(result);
      setSelectedId(result.team_id);
      pushToast({
        tone: 'success',
        title: 'TaskChad drill complete',
        description: `Team #${result.team_id} · Convoy #${result.convoy_id}`,
      });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'TaskChad drill failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function runTeamRoom(event: Event) {
    event.preventDefault();
    const goal = teamRoomGoal.trim();
    if (!goal) {
      pushToast({ tone: 'error', title: 'Team Room goal required' });
      return;
    }
    const parsedRounds = Number(teamRoomMaxRounds);
    const maxRounds = Number.isFinite(parsedRounds)
      ? Math.min(4, Math.max(1, Math.trunc(parsedRounds)))
      : 2;
    setBusy(true);
    try {
      const result = await apiPost<TeamRoomRunResponse>('/api/team/room/run', {
        goal,
        context: teamRoomContext.trim() || null,
        v2: teamRoomUseV2,
        max_rounds: teamRoomUseV2 ? maxRounds : null,
        use_runtime: teamRoomUseRuntime,
        runtime_lane: teamRoomUseRuntime ? (teamRoomRuntimeLane.trim() || null) : null,
      });
      setLastTeamRoomRun(result);
      setSelectedId(result.team_id);
      pushToast({
        tone: 'success',
        title: 'Team Room complete',
        description: `Team #${result.team_id} · ${result.progress.completed}/${result.progress.total}`,
      });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Team Room failed', description: errorMessage(err) });
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
            <label class="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text-muted)]">
              <input
                type="checkbox"
                checked={drillUseRuntime}
                onChange={(event) => setDrillUseRuntime((event.target as HTMLInputElement).checked)}
              />
              Runtime turns
            </label>
            <button
              type="button"
              disabled={busy}
              onClick={runTaskChadDrill}
              class="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2.5 py-1.5 text-[12px] text-[var(--color-text)] hover:border-[var(--color-accent)] disabled:opacity-60"
            >
              <ClipboardList size={14} /> TaskChad Drill
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

              <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
                <form class="grid gap-3" onSubmit={runTeamRoom}>
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex min-w-0 items-center gap-2 text-[13px] font-medium text-[var(--color-text)]">
                      <MessageSquare size={15} /> Team Room
                    </div>
                    <div class="flex flex-wrap gap-2">
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        {teamRoomUseV2 ? 'facilitated_boardroom' : 'classic_boardroom'}
                      </Badge>
                      <Badge className={teamRoomUseRuntime ? 'bg-emerald-500/10 text-emerald-300' : 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]'}>
                        runtime {teamRoomUseRuntime ? 'on' : 'off'}
                      </Badge>
                    </div>
                  </div>
                  <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                    Goal
                    <input
                      value={teamRoomGoal}
                      onInput={(event) => setTeamRoomGoal((event.target as HTMLInputElement).value)}
                      class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                    />
                  </label>
                  <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                    Context
                    <textarea
                      value={teamRoomContext}
                      onInput={(event) => setTeamRoomContext((event.target as HTMLTextAreaElement).value)}
                      class="min-h-[84px] resize-y rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                    />
                  </label>
                  <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(120px,160px)_minmax(150px,220px)_auto]">
                    <div class="flex flex-wrap items-end gap-3">
                      <label class="inline-flex items-center gap-2 rounded border border-[var(--color-border)] px-3 py-2 text-[12px] text-[var(--color-text-muted)]">
                        <input
                          type="checkbox"
                          checked={teamRoomUseV2}
                          onChange={(event) => setTeamRoomUseV2((event.target as HTMLInputElement).checked)}
                        />
                        Facilitated V2
                      </label>
                      <label class="inline-flex items-center gap-2 rounded border border-[var(--color-border)] px-3 py-2 text-[12px] text-[var(--color-text-muted)]">
                        <input
                          type="checkbox"
                          checked={teamRoomUseRuntime}
                          onChange={(event) => setTeamRoomUseRuntime((event.target as HTMLInputElement).checked)}
                        />
                        Runtime turns
                      </label>
                    </div>
                    <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                      Max rounds
                      <input
                        type="number"
                        min="1"
                        max="4"
                        disabled={!teamRoomUseV2}
                        value={teamRoomMaxRounds}
                        onInput={(event) => setTeamRoomMaxRounds((event.target as HTMLInputElement).value)}
                        class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)] disabled:opacity-60"
                      />
                    </label>
                    <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                      Runtime lane
                      <input
                        disabled={!teamRoomUseRuntime}
                        value={teamRoomRuntimeLane}
                        onInput={(event) => setTeamRoomRuntimeLane((event.target as HTMLInputElement).value)}
                        class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)] disabled:opacity-60"
                      />
                    </label>
                    <button
                      type="submit"
                      disabled={busy || !teamRoomGoal.trim()}
                      class="inline-flex items-center justify-center gap-1.5 rounded-md bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white hover:bg-[var(--color-accent-hover)] disabled:opacity-60"
                    >
                      <Play size={14} /> Run Team Room
                    </button>
                  </div>
                </form>
              </div>

              {lastTeamRoomRun && lastTeamRoomRun.team_id === session.id && (
                <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex min-w-0 items-center gap-2 text-[13px] font-medium text-[var(--color-text)]">
                      <MessageSquare size={15} /> Team Room Result
                    </div>
                    <div class="flex flex-wrap gap-2">
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        Team #{lastTeamRoomRun.team_id}
                      </Badge>
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        Convoy #{lastTeamRoomRun.convoy_id}
                      </Badge>
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        {lastTeamRoomRun.meeting_mode}
                      </Badge>
                    </div>
                  </div>
                  <div class="mt-3 grid gap-2 text-[11px] text-[var(--color-text-muted)] sm:grid-cols-2 xl:grid-cols-4">
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Progress</div>
                      <div>{lastTeamRoomRun.progress.completed}/{lastTeamRoomRun.progress.total} · {lastTeamRoomRun.progress.status}</div>
                    </div>
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Turn Summary</div>
                      <div>{lastTeamRoomRun.turn_summary}</div>
                    </div>
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Runtime</div>
                      <div>{lastTeamRoomRun.runtime.enabled ? 'on' : 'off'} · {lastTeamRoomRun.runtime.turn_count} turns · tools {lastTeamRoomRun.runtime.tool_call_count}</div>
                      <div>{formatList(lastTeamRoomRun.runtime.lanes)} · {formatList(lastTeamRoomRun.runtime.providers)}</div>
                    </div>
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Cost / Time</div>
                      <div>{formatCost(lastTeamRoomRun.runtime.cost_usd)}</div>
                      <div>{lastTeamRoomRun.runtime.execution_time_ms ?? 0}ms</div>
                    </div>
                  </div>
                  <div class="mt-4 grid gap-3 xl:grid-cols-[minmax(0,1.4fr)_minmax(280px,0.8fr)]">
                    <div class="grid gap-3">
                      <div>
                        <div class="text-[12px] font-medium text-[var(--color-text)]">Discussion Rounds</div>
                        <div class="mt-2 grid gap-2">
                          {lastTeamRoomRun.discussion_rounds.map((round) => (
                            <div key={round.round_number} class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                              <div class="flex flex-wrap items-center justify-between gap-2">
                                <div class="text-[12px] font-medium text-[var(--color-text)]">Round {round.round_number}</div>
                                <Badge className="bg-[var(--color-card)] text-[var(--color-text-muted)]">
                                  {(round.crosstalk_turns ?? []).length} cross-talk
                                </Badge>
                              </div>
                              {(round.facilitator_turn || round.facilitator_message) && (
                                <div class="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
                                  <div class="font-medium text-[var(--color-text)]">
                                    {round.facilitator_turn?.role_name ?? round.facilitator_message?.from_agent ?? 'Facilitator'}
                                  </div>
                                  <div class="mt-1 whitespace-pre-wrap break-words">
                                    {clipText(round.facilitator_turn?.reply?.body ?? round.facilitator_message?.body, 320)}
                                  </div>
                                </div>
                              )}
                              <div class="mt-2 grid gap-2 md:grid-cols-2">
                                {(round.crosstalk_turns ?? []).map((turn) => (
                                  <div key={`${round.round_number}-${turn.agent_id}-${turn.subtask_id}`} class="rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
                                    <div class="flex flex-wrap items-center justify-between gap-2">
                                      <span class="font-medium text-[var(--color-text)]">{turn.role_name}</span>
                                      <span>{turn.status || 'unknown'} · {turn.completed ? 'complete' : 'open'}</span>
                                    </div>
                                    <div class="mt-1 whitespace-pre-wrap break-words">
                                      {clipText(turn.reply?.body, 220)}
                                    </div>
                                  </div>
                                ))}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="mb-2 text-[12px] font-medium text-[var(--color-text)]">Final Brief</div>
                        <div class="whitespace-pre-wrap break-words text-[12px] leading-5 text-[var(--color-text-muted)]">
                          {lastTeamRoomRun.final_brief}
                        </div>
                      </div>
                    </div>
                    <div class="grid content-start gap-3">
                      <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="text-[12px] font-medium text-[var(--color-text)]">Decision Ledger</div>
                        <div class="mt-2 grid gap-2 text-[11px] text-[var(--color-text-muted)]">
                          {lastTeamRoomRun.decision_ledger.decisions.map((decision) => (
                            <div key={decision} class="break-words">Decision: {decision}</div>
                          ))}
                          <div class="break-words">Strongest objection: {lastTeamRoomRun.decision_ledger.strongest_objection}</div>
                          <div class="break-words">Next trigger: {lastTeamRoomRun.decision_ledger.next_meeting_trigger}</div>
                        </div>
                      </div>
                      <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="text-[12px] font-medium text-[var(--color-text)]">Owner Actions</div>
                        <div class="mt-2 grid gap-2">
                          {lastTeamRoomRun.decision_ledger.owner_actions.map((item) => (
                            <div key={`${item.owner}-${item.action}`} class="rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
                              <div class="font-medium text-[var(--color-text)]">{item.owner}</div>
                              <div class="mt-1 break-words">{item.action}</div>
                              <div class="mt-1 break-words">Signal: {item.validation_signal}</div>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {lastTaskChadDrill && lastTaskChadDrill.team_id === session.id && (
                <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex min-w-0 items-center gap-2 text-[13px] font-medium text-[var(--color-text)]">
                      <ClipboardList size={15} /> TaskChad Drill Result
                    </div>
                    <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                      Convoy #{lastTaskChadDrill.convoy_id}
                    </Badge>
                  </div>
                  <div class="mt-3 text-[12px] font-medium text-[var(--color-text)]">Round 1 Proposals</div>
                  <div class="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                    {lastTaskChadDrill.role_turns.map((turn) => (
                      <div key={turn.agent_id} class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2 text-[11px] text-[var(--color-text-muted)]">
                        <div class="truncate font-medium text-[var(--color-text)]">{turn.role_name}</div>
                        <div class="mt-1 break-words">{turn.role} · subtask #{turn.subtask_id}</div>
                        <div>{turn.status || 'unknown'} · {turn.completed ? 'complete' : 'open'}</div>
                      </div>
                    ))}
                  </div>
                  {lastTaskChadDrill.revision_turns && lastTaskChadDrill.revision_turns.length > 0 && (
                    <>
                      <div class="mt-3 text-[12px] font-medium text-[var(--color-text)]">Round 2 Revisions</div>
                      <div class="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                        {lastTaskChadDrill.revision_turns.map((turn) => (
                          <div key={`${turn.agent_id}-revision`} class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2 text-[11px] text-[var(--color-text-muted)]">
                            <div class="truncate font-medium text-[var(--color-text)]">{turn.role_name}</div>
                            <div class="mt-1 break-words">{turn.role} · revised subtask #{turn.subtask_id}</div>
                            <div>{turn.status || 'unknown'} · {turn.completed ? 'complete' : 'open'}</div>
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                  <div class="mt-3 rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                    <div class="mb-2 text-[12px] font-medium text-[var(--color-text)]">Final Plan</div>
                    <div class="whitespace-pre-wrap break-words text-[12px] leading-5 text-[var(--color-text-muted)]">
                      {lastTaskChadDrill.final_plan}
                    </div>
                  </div>
                </div>
              )}

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
                        <label class="flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                          <input
                            type="checkbox"
                            checked={tickExecuteRunning}
                            onChange={(event) => setTickExecuteRunning((event.target as HTMLInputElement).checked)}
                          />
                          Executor step
                        </label>
                        <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                          Tick command
                          <select
                            value={tickExecutorCommand}
                            onChange={(event) => setTickExecutorCommand((event.target as HTMLSelectElement).value)}
                            class="rounded border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                          >
                            {EXECUTOR_COMMANDS.map(([value, label]) => (
                              <option key={value} value={value}>{label}</option>
                            ))}
                          </select>
                        </label>
                        <label class="flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                          <input
                            type="checkbox"
                            checked={tickCompleteOnExecutorSuccess}
                            onChange={(event) => setTickCompleteOnExecutorSuccess((event.target as HTMLInputElement).checked)}
                          />
                          Complete after executor success
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
                            {lastTeamTick.executor && (
                              <div class="break-words">
                                executor {lastTeamTick.executor.command_key} · exit {lastTeamTick.executor.exit_code ?? 'timeout'} · {lastTeamTick.executor.success ? 'passed' : 'failed'}
                              </div>
                            )}
                            {lastTeamTick.waited && <div>waited</div>}
                            {lastTeamTick.error && <div class="break-words text-red-300">{lastTeamTick.error}</div>}
                          </div>
                        )}
                      </div>
                    </div>
                    <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                      <div class="mb-3 flex items-center gap-2 text-[12px] font-medium text-[var(--color-text)]">
                        <Terminal size={13} /> Executor Step
                      </div>
                      <div class="grid gap-3">
                        <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                          Run as
                          <select
                            value={executorAgent}
                            onChange={(event) => setExecutorAgent((event.target as HTMLSelectElement).value)}
                            class="rounded border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                          >
                            {agentOptions.map((member) => (
                              <option key={member.agent_id} value={member.agent_id}>{member.agent_name || member.agent_id}</option>
                            ))}
                          </select>
                        </label>
                        <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                          Command
                          <select
                            value={executorCommand}
                            onChange={(event) => setExecutorCommand((event.target as HTMLSelectElement).value)}
                            class="rounded border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                          >
                            {EXECUTOR_COMMANDS.map(([value, label]) => (
                              <option key={value} value={value}>{label}</option>
                            ))}
                          </select>
                        </label>
                        <label class="grid gap-1 text-[12px] text-[var(--color-text-muted)]">
                          Cwd override
                          <input
                            value={executorCwd}
                            onInput={(event) => setExecutorCwd((event.target as HTMLInputElement).value)}
                            class="rounded border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
                            placeholder="convoy repo path"
                          />
                        </label>
                        <label class="flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                          <input
                            type="checkbox"
                            checked={executorCompleteOnSuccess}
                            onChange={(event) => setExecutorCompleteOnSuccess((event.target as HTMLInputElement).checked)}
                          />
                          Complete on success
                        </label>
                        <button
                          type="button"
                          disabled={busy || !executorAgent || !session.convoy_id}
                          onClick={runExecutorStep}
                          class="inline-flex items-center justify-center gap-1.5 rounded-md bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white disabled:opacity-60"
                        >
                          <Terminal size={13} /> Run Executor Step
                        </button>
                        {lastExecutorStep && (
                          <div class="rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
                            <div class="font-medium text-[var(--color-text)]">
                              {lastExecutorStep.command_key} · {lastExecutorStep.success ? 'passed' : 'failed'}
                            </div>
                            <div class="break-words">{lastExecutorStep.agent_id} · subtask #{lastExecutorStep.subtask_id}</div>
                            <div>exit {lastExecutorStep.exit_code ?? 'timeout'} · {lastExecutorStep.duration_ms}ms</div>
                            <div class="break-words">{lastExecutorStep.cwd}</div>
                            {(lastExecutorStep.stdout || lastExecutorStep.stderr) && (
                              <pre class="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--color-elevated)] p-2 text-[10px] text-[var(--color-text)]">
                                {lastExecutorStep.stdout || lastExecutorStep.stderr}
                              </pre>
                            )}
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
