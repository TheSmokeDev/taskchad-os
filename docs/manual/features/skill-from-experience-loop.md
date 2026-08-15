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
/skills link <url | path>                       linked-skill intake — ingest, scan, promote, and add
                                                the result to THIS channel's homie (operator only)
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
| Recurrence + lifecycle state + persona scope | `.claude/chat/cognition/skill_usage.py` (`record_recurrence`, `get_usage`, `mark_state`, `list_eligible`, `prune_stale`, `record_persona_assignment`, `remove_persona_assignment`, `mark_scope_unrestricted`) |
| Promotion gate | `.claude/chat/cognition/skill_promotion.py` (`promote`, `rollback_promotion`, `reject_skill`, `archive_stale`, `list_promotable`, `resolve_promoted_skill`) |
| Linked-skill intake | `.claude/chat/cognition/skill_intake.py` (`intake_linked_skill`) |
| Persona install + ledger | `.claude/scripts/personas/skill_assignment.py` (`assign_skill_to_persona`, `installed_skill_names`) |
| Outbound fetch guard | `.claude/scripts/security/ssrf.py` (`resolve_pinned_target`, `assert_public_https_url`) |
| Audit sink | `.claude/chat/skill_audit.py` (`append_skill_audit_record`) |
| Operator command | `.claude/chat/core_handlers.py` (`handle_skills`, `resolve_requesting_persona`, `_SKILL_PROMOTE_STATUS_TEXT`) |
| Role at ingress | `.claude/chat/models.py` (`IncomingMessage.user_role`, `resolve_ingress_role`) — every adapter stamps it |
| Command registry | `.claude/chat/commands.py` (`COMMANDS`, `CATEGORIES`, `TELEGRAM_NATIVE_COMMANDS`) |
| Config knobs | `.claude/scripts/config.py` |
| Tests | `.claude/scripts/tests/test_skill_guard.py`, `test_skill_usage.py`, `test_skill_promotion.py`, `test_cognition_skills.py`, `test_skill_command_registration.py`, `test_skill_stale_seam.py`, `test_skill_intake.py`, `test_skill_learn.py`, `test_persona_skill_assignment.py`, `test_security_ssrf.py` |

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

## Linked-Skill Intake (`/skills link`)

The loop above starts with the assistant noticing a repeated workflow. Intake
starts with the operator pointing at one: drop a skill's URL or local path in a
persona's channel and it becomes something THAT persona can use, in one turn.

It adds no new lifecycle. `/skills link <source>` runs the existing rails in
order and then does the one thing they had no notion of — putting the result in
front of a single homie:

    role gate -> `/learn` ingest (draft, inert in `generated/`)
              -> security scan + promote gate (unchanged)
              -> install into that persona's own skills directory

What is load-bearing about it:

- **The scan still decides.** The promote gate re-scans the physical file and
  refuses the blocking verdict. Intake passes no override and exposes no bypass
  flag — a scan failure comes back as a refusal that NAMES the verdict and the
  findings. The draft stays under `generated/`, which every skill index excludes
  by path segment, so a refused skill has reached nobody's surface. If the
  operator inspects it and judges a `caution` safe, the explicit two-step
  (`/skills review` then `/skills promote <name> --override-caution`) is still
  there; that is the operator's own decision on the existing surface, not a
  bypass wired into intake.
- **Operator only, stamped at ingress.** The role is taken from
  `IncomingMessage.user_role`, which each adapter sets when it admits the
  message — the canonical role-ingress seam. Remote adapters (Telegram,
  Discord, Slack, WhatsApp, webhook) resolve it through
  `models.resolve_ingress_role()` against their OWN configured allowlist, the
  CLI stamps its operator constant, and Buzz resolves signed pubkeys. The field
  **defaults to `viewer`**, so an ingress path that forgets to stamp a role
  gets the least privilege rather than the operator's. Intake checks that
  stamped value and requires `admin`; it does NOT re-derive the role, because a
  second, command-local check would only know about the platforms it was
  written for (that is what the earlier draft did, and it was blind to
  Slack/WhatsApp/webhook). A non-operator's link is refused BEFORE ingest —
  never fetched, never distilled, never written.
- **One persona, not the org — and the restriction is recorded first.** The
  install target is the persona bound to the channel the command was typed in,
  falling back to the active profile. A slash command is dispatched by the main
  chat process, so keying off the ambient profile would put every linked skill
  in the main homie regardless of where it was asked for. Assignment writes into
  `<profile>/skills/`, the extra directory only that persona's runtime indexes.
  The scan/promote gate still moves the vetted skill into the SHARED central
  `promoted/` tree, and the `default` profile reads that tree with an
  unrestricted allowlist — so the scope ("this one is for sales") is committed
  to the usage sidecar BEFORE the move, never after it. Nothing is reachable
  while its scope is unrecorded, and if the scope cannot be recorded the whole
  intake refuses without publishing anything. Org-wide assignment stays a
  separate, explicit operator choice.
