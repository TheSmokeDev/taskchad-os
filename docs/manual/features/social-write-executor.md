# Social-Write Executor

Status: Shipped, default-denied, operator-gated per action
Owner: `.claude/scripts/orchestration/` browser executor plus `.claude/chat/` approval handlers and driver
Last updated: 2026-06-15

## What It Does

The Social-Write Executor is the execution half of the Social/LinkedIn Homie.
The draft side could always reason, draft a post, and prepare an exact approval
message — but the write itself was deferred to "the approved BrowserOps social
workflow" that did not exist. This slice is that workflow.

It lets four operator commands actually drive the existing visible Chrome CDP
session to land one social write, each behind its own per-action approval gate,
with a screenshot receipt and a redacted audit row per attempt:

- `/linkedin_post` — create a LinkedIn post.
- `/linkedin_connect` — send a LinkedIn connection request (with an optional note).
- `/reddit comment` — post a comment reply on a Reddit thread.
- `/reddit post` — create a Reddit self-post in a subreddit.

It is not an autoposter. There is no schedule, no batch, no loop, no fan-out,
and no API path. One operator approval lands exactly one action on whatever
account is already logged into the visible browser. Every write stays
default-denied until the operator's verbatim message carries the exact approval
phrase as an isolated trailing segment.

## Operator Entry Points

The slash command is the approval path — there is no auto-post. Approval is a
distinct trailing pipe-delimited segment that must exactly match the phrase for
that action. The post or comment body is never the approval source.

```text
/linkedin_post <feed_url> | <body> | post this to linkedin now
/linkedin_connect <profile_url> | <note> | send this linkedin connection request now
/reddit comment <thread_url> | <body> | post this comment to reddit now
/reddit post <subreddit> | <title> | <body> | post this to reddit now
```

Without the final approval segment, each command blocks by default and returns a
preview (the redacted target plus the drafted body) telling the operator exactly
how to resend with the confirmation. The Reddit read-only path,
`/reddit research <query or r/subreddit>`, needs no approval.

Sanitizer-safe example shapes (placeholder URLs only):

```text
/linkedin_post https://www.linkedin.com/feed/ | Shipped the gated social-write executor today. | post this to linkedin now
/linkedin_connect https://www.linkedin.com/in/sample/ | Enjoyed your thread on agent memory. | send this linkedin connection request now
/reddit comment https://reddit.com/r/sample/comments/abc123/sample/ | Here is what worked for us. | post this comment to reddit now
/reddit post sample | A title | A self-post body. | post this to reddit now
```

## Source Of Truth Files

| Slice | Files |
|---|---|
| Orchestration executor | `.claude/scripts/orchestration/browser_executor.py`, `.claude/scripts/orchestration/contract.py` (`SocialWriteAction`, `SOCIAL_WRITE_FIELDS`), `.claude/scripts/orchestration/models.py` (`SocialWriteTask`) |
| Chat approval handlers | `.claude/chat/core_handlers.py` (`_handle_social_write`, `handle_linkedin_post`, `handle_linkedin_connect`, `handle_reddit`, `_split_social_args`, `_normalize_confirmation`, `_build_social_write_subtask`, `_reddit_drive_comment`, `_reddit_drive_post`) |
| Injected driver | `.claude/chat/social_write_driver.py` (`AgentBrowserSocialWriteDriver`, `append_tracker_row`) |
| Write gate | `.claude/chat/browser_workflows.py` (`require_browser_workflow_permission`, `_has_explicit_approval`, the `linkedin.*` and `reddit.*` workflow registry entries) |
| Audit | `.claude/chat/browser_audit.py` (`append_browser_audit_record` with additive `subtask_id` / `executor_name`) |
| Command registry | `.claude/chat/commands.py` (`COMMANDS`, `CATEGORIES`, `TELEGRAM_NATIVE_COMMANDS`, `CORE_INTENTS`) |
| Command docs | `.claude/commands/linkedin.md`, `.claude/commands/reddit.md` |
| Tests | `.claude/scripts/tests/test_browser_executor.py`, `.claude/scripts/tests/test_browser_workflows.py`, `.claude/scripts/tests/test_linkedin_profile_command.py`, `.claude/scripts/tests/test_reddit_handler.py` |

## Vertical Slice Architecture

Approval authority stays in the chat slice; execution stays in the orchestration
slice; the agent-browser binding stays in the chat slice and is injected into
the executor so orchestration never imports agent-browser.

