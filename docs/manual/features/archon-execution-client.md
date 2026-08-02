# Archon Execution Client

Status: shipped — transport client + loopback posture probe
Owner: Integrations slice
Last updated: 2026-07-27

## What It Does

`.claude/scripts/integrations/archon_client.py` is the single module in the
framework that speaks HTTP to Archon. It gives the runtime four capabilities:

| Capability | Function | Archon endpoint |
|---|---|---|
| Deploy work | `dispatch_workflow(codebase_id, workflow, text)` | `POST /api/conversations` |
| Deploy a raw orchestrator message | `create_conversation_and_dispatch(codebase_id, message)` | `POST /api/conversations` |
| Steer by natural language | `send_message(conversation_id, text)` | `POST /api/conversations/{id}/message` |
| Read a run + its full event log | `get_run(run_id)` | `GET /api/workflows/runs/{runId}` |
| List recent runs | `list_runs(limit, ...)` | `GET /api/workflows/runs` |
| Steer by gate | `steer(run_id, action, note=…)` | `POST /api/workflows/runs/{runId}/{action}` |
| Check network exposure | `check_loopback_posture()` | none — raw TCP probe |

Everything is `async` except the posture probe. Errors are typed and carry a
`friendly_message` an operator surface can speak verbatim.

## The Rule That Matters Most

**There is no raw workflow-run path in Archon, so never build one.**

Archon's `POST /api/workflows/{name}/run` does not start a run directly. It
builds the string `/workflow run <name> <message>` and hands it to
`handleMessage()` — the same single funnel every adapter (Telegram, Slack,
Discord, webhooks, CLI, web) goes through. Reaching that funnel is what buys the
orchestrator's pre-flight:

- requirement gates evaluated **before** any spend
- conversation → codebase auto-binding
- isolation resolution, including stale-environment recovery and
  merged-worktree cleanup
- resume-before-fresh with a compare-and-swap, so two resumers cannot corrupt a
  shared worktree

A client that POSTed a run directly would skip all of it. That is why
`dispatch_workflow()` exists and why `build_workflow_message()` is a function
rather than a comment: the message format lives in exactly one place. A caller
that hand-assembles a near-miss string does not get an error — the orchestrator
just treats it as ordinary chat and no workflow ever runs.

## Correlating A Dispatch To Its Run

This is the trap. `ArchonDispatch` hands back two different ids:

- `conversation_id` — the **platform** id (`web-…`). Use it for
  `send_message()` and for anything under `/api/conversations/{id}/…`.
- `conversation_db_id` — the **database row** id.

For a web-dispatched workflow Archon spawns a separate *worker* conversation and
sets the run's `conversation_id` to that worker, putting yours in
`parent_conversation_id`. The `list_runs(conversation_id=…)` filter is a plain
equality match on the run's own column, so filtering by the id you were handed
matches **nothing** — and an empty list reads exactly like "the run has not
started yet."

To find the run you dispatched: list unfiltered and match
`run.parent_conversation_id == dispatch.conversation_db_id`.

## Steering

Two primitives, both Archon's own — the framework invents neither.

1. **Natural language.** When a run is paused, any non-slash message on its
   conversation becomes the approval. `send_message(conv_id, "looks good, ship
   it")` resumes a paused DAG. This is the voice-native path.
2. **Explicit gates.** `steer(run_id, action)` where action is one of
   `approve`, `reject`, `resume`, `abandon`, `cancel`.

`approve` and `reject` accept an operator `note` (sent as `comment` and `reason`
respectively — Archon uses different body keys for the two). The other three
actions have no body field to carry a note, so passing one raises rather than
silently dropping the operator's stated reason.

Steering only reaches workflows that have authored pause points. A workflow with
no `approval` node and no `loop: interactive: true` gate never pauses, so there
is nothing to approve.

## The Steering Surface (`manage_run`)

`archon_client` is transport. The operator-facing half is one voice tool,
`manage_run`, mirroring Archon's own `manage-run-tool.ts` action discriminator:

