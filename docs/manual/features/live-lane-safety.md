# Live Lane Safety Contract

Status: active baseline.

## Source Of Truth

- `.claude/scripts/orchestration/live_safety.py`
- `.claude/chat/diagnostics.py`
- `.claude/chat/cli.py`
- `.claude/scripts/orchestration/api.py`

## Operator Contract

Live agent/factory actions default to dry-run/read-only refusal. A live action
must opt in through one of these explicit controls:

- CLI/API field: `--allow-live-agent-run` or `allow_live_agent_run: true`
- Environment: `HOMIE_ALLOW_LIVE_AGENT_RUN=1`

The shared guard is intentionally above lower-level policies. Passing the live
lane guard does not bypass BrowserOps workflow approval, direct-integration
capability policies, or Cabinet tool/default-deny policy.

## Covered Live Surfaces

- `thehomie convoy dispatch`
- `thehomie team tick`
- `thehomie team room run`
- `/teamtick`
- `/teamroom`
- `/api/convoy/{convoy_id}/subtask/{subtask_id}/dispatch`
- `/api/team/room/run`
- `/api/team/operating-room/run`
- `/api/team/{team_id}/loop-step`
- `/api/team/{team_id}/tick`
- `/api/team/{team_id}/executor-step`

Read-only status/list/get endpoints do not require live opt-in.

## Diagnostics

`thehomie status --json`, `thehomie status`, and `thehomie doctor` expose:

- `mode`: `dry_run` or `live`
- `live_agent_run_allowed`
- `opt_in_sources`
- `default_contract`
- `lower_level_gates`

## Proof Command

Use the gate-only proof command when you need to prove refusal and opt-in
without running an agent, executor, browser workflow, direct integration, or
Cabinet turn:

```bash
uv run thehomie live-safety proof --json
uv run thehomie live-safety proof --allow-live-agent-run --json
```

This proves only the shared live gate. It does not claim Telegram, Discord,
BrowserOps, direct integrations, Cabinet voice, or external executor readiness.