| Layer | Owner | What It Owns | What It Must Not Own |
|---|---|---|---|
| Command registry | `.claude/chat/commands.py` | `/linkedin_post`, `/linkedin_connect`, `/reddit` registry entries, native menu tuple, categories, intent phrases. | Approval logic, browser navigation, or executor wiring. |
| Approval handlers | `.claude/chat/core_handlers.py` | Segment split, isolated-approval decision, CDP readiness gate, the workflow permission call, audit calls, task build, tracker append. The handler is the SOLE approval authority. | Re-evaluating approval after dispatch; passing the body into the gate. |
| Write gate | `.claude/chat/browser_workflows.py` | The default-deny `require_browser_workflow_permission` decision and the `linkedin.*` / `reddit.*` workflow registry rows. | Runtime command execution or content rendering. |
| Social-write executor | `.claude/scripts/orchestration/browser_executor.py` | `BrowserExecutor.dispatch`: parse the already-approved task, confirm readiness, drive through the injected driver, persist a screenshot path, audit every attempt. | Any approval path, any agent-browser import, any token inspection. |
| Task contract | `.claude/scripts/orchestration/contract.py` + `models.py` | `SocialWriteAction`, the `SOCIAL_WRITE_FIELDS` allowlist, and the `SocialWriteTask` dataclass that carries NO approval claim. | An `approval_token` field (deliberately absent — default-deny). |
| Injected driver | `.claude/chat/social_write_driver.py` | The concrete `SocialWriteDriver`: port resolution, physical readiness envelope, the visible-Chrome `agent-browser` drive steps, screenshot persistence, audit call. | Approval decisions for the executor. |
| Audit | `.claude/chat/browser_audit.py` | Append-only sanitized audit rows, now stamped with `subtask_id` / `executor_name`. | Cookies, tokens, raw URLs, query strings, page text. |

Rule of thumb: the chat handler decides whether a write is allowed and audits
it, the orchestration executor performs the already-approved write, and the
injected driver is the only thing that touches agent-browser.

Note on Reddit: the LinkedIn writes flow through the orchestration `BrowserExecutor`
plus the injected `AgentBrowserSocialWriteDriver`. The Reddit comment and post
writes drive directly inside the handler (`_reddit_drive_comment`,
`_reddit_drive_post`) while sharing the same approval split, gate, readiness
refusal, audit policy, and ban-safety invariant. Migrating Reddit onto the
shared executor is a stated non-blocking follow-up.

## Safety Boundaries

Policy before mechanism. These invariants are what five Opus adversarial review
passes hardened:

- **Per-action isolated approval.** Each write requires the operator's verbatim
  message to end with a DISTINCT trailing `| <approval phrase>` segment that
  exactly matches the phrase for that action. One approval lands one action.
  There is no batch, approve-all, schedule, or loop.
- **Default-deny.** Every write workflow (`linkedin.post.create`,
  `linkedin.connection.request`, `reddit.comment.create`, `reddit.post.create`)
  is registered `classification="write"`, `approval_level="explicit"` and blocks
  unless approved. A `SocialWriteTask` only exists after `decision.allowed`.
- **The body can never approve itself.** Approval is decided ONLY on the final
  pipe-delimited segment by exact equality (`_split_social_args`). A body that
  merely ends with the approval phrase — even verbatim — never approves, because
  there is no trailing confirmation segment to peel. This closes a
  body-ends-with-phrase auto-approve vector that had survived four earlier gates.
  Defense in depth: the gate is called with an EMPTY `user_text` plus the
  structurally-isolated `approved` flag, so the gate's own trailing-phrase scan
  can never even see the body.
- **The payload never reaches the gate.** `payload_text` (the post or comment
  body) is never passed to `require_browser_workflow_permission` as approval
  text and never enters the gate's `user_text`.
- **The executor has no approval path.** `BrowserExecutor` receives an
  already-allowed task, never re-evaluates approval, never inspects a token, and
  the `SocialWriteDriver` Protocol has no `gate` method. A stray
  `resolve("browser")` against the shared registry falls back to `LocalExecutor`
  and no-ops a write — the executor is constructed in-handler as a local
  variable and is never registered into `ExecutorRegistry.default()`.
