# Runtime Status And Model Control

Status: active baseline
Owner: lane-first runtime selection
Last updated: 2026-07-17

## What It Does

Runtime status and model control let the operator inspect and change the active
lane/model without editing config files. The contract is lane-first: operator
surfaces should talk about lanes first and keep provider-specific details behind
the runtime layer.

## Operator Entry Points

- Chat/Telegram: `/provider`, `/model`, `/diagnostics`
- CLI: `thehomie status --json`, `thehomie doctor`,
  `thehomie chat -m <lane-or-provider>`
- Dashboard: `/agents`, `/usage`
- API: `/api/agents/model`, `/api/tokens`, `/api/jarvis/status`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Runtime selection | `.claude/scripts/runtime/selection.py`, `.claude/scripts/runtime/lane_router.py`, `.claude/scripts/runtime/registry.py` |
| Chat/router | `.claude/chat/commands.py`, `.claude/chat/core_handlers.py`, `.claude/chat/cli.py`, `.claude/chat/diagnostics.py` |
| Dashboard API | `.claude/scripts/dashboard_api.py` |
| Dashboard web | `dashboard/web/src/pages/Agents.tsx`, `dashboard/web/src/pages/Usage.tsx`; `dashboard/web/src/pages/Jarvis.tsx` remains an internal status component hidden from public nav |
| Tests | `.claude/scripts/tests/test_runtime_selection.py`, `.claude/scripts/tests/test_cli.py`, `.claude/scripts/tests/test_diagnostics.py`, `.claude/scripts/tests/test_dashboard_api.py` |

## Safety Boundaries

- Preserve lane-first wording.
- Quiet-mode JSON is a machine contract; keep stable fields such as `success`,
  `error`, `session_id`, `lane`, `provider`, `model`, `cost_usd`,
  `tool_calls`, and `execution_time_ms`.
- Do not merge Claude Max subscription semantics with API cost semantics.
- Runtime selection changes go through canonical selection helpers.

## How To Run It

```powershell
cd <repo>\.claude\scripts
uv run thehomie chat -q "/provider" -Q
uv run thehomie chat -q "/model auto" -Q
uv run thehomie status --json
uv run thehomie doctor
```

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_runtime_selection.py tests/test_diagnostics.py tests/test_runtime_registry.py tests/test_cli.py tests/test_lane_router.py tests/test_runtime_routing.py tests/test_chat_runtime_engine.py -q
```

## Model Pinning And Codex Aliases

Runtime selection is lane-first: `/model claude`, `/model codex`, `/model
gemini`, `/model openrouter`, `/model openai`, `/model kimi`, and `/model auto`
choose where the next request runs. Provider-specific model pins use
`provider:model`, but Codex also accepts short GPT-style aliases:

```bash
uv run thehomie chat -q "/model codex:default" -Q   # Codex plan default; no --model flag passed
uv run thehomie chat -q "/model codex:gpt-5.5" -Q   # Pin a concrete Codex model
uv run thehomie chat -q "/model gpt5.5" -Q           # Same pin, easier shorthand
uv run thehomie chat -q "/model codex 5.5" -Q        # Same pin, provider + version shorthand
uv run thehomie chat -m codex:gpt-5.5 -q "Reply OK" -Q
```

`codex:default`, `codex latest`, and `gpt latest` clear the Codex model pin and
leave the Codex CLI/ChatGPT plan to choose its hidden backend model. Pinned
values such as `codex:gpt-5.5`, `gpt5.5`, `gpt 5.5`, `gbt 5.5`, `codex 5.5`,
and `codec 5.5` are normalized to `gpt-5.5`.

`/provider`, `/diagnostics`, and `thehomie status --json` report the configured
model. When Codex is set to `chatgpt-plan-default`, the CLI/ChatGPT plan
chooses the concrete backend model and The Homie reports that backend as
unobserved.

## Kimi Lane

`/model kimi` selects the Kimi lane: the Kimi Code coding endpoint
(`https://api.kimi.com/coding/v1`) with a plan-quota API key from
`KIMI_API_KEY` (Kimi Code Console; usage counts against the membership quota,
not separate pay-as-you-go billing). Default model is `k3`; pin with
`/model kimi:k3` or `SECOND_BRAIN_KIMI_MODEL`. The lane is text-route only
(last in `GENERIC_TEXT_ROUTE`, excluded from the tool route). The shared
OpenAI-compatible adapter uses the chat-completions call shape for this lane
because the coding endpoint does not serve the OpenAI Responses API
(`/responses` returns 404; probed 2026-07-17).

## Latest Live Proof

Use current CLI/status checks before making a new live claim. Tracker entries
record runtime proofs for Team Room and other runtime-backed lanes.

## Public Export Status

Runtime surfaces are framework core; public export status depends on the slice
and must be verified through `scripts/sanitize.py` and the public mirror.

## Next Slices

- Manual page for provider catalog/runtime overlays.
- Dashboard-specific lane/model diagnostics page if `/agents` grows too dense.