| Action | What it does |
|---|---|
| `help` | What the tool can reach, spoken. |
| `list` | Runs from the ledger, **paused first** — that is the set waiting on him. |
| `get` | The node a run is on plus its last few tool calls. |
| `say` | Sends the operator's own words to the run's conversation. |
| `approve` / `reject` | Answers a gate. |
| `resume` | Restarts a stopped run. |
| `cancel` / `abandon` | Stops one. |

`reject`, `cancel` and `abandon` destroy work in flight, so they **preview
first**: called without `confirm`, the tool reads the run's real state and
returns what would happen; it acts only when called again with `confirm: true`.
That is Archon's own two-step, and it matches the framework's announce-then-act
voice contract. `approve` and `resume` need no confirm — answering a gate the
operator was just told about is the answer, not a second decision.

### An approve is not a boolean

The one piece of competence a naive steering client gets wrong. Archon records
the approve **comment** as the gate node's captured output, and a bare approve
defaults it to the literal `"Approved"` — which fails every deterministic
`<gate>-check` node that greps for `APPROVE SPEND` / `APPROVE DEPLOY`
(`docs/manual/features/archon-steering-gates.md`). The surface would fail on
exactly the gates it exists to answer.

So `steer_now` reads the phrase **this** gate demands from the ledger at act
time and sends it as the comment. Two rules ride on that sentence:

- **At act time, not render time** (Rule 2). A gate can re-ask with different
  copy between the pause and the answer, so the phrase is resolved from physical
  ledger state when the answer is sent.
- **Through the uncapped reader.** `read_recent_events` is a DISPLAY reader that
  caps every value at 800 characters; real gate messages run 2,000–28,000 chars
  with the phrase at the END, so reading it there returns `""` silently.
  `archon_events.read_gate_data_raw` is the control-plane read — no cap, and its
  result never reaches the wire.

A gate with **no** phrase check still gets a bare approve, deliberately: an
`interactive_loop` gate reads any non-empty comment as FEEDBACK and iterates
instead of finalizing, so sending a phrase nobody asked for would silently
change what approving means.

The gate copy is workflow-authored and carries substituted node output, so
`extract_required_phrase` only asks WHICH of the framework's own constants the
message names and returns that constant. Nothing from the message reaches
Archon — an agent cannot smuggle a comment through the gate copy.

### "No" approves

`say` is the voice-native path and carries a sharp edge the tool states out
loud: a conversation reply on a paused run **can only approve**. "No, don't" is
a non-slash message, so it resumes the DAG. Only `reject` refuses a gate. This
is why every spend gate pairs with a deterministic `-check` node.

### Naming a run out loud

An Archon run id is a 32-character hex string. `resolve_archon_run` accepts
three things: nothing at all (the single paused run, else the single active
run — two candidates is an ambiguity it names rather than a coin flip), a
`WORK_STARTED` receipt number, or a real run id.

### Narration

`check_work` answers "how's it going?" with the node the run is on and its last
few tool calls, not just "still running", and leads with anything paused. It is
**on request only** — no firehose. The tool-call text comes through the *capped*
reader on purpose: `tool_input` is LLM-authored and this text is spoken back.

### Boundaries

- `HOMIE_KILLSWITCH_ARCHON_STEER` (ships ON) and the declared `archon.steer`
  capability action gate the whole surface. Same blast radius as the dispatch it
  corrects: local run state, nothing outward.
- Every attempt writes **two** append-only rows to `DATA_DIR/archon_steer.jsonl`
  — attempt, then result — on all four paths. Success is never encoded as the
  absence of a row. A steer whose attempt row cannot be written is refused.
- `steer_now` and `say_now` are BLOCKING and raise if they find a running event
  loop, same structural guard as `dispatch_now`.
- An Archon 200 with `accepted: false` is not a delivered message, and is
  reported as such rather than as success.

## Deploying Work Through The Gate

