---
name: talk-mode-setup
description: Give a second brain that is already running a voice. Interactive setup for Talk Mode real-time voice, on the dashboard /talk page (zero install) and in Discord voice channels (py-cord sidecar). Detects what is already configured, asks before every write, and defaults voice to a Codex subscription instead of a metered API key. Use when the user wants to add or repair voice: "give my second brain a voice", "set up talk mode", "add voice", "install talk mode", "talk mode isn't working", "discord voice", "voice channel bot", "the bot can't hear me", "/talk join fails".
---

# Talk Mode Setup

Adds voice to a framework install that is **already running**. This does not
bootstrap a second brain, it gives an existing one a mouth and ears.

For the architecture, the receive-pipeline story, and the design notes, send the
user to `docs/talk-mode-showcase.md`. Do not restate it here.

## Rules of engagement

This skill is **interactive**. The user drives.

1. **Reads and checks are free.** Run the preflight, inspect `.env`, check the
   OS, look for the venv. Never ask permission to look.
2. **Every write is gated.** Before touching `.env`, creating a venv, or
   starting a service, show exactly what you are about to do and get an explicit
   pick via `AskUserQuestion` (or the running provider's question tool).
3. **Detect, then ask only what is missing.** Somebody half set up must not
   re-answer solved questions. Skip any step the preflight already reports green.
4. **One question per real decision, with concrete options.** Never a freeform
   "ok?".

---

## Step 1. Detect (no questions yet)

```bash
cd .claude/scripts
uv run python ../skills/talk-mode-setup/scripts/preflight.py --json
```

The JSON gives you everything you need to branch:

| Field | Use it to |
|-------|-----------|
| `source` | Which credential currently wins: `codex-oauth`, `env`, or `configured`. |
| `codexAvailable` | Whether a Codex ChatGPT login exists at all. |
| `preferCodex` | Whether `TALK_PREFER_CODEX_OAUTH` already pins voice to the subscription. If true, skip the billing question. |
| `talkKeySet` / `envKeySet` | Which API keys are set. Drives the metering warning. |
| `Discord bot token` check | Whether the Discord surface is even possible. |
| `Discord voice sidecar` check | Whether the venv is built, and whether this OS can run it. |
| port checks | Whether the services are already up. |

Also record the OS. `sys.platform` is enough.

Report the findings back in one short block before asking anything. The user
should see the current state before making choices.

---

## Step 2. Ask which surface

Only ask about surfaces that are actually possible on this machine.

**`AskUserQuestion`**, header `Surface`:

- **Dashboard only**: browser tab, zero install, works on every OS. Fastest path.
- **Discord voice**: talk in a voice channel. Needs a bot token plus a sidecar venv.
- **Both**.

Both surfaces work on Windows, macOS, and Linux. The sidecar resolves its
interpreter in the local venv layout and tears down by process group, so
`uv sync` then `/talk join` behaves the same everywhere.

One caveat you do not have to reason about: a framework build older than the
OS-aware sidecar resolver only ever looked for the Windows layout, so Discord
voice could not start on macOS or Linux there. Step 1's preflight detects that
case by asking the installed lifecycle which interpreter it spawns, and names it
outright. Trust the preflight over any assumption about the platform.

---

## Step 3. Auth, defaulting to the subscription

By default Talk resolves a credential in this order, first hit wins:

1. `TALK_OPENAI_API_KEY`
2. `OPENAI_API_KEY`
3. Codex OAuth (an existing ChatGPT sign-in via the Codex CLI)

`TALK_PREFER_CODEX_OAUTH=true` overrides that order for voice only: Codex
becomes the sole source, ahead of both keys. It is a billing directive, not a
ranking, which is why an unusable login fails closed instead of quietly
switching to a metered key.

**Prefer Codex.** It rides a subscription the user already pays for, with no
per-minute API cost. Steer there whenever it is available.

Branch on what Step 1 found:

**A. `source` is already `codex-oauth`.** Nothing to do. Say it is using the
subscription and move on. Do not ask.

**B. `codexAvailable` is true but an API key is winning.** This is the metering
trap: by default the key outranks the subscription, so the user is billed per
minute without being told. Surface it and let them choose.

`AskUserQuestion`, header `Billing`:

- **Use the Codex subscription.** Voice rides a plan they already pay for, and
  it can never quietly bill them: if the Codex login later lapses, voice stops
  with an error naming the fix instead of silently falling back to a metered
  key. Frame that as the selling point it is. A surprise invoice is not a
  failure mode here.
  *Action:* write `TALK_PREFER_CODEX_OAUTH=true` to `.claude/scripts/.env`.
  Show the exact line and confirm before writing it.
  *Reassure them:* the key stays where it is. This knob is scoped to voice, so
  `OPENAI_API_KEY` keeps serving STT, the OpenAI-compatible runtime lane, and
  personas. Nothing else moves.
- **Keep the API key.**
  *Action:* nothing. Voice bills per minute. Fine if deliberate.

If `preferCodex` is already true, this branch does not apply. The directive is
set and Codex has been chosen; do not re-ask.

**C. No credential at all.** `AskUserQuestion`, header `Credential`:
- **Sign in with Codex (recommended, no per-minute cost)**: run `codex login`,
  then confirm with `codex login status`.
- **Wire an API key**: write `TALK_OPENAI_API_KEY=sk-...` to
  `.claude/scripts/.env`. Confirm the exact line before writing it.

A configured key that is set but **empty fails closed** and does not fall
through. If you find a blank key, remove the line rather than blanking it.

After any change, re-run the preflight and confirm `source` is what the user
picked. Do not assume the edit took.

---

## Step 3.5. Identity — should it know you?

The default voice prompt carries **SOUL only**: the behavioral contract, none of
the personal context. Out of the box the voice will not know the user's name,
projects, or memory — and it fails SILENTLY (status `ready`, audio fine, just a
stranger's personality). If the user expects "it opens knowing me", this step is
what delivers it.

`AskUserQuestion`, header `Identity`:

- **Load my identity on calls (recommended).** The voice opens with SOUL + USER +
  MEMORY + WORKING already in context — it knows who it's talking to without a
  single tool call.
  *Action:* write `TALK_IDENTITY_INCLUDE=SOUL,USER,MEMORY,WORKING` to
  `.claude/scripts/.env`. Show the exact line and confirm before writing.
- **Behavioral contract only.** SOUL rules, no personal context. Fine for a
  shared or demo install.
  *Action:* nothing (the default).

**The SOUL-drop trap:** the env list REPLACES the default, it does not extend
it. `TALK_IDENTITY_INCLUDE=USER,MEMORY` ships NO soul. Always list `SOUL`
explicitly — never write this variable without it.

---

## Step 4. Dashboard voice

Confirm before starting anything. `AskUserQuestion`, header `Services`, listing
which of these are already listening from Step 1:

```bash
cd .claude/scripts && uv run python orchestration/run_api.py        # 4322
cd dashboard/server && DASHBOARD_DEV_MODE_NO_AUTH=true npm start    # 3141
```

Then open `http://127.0.0.1:3141/talk`.

Two things that silently break this:

- **Microphone needs a secure context.** `localhost` or `127.0.0.1` or HTTPS. A
  LAN IP denies the mic with no useful error.
- **Proxy auth is all or nothing.** Either `DASHBOARD_DEV_MODE_NO_AUTH=true` on
  loopback, or `DASHBOARD_TOKEN` set **equal to** `ORCHESTRATION_API_TOKEN` and
  the page loaded once as `/talk?token=<DASHBOARD_TOKEN>`. Mismatched or
  half-set, the proxy refuses to start.

---

## Step 5. Discord voice

Skip entirely unless Step 2 chose it. Any OS is fine.

**5a. Bot token.** Confirm before writing. In the Discord Developer Portal:
Bot tab, copy the token, then write `DISCORD_BOT_TOKEN=...` into
`.claude/scripts/.env`. The main bot and the sidecar share this one token.
Enable Message Content Intent for the main bot. The sidecar itself only requests
`guilds` and `voice_states`.

**5b. Invite.** OAuth2 URL Generator, scopes `bot` and `applications.commands`.
Permissions: Send Messages, Read Message History, and under Voice, **Connect**
and **Speak**. Missing Connect or Speak fails the join at the Discord layer, not
in this code.

**5c. Sidecar venv.** Confirm before creating, then:

```bash
cd .claude/scripts/discord_voice
uv sync
```

That creates `.venv/` with py-cord[voice], PyNaCl, websockets, and httpx. The
sidecar needs its **own** venv because py-cord and discord.py both own the
`discord` import namespace and cannot share one.

**5d. Use it.** The user never runs `bridge.py` directly. `/talk join` spawns the
sidecar (control server on `127.0.0.1:7861`) and `/talk leave` reaps it.

```
/talk status    # state, plus channel, uptime, auth source, and log file once live
/talk join      # joins YOUR current voice channel
/talk leave     # leaves and writes a memory debrief
```

`/talk` is admin-only, Discord-only, and needs the orchestration API on 4322.
Join a voice channel yourself before running `/talk join`.

If `ORCHESTRATION_API_TOKEN` is set, the sidecar reads that same variable and
sends it as a bearer token. Mismatched values produce a confusing half-failure:
ordinary conversation still works while every tool call 401s.

---

## Step 6. Verify

Do not stop at "no errors". Confirm the loop end to end:

1. Preflight shows no `[FAIL]`, and `source` is the credential the user chose.
2. **Discord**: `/talk status` reports `ready` with a channel, uptime, and auth
   source. That is the success gate.
3. **Speech in**: transcripts show real words, not empty strings or fragments.
   Fragments mean the receive pipeline, not the model.
4. **Speech out**: an audible reply.
5. **Identity**: if Step 3.5 chose it, ask the voice something only the USER or
   MEMORY files know ("what am I working on?"). A generic answer means the
   prompt collapsed — **the knob being set is not proof it arrived.** On
   Discord, confirm with the receipt the sidecar logs at session start:
   `grep "identity roots:" <the log /talk status names>` must show
   `profile=default` and a `memory_dir` that exists on disk. A `profile=custom`
   you never configured, or a nonexistent `memory_dir`, is the collapse — see
   `references/troubleshooting.md`.
6. **Memory**: end the session and confirm the debrief landed in the daily log.

If any step fails, load `references/troubleshooting.md`.

---

## Step 7. Optional tuning

Only offer this if the user asks. Defaults are sane.

| Knob | Default | Effect |
|------|---------|--------|
| `TALK_OPENAI_VOICE` | `cedar` | alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar. An unknown name raises. |
| `TALK_OPENAI_MODEL` | `gpt-realtime-2.1` | Realtime model. |
| `TALK_IDENTITY_INCLUDE` | `SOUL` | Covered in Step 3.5 (not ask-only — it's the "opens knowing you" switch). **Replaces the default, does not extend it**, so `USER,MEMORY` ships no SOUL. List `SOUL` explicitly. Valid names: SOUL, USER, MEMORY, WORKING, GOALS, SELF. |
| `TALK_PREFER_CODEX_OAUTH` | off | Pins voice to the Codex subscription ahead of both API keys. Fails closed if the login is unusable rather than falling back to metered billing. Scoped to voice; other `OPENAI_API_KEY` consumers are untouched. |
| `TALK_ENABLE_CODE_EXEC` | off | Fail-closed opt-in for `run_python` and `run_shell`. |
| `DISCORD_VOICE_JITTER_FRAMES` | `3` | Input buffer depth, about 60 ms. |
| `DISCORD_VOICE_SILENCE_DBFS` | `-35` | Noise gate. Lower passes quieter audio. |
| `DISCORD_VOICE_DEBUG_PCM` | unset | Dumps the exact PCM sent to OpenAI. First thing to reach for on "it cannot hear me". |

Kill switches, set to `disabled` to refuse: `HOMIE_KILLSWITCH_VOICE` (all voice),
`HOMIE_KILLSWITCH_COMPUTER_USE` (the `computer` and `browse` tools).

---

## Bundled

- `scripts/preflight.py` : the detection pass. `--json` for branching.
- `references/troubleshooting.md` : symptom-first fixes, load on demand.

---

## Maintenance notes

Both original limits are now resolved. What replaced them is worth protecting.

**1. Codex preference (resolved).** `TALK_PREFER_CODEX_OAUTH` makes Codex the
sole voice credential, ahead of both key legs, and **fails closed** when the
login is unusable rather than falling back to a metered key. That fail-closed
behavior is the whole point of the flag, not an edge case: a silent fallback
would bill someone who explicitly asked not to be billed. If you ever see a
fallback path reintroduced under this flag, the copy in Step 3B becomes a lie
about billing. Treat that as a blocker, not a nit.

The knob is voice-scoped by design, so `OPENAI_API_KEY` keeps serving STT, the
OpenAI-compatible runtime lane, and personas. Do not "helpfully" broaden it.

**2. Discord voice is cross-platform (resolved).**
`discord_voice_lifecycle._sidecar_python()` is OS-aware, so the Windows-only
limitation is gone from the copy. `_check_sidecar` in `scripts/preflight.py`
deliberately does NOT hardcode either layout: it imports the lifecycle and asks
which interpreter that build spawns, then checks whether it exists. That keeps
it correct on older Windows-only builds too, where it reports the mismatch by
name instead of telling someone to re-run a `uv sync` they already ran. Do not
"simplify" it back to a layout guess.
