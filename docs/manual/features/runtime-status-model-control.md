# Runtime Status And Model Control

Status: active baseline
Owner: lane-first runtime selection
Last updated: 2026-09-12

## What It Does

Runtime status and model control let the operator inspect and change the active
lane/model without editing config files. The contract is lane-first: operator
surfaces should talk about lanes first and keep provider-specific details behind
the runtime layer.

`thehomie doctor` also reports curriculum readiness: optional video
dependencies, configured/enabled persona counts, the global curriculum kill
switch, and malformed profile config. Per-persona physical state is available
without side effects through `thehomie curriculum status <persona> --json`.

v1.9.0 adds learning-dispatcher diagnostics. Use
`thehomie profile learning summary default --json` and the persona's Learning tab
to inspect cognitive cycles, investigations, queue state, and context receipts.
A healthy runtime or configured model alone does not prove that reasoning
completed. See [Persona Harness Learning](persona-harness-learning.md).

## Operator Entry Points

- Chat/Telegram/Discord: `/provider`, `/model`, `/diagnostics`
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
gemini`, `/model openrouter`, `/model openai`, `/model kimi`, `/model free`, and `/model auto`
choose where the next request runs. Provider-specific model pins use
`provider:model`, but Codex also accepts short GPT-style aliases:

```bash
uv run thehomie chat -q "/model codex:default" -Q   # Codex plan default; no --model flag passed
uv run thehomie chat -q "/model codex:gpt-5.5" -Q   # Pin a concrete Codex model
uv run thehomie chat -q "/model gpt5.5" -Q           # Same pin, easier shorthand
uv run thehomie chat -q "/model codex 5.5" -Q        # Same pin, provider + version shorthand
uv run thehomie chat -q "/model sol" -Q              # GPT-5.6 Sol + xhigh reasoning
uv run thehomie chat -q "/model terra" -Q            # GPT-5.6 Terra; clears Sol effort
uv run thehomie chat -q "/model luna" -Q             # GPT-5.6 Luna; clears Sol effort
uv run thehomie chat -m codex:gpt-5.5 -q "Reply OK" -Q
```

`codex:default`, `codex latest`, and `gpt latest` clear the Codex model pin and
leave the Codex CLI/ChatGPT plan to choose its hidden backend model. Pinned
values such as `codex:gpt-5.5`, `gpt5.5`, `gpt 5.5`, `gbt 5.5`, `codex 5.5`,
and `codec 5.5` are normalized to `gpt-5.5`.

The bare tier aliases `sol`, `terra`, and `luna` pin their corresponding
GPT-5.6 Codex models. `sol` also owns an `xhigh` reasoning-effort override.
Selecting any other Codex tier or `codex:default` clears that override, so Sol's
cost/latency setting cannot silently stick to a later model choice.

`/provider`, `/diagnostics`, and `thehomie status --json` report the configured
model. When Codex is set to `chatgpt-plan-default`, the CLI/ChatGPT plan
chooses the concrete backend model; the configured setting alone does not
identify it. Inspect the completed execution receipt for the actual model when
the transport exposes it. Do not infer execution from the configured default.

## Learning When You Change Models

Learning belongs to the framework and persona. Selecting Codex, Kimi, Claude,
or another configured runtime keeps the same understanding, investigations,
evidence, and method history. Shared function hooks invoke the same cognitive
lifecycle; Claude-native hooks are an optional adapter, not a prerequisite.
The selected runtime still needs the capabilities and account availability
required by that request. Recorded attempts show any actual fallback.

Foreground model selection and background selection are distinct. A one-turn
CLI override selects that conversation; background cognition uses the configured
quality tier on `claude_native` and canonical configured selection on generic
lanes. Existing provider permissions, pause controls, and explicit budgets remain
in effect. See the [developer guide](persona-harness-learning-developer.md#hooks-dispatch-and-reporting).

| Runtime path | Learning behavior and capability boundary |
|---|---|
| Framework hooks and storage | Shared across models; retained state survives a switch or process restart |
| Codex strict background reasoning | Uses the verified isolated app-server bridge with no tools; supports numeric/text evidence. Its image transport remains unverified and unavailable. |
| Codex caller tools | Uses the same verified bridge with only the host-supplied tools; ordinary `codex exec` can retain a different global binary |
| Kimi / generic HTTP model-only calls | Explicitly disable tools; image carriage depends on the selected model/runtime capability and actual inclusion receipt |
| Claude | Uses framework callbacks or verified Mods for lifecycle capture; strict background reasoning remains framework-owned |

`SECOND_BRAIN_CODEX_APP_SERVER_COMMAND` can pin the proven bridge independently
of the global CLI. A version-gate failure is visible; upgrading the global CLI
does not automatically validate its protocol. Follow the
[pinned transport guide](persona-harness-learning-developer.md#pinned-codex-reasoning-and-caller-tool-transport)
instead of disabling that check. Explicit unsupported provider-token or USD
ceilings produce a refusal/fallback, not a silently unenforced budget.

To check continuity, inspect a retained version before changing models, run a
normal relevant conversation afterward, and confirm its ID/hash in a delivered
context receipt whose phase is `executed`. A report or response that merely
says it remembers is not the receipt. Provider outages defer pending work;
they do not erase retained understanding or turn it into a failed hypothesis.

## Kimi Lane

`/model kimi` selects the Kimi lane: the Kimi Code coding endpoint
(`https://api.kimi.com/coding/v1`) with a plan-quota API key from
`KIMI_API_KEY` (Kimi Code Console; usage counts against the membership quota,
not separate pay-as-you-go billing). Default model is `k3`; pin with
`/model kimi:k3` or `SECOND_BRAIN_KIMI_MODEL`. The lane is text-route only
(excluded from the provider-owned tool route). The shared
OpenAI-compatible adapter uses the chat-completions call shape for this lane
because the coding endpoint does not serve the OpenAI Responses API
(`/responses` returns 404; probed 2026-07-17).