The client is transport. Everything that decides *whether* a workflow is allowed
to spend a worktree lives one layer up, in `.claude/scripts/talk_archon.py`, and
`run_archon` / a `scope: substantial` `delegate_task` both go through it.

Four gates, in authority order. Each one writes its own append-only audit row to
`DATA_DIR/archon_dispatch.jsonl` before it refuses, and a granted dispatch writes
one too — so the trail answers "what did the Homie try to deploy" whether or not
it was allowed to.

1. **Kill switch** — `HOMIE_KILLSWITCH_ARCHON_DISPATCH`. Ships ON; setting it to
   `disabled` is the only way to turn the surface off, and the refusal is counted
   in `/api/health` like every other switch.
2. **Capability policy** — the declared `archon.dispatch` action is `write` and
   is **model-initiable on purpose**: a dispatch is bounded to a clone, a
   worktree, and tokens, which is the operator's free tier, and this lane
   cannot prove a spoken yes anyway (see Boundaries). Real spend and
   production deploys are gated where they happen, not here. Accountability
   for a dispatch is the append-only audit row plus the kill switch above.
3. **The brief contract (F2)** — see below.
4. **Codebase binding** — a dispatch with no resolvable codebase gets no
   isolation pre-flight and lands in the wrong tree, so it is refused, not
   guessed.

Everything above runs **synchronously**, so a refusal is spoken on the same turn.
What is left — the HTTP dispatch, the ledger writes, the status poll — is
detached into a worker thread, and `dispatch_now()` **raises if it finds a
running event loop**. That is the 2026-07-13 wedge class made structural: a long
external call on the bot's loop freezes Telegram, Discord, `/health` and the
liveness supervisor at once.

## The Brief Must Stand On Its Own (F2)

Archon does `workflowPrompt = synthesizedPrompt ?? originalMessage`
(`orchestrator-agent.ts:1953`). A voice turn is exactly where that breaks: the
operator says "yeah, do that", and the worker — which starts in a **fresh
worktree with no access to the conversation** — receives those four words as its
entire task.

So the Homie synthesizes the brief before dispatch, and the obligation is
enforced in three places rather than trusted once: the Talk voice preamble, the
`run_archon` / `delegate_task` tool descriptions (the descriptions ARE the prompt
surface on the Realtime lane), and a deterministic check that actually refuses.

