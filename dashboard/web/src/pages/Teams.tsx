import { useEffect, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import { Bot, Brain, CheckCircle2, CircleAlert, Inbox, MessageSquare, Play, Plus, RefreshCw, Scale, Send, ShieldAlert, Terminal, Trash2, UserPlus, Vote } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { Modal } from '@/components/Modal';
import { useFetch } from '@/lib/useFetch';
import { apiDelete, apiPost, describeApiError } from '@/lib/api';
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
  metadata?: string | Record<string, unknown> | null;
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

interface TeamRoomMeetingControls {
  agenda: string[];
  facilitator_authority: string[];
  decision_rules: string[];
  round_controls: Array<{
    round_number: number;
    focus: string;
    interrupt_rule: string;
    exit_criteria: string;
  }>;
  stop_conditions: string[];
}

interface TeamRoomVote {
  role: string;
  role_name: string;
  recommendation: string;
  confidence: number;
  rationale: string;
  blocking_issue?: string | null;
}

interface TeamRoomInterrupt {
  from_role: string;
  from_role_name: string;
  target_role: string;
  target_role_name: string;
  severity: string;
  challenge: string;
  required_response: string;
}

interface TeamRoomRoleMemory {
  role: string;
  role_name: string;
  previous_meeting_id?: number | null;
  carried_forward: string[];
  current_commitment: string;
  watch_item: string;
}

interface TeamRoomSynthesis {
  decision_summary: string;
  confidence: number;
  agreements: string[];
  disagreements: string[];
}

interface TeamRoomArtifacts {
  meeting_behavior_version?: string | null;
  meeting_mode?: string | null;
  goal_excerpt?: string | null;
  vote_board: TeamRoomVote[];
  interrupts: TeamRoomInterrupt[];
  role_memory: TeamRoomRoleMemory[];
  synthesis?: TeamRoomSynthesis | null;
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
  meeting_controls: TeamRoomMeetingControls;
  discussion_rounds: TeamRoomDiscussionRound[];
  vote_board: TeamRoomVote[];
  interrupts: TeamRoomInterrupt[];
  role_memory: TeamRoomRoleMemory[];
  synthesis: TeamRoomSynthesis;
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

interface OperatingRoomProofPacket {
  run_id: string;
  created_at: string;
  product_surface: string;
  sanitized: boolean;
  goal: string;
  workflow_id: string;
  meeting_mode: string;
  team_id: number;
  convoy_id: number;
  progress: TeamRoomRunResponse['progress'];
  runtime: TeamRoomRuntimeSummary;
  vote_board: TeamRoomVote[];
  interrupts: TeamRoomInterrupt[];
  owner_actions: TeamRoomDecisionLedger['owner_actions'];
  decisions: string[];
  open_questions: string[];
  strongest_objection?: string | null;
  next_meeting_trigger?: string | null;
  synthesis: TeamRoomSynthesis;
  tick_summary?: {
    selected_action?: string | null;
    reason?: string | null;
    agent_id?: string | null;
    convoy_id?: number | null;
    subtask_id?: number | null;
    waited?: boolean | null;
    error?: string | null;
    step_action?: string | null;
    step_claimed_count?: number | null;
    step_status?: string | null;
    executor_command?: string | null;
    executor_success?: boolean | null;
    executor_exit_code?: number | null;
    executor_completed?: boolean | null;
  } | null;
  final_brief: string;
}

interface OperatingRoomRunResponse {
  run_id: string;
  created_at: string;
  team_room: TeamRoomRunResponse;
  tick?: TeamTickResponse | null;
  proof_packet: OperatingRoomProofPacket;
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
  return describeApiError(err);
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

function formatConfidence(value?: number | null): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '0.00';
}

function confidencePercent(value?: number | null): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value * 100)));
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

function interruptTone(severity: string): string {
  if (severity === 'blocker') return 'bg-red-500/10 text-red-300';
  if (severity === 'control') return 'bg-sky-500/10 text-sky-300';
  if (severity === 'review') return 'bg-amber-500/10 text-amber-300';
  return 'bg-orange-500/10 text-orange-300';
}

