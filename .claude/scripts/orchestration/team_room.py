"""Homie-native team room workflows over convoy, mailbox, and runtime lanes.

The v1 workflow is deliberately concrete: one Growth Boardroom ritual that
creates a real team session, moves work through convoy subtasks, and keeps raw
intermediate turns in orchestration state instead of the chat orchestrator
context.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from orchestration.contract import DEFAULT_WORKSPACE_ID
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import (
    AddTeamMemberInput,
    AgentMessage,
    ConvoyWithSubtasks,
    CreateConvoyInput,
    CreateSubtaskInput,
    CreateTeamSessionInput,
    SendMessageInput,
    Subtask,
    TeamSessionWithMembers,
)
from orchestration.observability import orchestration_span, update_observation
from orchestration.team_loop import TeamLoopService, TeamLoopStepResult, result_to_dict
from orchestration.team_service import TeamService

TEAM_ROOM_WORKFLOW_ID = "growth_boardroom"
TEAM_ROOM_LEAD_AGENT_ID = "teamroom-lead"
TEAM_ROOM_LEAD_AGENT_NAME = "Team Room Lead"
TEAM_ROOM_MODE_CLASSIC = "classic_boardroom"
TEAM_ROOM_MODE_FACILITATED = "facilitated_boardroom"
TEAM_ROOM_FACILITATED_DEFAULT_ROUNDS = 2
TEAM_ROOM_MAX_ROUNDS = 4


@dataclass(frozen=True)
class TeamRoomRoleSpec:
    key: str
    agent_id: str
    agent_name: str
    subtask_title: str
    proposal_prompt: str
    crosstalk_prompt: str
    revision_prompt: str


@dataclass
class TeamRoomTurn:
    phase: str
    role: TeamRoomRoleSpec
    step: TeamLoopStepResult


@dataclass
class TeamRoomDiscussionRound:
    round_number: int
    facilitator_message: AgentMessage | None
    facilitator_turn: TeamRoomTurn | None
    crosstalk_messages: list[AgentMessage]
    crosstalk_turns: list[TeamRoomTurn]


@dataclass(frozen=True)
class TeamRoomDecisionLedger:
    decisions: list[str]
    accepted_bets: list[str]
    rejected_bets: list[str]
    owner_actions: list[dict[str, str]]
    open_questions: list[str]
    strongest_objection: str
    next_meeting_trigger: str


@dataclass
class TeamRoomWorkflowResult:
    workflow_id: str
    meeting_mode: str
    max_rounds: int
    goal: str
    context: str | None
    lead_frame: str
    convoy: ConvoyWithSubtasks
    team: TeamSessionWithMembers
    facilitator_messages: list[AgentMessage]
    facilitator_turns: list[TeamRoomTurn]
    proposal_messages: list[AgentMessage]
    proposal_turns: list[TeamRoomTurn]
    crosstalk_messages: list[AgentMessage]
    crosstalk_turns: list[TeamRoomTurn]
    discussion_rounds: list[TeamRoomDiscussionRound]
    reviewer_message: AgentMessage
    reviewer_turn: TeamRoomTurn
    revision_messages: list[AgentMessage]
    revision_turns: list[TeamRoomTurn]
    synthesis_message: AgentMessage
    final_turn: TeamRoomTurn
    decision_ledger: TeamRoomDecisionLedger
    final_brief: str


TEAM_ROOM_DEPARTMENT_ROLES: tuple[TeamRoomRoleSpec, ...] = (
    TeamRoomRoleSpec(
        key="sales",
        agent_id="teamroom-sales",
        agent_name="Sales",
        subtask_title="Sales growth proposal",
        proposal_prompt=(
            "Pitch the sales angle: buyer, wedge offer, first conversation, "
            "qualification, objection handling, and revenue path."
        ),
        crosstalk_prompt=(
            "React to the other departments. Build on one useful idea, challenge "
            "one weak assumption, or explicitly pass if sales has no useful delta."
        ),
        revision_prompt=(
            "Revise the sales plan after cross-talk and adversarial review. Name "
            "the first sales motion, owner, and validation signal."
        ),
    ),
    TeamRoomRoleSpec(
        key="marketing",
        agent_id="teamroom-marketing",
        agent_name="Marketing",
        subtask_title="Marketing growth proposal",
        proposal_prompt=(
            "Pitch the marketing angle: positioning, audience, channel, proof, "
            "campaign hook, and message hierarchy."
        ),
        crosstalk_prompt=(
            "React to the other departments. Build on one useful idea, challenge "
            "one weak assumption, or explicitly pass if marketing has no useful delta."
        ),
        revision_prompt=(
            "Revise the marketing plan after cross-talk and adversarial review. "
            "Name the campaign, proof asset, owner, and validation signal."
        ),
    ),
    TeamRoomRoleSpec(
        key="product_frontend",
        agent_id="teamroom-product",
        agent_name="Product/Frontend",
        subtask_title="Product and frontend growth proposal",
        proposal_prompt=(
            "Pitch the product/frontend angle: user journey, page/app changes, "
            "conversion path, friction removal, and implementation task."
        ),
        crosstalk_prompt=(
            "React to the other departments. Build on one useful idea, challenge "
            "one weak assumption, or explicitly pass if product has no useful delta."
        ),
        revision_prompt=(
            "Revise the product/frontend plan after cross-talk and adversarial "
            "review. Name the first shippable change, owner, and validation signal."
        ),
    ),
    TeamRoomRoleSpec(
        key="ops",
        agent_id="teamroom-ops",
        agent_name="Ops",
        subtask_title="Ops growth proposal",
        proposal_prompt=(
            "Pitch the ops angle: execution order, owners, instrumentation, "
            "handoffs, bottlenecks, and operating cadence."
        ),
        crosstalk_prompt=(
            "React to the other departments. Build on one useful idea, challenge "
            "one weak assumption, or explicitly pass if ops has no useful delta."
        ),
        revision_prompt=(
            "Revise the ops plan after cross-talk and adversarial review. Name "
            "the launch sequence, owner, and validation signal."
        ),
    ),
)

TEAM_ROOM_REVIEWER = TeamRoomRoleSpec(
    key="adversarial_reviewer",
    agent_id="teamroom-reviewer",
    agent_name="Adversarial Reviewer",
    subtask_title="Adversarial boardroom review",
    proposal_prompt=(
        "Challenge the department proposals and cross-talk. Identify weak claims, "
        "missing proof, false consensus, hidden dependencies, and the highest-risk trap."
    ),
    crosstalk_prompt="",
    revision_prompt="",
)

TEAM_ROOM_FACILITATOR = TeamRoomRoleSpec(
    key="facilitator",
    agent_id="teamroom-facilitator",
    agent_name="Facilitator",
    subtask_title="Facilitated boardroom agenda",
    proposal_prompt=(
        "Open the meeting with the agenda, decision criteria, speaking order, "
        "and what would justify another round."
    ),
    crosstalk_prompt=(
        "Review the room so far, choose the next cross-talk focus, name the "
        "weakest consensus point, and direct departments to answer one peer."
    ),
    revision_prompt="",
)

TEAM_ROOM_SYNTHESIZER = TeamRoomRoleSpec(
    key="synthesizer",
    agent_id="teamroom-synthesizer",
    agent_name="Synthesizer",
    subtask_title="Final boardroom synthesis",
    proposal_prompt=(
        "Synthesize the revised department plans into one decision brief with "
        "decision, bets, risks, objections, and next actions."
    ),
    crosstalk_prompt="",
    revision_prompt="",
)


class TeamRoomWorkflowService:
    """Runs bounded team-room rituals through existing orchestration services."""

    def __init__(self, db: OrchestrationDB):
        self.db = db
        self.convoy_svc = ConvoyService(db)
        self.mailbox_svc = MailboxService(db)
        self.team_svc = TeamService(db)
        self.loop_svc = TeamLoopService(db)

    def run_team_room(
        self,
        *,
        goal: str,
        workflow_id: str = TEAM_ROOM_WORKFLOW_ID,
        context: str | None = None,
        use_runtime: bool = False,
        runtime_lane: str | None = None,
        max_rounds: int | None = None,
        meeting_mode: str | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamRoomWorkflowResult:
        """Run the Growth Boardroom workflow and return a bounded final brief."""
        normalized_goal = (goal or "").strip()
        if not normalized_goal:
            raise ValueError("goal is required")
        if workflow_id != TEAM_ROOM_WORKFLOW_ID:
            raise ValueError(f"Unsupported team room workflow: {workflow_id}")
        resolved_mode, resolved_rounds = self._resolve_meeting_shape(
            meeting_mode=meeting_mode,
            max_rounds=max_rounds,
        )
        normalized_context = (context or "").strip() or None

        with orchestration_span(
            "team_room.run_team_room",
            metadata={
                "workflow_id": workflow_id,
                "meeting_mode": resolved_mode,
                "use_runtime": use_runtime,
                "runtime_lane": runtime_lane,
                "max_rounds": resolved_rounds,
            },
            trace_metadata={"feature_phase": 12, "workflow_id": workflow_id},
            expected_exceptions=(ValueError,),
        ):
            lead_frame = self._build_lead_frame(normalized_goal, normalized_context)
            convoy = self._create_convoy(
                normalized_goal,
                normalized_context,
                workflow_id=workflow_id,
                meeting_mode=resolved_mode,
                max_rounds=resolved_rounds,
                workspace_id=workspace_id,
            )
            team = self._create_team(
                convoy,
                goal=normalized_goal,
                workflow_id=workflow_id,
                meeting_mode=resolved_mode,
                workspace_id=workspace_id,
            )

            facilitator_messages: list[AgentMessage] = []
            facilitator_turns: list[TeamRoomTurn] = []
            if resolved_mode == TEAM_ROOM_MODE_FACILITATED:
                facilitator_messages.append(
                    self._send_facilitator_opening_brief(
                        lead_frame=lead_frame,
                        convoy_id=convoy.convoy.id,
                        max_rounds=resolved_rounds,
                        workspace_id=workspace_id,
                    )
                )
                opening_subtask = self._subtask_for_phase(
                    convoy.subtasks,
                    "facilitator_open",
                )
                opening_step = self.loop_svc.run_member_step(
                    team.session.id,
                    TEAM_ROOM_FACILITATOR.agent_id,
                    subtask_id=opening_subtask.id,
                    reply_body=self._default_facilitator_opening_reply(
                        normalized_goal,
                        resolved_rounds,
                    ),
                    use_runtime=use_runtime,
                    runtime_lane=runtime_lane,
                    complete=True,
                    workspace_id=workspace_id,
                )
                facilitator_turns.append(
                    TeamRoomTurn(
                        phase="facilitator_open",
                        role=TEAM_ROOM_FACILITATOR,
                        step=opening_step,
                    )
                )

            proposal_messages = self._seed_proposal_briefs(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                workspace_id=workspace_id,
            )
            proposal_turns = self._run_department_phase(
                team.session.id,
                self._subtasks_for_department_phase(convoy.subtasks, "proposal"),
                phase="proposal",
                reply_builder=lambda role: self._default_proposal_reply(role, normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                workspace_id=workspace_id,
            )

            crosstalk_messages: list[AgentMessage] = []
            crosstalk_turns: list[TeamRoomTurn] = []
            discussion_rounds: list[TeamRoomDiscussionRound] = []
            for round_number in range(1, resolved_rounds + 1):
                round_facilitator_message: AgentMessage | None = None
                round_facilitator_turn: TeamRoomTurn | None = None
                if resolved_mode == TEAM_ROOM_MODE_FACILITATED:
                    round_facilitator_message = self._send_facilitator_round_brief(
                        lead_frame=lead_frame,
                        convoy_id=convoy.convoy.id,
                        round_number=round_number,
                        proposal_turns=proposal_turns,
                        crosstalk_turns=crosstalk_turns,
                        workspace_id=workspace_id,
                    )
                    facilitator_messages.append(round_facilitator_message)
                    facilitator_subtask = self._subtask_for_phase(
                        convoy.subtasks,
                        "facilitator_round",
                        round_number=round_number,
                    )
                    facilitator_step = self.loop_svc.run_member_step(
                        team.session.id,
                        TEAM_ROOM_FACILITATOR.agent_id,
                        subtask_id=facilitator_subtask.id,
                        reply_body=self._default_facilitator_round_reply(
                            normalized_goal,
                            round_number,
                            resolved_rounds,
                        ),
                        use_runtime=use_runtime,
                        runtime_lane=runtime_lane,
                        complete=True,
                        workspace_id=workspace_id,
                    )
                    round_facilitator_turn = TeamRoomTurn(
                        phase=f"facilitator_round_{round_number}",
                        role=TEAM_ROOM_FACILITATOR,
                        step=facilitator_step,
                    )
                    facilitator_turns.append(round_facilitator_turn)

                round_messages = self._send_crosstalk_briefs(
                    lead_frame=lead_frame,
                    convoy_id=convoy.convoy.id,
                    proposal_turns=proposal_turns,
                    prior_crosstalk_turns=crosstalk_turns,
                    facilitator_turn=round_facilitator_turn,
                    round_number=round_number,
                    workspace_id=workspace_id,
                )
                round_turns = self._run_department_phase(
                    team.session.id,
                    self._subtasks_for_department_phase(
                        convoy.subtasks,
                        "crosstalk",
                        round_number=round_number,
                    ),
                    phase=(
                        f"crosstalk_round_{round_number}"
                        if resolved_mode == TEAM_ROOM_MODE_FACILITATED
                        else "crosstalk"
                    ),
                    reply_builder=lambda role, rn=round_number: self._default_crosstalk_reply(
                        role,
                        normalized_goal,
                        round_number=rn,
                    ),
                    use_runtime=use_runtime,
                    runtime_lane=runtime_lane,
                    workspace_id=workspace_id,
                )
                crosstalk_messages.extend(round_messages)
                crosstalk_turns.extend(round_turns)
                discussion_rounds.append(
                    TeamRoomDiscussionRound(
                        round_number=round_number,
                        facilitator_message=round_facilitator_message,
                        facilitator_turn=round_facilitator_turn,
                        crosstalk_messages=round_messages,
                        crosstalk_turns=round_turns,
                    )
                )

            reviewer_message = self._send_reviewer_brief(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                proposal_turns=proposal_turns,
                crosstalk_turns=crosstalk_turns,
                workspace_id=workspace_id,
            )
            reviewer_step = self.loop_svc.run_member_step(
                team.session.id,
                TEAM_ROOM_REVIEWER.agent_id,
                subtask_id=self._subtask_for_phase(
                    convoy.subtasks,
                    "adversarial_review",
                ).id,
                reply_body=self._default_reviewer_reply(normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                complete=True,
                workspace_id=workspace_id,
            )
            reviewer_turn = TeamRoomTurn(
                phase="adversarial_review",
                role=TEAM_ROOM_REVIEWER,
                step=reviewer_step,
            )

            revision_messages = self._send_revision_briefs(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                proposal_turns=proposal_turns,
                crosstalk_turns=crosstalk_turns,
                reviewer_turn=reviewer_turn,
                workspace_id=workspace_id,
            )
            revision_turns = self._run_department_phase(
                team.session.id,
                self._subtasks_for_department_phase(convoy.subtasks, "revision"),
                phase="revision",
                reply_builder=lambda role: self._default_revision_reply(role, normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                workspace_id=workspace_id,
            )

            synthesis_message = self._send_synthesis_brief(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                proposal_turns=proposal_turns,
                crosstalk_turns=crosstalk_turns,
                reviewer_turn=reviewer_turn,
                revision_turns=revision_turns,
                workspace_id=workspace_id,
            )
            final_step = self.loop_svc.run_member_step(
                team.session.id,
                TEAM_ROOM_SYNTHESIZER.agent_id,
                subtask_id=self._subtask_for_phase(convoy.subtasks, "synthesis").id,
                reply_body=self._default_final_brief(normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                complete=True,
                workspace_id=workspace_id,
            )
            final_turn = TeamRoomTurn(
                phase="synthesis",
                role=TEAM_ROOM_SYNTHESIZER,
                step=final_step,
            )

            refreshed_convoy = self.convoy_svc.get_convoy(
                convoy.convoy.id,
                workspace_id=workspace_id,
            )
            refreshed_team = self.team_svc.get_team_session(
                team.session.id,
                workspace_id=workspace_id,
            )
            if refreshed_convoy is None or refreshed_team is None:
                raise RuntimeError("Team room workflow state disappeared after run")

            final_brief = final_step.reply.body if final_step.reply else ""
            decision_ledger = self._build_decision_ledger(normalized_goal)
            update_observation(
                metadata={
                    "workflow_id": workflow_id,
                    "meeting_mode": resolved_mode,
                    "convoy_id": refreshed_convoy.convoy.id,
                    "team_id": refreshed_team.session.id,
                    "convoy_status": refreshed_convoy.convoy.status,
                    "facilitator_count": len(facilitator_turns),
                    "proposal_count": len(proposal_turns),
                    "crosstalk_count": len(crosstalk_turns),
                    "revision_count": len(revision_turns),
                },
                output={"final_brief_chars": len(final_brief)},
            )
            return TeamRoomWorkflowResult(
                workflow_id=workflow_id,
                meeting_mode=resolved_mode,
                max_rounds=resolved_rounds,
                goal=normalized_goal,
                context=normalized_context,
                lead_frame=lead_frame,
                convoy=refreshed_convoy,
                team=refreshed_team,
                facilitator_messages=facilitator_messages,
                facilitator_turns=facilitator_turns,
                proposal_messages=proposal_messages,
                proposal_turns=proposal_turns,
                crosstalk_messages=crosstalk_messages,
                crosstalk_turns=crosstalk_turns,
                discussion_rounds=discussion_rounds,
                reviewer_message=reviewer_message,
                reviewer_turn=reviewer_turn,
                revision_messages=revision_messages,
                revision_turns=revision_turns,
                synthesis_message=synthesis_message,
                final_turn=final_turn,
                decision_ledger=decision_ledger,
                final_brief=final_brief,
            )

    def _create_convoy(
        self,
        goal: str,
        context: str | None,
        *,
        workflow_id: str,
        meeting_mode: str,
        max_rounds: int,
        workspace_id: int,
    ) -> ConvoyWithSubtasks:
        metadata_base = {
            "workflow": "team_room",
            "workflow_id": workflow_id,
            "meeting_mode": meeting_mode,
            "max_rounds": max_rounds,
            "goal_excerpt": _clip_text(goal, max_chars=180),
            "has_context": bool(context),
        }
        subtasks: list[CreateSubtaskInput] = []

        def _append_subtask(
            *,
            title: str,
            role: TeamRoomRoleSpec,
            prompt: str,
            phase: str,
            depends_on: list[int] | None = None,
            round_number: int | None = None,
            role_order: int | None = None,
        ) -> int:
            metadata = {
                **metadata_base,
                "phase": phase,
                "role": role.key,
            }
            if round_number is not None:
                metadata["round"] = round_number
            if role_order is not None:
                metadata["role_order"] = role_order
            subtasks.append(
                CreateSubtaskInput(
                    title=title,
                    description=self._subtask_description(
                        goal,
                        context,
                        prompt,
                        phase=phase,
                    ),
                    assigned_agent_id=role.agent_id,
                    assigned_agent_name=role.agent_name,
                    depends_on_subtask_indexes=depends_on or [],
                    metadata=json.dumps(metadata, sort_keys=True),
                )
            )
            return len(subtasks) - 1

        opening_index: int | None = None
        if meeting_mode == TEAM_ROOM_MODE_FACILITATED:
            opening_index = _append_subtask(
                title=TEAM_ROOM_FACILITATOR.subtask_title,
                role=TEAM_ROOM_FACILITATOR,
                prompt=TEAM_ROOM_FACILITATOR.proposal_prompt,
                phase="facilitator_open",
            )

        proposal_indexes = [
            _append_subtask(
                title=role.subtask_title,
                role=role,
                prompt=role.proposal_prompt,
                phase="proposal",
                depends_on=[opening_index] if opening_index is not None else None,
                role_order=idx,
            )
            for idx, role in enumerate(TEAM_ROOM_DEPARTMENT_ROLES)
        ]

        previous_round_indexes = proposal_indexes
        for round_number in range(1, max_rounds + 1):
            facilitator_index: int | None = None
            if meeting_mode == TEAM_ROOM_MODE_FACILITATED:
                facilitator_index = _append_subtask(
                    title=f"Facilitator round {round_number} direction",
                    role=TEAM_ROOM_FACILITATOR,
                    prompt=TEAM_ROOM_FACILITATOR.crosstalk_prompt,
                    phase="facilitator_round",
                    depends_on=previous_round_indexes,
                    round_number=round_number,
                )

            crosstalk_depends = (
                [facilitator_index]
                if facilitator_index is not None
                else list(previous_round_indexes)
            )
            round_indexes = [
                _append_subtask(
                    title=(
                        f"{role.agent_name} cross-talk response"
                        if max_rounds == 1
                        else f"{role.agent_name} cross-talk round {round_number}"
                    ),
                    role=role,
                    prompt=role.crosstalk_prompt,
                    phase="crosstalk",
                    depends_on=crosstalk_depends,
                    round_number=round_number,
                    role_order=idx,
                )
                for idx, role in enumerate(TEAM_ROOM_DEPARTMENT_ROLES)
            ]
            previous_round_indexes = round_indexes

        reviewer_subtask = CreateSubtaskInput(
            title=TEAM_ROOM_REVIEWER.subtask_title,
            description=self._subtask_description(
                goal,
                context,
                TEAM_ROOM_REVIEWER.proposal_prompt,
                phase="adversarial_review",
            ),
            assigned_agent_id=TEAM_ROOM_REVIEWER.agent_id,
            assigned_agent_name=TEAM_ROOM_REVIEWER.agent_name,
            depends_on_subtask_indexes=previous_round_indexes,
            metadata=json.dumps(
                {
                    **metadata_base,
                    "phase": "adversarial_review",
                    "role": TEAM_ROOM_REVIEWER.key,
                },
                sort_keys=True,
            ),
        )
        reviewer_index = len(subtasks)
        subtasks.append(reviewer_subtask)

        revision_subtasks = [
            CreateSubtaskInput(
                title=f"{role.agent_name} revised plan",
                description=self._subtask_description(
                    goal,
                    context,
                    role.revision_prompt,
                    phase="revision",
                ),
                assigned_agent_id=role.agent_id,
                assigned_agent_name=role.agent_name,
                depends_on_subtask_indexes=[reviewer_index],
                metadata=json.dumps(
                    {
                        **metadata_base,
                        "phase": "revision",
                        "role": role.key,
                        "role_order": idx,
                    },
                    sort_keys=True,
                ),
            )
            for idx, role in enumerate(TEAM_ROOM_DEPARTMENT_ROLES)
        ]
        revision_indexes = list(range(len(subtasks), len(subtasks) + len(revision_subtasks)))
        subtasks.extend(revision_subtasks)

        synthesis_subtask = CreateSubtaskInput(
            title=TEAM_ROOM_SYNTHESIZER.subtask_title,
            description=self._subtask_description(
                goal,
                context,
                TEAM_ROOM_SYNTHESIZER.proposal_prompt,
                phase="synthesis",
            ),
            assigned_agent_id=TEAM_ROOM_SYNTHESIZER.agent_id,
            assigned_agent_name=TEAM_ROOM_SYNTHESIZER.agent_name,
            depends_on_subtask_indexes=revision_indexes,
            metadata=json.dumps(
                {**metadata_base, "phase": "synthesis", "role": TEAM_ROOM_SYNTHESIZER.key},
                sort_keys=True,
            ),
        )
        subtasks.append(synthesis_subtask)

        return self.convoy_svc.create_convoy(
            CreateConvoyInput(
                title=f"Team Room: {_clip_text(goal, max_chars=80)}",
                description=(
                    "Homie-native Growth Boardroom workflow: department proposals, "
                    "facilitated cross-talk, adversarial review, revisions, and synthesis."
                ),
                created_by=TEAM_ROOM_LEAD_AGENT_ID,
                repo_path=None,
                decomposition_mode="manual",
                subtasks=subtasks,
            ),
            workspace_id=workspace_id,
        )

    def _create_team(
        self,
        convoy: ConvoyWithSubtasks,
        *,
        goal: str,
        workflow_id: str,
        meeting_mode: str,
        workspace_id: int,
    ) -> TeamSessionWithMembers:
        team = self.team_svc.create_team_session(
            CreateTeamSessionInput(
                team_name="Growth Boardroom",
                lead_agent_id=TEAM_ROOM_LEAD_AGENT_ID,
                lead_agent_name=TEAM_ROOM_LEAD_AGENT_NAME,
                convoy_id=convoy.convoy.id,
                backend_type="local",
                metadata=json.dumps(
                    {
                        "workflow": "team_room",
                        "workflow_id": workflow_id,
                        "meeting_mode": meeting_mode,
                        "goal_excerpt": _clip_text(goal, max_chars=180),
                    },
                    sort_keys=True,
                ),
            ),
            workspace_id=workspace_id,
        )
        all_roles = [
            *TEAM_ROOM_DEPARTMENT_ROLES,
            TEAM_ROOM_REVIEWER,
            TEAM_ROOM_SYNTHESIZER,
        ]
        if meeting_mode == TEAM_ROOM_MODE_FACILITATED:
            all_roles.insert(0, TEAM_ROOM_FACILITATOR)
        for role in all_roles:
            subtask = self._first_subtask_for_agent(convoy.subtasks, role.agent_id)
            self.team_svc.add_member(
                team.session.id,
                AddTeamMemberInput(
                    agent_id=role.agent_id,
                    agent_name=role.agent_name,
                    role="worker",
                    subtask_id=subtask.id,
                ),
                workspace_id=workspace_id,
            )
        refreshed = self.team_svc.get_team_session(team.session.id, workspace_id=workspace_id)
        if refreshed is None:
            raise RuntimeError(f"Failed to read back team session {team.session.id}")
        return refreshed

    def _send_facilitator_opening_brief(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        max_rounds: int,
        workspace_id: int,
    ) -> AgentMessage:
        return self.mailbox_svc.send_message(
            SendMessageInput(
                from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                recipients=[TEAM_ROOM_FACILITATOR.agent_id],
                convoy_id=convoy_id,
                subject="Facilitator opening brief",
                body=(
                    f"{lead_frame}\n\n"
                    f"{TEAM_ROOM_FACILITATOR.proposal_prompt}\n\n"
                    f"This meeting has up to {max_rounds} cross-talk round(s). "
                    "Keep the room moving toward one decision, named owners, and "
                    "validation signals."
                ),
                message_type="message",
                msg_type="task_assignment",
            ),
            workspace_id=workspace_id,
        )

    def _send_facilitator_round_brief(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        round_number: int,
        proposal_turns: list[TeamRoomTurn],
        crosstalk_turns: list[TeamRoomTurn],
        workspace_id: int,
    ) -> AgentMessage:
        prior = self._format_turns(crosstalk_turns) or "No prior cross-talk yet."
        return self.mailbox_svc.send_message(
            SendMessageInput(
                from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                recipients=[TEAM_ROOM_FACILITATOR.agent_id],
                convoy_id=convoy_id,
                subject=f"Facilitator round {round_number} brief",
                body=(
                    f"{lead_frame}\n\n"
                    f"{TEAM_ROOM_FACILITATOR.crosstalk_prompt}\n\n"
                    "Department proposals:\n"
                    f"{self._format_turns(proposal_turns)}\n\n"
                    "Prior cross-talk:\n"
                    f"{prior}"
                ),
                message_type="message",
                msg_type="work_handoff",
            ),
            workspace_id=workspace_id,
        )

    def _seed_proposal_briefs(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        workspace_id: int,
    ) -> list[AgentMessage]:
        messages: list[AgentMessage] = []
        for role in TEAM_ROOM_DEPARTMENT_ROLES:
            messages.append(
                self.mailbox_svc.send_message(
                    SendMessageInput(
                        from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                        recipients=[role.agent_id],
                        convoy_id=convoy_id,
                        subject=f"Growth boardroom proposal brief: {role.agent_name}",
                        body=(
                            f"{lead_frame}\n\n"
                            f"{role.proposal_prompt}\n\n"
                            "Return one bounded written turn from your department. "
                            "Name what you would do, why, owner, and validation signal."
                        ),
                        message_type="message",
                        msg_type="task_assignment",
                    ),
                    workspace_id=workspace_id,
                )
            )
        return messages

    def _send_crosstalk_briefs(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        proposal_turns: list[TeamRoomTurn],
        prior_crosstalk_turns: list[TeamRoomTurn],
        facilitator_turn: TeamRoomTurn | None,
        round_number: int,
        workspace_id: int,
    ) -> list[AgentMessage]:
        peer_context = self._format_turns(proposal_turns)
        prior_context = self._format_turns(prior_crosstalk_turns)
        facilitator_context = (
            facilitator_turn.step.reply.body
            if facilitator_turn is not None and facilitator_turn.step.reply
            else ""
        )
        messages: list[AgentMessage] = []
        for role in TEAM_ROOM_DEPARTMENT_ROLES:
            subject = (
                f"Cross-talk round: {role.agent_name}"
                if round_number == 1 and not facilitator_context
                else f"Cross-talk round {round_number}: {role.agent_name}"
            )
            extra_context = ""
            if facilitator_context:
                extra_context += f"\n\nFacilitator direction:\n{facilitator_context}"
            if prior_context:
                extra_context += f"\n\nPrior cross-talk:\n{prior_context}"
            messages.append(
                self.mailbox_svc.send_message(
                    SendMessageInput(
                        from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                        recipients=[role.agent_id],
                        convoy_id=convoy_id,
                        subject=subject,
                        body=(
                            f"{lead_frame}\n\n"
                            f"{role.crosstalk_prompt}\n\n"
                            "Address one named peer directly when you have a useful "
                            "build, objection, or dependency request.\n\n"
                            "Department proposals:\n"
                            f"{peer_context}"
                            f"{extra_context}"
                        ),
                        message_type="message",
                        msg_type="work_handoff",
                    ),
                    workspace_id=workspace_id,
                )
            )
        return messages

    def _send_reviewer_brief(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        proposal_turns: list[TeamRoomTurn],
        crosstalk_turns: list[TeamRoomTurn],
        workspace_id: int,
    ) -> AgentMessage:
        return self.mailbox_svc.send_message(
            SendMessageInput(
                from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                recipients=[TEAM_ROOM_REVIEWER.agent_id],
                convoy_id=convoy_id,
                subject="Growth boardroom adversarial review brief",
                body=(
                    f"{lead_frame}\n\n"
                    f"{TEAM_ROOM_REVIEWER.proposal_prompt}\n\n"
                    "Department proposals:\n"
                    f"{self._format_turns(proposal_turns)}\n\n"
                    "Cross-talk:\n"
                    f"{self._format_turns(crosstalk_turns)}"
                ),
                message_type="message",
                msg_type="verifier_feedback",
            ),
            workspace_id=workspace_id,
        )

    def _send_revision_briefs(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        proposal_turns: list[TeamRoomTurn],
        crosstalk_turns: list[TeamRoomTurn],
        reviewer_turn: TeamRoomTurn,
        workspace_id: int,
    ) -> list[AgentMessage]:
        reviewer_body = reviewer_turn.step.reply.body if reviewer_turn.step.reply else ""
        messages: list[AgentMessage] = []
        for role in TEAM_ROOM_DEPARTMENT_ROLES:
            messages.append(
                self.mailbox_svc.send_message(
                    SendMessageInput(
                        from_agent=TEAM_ROOM_REVIEWER.agent_id,
                        recipients=[role.agent_id],
                        convoy_id=convoy_id,
                        subject=f"Revision round: {role.agent_name}",
                        body=(
                            f"{lead_frame}\n\n"
                            f"{role.revision_prompt}\n\n"
                            "Original proposals:\n"
                            f"{self._format_turns(proposal_turns)}\n\n"
                            "Cross-talk:\n"
                            f"{self._format_turns(crosstalk_turns)}\n\n"
                            f"Adversarial review:\n{reviewer_body}"
                        ),
                        message_type="interrupt",
                        msg_type="verifier_feedback",
                    ),
                    workspace_id=workspace_id,
                )
            )
        return messages

    def _send_synthesis_brief(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        proposal_turns: list[TeamRoomTurn],
        crosstalk_turns: list[TeamRoomTurn],
        reviewer_turn: TeamRoomTurn,
        revision_turns: list[TeamRoomTurn],
        workspace_id: int,
    ) -> AgentMessage:
        reviewer_body = reviewer_turn.step.reply.body if reviewer_turn.step.reply else ""
        return self.mailbox_svc.send_message(
            SendMessageInput(
                from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                recipients=[TEAM_ROOM_SYNTHESIZER.agent_id],
                convoy_id=convoy_id,
                subject="Growth boardroom final synthesis brief",
                body=(
                    f"{lead_frame}\n\n"
                    f"{TEAM_ROOM_SYNTHESIZER.proposal_prompt}\n\n"
                    "Department proposals:\n"
                    f"{self._format_turns(proposal_turns)}\n\n"
                    "Cross-talk:\n"
                    f"{self._format_turns(crosstalk_turns)}\n\n"
                    f"Adversarial review:\n{reviewer_body}\n\n"
                    "Revised department plans:\n"
                    f"{self._format_turns(revision_turns)}"
                ),
                message_type="message",
                msg_type="work_handoff",
            ),
            workspace_id=workspace_id,
        )

    def _run_department_phase(
        self,
        team_id: int,
        subtasks: list[Subtask],
        *,
        phase: str,
        reply_builder,
        use_runtime: bool,
        runtime_lane: str | None,
        workspace_id: int,
    ) -> list[TeamRoomTurn]:
        turns: list[TeamRoomTurn] = []
        for role, subtask in zip(TEAM_ROOM_DEPARTMENT_ROLES, subtasks):
            step = self.loop_svc.run_member_step(
                team_id,
                role.agent_id,
                subtask_id=subtask.id,
                reply_body=reply_builder(role),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                complete=True,
                workspace_id=workspace_id,
            )
            turns.append(TeamRoomTurn(phase=phase, role=role, step=step))
        return turns

    @staticmethod
    def _resolve_meeting_shape(
        *,
        meeting_mode: str | None,
        max_rounds: int | None,
    ) -> tuple[str, int]:
        normalized_mode = (meeting_mode or "").strip().lower()
        if normalized_mode in ("", "classic", "v1", TEAM_ROOM_MODE_CLASSIC):
            mode = TEAM_ROOM_MODE_CLASSIC
        elif normalized_mode in ("facilitated", "v2", TEAM_ROOM_MODE_FACILITATED):
            mode = TEAM_ROOM_MODE_FACILITATED
        else:
            raise ValueError(
                "meeting_mode must be classic_boardroom or facilitated_boardroom"
            )

        if max_rounds is None:
            rounds = (
                TEAM_ROOM_FACILITATED_DEFAULT_ROUNDS
                if mode == TEAM_ROOM_MODE_FACILITATED
                else 1
            )
        else:
            rounds = max_rounds
            if meeting_mode is None and rounds > 1:
                mode = TEAM_ROOM_MODE_FACILITATED

        if rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if rounds > TEAM_ROOM_MAX_ROUNDS:
            raise ValueError(f"max_rounds cannot exceed {TEAM_ROOM_MAX_ROUNDS}")
        if mode == TEAM_ROOM_MODE_CLASSIC and rounds != 1:
            raise ValueError("classic_boardroom supports max_rounds=1")
        return mode, rounds

    def _subtasks_for_department_phase(
        self,
        subtasks: list[Subtask],
        phase: str,
        *,
        round_number: int | None = None,
    ) -> list[Subtask]:
        phase_subtasks = [
            subtask
            for subtask in subtasks
            if self._subtask_metadata(subtask).get("phase") == phase
            and (
                round_number is None
                or self._subtask_metadata(subtask).get("round") == round_number
            )
            and self._subtask_metadata(subtask).get("role")
            in {role.key for role in TEAM_ROOM_DEPARTMENT_ROLES}
        ]
        phase_subtasks.sort(
            key=lambda subtask: int(
                self._subtask_metadata(subtask).get("role_order", 999)
            )
        )
        if len(phase_subtasks) != len(TEAM_ROOM_DEPARTMENT_ROLES):
            raise ValueError(
                f"Expected {len(TEAM_ROOM_DEPARTMENT_ROLES)} {phase} subtasks"
            )
        return phase_subtasks

    def _subtask_for_phase(
        self,
        subtasks: list[Subtask],
        phase: str,
        *,
        round_number: int | None = None,
    ) -> Subtask:
        matches = [
            subtask
            for subtask in subtasks
            if self._subtask_metadata(subtask).get("phase") == phase
            and (
                round_number is None
                or self._subtask_metadata(subtask).get("round") == round_number
            )
        ]
        if len(matches) != 1:
            raise ValueError(f"Expected one {phase} subtask, found {len(matches)}")
        return matches[0]

    @staticmethod
    def _subtask_metadata(subtask: Subtask) -> dict:
        if not subtask.metadata:
            return {}
        try:
            data = json.loads(subtask.metadata)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _subtask_description(
        goal: str,
        context: str | None,
        prompt: str,
        *,
        phase: str,
    ) -> str:
        parts = [f"Goal: {goal}", f"Phase: {phase}", prompt]
        if context:
            parts.insert(1, f"Context: {context}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_lead_frame(goal: str, context: str | None) -> str:
        frame = (
            "Lead frame:\n"
            f"Goal: {goal}\n"
            "Run this like a company boardroom. Each department should bring its "
            "own expertise, build or challenge peers when useful, and keep the "
            "conversation pointed at decisions and execution."
        )
        if context:
            frame += f"\nContext: {context}"
        return frame

    @staticmethod
    def _first_subtask_for_agent(subtasks: list[Subtask], agent_id: str) -> Subtask:
        for subtask in subtasks:
            if subtask.assigned_agent_id == agent_id:
                return subtask
        raise ValueError(f"No subtask assigned to {agent_id}")

    @staticmethod
    def _format_turns(turns: list[TeamRoomTurn]) -> str:
        lines: list[str] = []
        for turn in turns:
            body = turn.step.reply.body if turn.step.reply else ""
            lines.append(f"- {turn.role.agent_name} ({turn.phase}): {body}")
        return "\n".join(lines)

    @staticmethod
    def _default_proposal_reply(role: TeamRoomRoleSpec, goal: str) -> str:
        replies = {
            "sales": (
                f"Sales proposal for {goal}: define the highest-intent buyer, sell a "
                "narrow first outcome, and convert interest through a direct audit call. "
                "Owner: sales. Validation: qualified conversations, booked calls, and "
                "objections that repeat enough to sharpen the offer."
            ),
            "marketing": (
                f"Marketing proposal for {goal}: turn the goal into one sharp promise, "
                "one proof asset, and one campaign hook. Owner: marketing. Validation: "
                "message click-through, audit requests, and proof gaps people still ask about."
            ),
            "product_frontend": (
                f"Product/frontend proposal for {goal}: make the first user path obvious, "
                "remove conversion friction, and ship the smallest page/app change that "
                "demonstrates the offer. Owner: product/frontend. Validation: CTA rate and "
                "drop-off points."
            ),
            "ops": (
                f"Ops proposal for {goal}: sequence the work, assign owners, instrument the "
                "funnel, and force a daily readout. Owner: ops. Validation: completed launch "
                "checklist, clean handoffs, and visible metrics."
            ),
        }
        return replies.get(role.key, role.proposal_prompt)

    @staticmethod
    def _default_facilitator_opening_reply(goal: str, max_rounds: int) -> str:
        return (
            f"Facilitator opening for {goal}: run up to {max_rounds} focused "
            "cross-talk round(s). Decision criteria: one buyer, one offer, one "
            "owner per department, one validation signal, and no false consensus. "
            "Speaking order starts with department proposals, then peer challenges."
        )

    @staticmethod
    def _default_facilitator_round_reply(
        goal: str,
        round_number: int,
        max_rounds: int,
    ) -> str:
        if round_number >= max_rounds:
            close_instruction = "This is the final cross-talk round before review."
        else:
            close_instruction = "Continue only if a peer objection changes the plan."
        return (
            f"Facilitator round {round_number} for {goal}: answer the strongest "
            "peer dependency directly. Sales must pressure buyer proof, Marketing "
            "must pressure message proof, Product/Frontend must pressure shippable "
            f"conversion work, and Ops must pressure execution cadence. {close_instruction}"
        )

    @staticmethod
    def _default_crosstalk_reply(
        role: TeamRoomRoleSpec,
        goal: str,
        *,
        round_number: int = 1,
    ) -> str:
        if round_number > 1:
            replies = {
                "sales": (
                    f"Sales to Marketing and Product/Frontend for {goal}: the campaign "
                    "and page must prove one buyer will book the audit. Sales accepts "
                    "Ops' daily objection log, but rejects broad messaging until the "
                    "first buyer segment repeats the same pain."
                ),
                "marketing": (
                    f"Marketing to Sales and Product/Frontend for {goal}: sales needs "
                    "to supply exact buyer language, and product needs to turn the proof "
                    "asset into the conversion path. Marketing will not launch extra "
                    "channels until one proof-backed hook wins."
                ),
                "product_frontend": (
                    f"Product/Frontend to Sales and Ops for {goal}: the first shippable "
                    "change is a narrow proof-to-CTA path, not a platform rebuild. "
                    "Sales must define the objection we design against, and Ops must "
                    "confirm tracking is live before launch."
                ),
                "ops": (
                    f"Ops to all departments for {goal}: the plan is blocked unless each "
                    "owner names one deliverable, one due date, and one metric. Ops "
                    "accepts the narrow audit motion and rejects parallel experiments "
                    "until the two-week readout says otherwise."
                ),
            }
            return replies.get(role.key, "Pass: no useful second-round delta.")

        replies = {
            "sales": (
                f"Build: marketing's promise should be tested in live sales calls for {goal}. "
                "Challenge: product should not ship a polished path before we know which "
                "objection kills the deal. Sales can feed objections back daily."
            ),
            "marketing": (
                f"Build: sales gives us the buyer language for {goal}, and product gives us "
                "the proof surface. Challenge: ops needs one campaign owner so the story "
                "doesn't scatter across disconnected experiments."
            ),
            "product_frontend": (
                f"Build: marketing's proof asset can become the first conversion surface for "
                f"{goal}. Challenge: sales and ops need to keep the first CTA narrow so the "
                "page does not promise a broad platform before the workflow is proven."
            ),
            "ops": (
                f"Build: product's small shippable change and sales' objection loop make "
                f"{goal} measurable. Challenge: marketing should not launch multiple channels "
                "until one offer, one CTA, and one follow-up owner are working."
            ),
        }
        return replies.get(role.key, "Pass: no useful delta from this department.")

    @staticmethod
    def _default_reviewer_reply(goal: str) -> str:
        return (
            f"Adversarial review for {goal}: the plan can fail through false consensus. "
            "Sales wants buyer proof, marketing wants a campaign, product wants a cleaner "
            "path, and ops wants cadence, but none of that matters unless one offer and "
            "one validation loop are chosen. Highest-risk trap: launching activity before "
            "the team can name the buyer, CTA, owner, and metric in one sentence."
        )

    @staticmethod
    def _default_revision_reply(role: TeamRoomRoleSpec, goal: str) -> str:
        replies = {
            "sales": (
                f"Sales revision for {goal}: commit to one buyer segment and one audit-style "
                "CTA. Sales owns 20 targeted conversations, objection logging, and a weekly "
                "decision on whether the offer is resonating."
            ),
            "marketing": (
                f"Marketing revision for {goal}: commit to one proof-backed campaign around "
                "the audit CTA. Marketing owns the promise, proof asset, and channel test, "
                "then revises copy from sales objections."
            ),
            "product_frontend": (
                f"Product/frontend revision for {goal}: commit to one scan-friendly path from "
                "promise to proof to audit CTA. Product owns the page/app change, CTA tracking, "
                "and friction fixes found in review."
            ),
            "ops": (
                f"Ops revision for {goal}: commit to a two-week launch cadence with named "
                "owners, daily metric readout, and a stop/go review. Ops owns handoffs, QA, "
                "instrumentation, and keeping the team out of broad unfocused work."
            ),
        }
        return replies.get(role.key, role.revision_prompt)

    @staticmethod
    def _default_final_brief(goal: str) -> str:
        return (
            f"Final Team Room brief for {goal}:\n"
            "Decision: run one narrow growth boardroom bet instead of scattering into broad work.\n"
            "Bets: one buyer segment, one audit-style CTA, one proof-backed campaign, one "
            "small product/frontend conversion path, and one daily ops readout.\n"
            "Risks: false consensus, vague buyer language, overbuilt product work, and "
            "marketing activity without sales feedback.\n"
            "Objections: if the buyer will not book the audit, the offer is weak; if the "
            "page does not explain the audit, the product path is weak; if follow-up is not "
            "tracked, ops is weak.\n"
            "Next actions: Sales books 20 targeted conversations; Marketing ships the proof "
            "asset and campaign copy; Product/Frontend ships the CTA path and tracking; Ops "
            "runs the daily readout and the two-week stop/go review."
        )

    @staticmethod
    def _build_decision_ledger(goal: str) -> TeamRoomDecisionLedger:
        return TeamRoomDecisionLedger(
            decisions=[
                f"Run one narrow, proof-backed growth bet for {goal}.",
                "Use the audit-style CTA as the first revenue validation motion.",
                "Hold broad campaigns and product expansion until the first buyer proof loop is visible.",
            ],
            accepted_bets=[
                "One buyer segment.",
                "One proof-backed campaign hook.",
                "One scan-friendly proof-to-CTA product path.",
                "One daily operating readout.",
            ],
            rejected_bets=[
                "Multiple unfocused channels before offer proof.",
                "Broad platform polish before conversion proof.",
                "Consensus without named owners and validation signals.",
            ],
            owner_actions=[
                {
                    "owner": "Sales",
                    "action": "Run 20 targeted conversations and log repeated objections.",
                    "validation_signal": "Qualified calls and repeated buyer pain.",
                },
                {
                    "owner": "Marketing",
                    "action": "Ship the proof asset and first campaign hook around the audit CTA.",
                    "validation_signal": "Click-through, audit requests, and proof gaps.",
                },
                {
                    "owner": "Product/Frontend",
                    "action": "Ship the narrow proof-to-CTA path with tracking.",
                    "validation_signal": "CTA rate and drop-off points.",
                },
                {
                    "owner": "Ops",
                    "action": "Run the two-week cadence, handoffs, QA, and daily metric readout.",
                    "validation_signal": "Owners ship on cadence and stop/go review has data.",
                },
            ],
            open_questions=[
                "Which buyer segment repeats the pain fastest?",
                "Which proof asset removes the biggest objection?",
                "What threshold ends the experiment or earns a second round of investment?",
            ],
            strongest_objection=(
                "The team can still confuse activity with proof if it launches "
                "before the buyer, CTA, owner, and metric fit in one sentence."
            ),
            next_meeting_trigger=(
                "Reconvene after the first two-week readout or sooner if sales "
                "cannot book qualified audit conversations."
            ),
        )


def _clip_text(text: str | None, *, max_chars: int = 600) -> str:
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 14].rstrip() + "\n...[truncated]"


def team_room_turn_to_dict(turn: TeamRoomTurn) -> dict:
    return {
        "phase": turn.phase,
        "role": turn.role.key,
        "role_name": turn.role.agent_name,
        "agent_id": turn.role.agent_id,
        "subtask_id": turn.step.subtask_id,
        "action": turn.step.action,
        "status": turn.step.subtask_after.status if turn.step.subtask_after else None,
        "completed": turn.step.completed,
        "reply": dataclasses.asdict(turn.step.reply) if turn.step.reply else None,
        "runtime": team_room_runtime_metadata_for_step(turn.step),
        "step": _team_room_safe_step_dict(turn.step),
    }


def team_room_runtime_metadata_for_step(step: TeamLoopStepResult) -> dict | None:
    if step.runtime is None:
        return None
    return {
        "runtime_lane": step.runtime.runtime_lane,
        "provider": step.runtime.provider,
        "model": step.runtime.model,
        "profile_key": step.runtime.profile_key,
        "cost_usd": step.runtime.cost_usd,
        "execution_time_ms": step.runtime_execution_time_ms,
        "tool_call_count": step.runtime.tool_call_count,
        "error": step.runtime_error,
    }


def _team_room_safe_step_dict(step: TeamLoopStepResult) -> dict:
    data = result_to_dict(step)
    data["claimed"] = []
    runtime = data.get("runtime")
    if isinstance(runtime, dict):
        runtime.pop("session_id", None)
    for key in ("subtask_before", "subtask_after"):
        subtask = data.get(key)
        if isinstance(subtask, dict):
            subtask["description"] = ""
    newly_ready = data.get("newly_ready")
    if isinstance(newly_ready, list):
        for subtask in newly_ready:
            if isinstance(subtask, dict):
                subtask["description"] = ""
    return data


def _team_room_safe_subtask_dict(subtask: Subtask) -> dict:
    data = dataclasses.asdict(subtask)
    data["description"] = ""
    return data


def team_room_runtime_summary(result: TeamRoomWorkflowResult) -> dict:
    turns = [
        *result.facilitator_turns,
        *result.proposal_turns,
        *result.crosstalk_turns,
        result.reviewer_turn,
        *result.revision_turns,
        result.final_turn,
    ]
    runtime_turns = [turn for turn in turns if turn.step.runtime is not None]
    metadata = [
        team_room_runtime_metadata_for_step(turn.step)
        for turn in runtime_turns
    ]
    present = [item for item in metadata if item is not None]
    costs = [
        item["cost_usd"]
        for item in present
        if isinstance(item.get("cost_usd"), (int, float))
    ]
    elapsed = [
        item["execution_time_ms"]
        for item in present
        if isinstance(item.get("execution_time_ms"), int)
    ]
    errors = [
        str(item["error"])
        for item in present
        if item.get("error")
    ]
    return {
        "enabled": bool(runtime_turns),
        "turn_count": len(runtime_turns),
        "lanes": sorted(
            {str(item["runtime_lane"]) for item in present if item.get("runtime_lane")}
        ),
        "providers": sorted(
            {str(item["provider"]) for item in present if item.get("provider")}
        ),
        "models": sorted(
            {str(item["model"]) for item in present if item.get("model")}
        ),
        "tool_call_count": sum(
            int(item.get("tool_call_count") or 0) for item in present
        ),
        "cost_usd": round(sum(costs), 6) if costs else (0.0 if runtime_turns else None),
        "execution_time_ms": sum(elapsed) if elapsed else (0 if runtime_turns else None),
        "errors": errors,
    }


def team_room_discussion_round_to_dict(round_result: TeamRoomDiscussionRound) -> dict:
    return {
        "round_number": round_result.round_number,
        "facilitator_message": (
            dataclasses.asdict(round_result.facilitator_message)
            if round_result.facilitator_message
            else None
        ),
        "facilitator_turn": (
            team_room_turn_to_dict(round_result.facilitator_turn)
            if round_result.facilitator_turn
            else None
        ),
        "crosstalk_messages": [
            dataclasses.asdict(message)
            for message in round_result.crosstalk_messages
        ],
        "crosstalk_turns": [
            team_room_turn_to_dict(turn)
            for turn in round_result.crosstalk_turns
        ],
    }


def team_room_turn_summary(result: TeamRoomWorkflowResult) -> str:
    parts: list[str] = []
    if result.facilitator_turns:
        parts.append(f"{len(result.facilitator_turns)} facilitator")
    parts.extend(
        [
            f"{len(result.proposal_turns)} proposals",
            f"{len(result.crosstalk_turns)} cross-talk",
            "1 adversarial critique",
            f"{len(result.revision_turns)} revisions",
            "1 final synthesis",
        ]
    )
    return ", ".join(parts)


def team_room_workflow_result_to_dict(result: TeamRoomWorkflowResult) -> dict:
    convoy = result.convoy.convoy
    phase_results = {
        "facilitator": [
            team_room_turn_to_dict(turn)
            for turn in result.facilitator_turns
        ],
        "proposal": [team_room_turn_to_dict(turn) for turn in result.proposal_turns],
        "crosstalk": [team_room_turn_to_dict(turn) for turn in result.crosstalk_turns],
        "adversarial_review": team_room_turn_to_dict(result.reviewer_turn),
        "revision": [team_room_turn_to_dict(turn) for turn in result.revision_turns],
        "synthesis": team_room_turn_to_dict(result.final_turn),
    }
    return {
        "workflow_id": result.workflow_id,
        "meeting_mode": result.meeting_mode,
        "max_rounds": result.max_rounds,
        "goal": result.goal,
        "context_excerpt": _clip_text(result.context, max_chars=600) if result.context else None,
        "team_id": result.team.session.id,
        "convoy_id": result.convoy.convoy.id,
        "runtime": team_room_runtime_summary(result),
        "progress": {
            "completed": convoy.completed_subtasks,
            "total": convoy.total_subtasks,
            "status": convoy.status,
        },
        "team": {
            "session": dataclasses.asdict(result.team.session),
            "members": [dataclasses.asdict(member) for member in result.team.members],
        },
        "convoy": {
            "convoy": dataclasses.asdict(result.convoy.convoy),
            "subtasks": [
                _team_room_safe_subtask_dict(subtask)
                for subtask in result.convoy.subtasks
            ],
            "edges": [dataclasses.asdict(edge) for edge in result.convoy.edges],
        },
        "lead_frame_excerpt": _clip_text(result.lead_frame, max_chars=600),
        "message_counts": {
            "facilitator": len(result.facilitator_messages),
            "proposal": len(result.proposal_messages),
            "crosstalk": len(result.crosstalk_messages),
            "revision": len(result.revision_messages),
            "reviewer": 1,
            "synthesis": 1,
        },
        "turn_summary": team_room_turn_summary(result),
        "discussion_rounds": [
            team_room_discussion_round_to_dict(round_result)
            for round_result in result.discussion_rounds
        ],
        "decision_ledger": dataclasses.asdict(result.decision_ledger),
        "phase_results": phase_results,
        "final_brief": result.final_brief,
    }