- **Metadata is allowlist-filtered.** `parse_social_write_task` filters the
  decoded `Subtask.metadata` to `SOCIAL_WRITE_FIELDS`, so a tampered or
  over-broad blob cannot smuggle an approval claim or any extra field into the
  task. There is deliberately no `approval_token` field.
- **Visible-Chrome only.** The driver attaches to the existing logged-in visible
  Chrome CDP session (normally port `9222`); there is no launch, headless,
  fresh-profile, copied-profile, or stored-state path. The executor confirms the
  physical `browser_readiness` envelope (Rule 2 — physical state, not a cached
  "up" flag) and refuses plus audits `failed` when not ready, without driving.
- **Reddit URL and subreddit validation.** The Reddit thread URL must be an
  absolute `https://reddit.com` URL and the subreddit must match
  `^[A-Za-z0-9_]{2,21}$` (no slashes, path, or query) before driving, or the
  command is rejected and audited.
- **Audit every attempt, redacted.** Blocked, posted, failed, and rejected
  outcomes all write an append-only sanitized audit row. URLs are redacted; no
  cookies, tokens, or query strings are logged.
- **Screenshots are PII-bearing.** The receipt carries a local file PATH only,
  never the bytes, a URL, or page text. The PNG persists to
  `DATA_DIR/browser_writes/`, which is git-ignored and inside the sanitizer
  `DENY_DIR` `.claude/data/`.
- **Rules 1/2/3.** Config values resolve at call time, never in default args
  (Rule 1 — `DATA_DIR`, `MEMORY_DIR`, screenshot dir). Guards read the physical
  readiness backend, not a cache (Rule 2). The driver's optional-provider and
  config reads go through call-time resolution.

## How It Works

The runtime write flow for an approved LinkedIn write (Reddit follows the same
approval and audit shape but drives directly in the handler):

1. The operator sends `/linkedin_post <feed_url> | <body> | post this to linkedin now`.
   `_handle_social_write` runs `_split_social_args(raw, approval, body_segments=2)`,
   which peels ONLY the final pipe segment, exact-matches it to the approval
   phrase, and returns `(target_url, body)` plus an isolated `approved` flag. The
   body keeps any literal pipes it contained.
2. The handler resolves the CDP port from the env-name chain and reads the
   physical `browser_readiness(port=port)` envelope.
3. The handler calls `require_browser_workflow_permission(workflow_id, "", approved=approved, target_url=...)`
   — an EMPTY `user_text` plus the structurally-isolated `approved` flag — and
   writes an audit row with the decision outcome and reason. If blocked, it
   returns the formatted block plus a preview and stops.
4. On allow (and only with a non-empty target and body), the handler builds a
   `SocialWriteTask` (carrying no approval claim), serializes it into a
   `Subtask.metadata` JSON envelope, and constructs a local `BrowserExecutor`
   wrapping an `AgentBrowserSocialWriteDriver`.
   **Execution model (2026-07-13):** the dispatch runs OFF the chat event loop
   — `asyncio.to_thread` around `_dispatch_social_write_locked`, which holds
   the cross-process `shared.browser_write_lock` for the whole drive (one
   visible-Chrome session; the Browser Homie runner, cadence cron, and
   per-action writes must never drive it concurrently). The chat reply is
   bounded (`_BROWSER_WRITE_REPLY_TIMEOUT_S`, 300s): on timeout the drive
   finishes in the background and the operator is told to verify on the site
   before re-firing. Reddit's comment/post drives use the same to_thread +
   lock + bounded-reply shape. Queue-backed posts (`/social post`, approve
   taps) go further: they execute in a separate detached process entirely —
   see `docs/manual/features/social-post-pipeline.md` → Browser Homie Runner.
5. `BrowserExecutor.dispatch` parses the task off `Subtask.metadata` through the
   `SOCIAL_WRITE_FIELDS` allowlist, then re-checks the physical readiness
   envelope. If not ready, it audits `failed` and returns a failed receipt
   without driving.