- **A refusal means nothing shipped.** If anything after the promote fails —
  the `persona_mutation` kill-switch, a typo'd persona, a lock timeout, an
  `OSError` — the promotion is rolled back (`rollback_promotion`): the artifact
  returns to `generated/`, the scope this turn recorded is dropped, and the
  draft is left in a state a retry can promote from. In the rare case the
  rollback itself fails, the refusal SAYS the skill is live centrally and names
  the directory to remove, rather than reporting a clean "nothing happened".
- **The fetch is guarded, not just the caller.** A URL intake is an outbound
  request made with this process's network identity, so `security.ssrf` refuses
  non-`https`, credentialed URLs, and any host that resolves (on ANY of its
  addresses) into private/loopback/link-local/reserved space — then connects to
  the address it validated, carrying the hostname in `Host` and TLS SNI. Every
  redirect hop is re-validated the same way. Pinning is the load-bearing half:
  handing the client a hostname lets it resolve again at connect time, which is
  the DNS-rebinding window a pre-fetch check alone cannot close.
- **Reach is not action.** An installed skill widens what a persona can reach.
  Every per-tool default-deny gate (social writes, sends, spends, browser
  writes, integration actions) applies to it unchanged.

Refusals and outcomes both append to an operator-turn ledger in the target
persona's data directory (`persona_skill_assignments.jsonl`), carrying who,
what, when, the triggering turn's text, and the channel — so one grep answers
"what was linked at this homie, by whom, and what was refused". The install
honors the `persona_mutation` kill-switch; a disabled switch refuses after a
clean scan and audits the refusal.

Audit posture, stated honestly (#429 codex R5): the identity/kill-switch
refusals in the decision seam are STRICT (a failure to write the row fails the
operation), while the intake-side receipt writes are BEST-EFFORT by design —
an audit failure never blocks the refusal answer the operator is already
looking at, and a fully unwritable ledger means no row exists. If you ever
need a guaranteed record, the ledger directory must be writable; that is the
contract, and this paragraph is the only place it is promised.

Linking the SAME skill at a second persona works: the promote gate reads a usage
sidecar that says `promoted` forever after the first run, so intake re-decides
that one status against the filesystem (`resolve_promoted_skill`). Scan verdicts
are never re-decided that way.

**Who sees a centrally-promoted skill.** The scope lives on the usage row as
`assigned_personas` and gates exactly one reader: the `default` profile's
unrestricted central scan. It is read fail-CLOSED — a skill under `promoted/`
is indexed there only when the row positively permits it (it names `default`,
or carries the `*` sentinel a global `/skills promote` stamps). A promoted
skill with no row at all, or a sidecar that cannot be read this turn, is
hidden rather than shown. Two things are deliberately unaffected: a persona's
own installed copy (its `extra_skill_dirs` scan is scoped by construction) and
hand-authored central skills, which never went through the promotion gate and
so carry no scope to check. Rows that predate the sentinel have an empty scope
and still read as unrestricted, so upgrading does not make existing promoted
skills disappear.

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
uv run pytest tests/test_skill_guard.py tests/test_skill_usage.py tests/test_skill_promotion.py tests/test_cognition_skills.py tests/test_skill_command_registration.py tests/test_skill_stale_seam.py tests/test_skill_intake.py tests/test_skill_learn.py tests/test_persona_skill_assignment.py tests/test_security_ssrf.py -q
```

The suite covers the scan gate, the recurrence/eligibility state machine, the
default-deny promotion gate (every refusal status), the path-traversal and
YAML-injection write guards, the multi-word-name command parsing, and the
scheduled stale-archive seam. For linked-skill intake it also covers the
pre-ingest role gate (proven by ingest never running for a non-operator), both
scan-failure paths leaving the persona's surface empty, and the per-persona
scoping — asserted through the REAL `build_skill_index` with the same argument
shape the persona runtimes use, including the unrestricted (`allowlist=None`)
scan the `default` profile actually performs.

Three properties are pinned by tests that fail without their fix: a refused
intake leaves NOTHING in the central `promoted/` tree or in the unrestricted
index (the write-ordering + rollback), a scope that cannot be recorded refuses
before publishing anything, and an unreadable scope sidecar HIDES promoted
skills instead of exposing them. The fetch guard is proven against a rebinding
resolver — public on the first lookup, private afterwards — by asserting the
request is addressed to the validated IP with the hostname only in `Host`/SNI.

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
