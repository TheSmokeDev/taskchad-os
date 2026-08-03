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

The point for you: **this is the receive pipeline you want to copy**, because
the naive version is the one that "can't hear you" — and when voice "doesn't
work," instrument the actual frames before touching the code.

The mechanics live in `.claude/scripts/discord_voice/bridge.py` (`_pump_mic`) and
`patches.py`, with the tests in `.claude/scripts/discord_voice/tests/`.

---

## Build your own

You need an OpenAI API key with Realtime access. For the Discord surface you also
need a Discord bot token with voice permissions.

**1. Configure keys** (in your framework `.env`):

```env
OPENAI_API_KEY=sk-...            # Realtime access
DISCORD_BOT_TOKEN=...            # only for the Discord voice surface
```

**2. Dashboard voice** — open the dashboard, go to the Talk view, and start a
session. The browser negotiates WebRTC with OpenAI directly; there's nothing else
to run.

**3. Discord voice** — the sidecar has its own virtual environment (it depends on
py-cord, which is separate from the main framework deps). Install it once, then
from a text channel the bot can see:

```
/talk join      # the bot joins your current voice channel and starts listening
/talk leave     # it leaves and writes a debrief of the conversation
```

**4. Tune the receive pipeline** (optional — sane defaults ship):

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

---

Talk Mode ships in TaskChad OS. If you're running your own instance, the whole
implementation is in this repo — clone the receive pipeline, wire your keys, and
give your assistant a voice.
