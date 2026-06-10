# Heartbeat Runtime

Status: active baseline, runtime contract corrected
Owner: scheduled cognition/runtime layers
Last updated: 2026-06-07

## What It Does

Heartbeat is the proactive scheduled check loop. It gathers compact context from
direct Python integrations, memory state, drafts, habits, and `HEARTBEAT.md`,
then sends one assembled prompt through the Homie runtime so the selected model
can decide whether anything needs attention.

The heartbeat is not a script-only classifier. Even when gathered context looks
quiet, the expected contract is: gather deterministic context first, run runtime
reasoning second, and write `HEARTBEAT_OK` only after the runtime pass returns
that result.

## Operator Entry Points

- Manual run: `uv run python heartbeat.py --test`
- Validation probe: `uv run python heartbeat.py --json`
- Runtime status: `uv run thehomie chat -q "/provider" -Q`
- Checklist: `vault/memory/HEARTBEAT.md`
- State: `.claude/data/state/heartbeat-state.json`
- Daily log output: `vault/memory/daily/YYYY-MM-DD.md`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Heartbeat loop | `.claude/scripts/heartbeat.py` |
| Runtime routing | `.claude/scripts/runtime/lane_router.py`, `.claude/scripts/runtime/routing.py` |
| Provider adapters | `.claude/scripts/runtime/openai_codex.py`, `.claude/scripts/runtime/gemini_cli.py`, `.claude/scripts/runtime/claude_sdk.py` |
| Runtime profiles | `.claude/scripts/runtime/profiles.py`, `.claude/scripts/runtime/selection.py` |
| Tests | `.claude/scripts/tests/test_heartbeat_preflight.py`, `.claude/scripts/tests/test_openai_codex_runtime.py` |

## Token And Model Contract

- Context gathering is deterministic and token-efficient. Python calls direct
  integrations and local helpers for email, calendar, tasks, finance, drafts,
  habits, recall, alert history, and the heartbeat checklist before the model
  is invoked.
- The model receives preloaded context. It should not browse raw inboxes,
  calendars, or task systems from scratch during the normal heartbeat path.
- Main heartbeat reasoning uses
  `RuntimeRequest(task_name="heartbeat", capability=TOOL_REASONING)`.
- The Codex path uses `HEARTBEAT_CODEX_MODEL`, defaulting to
  `gpt-5.4-mini`, as a heartbeat-only model override. This does not mutate
  `SECOND_BRAIN_CODEX_MODEL`, so normal chat/model control remains unchanged.
- The Gemini path ignores the Codex-specific override and uses the configured
  Gemini profile model or fallback ladder.
- The Claude-native path uses the configured Claude profile model. It does not
  automatically switch to Haiku unless the Claude runtime model is explicitly
  configured that way.
- The lightweight alert formatter may use `model="haiku"` with an OpenAI
  fallback, but that formatter is separate from the main heartbeat reasoning
  pass.

## Runtime Selection Behavior

In the current generic lane, heartbeat is a tool-capable task. The generic tool
route prefers Codex and can fall back to Gemini. If the operator pins Gemini,
heartbeat runs through Gemini. If the operator pins Claude native, heartbeat
runs through the Claude SDK lane.

The heartbeat must stay lane-first and provider-agnostic at the routing layer.
Provider-specific model economy belongs in provider-specific profile or request
configuration, not in global chat model mutation.

## Safety Boundaries

- Do not reintroduce a deterministic quiet-context skip before runtime
  reasoning.
- Do not make heartbeat change the global chat model.
- Do not expose secrets from `.env`, OAuth files, vault user files, or provider
  token state in logs or manual pages.
- Keep scheduler cadence changes separate from runtime/model changes unless the
  slice explicitly includes scheduling.

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_heartbeat_preflight.py -q
uv run python -m py_compile heartbeat.py
uv run pytest tests/test_openai_codex_runtime.py -q
uv run thehomie chat -q "/provider" -Q
```

## Latest Proof

On 2026-06-07, focused tests proved that a quiet heartbeat still invokes
`run_with_runtime_lanes`, the heartbeat request keeps `task_name="heartbeat"`
and `capability=TOOL_REASONING`, the heartbeat Codex override defaults to
`gpt-5.4-mini`, and normal Codex chat model configuration remains `gpt-5.5`.

## Public Export Status

Manual page updated in the private repo. Public export requires the normal
`scripts/sanitize.py` private-to-public flow.
