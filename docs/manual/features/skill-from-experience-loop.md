# Skill-From-Experience Loop

Status: Shipped, default-denied, operator-gated
Owner: `.claude/chat/cognition/` (draft, scan, usage, promotion) plus `.claude/chat/core_handlers.py` (the `/skills` operator surface)
Last updated: 2026-06-21

## What It Does

The skill-from-experience loop lets the assistant notice a tool-call workflow it
keeps repeating, draft a reusable skill from it, and — only after an operator
approves — promote that draft into a live skill the prompt can use. It is the
self-authoring half of the skills system: the assistant proposes, the operator
disposes.

Nothing the assistant drafts can change its own behavior on its own. A drafted
skill is written to a quarantined `generated/` directory, is excluded from the
prompt, and stays inert until it passes a security scan AND an operator runs
`/skills promote`. The full path is:

draft → security scan → stage (default-deny in `generated/`) → recurrence
counting → operator-gated promote → stale archive.

This is not an autonomous self-improvement loop. There is no auto-promote, no
unattended graduation, and no path by which an unscanned draft enters the prompt.

## Operator Entry Points

The `/skills` command is the operator gate. It is operator-role and handled
instantly by the router.

```text
/skills review                                  list promotion-eligible drafts + a fresh scan preview
/skills promote <name>                          promote an eligible, scan-passed draft (operator approval)
/skills promote <name> --override-caution       promote despite a `caution` scan verdict
/skills reject <name> [| reason]                archive a draft so it stops being surfaced
```

