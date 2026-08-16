# Persona Self-Provisioning

Status: Shipped — executor (#426), the typed command surface (#427), and the counter-offer flow (#428) are in; linked-skill intake is a separate ticket.
Owner: Framework — personas + chat slices
Last updated: 2026-08-14

## What It Does

Gives one homie a new capability bundle from a chat command instead of YAML
surgery. `/persona grant sales research_read` validates the name against the
LIVE toolset registry, writes the persona's `config.yaml` through a
strict-read read-modify-write, appends an append-only audit row carrying the
operator turn that ordered it, and answers `added to sales — live next turn`.
`/persona revoke` is the exact mirror, because reversibility is the safety
argument for the whole feature.

Two hard edges, from the architecture doctrine
(`PRDs/active/PRD-persona-self-provisioning.architecture.md`):

1. **Operator imperative = authorization; autonomous self-granting = never.**
   The grant path fires only for the authenticated operator on a live
   interactive turn. A Discord stranger typing the command hits the role gate.
   Grant text arriving through recalled or ingested content is inert — it is
   not a live operator turn.
2. **Grants expand tool SURFACE, never bypass gates.** Every per-tool
   default-deny gate (social writes, sends, spends, browser writes,
   integration actions) applies unchanged to a self-provisioned tool.
   Provisioning grants REACH; the gates still govern ACTION.

## Operator Entry Points

- Chat (Telegram / Discord / CLI): `/persona grant|revoke` for direct grants;
  `/grant list|approve|deny` for counter-offer proposals
- Single-capability grants (#465 1c): `/persona grant sales tool:memory_search`
  moves ONE registered tool into the persona's `tools:` list — same executor,
  same gates, same ledger (rows carry `kind=tool` so a tool and a toolset
  sharing a name never alias in replay). The counter-offer marker form is
  `<<GRANT_REQUEST: tool:memory_search>>`. Grant remains reach-only: a
  `dedicated_gate` tool can be granted, and its action gate still authorizes
  every execution downstream.
- Telegram also gets one-tap Approve/Deny inline buttons on the counter-offer
  card itself (see the Counter-Offer Flow section)
- CLI: none yet — the executor is a Python entrypoint
  (`personas.services.add_persona_toolset`), not a `thehomie` subcommand
- Dashboard: none yet
- API: none yet

### Commands

| Command | What it does |
|---|---|
| `/persona` | Usage — the two forms and what a grant costs |
| `/persona grant <persona> <toolset>` | Add a registered toolset bundle to that homie |
| `/persona grant <toolset>` | Same, with the persona taken from the channel |
| `/persona revoke <persona> <toolset>` | Take a toolset back |
| `/persona revoke <toolset>` | Same, with the persona taken from the channel |
| `/grant list` | Show pending counter-offer proposals |
| `/grant approve <persona> <code>` | Approve one pending proposal (runs the #426 executor) |
| `/grant deny <persona> <code>` | Close one pending proposal without granting |

## Counter-Offer Flow (#428)

When a persona hits a missing tool mid-task — or asks for one free-form — its
reply carries a server-side marker and becomes a counter-offer CARD backed by
a PENDING row in that persona's proposal store
(`data/persona_grant_proposals.db`). The persona only ever creates a
proposal; no persona reply can reach a config mutation.

- **Telegram:** the card carries Approve/Deny inline buttons. One tap decides
  — the tap is provenance-checked server-side (it must ride one of this bot's
  own button messages, and the role gate re-runs at decide time) — and a
  sanitized receipt persists into the card's reply-thread transcript.
- **Other adapters (Discord, CLI):** the card names the exact command,
  `/grant approve <persona> <code>`, and `/grant list` shows what's pending.

`/grant` only ever DECIDES a proposal that already exists — it can never
conjure a grant. The direct-grant surface is `/persona grant` above, the only
command that reaches the #426 executor without a pending proposal. An
un-actioned proposal expires on its TTL via the heartbeat sweep, with an
audit row, and an expired proposal cannot be approved.

The short form resolves the persona from, in order: the Discord
channel → persona binding, then the profile this bot process runs as (a
persona bot's own channels are that persona's). With neither, the command
asks you to name the homie rather than guessing. An explicit persona argument
always wins.

Replies you can get, and what each means:

| Reply | Meaning |
|---|---|
| `` `research_read` added to sales — live next turn.`` | Config written, audit row written. `resolve_toolset()` has no cache, so the persona has it on its next turn. |
| `` `research_read` removed from sales — live next turn.`` | Same, in reverse. |
| `sales already has …` / `sales does not have …` | An honest statement of state. Nothing was rewritten. |
| `refused: 'reserch_raed' is not in the live toolset registry (…). Nearest: research_read.` | The name is not registered. The nearest matches come from string distance only — never a guessed grant. |
| `refused: toolset grant requires the admin role, got 'unauthenticated' (…)` | The role gate. The trailing clause names which check failed. |

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/personas/services.py` (`add_persona_toolset`, `remove_persona_toolset`, `_mutate_persona_toolset`), `.claude/scripts/personas/toolset_grants.py` (ledger, refusal types, registry lookup, replay), `.claude/scripts/personas/grant_proposals.py` (counter-offer proposal store, decide path, TTL sweep) |
| Chat/router | `.claude/chat/persona_grant_commands.py` (parse, server-side role resolution, channel-persona defaulting), `.claude/chat/core_handlers.py` (`handle_persona`, `handle_grant`, `decide_grant_proposal_with_receipt`), `.claude/chat/commands.py` (registry rows, category, native menu) |
| Tests | `.claude/scripts/tests/test_persona_grant_commands.py`, `.claude/scripts/tests/test_persona_toolset_grants.py`, `.claude/scripts/tests/test_persona_grant_proposals.py` |
| Docs/proof | This page, `PRDs/active/PRD-persona-self-provisioning.architecture.md`, `PRDs/active/PRD-persona-self-provisioning.md` |

## Safety Boundaries

**The role is resolved server-side — trusted from the adapter's stamp, never
read off caller-asserted data.** `IncomingMessage.user_role` is the canonical
role-ingress seam (issue #424): every remotely-reachable adapter stamps it,
at ingress, from ITS OWN authenticated identity data —
Telegram/Discord/Slack/WhatsApp compare the sender against their own operator
allowlist (`TELEGRAM_ALLOWED_USER_IDS`, `DISCORD_ALLOWED_USERS`,
`CHAT_ALLOWED_USERS`, `WHATSAPP_ALLOWED_NUMBERS`), Buzz resolves a
signature-verified pubkey through a per-pubkey role map, and the CLI stamps
`admin` unconditionally because reaching it needs a shell on the box, which
already carries write access to the same `config.yaml`. The field's default
is fail-closed `"viewer"`, and a repo-wide static test
(`test_ingress_role_seam.py`) fails the build if any producer of an
`IncomingMessage` forgets to state a role explicitly. `resolve_operator_identity`
used to re-derive that allowlist check itself; it now trusts the stamp and
keeps only what is genuinely this module's own to decide:

1. the turn's `source` is exactly `interactive` — a cron/tool/hook turn, or
   grant text recovered from a document, cannot authorize, even if it happens
   to carry an admin role (the CLI stamps `admin` for every invocation
   regardless of `source`, including a scripted query);
2. the platform is one this module recognizes as a role authority at all —
   a positive allowlist (`telegram`, `discord`, `slack`, `whatsapp`, `buzz`,
   `cli`), so an unrecognized platform is refused rather than trusted. `web`
   is deliberately excluded: the retired `ws_client.py` relay shares
   `Platform.WEB` with the live (always-`viewer`) web adapter but resolves
   its own role from client-supplied JSON with no allowlist behind it, so a
   "web" platform value carries no server-verified identity this module can
   rely on for a config mutation;
3. for WhatsApp and Slack specifically, that platform's own operator
   allowlist is actually configured — a belt-and-suspenders check (the #424
   design note) that can only ever refuse under the current wiring, since the
   stamp cannot legitimately be `admin` here unless the allowlist is already
   set. Telegram and Discord need no equivalent check here: they have
   carried an allowlist since the bot's inception, while Slack and WhatsApp
   only gained a real fail-closed one in the #449 role-ingress seam.

An EMPTY allowlist means DENY — the adapter itself stamps every sender
`viewer` when its allowlist is unset ("empty = anyone may chat" is a
conversational convenience; it is never a statement that the sender is the
operator). The local CLI is the one surface with no allowlist to check,
because its transport is the authentication.

Everything else:

- **One mutation path.** The command surface never edits YAML. Every grant,
  revoke, and refusal goes through the #426 executor, which owns the
  strict-read RMW, the `_validate_toolsets_section` check, the atomic write,
  and the ledger.
- **Command text never enters an LLM prompt** (the `cabinet/room_commands.py`
  precedent), and no model output ever reaches the executor.
- **Every exit that reaches the executor is audited**, including refusals.
  The ledger row carries who, what, when, the triggering turn's text
  (collapsed to one line, capped, and run through `security.redact`), and the
  channel — so the epic's "zero grants without a matching live operator turn"
  is greppable by construction. The one exception is a persona name that
  fails shape-checking BEFORE the executor is called (see next bullet) — that
  refusal is deliberately silent in the ledger, because the ledger path
  itself is derived from the (untrusted) name.
- **Hostile input is shape-checked at the seam.** The persona name is run
  through `validate_persona_name` before the executor sees it: the ledger path
  is built from that name and is not validated downstream, so a traversal
  token would otherwise create a directory outside the profile tree while
  being refused. Toolset tokens are identifier-shaped, checked in the parser,
  because a revoke deliberately skips the registry check (removing reach must
  keep working for an unregistered name).
- **Kill switch:** `HOMIE_KILLSWITCH_PERSONA_MUTATION=disabled` refuses every
  grant and revoke, with a counted refusal. It only turns the surface OFF.
- **The default profile is refused.** `chat/engine.py` resolves persona tools
  only for a non-default active profile, so the main homie's tools come from
  `DEFAULT_AGENT_TOOLSET` and never from config `toolsets:`. Granting there
  would write a file nothing reads. Main-homie self-grant is a filed
  follow-up (Q6 spike verdict).
- **No blocking work on the event loop.** The executor takes a cross-process
  lock and does file IO; the chat handler runs it inside `asyncio.to_thread`.

## How To Run It

```text
/persona grant sales research_read
/persona revoke sales research_read
/persona grant research_read          # inside sales' persona channel
```

Read the audit trail for one persona:

```powershell
Get-Content "$env:USERPROFILE\.homie\profiles\sales\data\persona_toolset_grants.jsonl"
```

## How To Test It

```powershell
cd .claude/scripts
uv run python -m pytest tests/test_persona_grant_commands.py tests/test_persona_toolset_grants.py -q
```

## Latest Live Proof

- Date: pending
- Surface: pending — the command ships with suite proof; a live Telegram
  grant + a live non-operator Discord refusal are the two receipts to capture.
- Result: pending
- Proof docs/artifacts: pending

## Public Export Status

Public-exported.

## Next Slices

- Linked-skill intake: an operator-dropped skill routes through the existing
  ingest → security scan → promote lifecycle and auto-assigns to the
  requesting persona on scan pass.
- Main-homie self-grant (Q6 follow-up) and a `thehomie` CLI mirror of the
  command. (The Q3 single-tool grants follow-up shipped with #465 1c.)