function TeamRoomArtifactPanel({
  title,
  icon,
  meta,
  children,
}: {
  title: string;
  icon: ComponentChildren;
  meta?: ComponentChildren;
  children: ComponentChildren;
}) {
  return (
    <section class="min-w-0 rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex min-w-0 items-center gap-2 text-[12px] font-medium text-[var(--color-text)]">
          {icon}
          <span class="min-w-0 truncate">{title}</span>
        </div>
        {meta && <div class="text-[11px] text-[var(--color-text-muted)]">{meta}</div>}
      </div>
      <div class="mt-3">{children}</div>
    </section>
  );
}

function metadataObject(value?: string | Record<string, unknown> | null): Record<string, unknown> | null {
  if (!value) return null;
  if (typeof value === 'object') return value;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function normalizeTeamRoomArtifacts(value: Partial<TeamRoomArtifacts> | null | undefined): TeamRoomArtifacts | null {
  if (!value) return null;
  const voteBoard = Array.isArray(value.vote_board) ? value.vote_board : [];
  const interrupts = Array.isArray(value.interrupts) ? value.interrupts : [];
  const roleMemory = Array.isArray(value.role_memory) ? value.role_memory : [];
  const synthesis = value.synthesis && typeof value.synthesis === 'object' ? value.synthesis : null;
  if (voteBoard.length === 0 && interrupts.length === 0 && roleMemory.length === 0 && !synthesis) {
    return null;
  }
  return {
    meeting_behavior_version: value.meeting_behavior_version ?? null,
    meeting_mode: value.meeting_mode ?? null,
    goal_excerpt: value.goal_excerpt ?? null,
    vote_board: voteBoard,
    interrupts,
    role_memory: roleMemory,
    synthesis,
  };
}

function teamRoomArtifactsFromSession(session: TeamSession | null): TeamRoomArtifacts | null {
  const metadata = metadataObject(session?.metadata);
  if (!metadata) return null;
  const workflow = typeof metadata.workflow === 'string' ? metadata.workflow : null;
  const version = typeof metadata.meeting_behavior_version === 'string' ? metadata.meeting_behavior_version : null;
  if (workflow !== 'team_room' && version !== 'v3') return null;
  return normalizeTeamRoomArtifacts({
    meeting_behavior_version: version,
    meeting_mode: typeof metadata.meeting_mode === 'string' ? metadata.meeting_mode : null,
    goal_excerpt: typeof metadata.goal_excerpt === 'string' ? metadata.goal_excerpt : null,
    vote_board: metadata.vote_board as TeamRoomVote[] | undefined,
    interrupts: metadata.interrupts as TeamRoomInterrupt[] | undefined,
    role_memory: metadata.role_memory as TeamRoomRoleMemory[] | undefined,
    synthesis: metadata.synthesis as TeamRoomSynthesis | undefined,
  });
}

function TeamRoomArtifactsGrid({ artifacts }: { artifacts: TeamRoomArtifacts }) {
  return (
    <div class="mt-2 grid gap-3 xl:grid-cols-2">
      <TeamRoomArtifactPanel
        title="Vote + Confidence Board"
        icon={<Vote size={14} />}
        meta={`Overall ${formatConfidence(artifacts.synthesis?.confidence)}`}
      >
        <div class="grid gap-2">
          {artifacts.vote_board.map((vote) => (
            <div key={vote.role} class="rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="font-medium text-[var(--color-text)]">{vote.role_name}</div>
                <div>{formatConfidence(vote.confidence)}</div>
              </div>
              <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--color-border)]">
                <div
                  class="h-full rounded-full bg-[var(--color-accent)]"
                  style={{ width: `${confidencePercent(vote.confidence)}%` }}
                />
              </div>
              <div class="mt-2 break-words">{vote.recommendation}</div>
              <div class="mt-1 break-words">Rationale: {vote.rationale}</div>
              {vote.blocking_issue && <div class="mt-1 break-words">Blocker: {vote.blocking_issue}</div>}
            </div>
          ))}
        </div>
      </TeamRoomArtifactPanel>
      <TeamRoomArtifactPanel
        title="Role Memory"
        icon={<Brain size={14} />}
        meta={`${artifacts.role_memory.length} roles`}
      >
        <div class="grid gap-2">
          {artifacts.role_memory.map((memory) => (
            <div key={memory.role} class="rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="font-medium text-[var(--color-text)]">{memory.role_name}</div>
                <div>{memory.previous_meeting_id ? `Prior meeting #${memory.previous_meeting_id}` : 'No prior meeting'}</div>
              </div>
              <div class="mt-2 break-words">Commitment: {memory.current_commitment || 'No current commitment recorded.'}</div>
              <div class="mt-1 break-words">Watch: {memory.watch_item || 'none'}</div>
              {(memory.carried_forward ?? []).length > 0 && (
                <div class="mt-2 grid gap-1">
                  {(memory.carried_forward ?? []).map((item) => (
                    <div key={`${memory.role}-${item}`} class="break-words">Carry-forward: {item}</div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </TeamRoomArtifactPanel>
      <TeamRoomArtifactPanel
        title="Interrupts + Challenges"
        icon={<CircleAlert size={14} />}
        meta={`${artifacts.interrupts.length} logged`}
      >
        <div class="grid gap-2">
          {artifacts.interrupts.map((interrupt) => (
            <div key={`${interrupt.from_role}-${interrupt.target_role}-${interrupt.severity}-${interrupt.challenge}`} class="rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="font-medium text-[var(--color-text)]">
                  {interrupt.from_role_name} to {interrupt.target_role_name}
                </div>
                <Badge className={interruptTone(interrupt.severity)}>
                  {interrupt.severity}
                </Badge>
              </div>
              <div class="mt-2 break-words">{interrupt.challenge}</div>
              <div class="mt-1 break-words">Required: {interrupt.required_response}</div>
            </div>
          ))}
        </div>
      </TeamRoomArtifactPanel>
      <TeamRoomArtifactPanel
        title="Agreements / Disagreements"
        icon={<Scale size={14} />}
        meta="synthesis"
      >
        <div class="grid gap-3 text-[11px] text-[var(--color-text-muted)] md:grid-cols-2">
          <div class="min-w-0">
            <div class="flex items-center gap-1.5 font-medium text-[var(--color-text)]">
              <CheckCircle2 size={13} /> Agreements
            </div>
            <div class="mt-2 grid gap-1">
              {(artifacts.synthesis?.agreements ?? []).map((item) => (
                <div key={item} class="break-words">{item}</div>
              ))}
            </div>
          </div>
          <div class="min-w-0">
            <div class="flex items-center gap-1.5 font-medium text-[var(--color-text)]">
              <ShieldAlert size={13} /> Disagreements
            </div>
            <div class="mt-2 grid gap-1">
              {(artifacts.synthesis?.disagreements ?? []).map((item) => (
                <div key={item} class="break-words">{item}</div>
              ))}
            </div>
          </div>
        </div>
        <div class="mt-3 rounded border border-[var(--color-border)] bg-[var(--color-card)] p-2 text-[11px] text-[var(--color-text-muted)]">
          <div class="font-medium text-[var(--color-text)]">Decision Summary</div>
          <div class="mt-1 break-words">{artifacts.synthesis?.decision_summary ?? 'No decision summary recorded.'}</div>
        </div>
      </TeamRoomArtifactPanel>
    </div>
  );
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
  const [lastTeamRoomRun, setLastTeamRoomRun] = useState<TeamRoomRunResponse | null>(null);
  const [lastOperatingRoomRun, setLastOperatingRoomRun] = useState<OperatingRoomRunResponse | null>(null);
  const [teamRoomGoal, setTeamRoomGoal] = useState('How should the team prioritize the next release?');
  const [teamRoomContext, setTeamRoomContext] = useState('');
  const [teamRoomUseV2, setTeamRoomUseV2] = useState(true);
  const [teamRoomUseRuntime, setTeamRoomUseRuntime] = useState(false);
  const [teamRoomRuntimeLane, setTeamRoomRuntimeLane] = useState('generic_runtime');
  const [teamRoomMaxRounds, setTeamRoomMaxRounds] = useState('2');
  const [operatingRoomRunTick, setOperatingRoomRunTick] = useState(true);
  const currentOperatingRoomRun = lastOperatingRoomRun && session && lastOperatingRoomRun.team_room.team_id === session.id
    ? lastOperatingRoomRun
    : null;
  const currentTeamRoomRun = currentOperatingRoomRun
    ? currentOperatingRoomRun.team_room
    : lastTeamRoomRun && session && lastTeamRoomRun.team_id === session.id
    ? lastTeamRoomRun
    : null;
  const currentTeamRoomArtifacts = currentTeamRoomRun
    ? normalizeTeamRoomArtifacts(currentTeamRoomRun)
    : null;
  const currentOperatingRoomProof = currentOperatingRoomRun?.proof_packet ?? null;
  const selectedTeamRoomArtifacts = teamRoomArtifactsFromSession(session);

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

  async function runTeamRoom(event: Event) {
    event.preventDefault();
    const goal = teamRoomGoal.trim();
    if (!goal) {
      pushToast({ tone: 'error', title: 'Operating Room goal required' });
      return;
    }
    const parsedRounds = Number(teamRoomMaxRounds);
    const maxRounds = Number.isFinite(parsedRounds)
      ? Math.min(4, Math.max(1, Math.trunc(parsedRounds)))
      : 2;
    setBusy(true);
    try {
      const result = await apiPost<OperatingRoomRunResponse>('/api/team/operating-room/run', {
        goal,
        context: teamRoomContext.trim() || null,
        meeting_mode: teamRoomUseV2 ? 'facilitated_boardroom' : 'classic_boardroom',
        max_rounds: teamRoomUseV2 ? maxRounds : null,
        use_runtime: teamRoomUseRuntime,
        runtime_lane: teamRoomUseRuntime ? (teamRoomRuntimeLane.trim() || null) : null,
        run_tick: operatingRoomRunTick,
      });
      setLastOperatingRoomRun(result);
      setLastTeamRoomRun(result.team_room);
      if (result.tick) setLastTeamTick(result.tick);
      setSelectedId(result.team_room.team_id);
      pushToast({
        tone: 'success',
        title: 'Operating Room complete',
        description: `Team #${result.team_room.team_id} · proof ${result.proof_packet.sanitized ? 'sanitized' : 'raw'}`,
      });
      refreshAll();
    } catch (err: unknown) {
      pushToast({ tone: 'error', title: 'Operating Room failed', description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Operating Room"
        subtitle={`${activeCount} active rooms · ${teams.length} total teams`}
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

              <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
                <form class="grid gap-3" onSubmit={runTeamRoom}>
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex min-w-0 items-center gap-2 text-[13px] font-medium text-[var(--color-text)]">
                      <MessageSquare size={15} /> Operating Room Run
                    </div>
                    <div class="flex flex-wrap gap-2">
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        {teamRoomUseV2 ? 'facilitated_boardroom' : 'classic_boardroom'}
                      </Badge>
                      <Badge className={teamRoomUseRuntime ? 'bg-emerald-500/10 text-emerald-300' : 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]'}>
                        runtime {teamRoomUseRuntime ? 'on' : 'off'}
                      </Badge>
                      <Badge className={operatingRoomRunTick ? 'bg-sky-500/10 text-sky-300' : 'bg-[var(--color-elevated)] text-[var(--color-text-muted)]'}>
                        tick {operatingRoomRunTick ? 'on' : 'off'}
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
                      <label class="inline-flex items-center gap-2 rounded border border-[var(--color-border)] px-3 py-2 text-[12px] text-[var(--color-text-muted)]">
                        <input
                          type="checkbox"
                          checked={operatingRoomRunTick}
                          onChange={(event) => setOperatingRoomRunTick((event.target as HTMLInputElement).checked)}
                        />
                        Auto tick
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
                      <Play size={14} /> Run Operating Room
                    </button>
                  </div>
                </form>
              </div>

              {!currentTeamRoomRun && selectedTeamRoomArtifacts && (
                <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex min-w-0 items-center gap-2 text-[13px] font-medium text-[var(--color-text)]">
                      <Brain size={15} /> Persisted Operating Room Artifacts
                    </div>
                    <div class="flex flex-wrap gap-2">
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        persisted
                      </Badge>
                      {selectedTeamRoomArtifacts.meeting_mode && (
                        <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                          {selectedTeamRoomArtifacts.meeting_mode}
                        </Badge>
                      )}
                      {selectedTeamRoomArtifacts.goal_excerpt && (
                        <Badge className="max-w-full truncate bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                          {clipText(selectedTeamRoomArtifacts.goal_excerpt, 80)}
                        </Badge>
                      )}
                    </div>
                  </div>
                  <TeamRoomArtifactsGrid artifacts={selectedTeamRoomArtifacts} />
                </div>
              )}

              {currentTeamRoomRun && currentTeamRoomArtifacts && (
                <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-4">
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex min-w-0 items-center gap-2 text-[13px] font-medium text-[var(--color-text)]">
                      <MessageSquare size={15} /> Operating Room Proof
                    </div>
                    <div class="flex flex-wrap gap-2">
                      {currentOperatingRoomProof && (
                        <Badge className="max-w-full truncate bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                          {currentOperatingRoomProof.run_id}
                        </Badge>
                      )}
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        Team #{currentTeamRoomRun.team_id}
                      </Badge>
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        Convoy #{currentTeamRoomRun.convoy_id}
                      </Badge>
                      {currentOperatingRoomProof?.sanitized && (
                        <Badge className="bg-emerald-500/10 text-emerald-300">
                          sanitized
                        </Badge>
                      )}
                      <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                        {currentTeamRoomRun.meeting_mode}
                      </Badge>
                    </div>
                  </div>
                  {currentOperatingRoomProof && (
                    <div class="mt-3 grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(260px,0.8fr)]">
                      <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
                          <div class="text-[12px] font-medium text-[var(--color-text)]">Final Brief</div>
                          <Badge className="bg-[var(--color-card)] text-[var(--color-text-muted)]">
                            {formatConfidence(currentOperatingRoomProof.synthesis?.confidence)} confidence
                          </Badge>
                        </div>
                        <div class="whitespace-pre-wrap break-words text-[12px] leading-5 text-[var(--color-text-muted)]">
                          {currentOperatingRoomProof.final_brief}
                        </div>
                      </div>
                      <div class="grid gap-3">
                        <div class="grid gap-2 text-[11px] text-[var(--color-text-muted)] sm:grid-cols-2">
                          <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                            <div class="font-medium text-[var(--color-text)]">Vote Board</div>
                            <div>{currentOperatingRoomProof.vote_board.length} votes</div>
                          </div>
                          <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                            <div class="font-medium text-[var(--color-text)]">Interrupts</div>
                            <div>{currentOperatingRoomProof.interrupts.length} challenges</div>
                          </div>
                          <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                            <div class="font-medium text-[var(--color-text)]">Owner Actions</div>
                            <div>{currentOperatingRoomProof.owner_actions.length} assigned</div>
                          </div>
                          <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                            <div class="font-medium text-[var(--color-text)]">Tick</div>
                            <div>{currentOperatingRoomProof.tick_summary?.selected_action ?? 'not run'}</div>
                          </div>
                        </div>
                        {currentOperatingRoomProof.tick_summary && (
                          <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3 text-[11px] text-[var(--color-text-muted)]">
                            <div class="font-medium text-[var(--color-text)]">Tick / Executor Summary</div>
                            <div class="mt-1 break-words">{currentOperatingRoomProof.tick_summary.reason ?? 'No reason recorded.'}</div>
                            <div class="mt-1 break-words">
                              {currentOperatingRoomProof.tick_summary.agent_id ?? 'no agent'} · subtask #{currentOperatingRoomProof.tick_summary.subtask_id ?? 'none'} · {currentOperatingRoomProof.tick_summary.step_status ?? 'unknown'}
                            </div>
                            {currentOperatingRoomProof.tick_summary.executor_command && (
                              <div class="mt-1 break-words">
                                executor {currentOperatingRoomProof.tick_summary.executor_command} · {currentOperatingRoomProof.tick_summary.executor_success ? 'passed' : 'not passed'}
                              </div>
                            )}
                            {currentOperatingRoomProof.tick_summary.error && (
                              <div class="mt-1 break-words text-red-300">{currentOperatingRoomProof.tick_summary.error}</div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                  <div class="mt-3 grid gap-2 text-[11px] text-[var(--color-text-muted)] sm:grid-cols-2 xl:grid-cols-5">
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Progress</div>
                      <div>{currentTeamRoomRun.progress.completed}/{currentTeamRoomRun.progress.total} · {currentTeamRoomRun.progress.status}</div>
                    </div>
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Turn Summary</div>
                      <div>{currentTeamRoomRun.turn_summary}</div>
                    </div>
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Confidence</div>
                      <div>{formatConfidence(currentTeamRoomRun.synthesis?.confidence)}</div>
                      <div>{(currentTeamRoomRun.vote_board ?? []).length} votes · {(currentTeamRoomRun.interrupts ?? []).length} interrupts</div>
                    </div>
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Runtime</div>
                      <div>{currentTeamRoomRun.runtime.enabled ? 'on' : 'off'} · {currentTeamRoomRun.runtime.turn_count} turns · tools {currentTeamRoomRun.runtime.tool_call_count}</div>
                      <div>{formatList(currentTeamRoomRun.runtime.lanes)} · {formatList(currentTeamRoomRun.runtime.providers)}</div>
                    </div>
                    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-2">
                      <div class="font-medium text-[var(--color-text)]">Cost / Time</div>
                      <div>{formatCost(currentTeamRoomRun.runtime.cost_usd)}</div>
                      <div>{currentTeamRoomRun.runtime.execution_time_ms ?? 0}ms</div>
                    </div>
                  </div>
                  <div class="mt-4">
                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <div class="text-[12px] font-medium text-[var(--color-text)]">V3 Meeting Artifacts</div>
                      <div class="flex flex-wrap gap-2">
                        <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                          {formatConfidence(currentTeamRoomArtifacts.synthesis?.confidence)} confidence
                        </Badge>
                        <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                          {currentTeamRoomArtifacts.vote_board.length} votes
                        </Badge>
                        <Badge className="bg-[var(--color-elevated)] text-[var(--color-text-muted)]">
                          {currentTeamRoomArtifacts.interrupts.length} interrupts
                        </Badge>
                      </div>
                    </div>
                    <TeamRoomArtifactsGrid artifacts={currentTeamRoomArtifacts} />
                  </div>
                  <div class="mt-4 grid gap-3 xl:grid-cols-[minmax(0,1.4fr)_minmax(280px,0.8fr)]">
                    <div class="grid gap-3">
                      <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="text-[12px] font-medium text-[var(--color-text)]">Facilitator Controls</div>
                        <div class="mt-2 grid gap-2 text-[11px] text-[var(--color-text-muted)] md:grid-cols-2">
                          {(currentTeamRoomRun.meeting_controls?.decision_rules ?? []).map((rule) => (
                            <div key={rule} class="break-words">Rule: {rule}</div>
                          ))}
                          {(currentTeamRoomRun.meeting_controls?.round_controls ?? []).map((round) => (
                            <div key={`round-control-${round.round_number}`} class="break-words">
                              Round {round.round_number}: {round.focus}
                            </div>
                          ))}
                        </div>
                      </div>
                      <div>
                        <div class="text-[12px] font-medium text-[var(--color-text)]">Discussion Rounds</div>
                        <div class="mt-2 grid gap-2">
                          {currentTeamRoomRun.discussion_rounds.map((round) => (
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
                          {currentTeamRoomRun.final_brief}
                        </div>
                      </div>
                    </div>
                    <div class="grid content-start gap-3">
                      <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="text-[12px] font-medium text-[var(--color-text)]">Decision Ledger</div>
                        <div class="mt-2 grid gap-2 text-[11px] text-[var(--color-text-muted)]">
                          {currentTeamRoomRun.decision_ledger.decisions.map((decision) => (
                            <div key={decision} class="break-words">Decision: {decision}</div>
                          ))}
                          <div class="break-words">Strongest objection: {currentTeamRoomRun.decision_ledger.strongest_objection}</div>
                          <div class="break-words">Next trigger: {currentTeamRoomRun.decision_ledger.next_meeting_trigger}</div>
                        </div>
                      </div>
                      <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                        <div class="text-[12px] font-medium text-[var(--color-text)]">Owner Actions</div>
                        <div class="mt-2 grid gap-2">
                          {currentTeamRoomRun.decision_ledger.owner_actions.map((item) => (
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