The draft NAME may contain spaces (the display name is kept verbatim in the
draft's frontmatter, and recurrence is keyed on that exact name). The parser
treats the full remainder of the line as the name:

- `promote` strips the `--override-caution` flag first; everything else is the name.
- `reject` takes an optional reason after a single `|` delimiter. With no `|`,
  the whole remainder is the name and the reason defaults to `operator_rejected`.

```text
/skills promote Daily Spend
/skills promote Daily Spend --override-caution
/skills reject Daily Spend | not worth keeping
```

`/skills review` and `/skills` with no arguments are read-only.

## Source Of Truth Files

| Concern | Files |
|---|---|
| Draft authoring + index | `.claude/chat/cognition/skills.py` (`propose_skill`, `write_skill`, `build_skill_index`, conflict detection) |
| Security scan | `.claude/chat/cognition/skill_guard.py` (`scan_skill`, `sanitize_skill_path_component`) |
| Recurrence + lifecycle state | `.claude/chat/cognition/skill_usage.py` (`record_recurrence`, `get_usage`, `mark_state`, `list_eligible`, `prune_stale`) |
| Promotion gate | `.claude/chat/cognition/skill_promotion.py` (`promote`, `reject_skill`, `archive_stale`, `list_promotable`) |
| Audit sink | `.claude/chat/skill_audit.py` (`append_skill_audit_record`) |
| Operator command | `.claude/chat/core_handlers.py` (`handle_skills`, `_SKILL_PROMOTE_STATUS_TEXT`) |
| Command registry | `.claude/chat/commands.py` (`COMMANDS`, `CATEGORIES`, `TELEGRAM_NATIVE_COMMANDS`) |
| Config knobs | `.claude/scripts/config.py` |
| Tests | `.claude/scripts/tests/test_skill_guard.py`, `test_skill_usage.py`, `test_skill_promotion.py`, `test_cognition_skills.py`, `test_skill_command_registration.py`, `test_skill_stale_seam.py` |

## Safety Model

Policy before mechanism. The loop is built so the assistant cannot grant itself a
new capability:

- **Default-deny staging.** Drafts are written under `generated/`. `build_skill_index`
  excludes anything under a `generated/` path segment, so a draft never enters the
  procedural-memory prompt region until it is promoted out of `generated/`.
- **Security-scan gate.** `promote` runs `scan_skill` and refuses on the configured
  blocking verdict (default `dangerous`). A `caution` verdict also refuses unless
  the operator passes `--override-caution`. The blocking verdict is resolved at
  call time, so it is a live knob.
- **Operator approval is mandatory.** `promote` is default-deny: the operator
  command injects approval explicitly. There is no programmatic approval path and
  no auto-promote.
- **Kill-switch.** The operator-toggleable `skill_promotion` kill-switch
  (env `HOMIE_KILLSWITCH_SKILL_PROMOTION`) can refuse all promotions; a disabled
  switch returns a refusal and writes an audit row.
- **Path-traversal guard.** Model-authored name/category are sanitized for the
  PATH (`sanitize_skill_path_component` rejects `..`, separators, absolute paths,
  dotfiles) and the resolved write directory is asserted to stay under
  `generated/`. A traversal attempt raises, and nothing is written outside
  `generated/`.
- **YAML field-injection guard.** Model-authored frontmatter VALUES
  (name/category/description) are hard-rejected if they carry a newline or other
  control character, so a crafted value cannot forge extra frontmatter keys
  before the scan gate sees the file.
- **Physical-state eligibility.** Promotion reads the physical usage sidecar and
  the file on disk, not a cached flag — an existing target directory is treated
  as derived state and re-validated before a draft is marked promoted.
- **Audit every action.** Every promote/reject/scan-preview/archive outcome
  appends a row to `DATA_DIR/skill_actions.jsonl`. Audit writes are fail-open —
  an audit failure never aborts the security decision.
- **Fire-and-forget at the cognition hooks.** Draft proposal and recurrence
  telemetry run post-response and never raise into the turn.

## How It Works

1. After a turn that used several tools, a post-response cognition hook calls
   `propose_skill`. Below the trigger threshold it does nothing.
2. If the proposal collides with an existing hand-authored skill, it is skipped.
   If it collides with an existing generated draft, that draft's recurrence count
   is incremented (the reuse signal), keyed on the matched draft's name.
3. A genuinely new proposal is written via `write_skill` into
   `generated/<category>/<name>/SKILL.md` with `generated: true` frontmatter.
   It is inert and excluded from the prompt.
4. As the same workflow recurs, the draft's recurrence count climbs. Once it
   reaches the reuse threshold its usage state becomes `eligible`.
5. The operator runs `/skills review` to see eligible drafts with a fresh scan
   verdict, then `/skills promote <name>` to graduate one. Promotion re-checks
   the kill-switch, eligibility, the file on disk, the scan verdict, and operator
   approval, then physically moves the draft out of `generated/`, flips its
   frontmatter, marks it promoted, and audits the result.
6. Drafts that never recur are archived by the scheduled stale-archive seam after
   the stale-days window, each with its own audit row.

## How To Run It

`/skills` runs from any adapter (Telegram or CLI). From the CLI:

```powershell
cd .claude/scripts
uv run thehomie chat -q "/skills review" -Q
uv run thehomie chat -q "/skills promote Daily Spend" -Q
uv run thehomie chat -q "/skills reject Daily Spend | not worth keeping" -Q
```

If a draft is not yet eligible, `promote` refuses with a friendly reason (it
needs more recurrences). If the scan returns `caution`, re-run with
`--override-caution`. If the kill-switch is disabled, promotion refuses and says
so.

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_skill_guard.py tests/test_skill_usage.py tests/test_skill_promotion.py tests/test_cognition_skills.py tests/test_skill_command_registration.py tests/test_skill_stale_seam.py -q
```

The suite covers the scan gate, the recurrence/eligibility state machine, the
default-deny promotion gate (every refusal status), the path-traversal and
YAML-injection write guards, the multi-word-name command parsing, and the
scheduled stale-archive seam.

## Config Knobs

All knobs are read at call time (env-overridable). Defaults shown.

| Env var | Default | Effect |
|---|---|---|
| `SKILL_TRIGGER_TOOLS` | `5` | Minimum tool calls in a turn before a draft is proposed. |
| `SKILL_PROMOTE_REUSE_THRESHOLD` | `3` | Recurrences a draft needs before it becomes promotion-eligible. |
| `SKILL_STALE_DAYS` | `30` | Days without recurrence before a staged draft is archived by the stale seam. |
| `SKILL_SCAN_BLOCK_VERDICT` | `dangerous` | The scan verdict that always refuses promotion. |
| `HOMIE_KILLSWITCH_SKILL_PROMOTION` | enabled | Operator kill-switch; set to a disabled value to refuse all promotions. |

## Common Failure Modes

Promote says "not eligible yet":

- The draft has not recurred enough times. It becomes eligible once its
  recurrence count reaches `SKILL_PROMOTE_REUSE_THRESHOLD`. Use `/skills review`
  to see current counts.

Promote says "the scan returned CAUTION":

- The security scan flagged the draft as `caution`. Inspect it, and if it is
  safe, re-run with `/skills promote <name> --override-caution`. A `dangerous`
  verdict cannot be overridden from the command.

Multi-word name looks truncated:

- The name is the full remainder of the line after the verb. For `reject`, put
  the reason after a `|` so it is not absorbed into the name.

Promote says "a promoted/<name> dir already exists but is empty or invalid":

- A previous promote left a partial/aborted target directory. Remove that
  directory and retry; an existing target is not treated as proof of a prior
  successful promote.

Promotion refused by the kill-switch:

- The `skill_promotion` kill-switch is disabled. Re-enable it (clear or set
  `HOMIE_KILLSWITCH_SKILL_PROMOTION` to an enabled value) and retry.

## Public Export Status

The loop ships through the normal framework export path (`scripts/sanitize.py`).
This manual page is public-safe by construction (mechanism only, generic
`.claude/...` paths, no personal data). Because `docs/` is in the sanitizer deny
list, this page exports only through an explicit per-file entry in the sanitizer
include list; never copy files between repos by hand.
