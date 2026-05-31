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
          cwd: '~/thehomie',
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
      if (path === '/api/team/room/run') {
        return new Response(JSON.stringify({
          workflow_id: 'growth_boardroom',
          meeting_mode: 'facilitated_boardroom',
          max_rounds: 2,
          goal: 'How do we get TaskChad to one million dollars?',
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
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (path === '/api/team/taskchad-drill') {
        return new Response(JSON.stringify({
          target_url: 'https://www.taskchad.com/',
          team_id: 9,
          convoy_id: 1,
          initial_message_count: 4,
          revision_message_count: 4,
          role_turns: [
            {
              role: 'sales',
              role_name: 'TaskChad Sales',
              agent_id: 'taskchad-sales',
              subtask_id: 21,
              action: 'completed',
              status: 'completed',
              completed: true,
              reply: { id: 501, from_agent: 'taskchad-sales', body: 'Sales turn.', created_at: 1770000600 },
            },
          ],
          revision_turns: [
            {
              role: 'sales',
              role_name: 'TaskChad Sales',
              agent_id: 'taskchad-sales',
              subtask_id: 27,
              action: 'completed',
              status: 'completed',
              completed: true,
              reply: { id: 504, from_agent: 'taskchad-sales', body: 'Sales revision.', created_at: 1770000630 },
            },
          ],
          reviewer_turn: {
            role: 'adversarial_reviewer',
            role_name: 'TaskChad Adversarial Reviewer',
            agent_id: 'taskchad-reviewer',
            subtask_id: 25,
            action: 'completed',
            status: 'completed',
            completed: true,
            reply: { id: 502, from_agent: 'taskchad-reviewer', body: 'Review turn.', created_at: 1770000610 },
          },
          final_turn: {
            role: 'final_plan',
            role_name: 'TaskChad Plan Synthesizer',
            agent_id: 'taskchad-synthesizer',
            subtask_id: 26,
            action: 'completed',
            status: 'completed',
            completed: true,
            reply: { id: 503, from_agent: 'taskchad-synthesizer', body: 'Final TaskChad plan.', created_at: 1770000620 },
          },
          final_plan: 'Final revised TaskChad plan: clarify offer, page, sales follow-up, ops, and validation.',
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return new Response(JSON.stringify({}), { status: 404, headers: { 'content-type': 'application/json' } });
    }) as any;

    render(<Teams />);
    await waitFor(() => expect(screen.getAllByText(/dag team/i).length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getAllByText(/team worker/i).length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByText(/sales needs the landing page angle/i)).toBeInTheDocument());
    expect(screen.getByText(/Convoy: #1/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add member/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /taskchad drill/i }));
    await waitFor(() => expect(requests).toContain('/api/team/taskchad-drill'));
    expect(await screen.findByText(/round 2 revisions/i)).toBeInTheDocument();
    expect(await screen.findByText(/final revised taskchad plan: clarify offer/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /run team room/i }));
    await waitFor(() => expect(requests).toContain('/api/team/room/run'));
    expect(await screen.findByText(/team room result/i)).toBeInTheDocument();
    expect(screen.getAllByText(/facilitated_boardroom/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/21\/21 · completed/i)).toBeInTheDocument();
    expect(screen.getByText(/3 facilitator, 4 proposals/i)).toBeInTheDocument();
    expect(screen.getByText(/Decision: Validate demand before building/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Sales owns qualified outreach/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Final Team Room brief: pick the sales-led audit wedge/i)).toBeInTheDocument();
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
    expect(await screen.findByText(/claim_respond/i)).toBeInTheDocument();
    expect(screen.getByText(/1 pending convoy mailbox item/i)).toBeInTheDocument();
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
});
