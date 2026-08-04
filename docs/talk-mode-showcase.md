# Talk Mode — Give Your AI Co-Founder a Voice

> **What this is:** a real-time voice co-founder for TaskChad OS. You talk to
> your assistant out loud — on the dashboard or in a Discord voice channel — and
> it talks back, runs real work while you keep talking, and remembers the
> conversation after you hang up. This page is the "here's how it's built, go
> build your own" guide. Every file it points at ships in this repo.

Most "AI voice" demos are a microphone wired to a text model — pretty, but it
forgets you the moment the call ends and it can't *do* anything. Talk Mode is
the opposite bet: the voice is just a new door into the same assistant that
already knows you, runs your skills and agents, and writes to your memory. It's
a co-founder you can talk to, not a chatbot with a speaker.

---

## What it can do

- **Opens knowing you.** Every session is minted with your identity and memory
  files already in context (SOUL / USER / MEMORY / WORKING). You don't
  re-introduce yourself — it starts mid-relationship.
- **Runs real work while you talk.** Ask it to run a skill, spin up a coding
  agent, kick off an Archon workflow, or look something up. The work runs in the
  background; it narrates the result out loud the moment it lands — no dead air
  while it "thinks."
- **You can steer mid-flight.** Redirect or cancel a running agent by voice at a
  turn boundary — the same way you'd interrupt a human who's off track.
- **Remembers the conversation.** When the session ends, a debrief distills what
  happened into your daily log and a structured episode, so the next session —
  voice or text — picks up where you left off.
- **Two surfaces, one brain.** A browser tab on the dashboard, or a Discord
  voice channel you `/talk join`. Same assistant, same memory, same tools.

---

## Why not just wire a speech-to-text bot?

The obvious build is a transcript pipeline: record the caller, wait for a
stretch of silence, transcribe, hand the text to the agent, synthesize a reply,
play the file. It works on every platform and it's easy to reason about. It's
also a voicemail exchange: the model never hears tone or pacing, nothing can be
interrupted once it starts talking, and every turn pays the full
record → transcribe → think → speak round trip before the caller hears a
syllable.

Talk Mode is the other architecture: the model is ON the call. Your audio
streams into a realtime session as you speak, the reply streams back as audio,
you can cut it off mid-sentence, and you can steer running work by voice
instead of waiting for your turn. The difference isn't polish — it's which
conversations are possible.

Both shapes are valid. Voice notes and maximum platform breadth favor the
transcript pipeline; a live call favors realtime. This repo ships the realtime
one because a co-founder is someone you talk *with*, not leave messages for.

---

## How it's built

Talk Mode is two thin transports feeding one shared runtime — the assistant, its
memory, and its tools are unchanged; voice is an adapter, not a fork.

```
                          ┌──────────────────────────┐
   Dashboard /talk  ─────▶│  OpenAI Realtime API      │
   (browser WebRTC)       │  (speech in / speech out) │
                          └────────────┬─────────────┘
   Discord /talk join ─┐               │
   (voice channel)     │               ▼
        │              │      tool calls + run sentinels
        ▼              │               │
   py-cord sidecar ────┘               ▼
   (opus decode →            ┌──────────────────────┐
    resample → mic gate)     │  Your assistant's    │
                             │  runtime + memory +  │
                             │  skills / agents     │
                             └──────────────────────┘
```

- **Dashboard `/talk`** — the browser talks WebRTC straight to the OpenAI
  Realtime API. Lowest-latency path; nothing to install.
