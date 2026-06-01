# Direct Integration Capability Contract

Status: shipped, policy-enforced, public-exported
Owner: Python integrations/runtime policy
Last updated: 2026-06-01

## What It Does

The Direct Integration Capability Contract is the canonical software policy for
what Homie integrations may do. It declares integration actions, effect levels,
exposed surfaces, default policy state, and the enforcement helper that mutating
callers must use before posting, writing, archiving, sending, or deleting.

## Operator Entry Points

- Chat/Telegram: `/email`, `/pemail`, `/gsc`, `/analytics`, `/send`
- CLI/status: `thehomie status --json`, `thehomie doctor`, `/diagnostics`
- Direct wrapper: `.claude/skills/direct-integrations/scripts/query.py`
- API/dashboard: no policy-edit UI yet; dashboard consumers should treat Python
  integration policy as read-only source of truth

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/integrations/capabilities.py`, `.claude/scripts/integrations/registry.py`, `.claude/scripts/runtime/prompt_builder.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/chat/commands.py`, `.claude/chat/diagnostics.py` |
| Integration wrappers | `.claude/skills/direct-integrations/scripts/query.py`, `.claude/scripts/integrations/gmail.py`, `.claude/scripts/integrations/outlook.py`, `.claude/scripts/integrations/slack_api.py`, `.claude/scripts/integrations/sheets_api.py` |
| Background jobs | `.claude/scripts/notifications.py`, `.claude/scripts/heartbeat.py` |
| Tests | `.claude/scripts/tests/test_integration_capabilities.py`, `.claude/scripts/tests/test_prompt_builder.py`, `.claude/scripts/tests/test_extension_manager.py`, `.claude/scripts/tests/test_email_sanitizer.py` |
| Docs/proof | `PRPs/active/PRP-direct-integration-capability-contract.md`, `PRPs/active/TRACKER.md`, `.claude/sections/05_integrations.md`, `.claude/skills/direct-integrations/SKILL.md` |

## Safety Boundaries

- `capabilities.py` is the action-level policy source of truth. Do not infer
  permission from OAuth token presence, wrapper shape, or registry availability.
- `registry.py` reports configured/available integrations plus declared actions;
  it does not authorize mutating calls.
- Mutating entrypoints must call `require_integration_action()` near the real
  external effect, not only in a dashboard, router, or CLI argument layer.
- Model-facing reads may use `surface="model"` only when the action exposes the
  model surface.
- External effects such as Slack send, Outlook send, Gmail archive, and Sheets
  write/append must use `operator_confirmed` or `internal` only when declared.
- Keep secrets out of docs and status output. Config hints may name expected env
  variables or token files, but never include token values, cookies, account
  IDs, raw phone/email details, or OAuth payloads.
- Shared Google OAuth is still broader than the software policy. Per-service
  token/scope segmentation remains future hardening.
- Browser or Zapier fallbacks are not canonical integration policy surfaces.

## Capability Model

`IntegrationAction` fields:

| Field | Meaning |
|---|---|
| `integration` | Normalized integration id such as `gmail`, `slack`, or `analytics`. |
| `action` | Normalized action name such as `list`, `send`, `write`, or `archive`. |
| `effect` | One of `read`, `write`, `send`, `archive`, `delete`, or `external_post`. |
| `exposures` | Allowed surfaces: `model`, `internal`, and/or `operator_confirmed`. |
| `default_enabled` | Software-policy default before any explicit override hook. |
| `required_scopes` | Scope names required by the backing provider. |
| `config_hints` | Non-secret config names that help operators diagnose setup. |
| `description` | Human-readable policy note. |

Declared mutating actions include:

| Integration | Actions | Required surface |
|---|---|---|
| Gmail | `archive` | `operator_confirmed` or `internal` |
| Asana | `complete`, `create`, `comment`, `move` | `operator_confirmed` |
| Slack | `send` | `operator_confirmed` or `internal` |
| Sheets | `write`, `append` | `operator_confirmed` |
| Outlook | `archive`, `send_email` | `archive`: `operator_confirmed` or `internal`; `send_email`: `operator_confirmed` |

Personal Gmail, Docs, Drive, Circle, Search Console, and Analytics are
read-only through the current wrapper policy.

## How To Run It

List direct wrapper commands:

```powershell
cd C:\Users\YourUser\thehomie
python .claude\skills\direct-integrations\scripts\query.py --help
```

Run framework status surfaces:

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run thehomie status --json
uv run thehomie doctor
uv run thehomie chat -q "/diagnostics" -Q
```

Use read-only direct integrations when configured:

```powershell
cd C:\Users\YourUser\thehomie
python .claude\skills\direct-integrations\scripts\query.py gmail unread
python .claude\skills\direct-integrations\scripts\query.py analytics overview
```

Use mutating commands only from explicit operator-confirmed flows:

```powershell
cd C:\Users\YourUser\thehomie
python .claude\skills\direct-integrations\scripts\query.py slack send <channel> <message>
python .claude\skills\direct-integrations\scripts\query.py sheets append <spreadsheet_id> --range "A:Z" --values '[["value"]]'
```

## How To Test It

Focused policy bundle:

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run pytest tests/test_integration_capabilities.py tests/test_prompt_builder.py tests/test_extension_manager.py tests/test_email_sanitizer.py -q
```

Smoke the chat/status surfaces:

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run python ../chat/main.py --test
uv run thehomie status --json
```

Expected proof points:

- `require_integration_action("gmail", "list", surface="model")` allows a read.
- `require_integration_action("sheets", "write", surface="model")` raises a
  deterministic `IntegrationPolicyError`.
- Slack send, Sheets write/append, Gmail archive, Outlook send/archive,
  notifications, heartbeat Slack alerts, and `/send` call the policy gate before
  constructing the external client or Graph/API request.

## Latest Live Proof

- Date: 2026-05-22
- Surface: focused Python tests, chat smoke, and framework status smoke
- Result: `97 passed`; `uv run python ../chat/main.py --test` passed;
  `uv run thehomie status --json` passed and reported the cognitive loop live.
- Limitation: full `uv run ruff check .` was attempted and still failed on
  pre-existing unrelated repo lint debt; do not claim a full-repo Ruff pass for
  this slice.

## Related Handoffs

- `PRPs/active/PRP-direct-integration-capability-contract.md`
- `PRPs/active/TRACKER.md`
- `.claude/handoffs/2026-05-22-cognitive-loop-to-direct-integration-clear.md`

## Public Export Status

Policy code is public-exported through `scripts/sanitize.py`.

- Private code commit: `83719827`
- Public framework code commit: `c1d33d5`
- Manual page status: public-exported through the `docs/manual/` sanitizer
  allowlist.

## Next Slices

- Google OAuth token/scope segmentation by service.
- Mission Control policy/status UI that reads the Python capability contract.
- Real `thehomie chat --toolsets` and a Homie-native messaging dispatcher.
- Broader mutator coverage for integrations beyond the current high-value
  wrapper/internal entrypoints.
