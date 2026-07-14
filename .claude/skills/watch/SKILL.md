---
name: watch
description: Learn from a single YouTube or public video through The Homie's native video-learning engine. Use when the user asks Claude Code to watch, digest, study, summarize, extract strategy from, remember, compare, or apply lessons from one video URL.
---

# Watch

Delegate to the shared Homie engine. Do not implement a separate Claude-only extractor.

```powershell
uv run --project .claude/scripts thehomie chat -q "/watch <video-url> <optional question> --detail smart" -Q
```

- `smart`: transcript first; read bounded frames only when visual cues matter.
- `transcript`: captions/audio only.
- `deep`: bounded visual-frame inspection as well as transcript analysis.
- `--no-save`: skip the sourced vault note only when explicitly requested.

The engine analyzes one video, compares it with current/recalled context, keeps raw artifacts out of the vault, and saves a paraphrased dossier in `research/videos/` by default.

Application is two-step and default-deny: `/watch apply <job_id>` produces a local proposal without edits; `/watch approve <job_id> <exact-token>` applies only that exact local proposal. External actions remain separately gated.

Use `/watch status [job_id]`, `/watch retry <job_id>`, and `/watch cancel <job_id>` for operations.
