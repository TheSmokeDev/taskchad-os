---
description: Normalize a raw video request into the canonical intake JSON for the video-production workflow.
argument-hint: <brief, URL, theme, and/or flags like --kind promo --aspect 9:16 --duration 20 --style blockframe>
---

# Video Intake

You are the intake step of the video-production workflow. Turn the raw
request below into ONE normalized intake object. Do not research, do not
write copy, do not render.

## Request

$ARGUMENTS

## Rules

- `brief`: the free-text idea with any flags and the URL stripped out. May be
  empty when the request is only a URL or flags.
- `kind`: one of `event | promo | launch | explainer | hype | surprise`, or
  empty. Map natural language ("recap", "teaser", "walkthrough", "hype",
  "launch") onto the closest kind; leave empty when genuinely unclear.
- `url`: the first http(s) URL in the request, trailing punctuation stripped,
  or empty.
- `duration`: integer seconds clamped 8..120 when the request states a length
  ("20 seconds", "two minute video"), else empty string.
- `aspect`: `16:9 | 9:16 | 1:1` when stated ("vertical" means 9:16,
  "square" means 1:1), else empty string.
- `style`: an explicit style name only when the request names one
  (`--style <name>` or "in the <name> style"), else empty string. Never
  invent a style here; ranking happens later.
- Never invent facts, numbers, or intent that is not in the request.

## Output

1. Write the object to `$ARTIFACTS_DIR/intake.json` (UTF-8, 2-space indent).
2. Your final output must be ONLY the same JSON object, nothing else:

```json
{"brief": "...", "kind": "...", "url": "...", "duration": "", "aspect": "", "style": ""}
```