6. The executor calls the injected driver's `drive(task, port=port)`, which runs
   the sequential `agent-browser --cdp` steps on the visible browser. The LinkedIn
   feed-post composer resists naive automation — the editor sits behind a
   frame/shadow boundary, ignores synthetic paste events, and hydrates a few
   seconds after an empty shell opens — so the proven post drive is: open the feed
   in a FRESH tab (a reused tab can carry an injected overlay that blocks the
   composer), locate "Start a post" from a `snapshot` and click it BY REF, poll
   until the editor hydrates, focus the editor by ref and type the body LINE BY
   LINE (`keyboard inserttext` per line plus a top-level `press Enter` between
   lines — a single multi-line insert truncates at the first newline through the
   shell), then deep-element-find the enabled "Post" button and click it,
   confirming via the success toast. The connect drive opens the profile,
   "Connect", optional "Add a note" plus note fill, "Send". A drive exception is
   caught, audited `failed`, and returned as a failed receipt — it never crashes
   dispatch.
7. On success, if `post_action_snapshot` is set the driver persists a screenshot
   to the git-ignored path and returns it. The executor audits the
   `succeeded`/`failed` outcome (stamped with `subtask_id` and `executor_name`)
   and returns an `ExecutorReceipt` whose metadata carries the screenshot path
   only. The handler appends one redacted row to the outreach tracker on
   completion (fail-open — a tracker write never fails a landed write).

## How To Run It

Confirm the existing visible Chrome CDP session is reachable first, exactly as
for BrowserOps:

```powershell
agent-browser --cdp 9222 stream status
```

Then drive an approved write from chat or CLI (placeholder URLs):

```powershell
cd .claude/scripts
uv run thehomie chat -q "/reddit research agent memory" -Q
uv run thehomie chat -q "/linkedin_post https://www.linkedin.com/feed/ | A short update. | post this to linkedin now" -Q
uv run thehomie chat -q "/reddit comment https://reddit.com/r/sample/comments/abc123/sample/ | A helpful reply. | post this comment to reddit now" -Q
```

Without the trailing approval segment the same command blocks and returns the
draft preview plus the exact resend instruction. If CDP is unreachable the
handler refuses cleanly and audits `failed`; it never spawns a headless or
fresh-profile fallback.

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_browser_executor.py tests/test_browser_workflows.py tests/test_linkedin_profile_command.py tests/test_reddit_handler.py -q
```

51 tests pass (8 executor, 17 workflow gate, 14 LinkedIn command, 12 Reddit
handler). They prove the no-approval-token contract and metadata allowlist
round-trip, the executor's readiness refusal and drive-failure receipts, the
executor-never-gates invariant even when the body contains the phrase, the
registry fallback to `LocalExecutor`, the trailing-only approval (body-ends-with
and mid-body phrases stay blocked for both platforms), the isolated-confirmation
allow path, literal-pipe-in-body preservation, the Reddit URL/subreddit
injection rejections, and the readiness refusals.

## Latest Live Proof

- Commit `63f28827` — "feat(social-write): gated browser-write executor for
  LinkedIn + Reddit" on master.
- Built via CLUTCH behind five Opus 4.8 adversarial gates
  (R1 -> revise -> R2 -> build -> validator -> post-build -> Reddit pass -> fix
  -> re-review). The gates caught a real ban-safety bug — a post body ending in
  the approval phrase could self-approve — that had survived four earlier passes,
  and the fix (isolated trailing-segment exact match) is now the load-bearing
  invariant.
- 51 new tests green.
- The LinkedIn post drive was verified live during a supervised first run
  (2026-06-23): the rewritten `_drive_post` published real feed posts end to end
  (open the composer, type the body with line breaks, click Post, confirm the
  toast). The visible-Chrome composer method in How It Works step 6 is the
  load-bearing technique. The Reddit submit selectors remain deferred to a
  supervised Reddit run.

## Common Failure Modes

Blocked without approval:

- The command had no trailing `| <approval phrase>` segment, or the segment did
  not exactly match. This is the default-deny design. Resend with the exact
  phrase as the final pipe segment. The block response shows the required phrase.

Body ends with the approval phrase but still blocked:

- This is intentional. Approval is decided only on the isolated final segment; a
  body that ends with the phrase has no trailing confirmation segment to peel, so
  it never approves. Add the phrase as its own `| <phrase>` segment.

Visible Chrome not ready:

- Both the handler and the executor confirm the physical `browser_readiness`
  envelope and refuse plus audit `failed` without driving. Start the visible
  Chrome with the debug port (normally `9222`) and retry. On Chrome 136+, verify
  it was started with a non-default `--user-data-dir`.

Reddit URL or subreddit rejected:

- The thread URL was not an absolute `https://reddit.com` URL, or the subreddit
  did not match `^[A-Za-z0-9_]{2,21}$`. Fix the target and resend.