- **Discord `/talk join`** — a small Python **sidecar** (its own venv) joins the
  voice channel with [py-cord](https://pycord.dev/), decodes each speaker's Opus
  frames (through Discord's DAVE end-to-end encryption), resamples 48 kHz → 24 kHz
  mono, runs a noise gate, and pumps 20 ms frames into the same Realtime session.
- **The co-founder loop** — tool calls from the model dispatch real work; a
  `WORK_STARTED` sentinel registers each run so the assistant can poll it and
  narrate completion. Session end fires a memory debrief.

Code map (everything is in this repo):

| Piece | Where |
|-------|-------|
| Discord voice sidecar (py-cord, DAVE, decode/resample/gate) | `.claude/scripts/discord_voice/` |
| The receive pipeline + mic pump | `.claude/scripts/discord_voice/bridge.py` |
| OpenAI Realtime session wrapper | `.claude/scripts/discord_voice/realtime.py` |
| Session-end vault debrief | `.claude/scripts/talk_flush.py` |
| Run registry + steering | `.claude/scripts/talk_runs.py`, `talk_tools.py` |
| Dashboard `/talk` surface | the dashboard web bundle + its Talk view |

---

## The part that makes it real: a battle-tested receive pipeline

Anyone can wire a mic to a model in an afternoon. The hard part of *Discord*
voice is that the audio you receive is hostile — end-to-end encrypted, delivered
in jittery 20 ms packets, interleaved across speakers, and padded with probe
frames. Getting clean, intelligible speech out the other side is where toy demos
fall over.

One real bug from this codebase, worth sharing because it's the exact class of
thing you'll hit:

> The bot "wasn't listening." Speech reached it as **real, clean audio**, but a
> capture showed it **shredded into 71 mid-word one-frame silences** — so the
> transcription came back empty or as fragments like "dla" / "mhm."
>
> **Root cause:** the mic pump waited exactly 20 ms for each packet, then gave
> up and inserted silence. But 20 ms is *exactly* Discord's packet spacing, so
> any packet arriving even 1 ms late tripped the timeout and jammed a silent
> frame into the middle of a word.
>
> **Fix:** a **paced jitter buffer** — buffer a few frames so a late packet has
> a spare to cover it, and emit exactly one 20 ms frame per 20 ms of wall time
> from a monotonic clock (so the server-side voice-activity detector still
> measures silence in real time and doesn't glue separate sentences together).
> A discontinuity policy sheds backlog to keep latency bounded without dropping
> the timeline or leaking noise.

That fix went through two rounds of adversarial code review plus an independent
design gate before shipping — the buffer, the gate, and the padding classifier
are all covered by a deterministic virtual-clock test harness.

And it wasn't even the deepest one. Under it sat **the shredder**: the RTP
extension header gets parsed and removed at the transport-decrypt layer, but
the upstream library's E2EE branch re-parses the *decrypted audio itself* as if
it began with another extension header — slicing a garbage offset off the front
of every frame. Every packet from a real client carries the extension flag, so
**100% of received voice decoded to noise**: half threw "corrupted stream", the
rest decoded garbage, and the transcription model hallucinated phrases nobody
said. It was found with per-frame forensics (log the frame length, opus TOC
byte, sequence, and extension flag on both decode paths — failed frames had
*random* TOC bytes, the fingerprint of frames starting mid-buffer), and fixed
by one rule: **the E2EE plaintext IS the codec frame — never re-parse it.**

After the fix shipped, we read another major agent framework's hand-rolled
Discord receiver for comparison. Different codebase, different Discord
library — and the same conclusion baked into their pipeline: extension math
happens before E2EE decryption, and the decrypted frame reaches the decoder
whole. Two implementations arriving independently at the same invariant is the
strongest validation a receive pipeline gets.

The point for you: **this is the receive pipeline you want to copy**, because
the naive version is the one that "can't hear you" — and when voice "doesn't
work," instrument the actual frames before touching the code.

The mechanics live in `.claude/scripts/discord_voice/bridge.py` (`_pump_mic`) and
`patches.py`, with the tests in `.claude/scripts/discord_voice/tests/`.

---

## Build your own

Two surfaces, two very different setups. The dashboard Talk view is zero-install
(the browser negotiates WebRTC with OpenAI directly). The Discord sidecar needs a
one-time install, because it runs in its own virtual environment.

If you would rather not do this by hand, the repo ships a `talk-mode-setup`
skill. Point your agent at it ("run talk-mode-setup", or just "add voice") and it detects
what you already have, asks before it changes anything, and walks the rest of
this page for you. The manual version follows.

**1. Authentication.** Talk Mode resolves an OpenAI Platform credential in this
order, first hit wins:

| Source | How you set it |
|--------|----------------|
| `TALK_OPENAI_API_KEY` | A Talk-scoped API key. Present but blank fails closed, with no fallback. |
| `OPENAI_API_KEY` | The standard environment variable. |
| Codex OAuth | Run `codex login`. Reuses an existing ChatGPT sign-in, so no API key at all. |

Realtime access follows whichever account ends up authenticated. The credential
never leaves the framework process: the browser only ever receives a short-lived
ephemeral secret.

The Discord surface also needs a bot token, from a bot with voice permissions.
Both go in `.claude/scripts/.env`:

```env
# Set ONE of these two, or set neither and run `codex login` instead.
TALK_OPENAI_API_KEY=sk-...
OPENAI_API_KEY=sk-...

DISCORD_BOT_TOKEN=...            # only for the Discord voice surface

TALK_IDENTITY_INCLUDE=SOUL,USER,MEMORY,WORKING   # what "opens knowing you" requires
```

The identity line matters more than it looks: the default is `SOUL` only — the
behavioral contract with none of your personal context — so without it the voice
is polite, fluent, and a stranger. The list REPLACES the default rather than
extending it, so always include `SOUL`.

**2. Dashboard voice.** Open the dashboard, go to the Talk view, and start a
session. There is nothing else to run.

**3. Discord voice: install the sidecar (once).** py-cord shares the `discord`
import namespace with discord.py, so the two can never live in the same
environment. The sidecar gets its own:

```bash
cd .claude/scripts/discord_voice
uv sync
```

That creates `.venv/` with py-cord[voice], PyNaCl, websockets, and httpx. The
supervisor resolves the interpreter in the local venv layout and reaps the
sidecar by process group, so this works on Windows, macOS, and Linux alike.

**4. Start the orchestration API.** The `/api/discord/voice/*` routes are mounted
on it, and the sidecar relays every tool call back through it over loopback
(default `http://127.0.0.1:4322`). It has to be up before you join:

```bash
cd .claude/scripts
uv run python orchestration/run_api.py
```

If you set `ORCHESTRATION_API_TOKEN`, the sidecar reads that same variable and
sends it as a bearer token. The two values have to match, or the relay gets a 401
and tool calls fail while ordinary conversation still works.

**5. Talk.** Join a voice channel yourself first, then from any text channel the
bot can see:

```
/talk join      # bot joins YOUR current voice channel and starts listening
/talk status    # state, channel, uptime, auth source, log file
/talk leave     # it leaves and writes a debrief of the conversation
```

You never launch `bridge.py` yourself. `/talk join` spawns it, waits for its
control server on `127.0.0.1:7861`, and reaps the process tree on `/talk leave`.

**Did it work?** Run `/talk status`. A live session reports `ready` plus the
channel, uptime, which credential source won, and the log file name:

```
Voice talk: *ready*, channel `1234...`, uptime 12.4s, auth codex-oauth, log discord-voice.log
```

Three failures worth recognizing:

| What you see | What it means |
|--------------|---------------|
| `sidecar venv missing ... run uv sync` | Step 3 was skipped, or the venv is not on the Windows layout. |
| `sidecar control server did not come up in 30s` | It spawned but could not boot. The message names the full log path, so read that file. |
| `Join a voice channel first, then /talk join` | The bot resolves *your* current voice channel, and you are not in one. |

**6. Tune the receive pipeline** (optional — sane defaults ship):

| Knob | Default | What it does |
|------|---------|--------------|
| `DISCORD_VOICE_JITTER_FRAMES` | `3` | Input buffer depth (~60 ms). Higher absorbs more jitter at the cost of latency. |
| `DISCORD_VOICE_JITTER_SOFT_FRAMES` | `2×` frames | Soft high-water — sheds a frozen backlog so latency recovers. |
| `DISCORD_VOICE_JITTER_MAX_FRAMES` | `+25` | Hard latency ceiling (~500 ms) before dropping oldest. |
| `DISCORD_VOICE_SILENCE_DBFS` | `-35` | Noise-gate threshold. Lower passes quieter audio. |
| `DISCORD_VOICE_DEBUG_PCM` | unset | Path to dump the exact PCM sent to OpenAI — invaluable when debugging "it can't hear me." |

---

## Design notes worth stealing

- **Voice is an adapter, not a fork.** The assistant, memory, and tools don't
  know or care that the input arrived as speech. Keep the transport thin and the
  brain shared — otherwise you maintain two assistants.
- **Pace on a clock, not on arrivals.** Real-time voice into a VAD must emit one
  frame per frame-interval of *wall time*. An event-driven pump that emits only
  when audio arrives makes a 500 ms pause take seconds of real time and glues
  turns together.
- **Buffer the input, bound the latency.** A tiny jitter buffer kills the
  butt-splicing; a shed-to-priming policy on overflow keeps latency from growing
  unbounded on a stall.
- **Run work off the event loop.** Never drive a long-running tool or a browser
  on the same loop that's servicing audio — one hung call freezes the whole
  conversation.
- **Debug with the actual waveform.** When voice "doesn't work," dump the exact
  PCM you're sending and *look at it*. Ninety percent of "the model is broken"
  turns out to be "the audio reaching it is shredded."
- **A child that re-derives its own root will silently disagree with its
  parent.** The sidecar was once handed a home path it reclassified as a
  different profile, re-rooted its memory at a directory that didn't exist, and
  failed open to a bare prompt — so the voice was polite, fluent, and knew
  nothing, while every status check stayed green. Pass the child a value that
  round-trips to the SAME resolution the parent made, and make the child log
  what it actually resolved (`identity roots: profile=... memory_dir=...`) so
  the next collapse is a grep, not a live debugging session.

---

Talk Mode ships in TaskChad OS. If you're running your own instance, the whole
implementation is in this repo — clone the receive pipeline, wire your keys, and
give your assistant a voice.
