import { describe, test, expect, beforeEach, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { Agents } from '@/pages/Agents';
import { Memories } from '@/pages/Memories';
import { Scheduled } from '@/pages/Scheduled';
import { WorkQueue } from '@/pages/WorkQueue';
import { Convoy } from '@/pages/Convoy';
import { Teams } from '@/pages/Teams';
import { Usage } from '@/pages/Usage';
import { Jarvis } from '@/pages/Jarvis';
import { CapabilityGateway } from '@/pages/CapabilityGateway';

function mockFetchOnce(payload: unknown) {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } }),
  ) as any;
}

describe('panels populate from fixture API responses', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test('Agents page renders agent name from /api/agents', async () => {
    mockFetchOnce({
      agents: [
        { id: 'main', name: 'Homie', description: 'Default', model: 'claude-opus-4-7', running: true, todayTurns: 12, lane: 'claude_native', planQuotaPct: 8 },
      ],
    });
    render(<Agents />);
    await waitFor(() => expect(screen.getByText('Homie')).toBeInTheDocument());
  });

  test('Memories page renders memory text', async () => {
    globalThis.fetch = vi.fn(async (url: string) => {
      if (url.includes('/api/brain/graph')) {
        return new Response(JSON.stringify({
          nodes: [
            {
              id: 'chunk:1',
              label: 'Mission Control',
              kind: 'chunk',
              scope_type: 'global',
              scope_id: 'main',
              source_path: 'daily/2026-05-15.md',
              section_title: 'Mission Control',
              text: 'Hello world memory',
              tags: ['vault-chunk'],
              created_at: Date.now() / 1000 - 60,
            },
          ],
          edges: [],
          stats: { total_nodes: 1, total_edges: 0, total_chunks: 1 },
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return new Response(JSON.stringify({
        memories: [
          {
            id: 1,
            persona_id: 'main',
            source_path: 'daily/2026-05-15.md',
            chunk_text: 'Hello world memory',
            tags: ['vault-chunk'],
            created_at: Date.now() / 1000 - 60,
          },
        ],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }) as any;
    render(<Memories />);
    await waitFor(() => expect(screen.getByText(/hello world memory/i)).toBeInTheDocument());
    expect(screen.getByText(/daily\/2026-05-15\.md/i)).toBeInTheDocument();
  });

  test('Memories list tab renders memory rows', async () => {
    globalThis.fetch = vi.fn(async (url: string) => {
      if (url.includes('/api/brain/graph')) {
        return new Response(JSON.stringify({ nodes: [], edges: [], stats: {} }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({
        memories: [
          {
            id: 1,
            persona_id: 'main',
            source_path: 'daily/2026-05-15.md',
            chunk_text: 'Hello world memory',
            tags: ['vault-chunk'],
            created_at: Date.now() / 1000 - 60,
          },
        ],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }) as any;
    render(<Memories />);
    fireEvent.click(screen.getByRole('button', { name: /memory list/i }));
    await waitFor(() => expect(screen.getByText(/hello world memory/i)).toBeInTheDocument());
    expect(screen.getByText(/daily\/2026-05-15\.md/i)).toBeInTheDocument();
  });

  test('Scheduled page renders task prompt', async () => {
    mockFetchOnce({
      tasks: [
        { taskId: 't1', personaId: 'main', cron: '0 9 * * *', prompt: 'Daily standup', enabled: true },
      ],
    });
    render(<Scheduled />);
    await waitFor(() => expect(screen.getByText(/daily standup/i)).toBeInTheDocument());
  });

  test('Work Queue page renders orchestration task cards', async () => {
    mockFetchOnce({
      tasks: [
        {
          id: 7,
          task_id: 7,
          convoy_id: 2,
          convoy_title: 'Dashboard slice',
          title: 'Wire task board',
          description: 'Expose Homie orchestration subtasks.',
          status: 'ready',
          assigned_agent_id: 'codex',
          assigned_agent_name: 'Codex',
          remaining_dependencies: 0,
          priority: 'high',
          tags: ['dashboard'],
          updated_at: 1770000000,
        },
      ],
      columns: [
        { id: 'ready', label: 'Ready' },
        { id: 'running', label: 'Running' },
      ],
      summary: { total: 1, ready: 1, running: 0 },
    });
    render(<WorkQueue />);
    await waitFor(() => expect(screen.getByText(/wire task board/i)).toBeInTheDocument());
    expect(screen.getByText(/dashboard slice/i)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /dispatch/i }).length).toBeGreaterThan(0);
  });

  test('Work Queue page explains offline local stack instead of raw fetch errors', async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }) as any;
    render(<WorkQueue />);
    await waitFor(() => expect(screen.getByText(/local stack is offline/i)).toBeInTheDocument());
    expect(screen.queryByText(/typeerror/i)).not.toBeInTheDocument();
  });

  test('Convoy page renders dependency graph, subtasks, and mailbox', async () => {
    globalThis.fetch = vi.fn(async (url: string) => {
      const path = String(url);
      if (path === '/api/convoy') {
        return new Response(JSON.stringify([
          {
            id: 1,
            title: 'Plan Dashboard DAG',
            description: 'Coordinate the teammate graph slice.',
            status: 'active',
            decomposition_mode: 'manual',
            created_by: 'dashboard',
            base_branch: 'main',
            merge_strategy: 'squash',
            total_subtasks: 2,
            completed_subtasks: 1,
            failed_subtasks: 0,
            updated_at: 1770000000,
          },
        ]), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/team') {
        return new Response(JSON.stringify([
          { id: 9, team_name: 'DAG Team', status: 'active', convoy_id: 1, backend_type: 'local' },
        ]), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/convoy/1') {
        return new Response(JSON.stringify({
          convoy: {
            id: 1,
            title: 'Plan Dashboard DAG',
            description: 'Coordinate the teammate graph slice.',
            status: 'active',
            decomposition_mode: 'manual',
            created_by: 'dashboard',
            base_branch: 'main',
            merge_strategy: 'squash',
            total_subtasks: 2,
            completed_subtasks: 1,
            failed_subtasks: 0,
            updated_at: 1770000000,
          },
          subtasks: [
            {
              id: 11,
              convoy_id: 1,
              title: 'Implement DAG page',
              status: 'completed',
              assigned_agent_id: 'codex',
              assigned_agent_name: 'Codex',
              remaining_dependencies: 0,
              seq: 0,
              updated_at: 1770000000,
            },
            {
              id: 12,
              convoy_id: 1,
              title: 'Bind team controls',
              status: 'ready',
              assigned_agent_id: 'team-worker',
              remaining_dependencies: 0,
              seq: 1,
              updated_at: 1770000000,
            },
          ],
          edges: [{ id: 1, from_subtask_id: 11, to_subtask_id: 12 }],
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/mailbox/convoy/1') {
        return new Response(JSON.stringify([
          {
            message: {
              id: 21,
              from_agent: 'codex',
              subject: 'Handoff',
              body: 'Ready for team dispatch.',
              message_type: 'message',
              created_at: 1770000000,
            },
            deliveries: [{ id: 31, recipient_agent: 'team-worker', status: 'pending' }],
          },
        ]), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return new Response(JSON.stringify({}), { status: 404, headers: { 'content-type': 'application/json' } });
    }) as any;

    render(<Convoy />);
    await waitFor(() => expect(screen.getAllByText(/plan dashboard dag/i).length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByText(/implement dag page/i)).toBeInTheDocument());
    expect(screen.getByLabelText(/convoy dependency graph/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /mailbox/i }));
    await waitFor(() => expect(screen.getByText(/ready for team dispatch/i)).toBeInTheDocument());
  });

  test('Convoy page explains offline local stack instead of raw fetch errors', async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }) as any;
    render(<Convoy />);
    await waitFor(() => expect(screen.getAllByText(/local stack is offline/i).length).toBeGreaterThan(0));
    expect(screen.queryByText(/typeerror/i)).not.toBeInTheDocument();
  });

  test('Teams page renders framework-owned team session detail', async () => {
    const requests: string[] = [];
    globalThis.fetch = vi.fn(async (url: string) => {
      const path = String(url);
      requests.push(path);
      if (path === '/api/team') {
        return new Response(JSON.stringify([
          {
            id: 9,
            team_name: 'DAG Team',
            lead_agent_id: 'codex',
            lead_agent_name: 'Codex',
            convoy_id: 1,
            status: 'active',
            backend_type: 'local',
            updated_at: 1770000000,
            metadata: JSON.stringify({
              workflow: 'team_room',
              workflow_id: 'growth_boardroom',
              meeting_behavior_version: 'v3',
              meeting_mode: 'facilitated_boardroom',
              goal_excerpt: 'Persisted dashboard artifact proof',
              vote_board: [
                {
                  role: 'sales',
                  role_name: 'Sales',
                  recommendation: 'Persisted vote backs the audit wedge.',
                  confidence: 0.81,
                  rationale: 'Persisted rationale keeps the session useful after reload.',
                  blocking_issue: 'Persisted blocker must stay visible.',
                },
              ],
              interrupts: [
                {
                  from_role: 'ops',
                  from_role_name: 'Ops',
                  target_role: 'sales',
                  target_role_name: 'Sales',
                  severity: 'control',
                  challenge: 'Persisted interrupt keeps owner pressure visible.',
                  required_response: 'Persisted required response must be shown.',
                },
              ],
              role_memory: [
                {
                  role: 'sales',
                  role_name: 'Sales',
                  previous_meeting_id: 8,
                  carried_forward: ['Persisted carry-forward survives reload.'],
                  current_commitment: 'Persisted commitment survives reload.',
                  watch_item: 'Persisted watch item survives reload.',
                },
              ],
              synthesis: {
                decision_summary: 'Persisted summary survives reload.',
                confidence: 0.81,
                agreements: ['Persisted agreement survives reload.'],
                disagreements: ['Persisted disagreement survives reload.'],
              },
            }),
          },
        ]), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/team/9') {
        return new Response(JSON.stringify({
          session: {
            id: 9,
            team_name: 'DAG Team',
            lead_agent_id: 'codex',
            lead_agent_name: 'Codex',
            convoy_id: 1,
            status: 'active',
            backend_type: 'local',
            updated_at: 1770000000,
            metadata: JSON.stringify({
              workflow: 'team_room',
              workflow_id: 'growth_boardroom',
              meeting_behavior_version: 'v3',
              meeting_mode: 'facilitated_boardroom',
              goal_excerpt: 'Persisted dashboard artifact proof',
              vote_board: [
                {
                  role: 'sales',
                  role_name: 'Sales',
                  recommendation: 'Persisted vote backs the audit wedge.',
                  confidence: 0.81,
                  rationale: 'Persisted rationale keeps the session useful after reload.',
                  blocking_issue: 'Persisted blocker must stay visible.',
                },
              ],
              interrupts: [
                {
                  from_role: 'ops',
                  from_role_name: 'Ops',
                  target_role: 'sales',
                  target_role_name: 'Sales',
                  severity: 'control',
                  challenge: 'Persisted interrupt keeps owner pressure visible.',
                  required_response: 'Persisted required response must be shown.',
                },
              ],
              role_memory: [
                {
                  role: 'sales',
                  role_name: 'Sales',
                  previous_meeting_id: 8,
                  carried_forward: ['Persisted carry-forward survives reload.'],
                  current_commitment: 'Persisted commitment survives reload.',
                  watch_item: 'Persisted watch item survives reload.',
                },
              ],
              synthesis: {
                decision_summary: 'Persisted summary survives reload.',
                confidence: 0.81,
                agreements: ['Persisted agreement survives reload.'],
                disagreements: ['Persisted disagreement survives reload.'],
              },
            }),
          },
          members: [
            {
              id: 91,
              team_session_id: 9,
              agent_id: 'team-worker',
              agent_name: 'Team Worker',
              role: 'worker',
              subtask_id: 12,
              status: 'active',
              joined_at: 1770000000,
              last_activity_at: 1770000100,
            },
            {
              id: 92,
              team_session_id: 9,
              agent_id: 'sales-worker',
              agent_name: 'Sales Worker',
              role: 'worker',
              subtask_id: 13,
              status: 'active',
              joined_at: 1770000000,
              last_activity_at: 1770000100,
            },
          ],
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/mailbox/convoy/1') {
        return new Response(JSON.stringify([
          {
            message: {
              id: 301,
              from_agent: 'team-worker',
              subject: 'Marketing handoff',
              body: 'Sales needs the landing page angle.',
              message_type: 'message',
              msg_type: 'team_message',
              convoy_id: 1,
              created_at: 1770000200,
            },
            deliveries: [{ id: 401, recipient_agent: 'sales-worker', status: 'pending' }],
          },
        ]), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/mailbox/send') {
        return new Response(JSON.stringify({
          id: 302,
          from_agent: 'team-worker',
          subject: 'Sales reply',
          body: 'Lead list is ready.',
          created_at: 1770000300,
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path.startsWith('/api/mailbox/claim/sales-worker')) {
        return new Response(JSON.stringify([
          {
            message: {
              id: 301,
              from_agent: 'team-worker',
              subject: 'Marketing handoff',
              body: 'Sales needs the landing page angle.',
              message_type: 'message',
              msg_type: 'team_message',
              convoy_id: 1,
              created_at: 1770000200,
            },
            deliveries: [{ id: 401, recipient_agent: 'sales-worker', status: 'claimed', claim_token: 'claim-1' }],
          },
        ]), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/team/9/loop-step') {
        return new Response(JSON.stringify({
          agent_id: 'sales-worker',
          subtask_id: 13,
          claimed_count: 1,
          action: 'running',
          completed: false,
          convoy_completed: false,
          subtask_after: { status: 'running' },
          runtime: {
            runtime_lane: 'generic_runtime',
            provider: 'codex',
            model: 'test-model',
            session_id: 'runtime-session',
            tool_call_count: 0,
          },
          reply: {
            id: 303,
            from_agent: 'sales-worker',
            body: 'Loop step complete.',
            message_type: 'handoff',
            msg_type: 'work_handoff',
            created_at: 1770000400,
          },
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/team/9/tick') {
        return new Response(JSON.stringify({
          team_id: 9,
          selected_action: 'claim_respond',
          reason: '1 pending convoy mailbox item(s)',
          agent_id: 'sales-worker',
          convoy_id: 1,
          subtask_id: 13,
          waited: false,
          error: null,
          step: {
            agent_id: 'sales-worker',
            subtask_id: 13,
            claimed_count: 1,
            action: 'running',
            completed: false,
            convoy_completed: false,
            subtask_after: { status: 'running' },
            runtime: null,
            reply: {
              id: 304,
              from_agent: 'sales-worker',
              body: 'Auto tick handoff.',
              message_type: 'handoff',
              msg_type: 'work_handoff',
              created_at: 1770000500,
            },
          },
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/team/9/executor-step') {
        return new Response(JSON.stringify({
          team_id: 9,
          agent_id: 'sales-worker',
          convoy_id: 1,
          subtask_id: 13,
          command_key: 'git_status',
          argv: ['git', 'status', '--short'],
          cwd: '<repo>',
          success: true,
          exit_code: 0,
          timed_out: false,
          duration_ms: 42,
          stdout: ' M dashboard/web/src/pages/Teams.tsx',
          stderr: '',
          completed: false,
          convoy_completed: false,
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/team/operating-room/run') {
        const teamRoomRun = {
          workflow_id: 'growth_boardroom',
          meeting_mode: 'facilitated_boardroom',
          max_rounds: 2,
          goal: 'How should the team prioritize the next release?',
          context_excerpt: null,
          team_id: 9,
          convoy_id: 1,
          runtime: {
            enabled: false,
            turn_count: 0,
            lanes: [],
            providers: [],
            models: [],
            tool_call_count: 0,
            cost_usd: null,
            execution_time_ms: null,
            errors: [],
          },
          progress: { completed: 21, total: 21, status: 'completed' },
          lead_frame_excerpt: 'Frame the goal, call departments, and force owner-level decisions.',
          message_counts: { facilitator: 3, proposal: 4, crosstalk: 8, revision: 4, reviewer: 1, synthesis: 1 },
          turn_summary: '3 facilitator, 4 proposals, 8 cross-talk, 1 adversarial critique, 4 revisions, 1 final synthesis',
          meeting_controls: {
            agenda: ['Frame the decision.', 'Close with votes.'],
            facilitator_authority: ['Cut off repetition.'],
            decision_rules: ['No consensus without confidence.'],
            round_controls: [
              {
                round_number: 1,
                focus: 'Expose peer dependencies.',
                interrupt_rule: 'Interrupt for blockers.',
                exit_criteria: 'Owners and confidence named.',
              },
            ],
            stop_conditions: ['Follow-up trigger is explicit.'],
          },
          discussion_rounds: [
            {
              round_number: 1,
              facilitator_message: {
                id: 601,
                from_agent: 'teamroom-facilitator',
                subject: 'Round 1',
                body: 'Sales, marketing, product, and ops should pitch the highest-leverage wedge.',
                message_type: 'facilitator',
                created_at: 1770000700,
              },
              facilitator_turn: {
                phase: 'facilitator',
                role: 'facilitator',
                role_name: 'Facilitator',
                agent_id: 'teamroom-facilitator',
                subtask_id: 31,
                action: 'completed',
                status: 'completed',
                completed: true,
                reply: {
                  id: 601,
                  from_agent: 'teamroom-facilitator',
                  subject: 'Round 1',
                  body: 'Sales, marketing, product, and ops should pitch the highest-leverage wedge.',
                  message_type: 'facilitator',
                  created_at: 1770000700,
                },
                runtime: null,
              },
              crosstalk_messages: [],
              crosstalk_turns: [
                {
                  phase: 'crosstalk',
                  role: 'sales',
                  role_name: 'Sales',
                  agent_id: 'teamroom-sales',
                  subtask_id: 35,
                  action: 'completed',
                  status: 'completed',
                  completed: true,
                  reply: {
                    id: 602,
                    from_agent: 'teamroom-sales',
                    subject: 'Sales cross-talk',
                    body: 'Marketing angle works if sales owns qualified outreach and measures booked audits.',
                    message_type: 'team_message',
                    created_at: 1770000710,
                  },
                  runtime: null,
                },
              ],
            },
          ],
          vote_board: [
            {
              role: 'sales',
              role_name: 'Sales',
              recommendation: 'Approve the sales-led audit wedge.',
              confidence: 0.78,
              rationale: 'Sales owns qualified outreach.',
              blocking_issue: 'Buyer segment proof still has to come from live calls.',
            },
          ],
          interrupts: [
            {
              from_role: 'sales',
              from_role_name: 'Sales',
              target_role: 'marketing',
              target_role_name: 'Marketing',
              severity: 'challenge',
              challenge: 'Do not scale channels until calls prove the buyer language.',
              required_response: 'Use the objection log in the first hook.',
            },
          ],
          role_memory: [
            {
              role: 'sales',
              role_name: 'Sales',
              previous_meeting_id: 8,
              carried_forward: ['Carry forward the audit motion.'],
              current_commitment: 'Carry forward the buyer-segment audit motion.',
              watch_item: 'Buyer segment proof still has to come from live calls.',
            },
          ],
          synthesis: {
            decision_summary: 'Run the sales-led audit wedge with owner actions.',
            confidence: 0.78,
            agreements: ['The first motion should be a narrow audit-style CTA.'],
            disagreements: ['Sales and Marketing need live buyer language before scaling.'],
          },
          decision_ledger: {
            decisions: ['Validate demand before building the next heavy feature.'],
            accepted_bets: ['Sales-led audit wedge.'],
            rejected_bets: ['Broad generic productivity positioning.'],
            owner_actions: [
              {
                owner: 'Sales',
                action: 'Sales owns qualified outreach and booked audit targets.',
                validation_signal: 'Ten qualified audit calls booked.',
              },
            ],
            open_questions: ['Which buyer segment repeats the pain fastest?'],
            strongest_objection: 'The plan can still mistake activity for proof.',
            next_meeting_trigger: 'Reconvene after the first two-week readout.',
          },
          phase_results: {
            facilitator: [],
            proposal: [],
            crosstalk: [],
            adversarial_review: {
              phase: 'review',
              role: 'adversarial_reviewer',
              role_name: 'Adversarial Reviewer',
              agent_id: 'teamroom-reviewer',
              subtask_id: 39,
              action: 'completed',
              status: 'completed',
              completed: true,
              reply: null,
              runtime: null,
            },
            revision: [],
            synthesis: {
              phase: 'synthesis',
              role: 'synthesis',
              role_name: 'Synthesizer',
              agent_id: 'teamroom-synthesizer',
              subtask_id: 40,
              action: 'completed',
              status: 'completed',
              completed: true,
              reply: null,
              runtime: null,
            },
          },
          final_brief: 'Final Team Room brief: pick the sales-led audit wedge, measure booked audits, and revise after the two-week readout.',
        };
        return new Response(JSON.stringify({
          run_id: 'opr-test-run',
          created_at: '2026-06-04T00:00:00Z',
          team_room: teamRoomRun,
          tick: {
            team_id: 9,
            selected_action: 'claim_respond',
            reason: '1 pending convoy mailbox item.',
            agent_id: 'sales-worker',
            convoy_id: 1,
            subtask_id: 13,
            step: null,
            executor: null,
            waited: false,
            error: null,
          },
          proof_packet: {
            run_id: 'opr-test-run',
            created_at: '2026-06-04T00:00:00Z',
            product_surface: 'homie_operating_room',
            sanitized: true,
            goal: teamRoomRun.goal,
            workflow_id: teamRoomRun.workflow_id,
            meeting_mode: teamRoomRun.meeting_mode,
            team_id: teamRoomRun.team_id,
            convoy_id: teamRoomRun.convoy_id,
            progress: teamRoomRun.progress,
            runtime: teamRoomRun.runtime,
            vote_board: teamRoomRun.vote_board,
            interrupts: teamRoomRun.interrupts,
            owner_actions: teamRoomRun.decision_ledger.owner_actions,
            decisions: teamRoomRun.decision_ledger.decisions,
            open_questions: teamRoomRun.decision_ledger.open_questions,
            strongest_objection: teamRoomRun.decision_ledger.strongest_objection,
            next_meeting_trigger: teamRoomRun.decision_ledger.next_meeting_trigger,
            synthesis: teamRoomRun.synthesis,
            tick_summary: {
              selected_action: 'claim_respond',
              reason: '1 pending convoy mailbox item.',
              agent_id: 'sales-worker',
              convoy_id: 1,
              subtask_id: 13,
              waited: false,
              error: null,
              step_status: 'running',
            },
            final_brief: teamRoomRun.final_brief,
          },
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return new Response(JSON.stringify({}), { status: 404, headers: { 'content-type': 'application/json' } });
    }) as any;

    render(<Teams />);
    await waitFor(() => expect(screen.getAllByText(/dag team/i).length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getAllByText(/team worker/i).length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByText(/sales needs the landing page angle/i)).toBeInTheDocument());
    expect(screen.getByText(/Convoy: #1/i)).toBeInTheDocument();
    expect(screen.getByText(/persisted operating room artifacts/i)).toBeInTheDocument();
    expect(screen.getByText(/Persisted dashboard artifact proof/i)).toBeInTheDocument();
    expect(screen.getByText(/Persisted vote backs the audit wedge/i)).toBeInTheDocument();
    expect(screen.getByText(/Persisted interrupt keeps owner pressure visible/i)).toBeInTheDocument();
    expect(screen.getByText(/Persisted carry-forward survives reload/i)).toBeInTheDocument();
    expect(screen.getByText(/Persisted summary survives reload/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add member/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /run operating room/i }));
    await waitFor(() => expect(requests).toContain('/api/team/operating-room/run'));
    expect(await screen.findByText(/operating room proof/i)).toBeInTheDocument();
    expect(screen.getByText(/opr-test-run/i)).toBeInTheDocument();
    expect(screen.getByText(/sanitized/i)).toBeInTheDocument();
    expect(screen.getAllByText(/facilitated_boardroom/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/21\/21 · completed/i)).toBeInTheDocument();
    expect(screen.getByText(/3 facilitator, 4 proposals/i)).toBeInTheDocument();
    expect(screen.getAllByText(/0.78/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/v3 meeting artifacts/i)).toBeInTheDocument();
    expect(screen.getByText(/vote \+ confidence board/i)).toBeInTheDocument();
    expect(screen.getByText(/role memory/i)).toBeInTheDocument();
    expect(screen.getByText(/interrupts \+ challenges/i)).toBeInTheDocument();
    expect(screen.getByText(/agreements \/ disagreements/i)).toBeInTheDocument();
    expect(screen.getByText(/Rule: No consensus without confidence/i)).toBeInTheDocument();
    expect(screen.getByText(/Approve the sales-led audit wedge/i)).toBeInTheDocument();
    expect(screen.getByText(/Rationale: Sales owns qualified outreach/i)).toBeInTheDocument();
    expect(screen.getByText(/Do not scale channels until calls prove/i)).toBeInTheDocument();
    expect(screen.getByText(/Required: Use the objection log in the first hook/i)).toBeInTheDocument();
    expect(screen.getByText(/Prior meeting #8/i)).toBeInTheDocument();
    expect(screen.getByText(/Carry-forward: Carry forward the audit motion/i)).toBeInTheDocument();
    expect(screen.getByText(/Carry forward the buyer-segment audit motion/i)).toBeInTheDocument();
    expect(screen.getByText(/Sales and Marketing need live buyer language/i)).toBeInTheDocument();
    expect(screen.getByText(/Decision Summary/i)).toBeInTheDocument();
    expect(screen.getByText(/Decision: Validate demand before building/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Sales owns qualified outreach/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Final Team Room brief: pick the sales-led audit wedge/i).length).toBeGreaterThan(0);
    fireEvent.change(screen.getByLabelText(/^to$/i), { target: { value: 'sales-worker' } });
    fireEvent.input(screen.getByLabelText(/subject/i), { target: { value: 'Sales reply' } });
    fireEvent.input(screen.getByLabelText(/message/i), { target: { value: 'Lead list is ready.' } });
    fireEvent.click(screen.getByRole('button', { name: /send message/i }));
    await waitFor(() => expect(requests).toContain('/api/mailbox/send'));
    await waitFor(() => expect(screen.getByRole('button', { name: /claim inbox/i })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /claim inbox/i }));
    await waitFor(() => expect(requests.some((path) => path.startsWith('/api/mailbox/claim/sales-worker'))).toBe(true));
    expect(await screen.findByText(/claimed \(1\)/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /run loop step/i }));
    await waitFor(() => expect(requests).toContain('/api/team/9/loop-step'));
    expect(await screen.findByText(/claimed 1 · status running/i)).toBeInTheDocument();
    expect(screen.getByText(/generic_runtime · codex/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /run auto tick/i }));
    await waitFor(() => expect(requests).toContain('/api/team/9/tick'));
    expect((await screen.findAllByText(/claim_respond/i)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/1 pending convoy mailbox item/i).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: /run executor step/i }));
    await waitFor(() => expect(requests).toContain('/api/team/9/executor-step'));
    expect(await screen.findByText(/git_status · passed/i)).toBeInTheDocument();
    expect(screen.getByText(/exit 0 · 42ms/i)).toBeInTheDocument();
  });

  test('Usage page renders lane-aware summary', async () => {
    mockFetchOnce({
      timeline: [],
      summary: {
        claude_native: { turns_today: 17, messages_today: 24, plan_quota_estimate_pct: 12 },
        generic: { by_provider: { 'openai-compatible': { cost_usd: 1.42, messages: 8, model: 'gpt-4o' } }, total_cost_usd: 1.42 },
      },
    });
    render(<Usage />);
    await waitFor(() => {
      // Both lane labels present (may appear multiple times — card title
      // + pill title attribute).
      expect(screen.getAllByText(/Claude Max/i).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/Generic providers/i).length).toBeGreaterThan(0);
      // Both lane values present (turns + cost).
      expect(screen.getByText('17')).toBeInTheDocument();
    });
  });

  test('Jarvis page renders runtime, autonomy, channel, and trace truth', async () => {
    mockFetchOnce({
      status: 'ok',
      timestamp: '2026-05-23T23:47:00Z',
      runtime: {
        selected_lane: 'claude_native',
        selected_model: 'claude-sonnet-4-6',
        selected_generic_provider: 'codex',
        generic_text_route: ['codex', 'gemini'],
        generic_tool_route: ['claude_native', 'codex'],
        configured_models: { claude_native: 'claude-sonnet-4-6' },
        providers: { claude_native: 'ready', codex: 'ready' },
      },
      autonomy: {
        autonomy_overall: 'live',
        autonomous_loop_overall: 'live',
        cognitive_loop_overall: 'live',
        source_wiring_overall: 'live',
      },
      memory: { doc_count: 2932, embedding_status: 'ready' },
      channels: {
        telegram: {
          connected: true,
          sessions_active: 1,
          metadata_alignment: {
            runtime_providers_populated: true,
            memory_doc_count_matches_cli: true,
          },
        },
        mission_control_relay: {
          health_check_port: 8787,
          orchestration_api_port: 4322,
        },
      },
      capabilities: {
        enabled_count: 7,
        total_count: 9,
        toolsets: ['google', 'telegram'],
        enabled: [
          { id: 'telegram_bot', display_name: 'Telegram Bot', source: 'direct' },
        ],
      },
      observability: {
        lookup_status: 'documented_local_proof',
        langfuse_trace_id: '34723c42e7103e986274c4825b0e68a3',
        sentry_event_id: 'f822285b539e4820bd50988bc7ec6984',
        self_amendment_proposal_id: '0b1f70e3-1d2d-4275-85b8-5aafa4ae8f7d',
      },
    });

    render(<Jarvis />);

    await waitFor(() => expect(screen.getByText('claude-sonnet-4-6')).toBeInTheDocument());
    expect(screen.getByText('2932')).toBeInTheDocument();
    expect(screen.getByText('Telegram Bot')).toBeInTheDocument();
    expect(screen.getByText('34723c42e7103e986274c4825b0e68a3')).toBeInTheDocument();
    expect(screen.getByText('f822285b539e4820bd50988bc7ec6984')).toBeInTheDocument();
  });

  test('Capability Gateway page renders default-deny policy and integrations', async () => {
    mockFetchOnce({
      status: 'ok',
      timestamp: '2026-06-04T00:00:00Z',
      runtime: {
        selected_lane: 'generic_runtime',
        selected_generic_provider: 'codex',
        selected_model: 'chatgpt-plan-default',
        generic_text_route: ['codex', 'gemini'],
        generic_tool_route: ['claude_native', 'codex'],
      },
      capabilities: {
        total_count: 2,
        enabled_count: 1,
        sources: { integrations: 1 },
        items: [
          { id: 'telegram_bot', display_name: 'Telegram Bot', enabled: true, source: 'integrations' },
        ],
      },
      toolsets: [
        { name: 'google', capability_count: 3 },
        { name: 'browserops', capability_count: 1 },
      ],
      integrations: {
        total_count: 2,
        enabled_count: 1,
        items: [
          { id: 'telegram', display_name: 'Telegram', enabled: true, action_count: 4 },
          { id: 'slack', display_name: 'Slack', enabled: false, action_count: 2 },
        ],
      },
      browserops: {
        enabled: false,
        status: 'attention',
        cdp_port: 9222,
        reason: 'CDP not connected',
      },
      outbound_messaging: {
        status: 'policy_gated',
        requires_operator_confirmation: true,
        actions: [
          { id: 'telegram.send_message', effect: 'send' },
        ],
      },
      approval_policy: {
        default_deny: true,
        mutating_actions_require_operator_confirmation: true,
        dashboard_mode: 'read_only',
        model_exposed_mutating_actions: [],
      },
    });

    render(<CapabilityGateway />);

    await waitFor(() => expect(screen.getByText(/capability gateway/i)).toBeInTheDocument());
    expect(screen.getAllByText('chatgpt-plan-default').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/default deny/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/true/i).length).toBeGreaterThan(0);
    expect(screen.getByText('Telegram')).toBeInTheDocument();
    expect(screen.getByText('telegram.send_message')).toBeInTheDocument();
    expect(screen.getAllByText(/read_only/i).length).toBeGreaterThan(0);
  });
});