LinkedIn post lands empty / composer never opens:

- The composer editor is behind a frame/shadow boundary, ignores the synthetic
  ClipboardEvent paste, and hydrates a few seconds after an empty shell. The
  proven drive (How It Works step 6) opens from a FRESH tab, locates elements by
  `snapshot` ref, focuses the editor by ref, types LINE BY LINE via
  `keyboard inserttext` plus a top-level `press Enter`, and clicks Post via a deep
  element-find. Two infra prerequisites: pre-warm the agent-browser daemon from a
  separate process (a daemon spawned by the posting subprocess inherits its stdout
  pipe and hangs the caller), and decode agent-browser CLI output as utf-8 (a
  default Windows codec can crash on snapshot output).

## File Ownership Map

| File | Responsibility |
|---|---|
| `.claude/scripts/orchestration/browser_executor.py` | `BrowserExecutor` and the `SocialWriteDriver` Protocol: parse the approved task, confirm readiness, drive via the injected driver, persist a screenshot path, audit every attempt. No approval path, no agent-browser import. |
| `.claude/scripts/orchestration/contract.py` | `SocialWriteAction` literal and the `SOCIAL_WRITE_FIELDS` allowlist (no `approval_token`). |
| `.claude/scripts/orchestration/models.py` | The `SocialWriteTask` dataclass carried in `Subtask.metadata`; carries no approval claim. |
| `.claude/chat/core_handlers.py` | The sole approval authority: `_handle_social_write`, the LinkedIn post/connect handlers, the Reddit handler and direct drives, the segment split, and the subtask builder. |
| `.claude/chat/social_write_driver.py` | `AgentBrowserSocialWriteDriver` (the injected concrete driver) and `append_tracker_row`. Visible-Chrome only; screenshot path only. |
| `.claude/chat/browser_workflows.py` | The default-deny gate and the `linkedin.*` / `reddit.*` write workflow registry rows. |
| `.claude/chat/browser_audit.py` | Append-only sanitized audit rows with additive `subtask_id` / `executor_name`. |
| `.claude/chat/commands.py` | `/linkedin_post`, `/linkedin_connect`, `/reddit` registry, native menu, categories, and intent phrases. |
| `.claude/commands/linkedin.md`, `.claude/commands/reddit.md` | Operator command docs and the draft -> approve -> write flow. |
| `.claude/scripts/tests/test_browser_executor.py`, `tests/test_browser_workflows.py`, `tests/test_linkedin_profile_command.py`, `tests/test_reddit_handler.py` | The 51-test suite. |
| `docs/manual/features/social-write-executor.md` | This manual page. Update it when social-write behavior changes. |
| `docs/social-write-executor-manual.md` | The deep operating contract. |

## Public Export Status

The slice was committed to the private `thehomie` workspace as `63f28827`.
The executor, contract, gate, audit, driver, and command code ship through the
normal framework export path (`scripts/sanitize.py`).

This feature page and the deep manual are public-safe by construction (mechanism
only, placeholder URLs, no personal data). Because `docs/` is in the sanitizer
`DENY_DIRS`, each public doc ships only through an explicit per-file lift in the
sanitizer `INCLUDE_FILES` list — these two pages must be added there for them to
export. Export still goes only through `scripts/sanitize.py`; never copy files
between repos by hand.

## Next Slices

Non-blocking follow-ups (from the commit and review artifacts):

- A per-window write-rate cap (ban safety beyond per-action approval).
- An empty-body post guard.
- Delete the two orphaned helpers (`_strip_phrase`, `driver.gate`) once the
  approval split is the single owner.
- Migrate the Reddit comment/post writes onto the shared `BrowserExecutor` so
  both platforms run one execution path.
- Reddit submit-selector verification (LinkedIn post selectors verified live 2026-06-23).
- A visible-Chrome session keeper: an idempotent, health-gated supervisor that
  relaunches the logged-in CDP browser ONLY when the debug port is dead (so it
  never stacks instances) and re-warms the agent-browser daemon, so scheduled or
  approved writes always have a live session to attach to.

Phase 2 candidates: outreach-tracker-memory injection into drafting, an
autonomous scheduler, and Telegram delivery of receipts.

Intentional non-goals: bulk or unattended posting, approve-all, scheduling or
looping writes, API-based posting, and any cookie/token/profile handling.
