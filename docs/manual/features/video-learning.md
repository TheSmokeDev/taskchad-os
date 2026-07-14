# Video Learning (`/watch`)

## Status

Shipped and live-proven as a Homie-native, model-agnostic single-video learning lane. Verification covers extraction helpers, persistent jobs, command registration, read-only multimodal runtime controls, sourced-note behavior, exact-proposal approval gating, a real public-video run, and immediate recall of the resulting note.

## What it does

`/watch` turns one public video into durable, sourced operational knowledge:

1. validate a public `http(s)` source and explicitly disable playlist expansion;
2. read native/automatic captions when available;
3. fall back to Homie's existing speech-to-text cascade when captions are absent;
4. in `smart` mode, inspect a bounded set of frames only when transcript/title cues indicate charts, slides, screens, diagrams, or demonstrations matter;
5. synthesize the evidence through the active runtime lane;
6. compare it with recent conversation context and canonical Homie recall;
7. save a paraphrased note under `research/videos/` and index it for later recall;
8. offer a local-workspace application proposal, with a second approval required before edits.

This is a learning feature, not a content reposting or automatic external-action feature.

## Operator commands

```text
/watch <video-url> [question] [--detail smart|transcript|deep] [--no-save]
/watch status [job_id]
/watch retry <job_id>
/watch cancel <job_id>
/watch apply <job_id>
/watch approve <job_id> <exact-token>
```

The default is `smart`, saves a sourced note, and contextualizes the video against the current conversation. Initial scope is exactly one video; playlists and channels are out of scope.

`/watch apply` is read-only: it produces a bounded proposal and an approval token derived from that exact proposal. `/watch approve` accepts only the matching token. That approval covers local workspace edits only. Posts, messages, deploys, commits, pushes, purchases, browser writes, and other external effects retain their own gates.

Remote chat channels accept public URLs only. Local video paths are intentionally CLI-only so a chat message cannot request arbitrary machine-file access.

## Evidence and memory boundaries

- Video transcripts and metadata are untrusted source data, never model instructions.
- Transcript findings, visual findings, remembered context, and inference remain separately labeled.
- Speech-to-text fallback discloses when source timestamps are unavailable.
- Raw captions, audio, downloaded video, and frames live under the active profile's operational `DATA_DIR/video_learning/` and expire after seven days.
- Durable notes live under the active vault's `research/videos/`; full raw transcripts are not written there.
- Notes record source URL/type, video ID/title/channel, transcript source, ingest time, job ID, runtime lane, provider/model, and frame count.
- Indexing failure does not destroy the source-of-truth Markdown note; a later normal memory sync can index it.

## Runtime behavior

Text synthesis uses the lane-first runtime facade. Visual inspection uses an additive read-only request contract:

- Claude receives only the `Read` tool.
- Codex receives image attachments and a read-only sandbox.
- Gemini receives exact paths with only `read_file`, without `--yolo`.

This prevents a visual-analysis turn from inheriting write-capable tool authority. The final application turn is separate and runs only after exact approval.

The approved application turn is also narrower than ordinary tool reasoning: Claude receives only file read/edit tools, Codex uses `workspace-write` instead of `danger-full-access`, and Gemini uses `auto_edit` with an explicit file-tool allowlist instead of `--yolo`. The approval still does not authorize external effects.

Jobs persist as atomic JSON manifests and move through `queued`, `extracting`, `analyzing`, `saving`, and `ready`. A process restart marks an active job `interrupted`; it is never silently duplicated and can be retried explicitly.

## Source map

| Responsibility | Source |
|---|---|
| Command and native-menu registration | `.claude/chat/commands.py` |
| Cross-channel handler and buttons | `.claude/chat/core_handlers.py`, `.claude/chat/router.py` |
| Extraction and validation | `.claude/scripts/video_learning/extract.py` |
| Strategy and visual synthesis | `.claude/scripts/video_learning/analyze.py` |
| Jobs, notes, recall, apply gate | `.claude/scripts/video_learning/service.py`, `store.py` |
| Read-only multimodal contract | `.claude/scripts/runtime/base.py`, runtime adapters, `prompt_builder.py` |
| Codex and Claude wrappers | `.agents/skills/watch/`, `.claude/skills/watch/` |

## Dependencies and operations

The local lane requires `yt-dlp`, `ffmpeg`, and `ffprobe`. Caption-first videos may not need media conversion, but the command preflight keeps the full fallback chain deterministic. Use `/watch status` for stage detail and `/watch retry` after transient source/runtime failure.

The extraction approach was adapted from Brad Cassey's MIT-licensed `bradautomates/claude-video` project, pinned during implementation to commit `83da59fa78c3eee9e20f515fe75c438bb5166efd`. The Homie implementation is independent: it routes through Homie's lane selection, memory, channel, job, and approval systems rather than spawning a nested Claude process.

## Verification

```powershell
cd .claude/scripts
uv run pytest tests/test_video_learning.py tests/test_watch_command.py -q
uv run pytest tests/test_command_menu.py tests/test_prompt_builder.py tests/test_openai_codex_runtime.py tests/test_gemini_cli_runtime.py tests/test_lane_router.py -q
uv run thehomie chat -q "/watch status" -Q
```

Live acceptance is one public video URL that completes, writes a sourced note, and is later returned by `thehomie recall` using a unique phrase from the dossier. Application acceptance additionally proves that proposal generation changes no files and that only the matching proposal token reaches the local edit lane.

Latest live acceptance (2026-07-12): a 7:28 public AI-workspace strategy video completed through the generic runtime lane using automatic captions, wrote the sourced dossier, and `thehomie recall "artifact-native operator workbench" --mode hybrid` returned the new note as the top evidence family. Telegram then registered all 57 current native commands after the bot refresh. No application proposal was approved or applied during this smoke.

## Public export status

The engine, wrappers, tests, and this generic manual are public-framework safe. Export still runs private to public through `scripts/sanitize.py`; runtime data, raw media, job manifests, and vault notes remain excluded.