## NVIDIA Kimi Lane

`/model nvidia` selects NVIDIA's hosted Kimi lane through the NIM API at
`https://integrate.api.nvidia.com/v1`. It reads `NVIDIA_API_KEY` and defaults
to the NVIDIA catalog model `moonshotai/kimi-k2.6`; pin another NVIDIA model
with `/model nvidia:<model>` or `SECOND_BRAIN_NVIDIA_KIMI_MODEL`. NVIDIA does
not currently list a Kimi K3 endpoint, so this route is intentionally separate
from the native `/model kimi:k3` coding lane. The NVIDIA route uses the shared
OpenAI-compatible chat-completions adapter and is last in the generic text
fallback route.

## OpenCode Free Lane

Select `/model free` or `/model free:<model>` in Telegram or Discord. In
Discord's native `/model` command, put `free` in the **args** option; typed
commands work too. CLI equivalents are `thehomie chat -q "/model free" -Q`
(persistent profile selection) and `thehomie chat -m free -q "hi" -Q`
(process-local selection). Existing role gates and profile scope are unchanged.

The keyless relay is `https://opencode.ai/zen/v1`. No account, API key, OAuth,
or subscription is needed. The bundled default is `deepseek-v4-flash-free`;
`/model free:mimo-v2.5-free` illustrates a model pin. Free promotions rotate:
there is no automatic catalog discovery in this release. An unsupported or
retired model is an error, not permission to use another provider.

### Verify before changing selection

`/model free` and Free CLI overrides first make a maximum-30-second anonymous
inference probe containing only `Reply exactly OK.` No system prompt, vault,
history, images, or tool schemas are included. Only a completed response allows
the canonical selection/model keys to be committed. Persistence uses a file
lock and atomic replacement; a superseded probe cannot overwrite a newer choice.

On failure, the previous selection stays active, with an explicit error such as:

