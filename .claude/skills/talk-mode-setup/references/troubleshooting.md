# Talk Mode Troubleshooting

Symptom first. Run `scripts/preflight.py` before and after every fix.

---

## Auth

**"OpenAI Realtime voice requires an OpenAI API key or Codex OAuth sign-in", but a key is set.**
A configured key that is *set but empty* fails closed by design; it does not
fall through to the next source. Delete the line entirely instead of blanking
it. Check `TALK_OPENAI_API_KEY` first; it wins over `OPENAI_API_KEY`.

**Auth resolves but session minting still fails.**
The account lacks Realtime access. When preflight reports source
`codex-oauth`, entitlement and billing follow that ChatGPT account, not an API
key. Try an API key on an account with Realtime enabled.

**"Voice is pinned to your Codex OAuth subscription and no usable sign-in was found."**
Working as designed. `TALK_PREFER_CODEX_OAUTH` is on, and the Codex login is
missing, expired, or failed to refresh. It refuses rather than switching to a
metered API key behind your back, which is the entire reason the flag exists.
Two ways out, and the error names both: run `codex login` to restore the
subscription, or unset `TALK_PREFER_CODEX_OAUTH` to allow the key and accept
per-minute billing. An API key sitting right there is deliberately not used.

**Everything is configured and it still refuses.**
Check the kill switch: `HOMIE_KILLSWITCH_VOICE=disabled` refuses session mint,
tool calls, flush, and Discord join. Preflight reports this as `[FAIL]`.

---

## Dashboard

**The proxy exits immediately on start.**
Auth policy: `DASHBOARD_TOKEN` and `ORCHESTRATION_API_TOKEN` must both be set
**and equal**, or you must use `DASHBOARD_DEV_MODE_NO_AUTH=true` on loopback.
One set without the other, or a mismatch, is a refusal to start.

**The page loads but every request 401s.**
Token mode needs the token in the URL once: `.../talk?token=<DASHBOARD_TOKEN>`.
It is persisted to `sessionStorage`, so clearing site data wipes it and the page
falls back to 401. Reload with `?token=` again.

**The browser never asks for microphone permission.**
Mic capture requires a secure context: `localhost` / `127.0.0.1` or HTTPS. A
LAN IP silently denies it.

**Vite is running but the page won't load on `127.0.0.1:5173`.**
Vite may bind IPv6 only. Use `http://localhost:5173`.

**`EADDRINUSE` after restarting a service.**
Task wrappers (`npm start`, `uv run`) die leaving the child holding the port.
Kill by port, never by process name:

```bash
netstat -ano | grep <port>
taskkill //PID <pid> //F        # Windows
```

Never `killall node`; other projects share it.

---

## Discord

**`/talk` replies "Voice talk is Discord-only right now."**
Expected. The command only works from Discord; the browser surface is the
dashboard `/talk` page.

**`/talk` does nothing / isn't offered.**
It is admin-only. Native slash commands also need a successful `CommandTree`
sync. Check startup logs, and confirm the invite used the
`applications.commands` scope.

**The join fails with `sidecar venv missing: ...python.exe`.**
Exactly what it says:

```bash
cd .claude/scripts/discord_voice && uv sync
```

If the venv *does* exist and you are on Linux or macOS, your framework predates
the OS-aware sidecar resolver: that older build only looked for
`.venv/Scripts/python.exe`, so a POSIX `.venv/bin/python` is never found and
re-running `uv sync` cannot help. Update the framework, or use dashboard voice
there. The preflight names this case explicitly rather than reporting a missing
venv.

**`sidecar control server did not come up in 30s`.**
It spawned but could not boot. The message names the full log path. Read that
file; the real error is in it.

**Conversation works but every tool call fails.**
`ORCHESTRATION_API_TOKEN` mismatch. The sidecar sends that same variable as a
bearer token to the orchestration API, so unequal values 401 the relay while
ordinary speech keeps working. Make the two match, or unset both.

**`/talk join` fails to connect to the API.**
The handler calls the orchestration API over loopback. Start it:
`cd .claude/scripts && uv run python -m orchestration.run_api`.

**The bot joins but never speaks, or joins nothing at all.**
Check the invite's voice permissions: **Connect** and **Speak**. Missing them
fails at the Discord layer, not in this code.

**Every join silently attaches to a dead session.**
A zombie bridge squatting control port 7861 hijacks later joins. The sidecar
writes `.claude/scripts/discord_voice/bridge.pid`. Kill by that pidfile, then
re-join.

---

## Audio quality

**The bot "can't hear me": empty transcriptions, or fragments like "dla" / "mhm".**
Do not start by editing code. Dump the actual waveform:

```env
DISCORD_VOICE_DEBUG_PCM=/path/to/dump.pcm
```

Then listen to it. This is the single highest-yield debugging move, because most
"the model is broken" reports are "the audio reaching it is shredded."

- **Speech present but chopped mid-word** -> jitter. Raise
  `DISCORD_VOICE_JITTER_FRAMES` (default `3`, ~60 ms) so a late packet has a
  spare to cover it.
- **Quiet speech dropped entirely** -> the noise gate. Lower
  `DISCORD_VOICE_SILENCE_DBFS` (default `-35`) to pass quieter audio.
- **Noise, or transcriptions of phrases nobody said** -> frame-level corruption.
  Log per-frame length, opus TOC byte, sequence, and extension flag on both
  decode paths; random TOC bytes are the fingerprint of frames starting
  mid-buffer.

**Separate sentences get glued into one turn.**
The mic pump must emit one frame per frame-interval of *wall time* from a
monotonic clock. An event-driven pump that emits only when audio arrives makes
a 500 ms pause take seconds, so the server-side voice-activity detector never
sees the silence.

**Latency grows without recovering after a stall.**
That is the backlog. `DISCORD_VOICE_JITTER_SOFT_FRAMES` sheds down to priming
depth; `DISCORD_VOICE_JITTER_MAX_FRAMES` is the hard ceiling.

---

## Behavior

**The voice replies but doesn't know who I am.**
`TALK_IDENTITY_INCLUDE` **replaces** the default rather than extending it, so
`TALK_IDENTITY_INCLUDE=USER,MEMORY` sends **no SOUL**, so the behavioral contract
is gone. List `SOUL` explicitly: `SOUL,USER,MEMORY`.

**Session start raises on the voice name.**
`TALK_OPENAI_VOICE` must be one of: alloy, ash, ballad, coral, echo, sage,
shimmer, verse, marin, cedar. An unknown name raises rather than falling back.

**`run_python` / `run_shell` are refused.**
`TALK_ENABLE_CODE_EXEC` is a fail-closed opt-in and is off unless set.

**`computer` / `browse` are refused.**
`HOMIE_KILLSWITCH_COMPUTER_USE=disabled`.

**The conversation left no trace in memory.**
The debrief fires on session end: `/talk leave` in Discord, stop/pagehide on
the dashboard. Killing the process instead skips it. Note that with the voice
kill switch disabled, transcripts are parked `.disabled` rather than flushed
(privacy-first), so they never reach the vault.
