---
name: voice-transcribe
description: Transcribe inbound voice notes and audio messages to text before the chat engine processes them, so the agent can answer spoken messages. Use when a chat channel delivers an audio/voice attachment, or when the user asks the bot to handle voice notes.
version: 1.0.0
author: YourProduct OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  YourProduct:
    category: communication
    tags: [voice, transcription, stt, whisper, audio]
    related_skills: [whatsapp-bridge]
    mutates: false
---

# Voice Transcribe

Convert a voice note into text so the normal chat pipeline can handle it. This
runs as a pre-processing step on inbound audio, before routing.

## Pipeline

1. **Receive** — the channel adapter saves the audio attachment to a temp path.
2. **Transcribe** — run speech-to-text (preference order):
   - **OpenAI Whisper API** if `OPENAI_API_KEY` is set (fast, no local deps):
     ```bash
     curl -s https://api.openai.com/v1/audio/transcriptions \
       -H "Authorization: Bearer $OPENAI_API_KEY" \
       -F model=whisper-1 -F file=@note.ogg
     ```
   - **Local `faster-whisper`** if no key is configured and CPU/GPU allows.
3. **Inject** — replace the message body with the transcript, tag it
   `[voice]`, and hand it to the router as if the user had typed it.
4. **Clean up** — delete the temp audio file after transcription.

## Notes

- WhatsApp/Telegram voice notes are usually `.ogg` (Opus). Whisper accepts it
  directly; `faster-whisper` may need `ffmpeg` installed.
- Keep the original duration in metadata so the agent can say "got your 0:42
  voice note" when useful.
- Transcription is read-only — no capability gate needed. The *reply* still goes
  out through the normal gated `chat.send` path.
- Never persist raw audio beyond the transcription step unless the user opts in.