> Free switch failed — HTTP 429. Selection unchanged by this request: generic
> runtime via Codex [model: chatgpt-plan-default]. Try again later.

After switching successfully, **Free stays Free**. Rate limits, timeouts,
unsupported tools, and provider errors terminate that request. There is no
retry through paid APIs, Claude, Codex, Gemini, or subscription quota. Other
providers' normal fallback behavior is unchanged. Free is never inserted into
an automatic route or selected for unrelated profiles.

### Harness and capabilities

Free reuses the existing model-neutral memory/learning lifecycle and scoped
caller-tool dispatcher. It does not implement provider-native shell tools,
CLI session resume, or provider hooks. Requests requiring those features fail
visibly; the runtime never silently drops required tools. This means a legacy
chat profile still using only native CLI tools may need an existing scoped
caller-tool configuration for tool-enabled Free turns. No grants are added by
selecting a model.

Model-only learning continues to send `tools=[]`, `tool_choice=none` and an
output-token limit. A non-null USD budget is accepted only for the fixed,
anonymous Free endpoint; its runtime inference cost is reported as zero under
that contract. Other HTTP adapters still reject budgets they cannot enforce.
Free does not make external tool services free, and a model switch is not
permission for external actions.

The SDK placeholder key never goes on the wire: Authorization is empty, SDK
account/project headers cannot inherit credentials, environment proxies are
not inherited, and redirects and SDK retries are disabled. Ordinary Free turns
send their normal assembled context to an external provider; **keyless is not
local or a data-retention guarantee**. Avoid sensitive inputs unless you accept
the provider's current data policy. The probe verifies availability, not privacy.

`/provider`, diagnostics and status distinguish **configured** Free from a
completed execution. Quiet JSON failures retain `success=false` and the error;
execution receipts retain the actual provider/model. No automatic bot restart
or change to installation defaults accompanies this source release. Native
menus refresh through the existing adapter startup/sync path when deployed.

### Regression tests

Run `tests/test_opencode_free.py`, the runtime selection/routing/CLI suites,
and the channel and persona-learning suites. Tests use synthetic messages,
mock HTTP transports and temporary state, never the operator's live profile.
The wire tests inspect SDK-built headers, not only constructor arguments.

## Per-Adapter Runtime Deadlines (#133)

Every `adapter.run()` at the lane chokepoint is bounded by `asyncio.wait_for`,
so a wedged provider CLI (a Codex/Gemini child that never exits, a stalled SDK
stream) can no longer hang scheduled pipelines (heartbeat, reflection, weekly,
dream, cabinet, persona learning) that have no outer deadline of their own.

| Env var | Default | Meaning |
|---|---|---|
| `SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS` | `300` | Deadline per adapter attempt for TEXT_REASONING requests |
| `SECOND_BRAIN_RUNTIME_TIMEOUT_TOOL_SECONDS` | `1800` | Deadline per adapter attempt for TOOL_REASONING requests |

Semantics to know:
- The deadline is **per adapter attempt, not per turn** — a fallback chain of N
  providers can legitimately take up to N x timeout before the request fails.
- `<= 0` disables the deadline entirely (escape hatch — nothing bounds the call).
- On timeout the profile is marked retryable-failed and the chain **continues**
  to the next provider **except under the strict Free-only policy**; an operator cancel (`CancelledError`) propagates
  untouched instead of being mislabeled a timeout.
- On Windows the cancelled CLI child is **tree-killed** (`taskkill /T`) — the
  npm `.CMD` wrapper trap left the real Node process alive under a plain kill.

## Latest Live Proof

Use current CLI/status checks before making a new live claim. Tracker entries
record runtime proofs for Team Room and other runtime-backed lanes.

## Public Export Status

Runtime surfaces are framework core; public export status depends on the slice
and must be verified through `scripts/sanitize.py` and the public mirror.

## Next Slices

- Manual page for provider catalog/runtime overlays.
- Dashboard-specific lane/model diagnostics page if `/agents` grows too dense.