`brief_refusal_reason()` strips referential pointers ("do that", "what we
discussed", "the usual", "same as before") and then requires what remains to
clear both a character floor and a content-word floor. A brief made only of
pointers scores zero real words no matter how long it is. The check is
deliberately deterministic — an LLM asked "is this brief good enough?" would be
the same LLM that just wrote the vague one.

A refused brief is spoken back with instructions to restate the task, and the
vague string never reaches `dispatch_workflow()`.

## The Correlation Key

One dispatch produces three ids that have to stay joined: the Homie's run id,
Archon's conversation DB id, and Archon's platform conversation id. They land in
two places — the in-memory `talk_runs` entry, and (durably) the convoy subtask's
`paperclip_issue_id` external ref:

```
talk:<run_id>:archon:<conversation_db_id>:conv:<platform_conversation_id>
```

The `talk:<run_id>` prefix is unchanged from before this ticket, so existing
consumers keep matching; the Archon ids are appended. `parse_correlation_ref()`
reads both the new form and the legacy `talk:<run_id>` rows still in the ledger.

Both Archon ids are carried because they answer different questions:
`conversation_db_id` is the **join key** (a web dispatch leaves it in the run's
`parent_conversation_id`), and `conversation_id` is the only id
`POST /api/conversations/{id}/message` accepts — which is what natural-language
steering needs.

The ref must be written when the subtask is dispatched: `paperclip_issue_id` is
not in `UPDATABLE_SUBTASK_FIELDS`, so it cannot be patched on afterwards.

## Codebase Binding

Resolution order, first hit wins:

1. `ARCHON_CODEBASE_ID` — the operator's explicit binding.
2. a registered `remote_agent_codebases` row whose `default_cwd` **is** this repo
   root.
3. the same, for the **main checkout** this worktree points at. In a git worktree
   `.git` is a file holding `gitdir: <main>/.git/worktrees/<name>`; Archon
   registers the main checkout, so without this rung every dispatch from a
   worktree would refuse. Reading the pointer is pure file IO — no subprocess on
   a voice path.

Nothing resolved → refusal that names `ARCHON_CODEBASE_ID` and lists what Archon
actually has registered. This reads physical state (the ro ledger + the real
paths on disk), not a config claim about which repo this is.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `ARCHON_API_BASE_URL` | `http://127.0.0.1:3090` | Resolved at call time. A value that is not an http(s) URL is ignored with a stderr receipt. |
| `ARCHON_API_TIMEOUT_S` | `15.0` | Per-request. Non-numeric or non-positive values fall back with a receipt. |
| `ARCHON_CODEBASE_ID` | *(resolved from the ledger)* | Explicit codebase binding; skips path resolution entirely. |
| `HOMIE_KILLSWITCH_ARCHON_DISPATCH` | *(on)* | Set to `disabled` to refuse every dispatch, counted. |
| `TALK_ARCHON_MIN_BRIEF_CHARS` | `40` | F2 character floor. Garbage values fall back with a receipt. |
| `TALK_ARCHON_MIN_BRIEF_WORDS` | `6` | F2 content-word floor, counted AFTER referential pointers are stripped. |
| `TALK_ARCHON_DEFAULT_WORKFLOW` | `archon-ralph-dag` | Workflow used by `delegate_task` with `scope: substantial`. |
| `TALK_ARCHON_POLL_S` | `15` | Status-poll cadence in the detached worker. |
| `TALK_ARCHON_BUDGET_S` | `10800` | How long the worker watches before handing off to `check_work`. |
| `HOMIE_KILLSWITCH_ARCHON_STEER` | *(on)* | Set to `disabled` to refuse every steer, counted. Separate from the dispatch switch on purpose: stopping new deploys should not cost the ability to cancel work already in flight. |

A short timeout is safe for dispatch: Archon's conversation lock stores the
handler promise and returns a status without awaiting it, so the HTTP response
comes back immediately even for a run that will take a minute.

## Loopback Pinning (Security)

Archon binds `process.env.HOST || '0.0.0.0'` and its auth resolver falls through
to *undefined* — on a default install the API is **unauthenticated and reachable
from the whole network**. Anything that can route to the port can list runs, read
prompt text, and approve or cancel work.

Pin it by setting `HOST=127.0.0.1` in Archon's repo-scope env file,
`<archon-repo>/.archon/.env`. That file is loaded with `override: true` and wins
over both the user-scope `~/.archon/.env` and anything the process manager
inherited from the shell, and it applies only when the process cwd is that repo
— so Archon CLI runs from other repositories are unaffected.

**The pin takes effect on the next Archon restart.** Verify afterwards:

```bash
cd .claude/scripts && uv run python -m integrations.archon_client posture
```

Exit code 0 means pinned AND verified. Any nonzero exit means the pin is NOT
proven — either a LAN address answered (exposed) or this host had no
non-loopback address to probe (untestable); the probe fails closed rather than
certifying a pin nobody checked, so read the printed summary line to tell
which one happened. The probe reads **physical state** — it enumerates this
host's non-loopback addresses and actually tries to open a TCP connection to
each one. A pin that was configured but never took effect (server not
restarted, precedence lost) fails this check, which is the entire point of
probing rather than reading the config back.

Same rule family as the Docker public-box port hardening: a host firewall is not
a substitute, and "it's only on my LAN" is not a boundary once a mesh VPN is
attached to the same interface list.

**If you want remote console access**, do not skip the pin — set `HOST` to the
mesh-VPN address instead. That keeps the physical LAN and any hypervisor
vSwitch interfaces closed while leaving the tunnel open.

## Boundaries

- `archon_client.py` is transport only. The dispatch capability gate, the audit
  row, prompt synthesis and the correlation key live in `talk_archon.py`; the
  tool surface and the run lifecycle live in `talk_tools.py`.
- **A dispatch needs no confirmation, on purpose.** The operator's rule is
  tier-by-BLAST-RADIUS: work whose worst case is a worktree and some tokens
  fires on his word with no ceremony, while money and outward mutations get
  drafted and then approved through an authed channel. A dispatch is the
  former — it clones a repo, cuts a worktree, and spends tokens; it posts,
  sends, and publishes nothing. So `archon.dispatch` is declared honestly as
  model-initiable rather than carrying an `operator_confirmed` claim this
  lane cannot back: `talk_api.py` receives only a model-authored tool name
  and argument dict, and the Realtime input transcript stays in the browser's
  session with OpenAI and never reaches this process. A confirmation gate
  here would assert something unprovable. Accountability is the append-only
  audit row per attempt (a grant whose row cannot be written is refused) plus
  `HOMIE_KILLSWITCH_ARCHON_DISPATCH`.
- **The real gate lives where the money is spent.** Workflow-level
  `APPROVE SPEND` pause nodes halt a run before any render, deploy, or paid
  API call and require the verbatim phrase
  (`docs/manual/features/archon-steering-gates.md`). Routing those pauses to
  Telegram or the matching Discord agent for an operator tap — plus a spoken
  override for when the phone is not to hand — is its own ticket in epic
  #252.
- It is **not** the live telemetry path. `get_run()` returns a run's entire event
  log in one unpaginated call — correct for on-demand narration, wrong as a poll
  loop. Live event tailing is a read-only cursor-tail of Archon's event table,
  because Archon's dashboard SSE stream is single-slot and evicts any second
  subscriber.
- Failures are **reported**, not swallowed. Every helper raises a typed error. A
  caller on a fail-open path owns its own `try/except`; contract violations
  (bad action, blank brief, malformed id) raise `ValueError` before any network
  work happens.
- `check_loopback_posture()` does blocking socket work. From async code call it
  through `asyncio.to_thread` — and leave the port argument at its default so it
  resolves inside the thread rather than on the event loop.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Client (transport) | `.claude/scripts/integrations/archon_client.py` |
| Gate + F2 + correlation + steering | `.claude/scripts/talk_archon.py` |
| Gate phrases (control-plane) | `.claude/scripts/integrations/archon_approvals.py` |
| Uncapped gate read | `.claude/scripts/integrations/archon_events.py` (`read_gate_data_raw`) |
| Tool surface + run lifecycle | `.claude/scripts/talk_tools.py` |
| Declared capability actions | `.claude/scripts/integrations/capabilities.py` (`archon.dispatch`, `archon.steer`) |
| Voice obligation | `.claude/scripts/talk_session.py` (`_VOICE_PREAMBLE`) |
| Audit trails | `DATA_DIR/archon_dispatch.jsonl`, `DATA_DIR/archon_steer.jsonl` |
| Tests | `.claude/scripts/tests/test_archon_client.py`, `test_talk_archon_dispatch.py`, `test_talk_deploy_control.py`, `test_talk_steering.py` |
| Ops pin | `<archon-repo>/.archon/.env` (`HOST=127.0.0.1`) |

## Live-Optional Tests

Both are skipped by default.

```bash
# Read-only round trip against a running Archon — mutates nothing.
ARCHON_LIVE_TESTS=1 uv run python -m pytest tests/test_archon_client.py -q -k live

# The dispatch smoke. Costs roughly a minute and one worktree.
ARCHON_LIVE_TESTS=1 ARCHON_LIVE_DISPATCH=1 ARCHON_LIVE_CODEBASE_ID=<id> \
  uv run python -m pytest tests/test_archon_client.py -q -k spike_echo
```
