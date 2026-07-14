---
description: Guided LinkedIn post workshop with copy, image, and approval
argument-hint: "[cook <rough-idea>|run|cancel]"
---

# LinkedIn Workshop

You are handling the `/linkedin` command.

## User Arguments

`$ARGUMENTS`

## Job

Run the deterministic LinkedIn workshop owned by `core_handlers.handle_linkedin`.
This file remains as compatibility context for runtime surfaces that inspect
command docs; the slash command itself is router-owned.

## Supported Forms

- No arguments: show Cook Together and Run It for Me buttons.
- `cook`: ask for rough material.
- `cook <topic>` or any other text: generate a draft and image from that idea.
- `run`: choose a configured topic and generate the draft and image.
- `cancel`: clear the current workshop.

## Writing Rules

- Use a specific, natural voice, not a template.
- Avoid hashtags and engagement-bait CTAs.
- Include concrete details when the user provides them.
- If the topic needs facts the user did not provide, ask for context or state
  what assumptions you are making.
- Store revisions on the same editable queue row.

## Safety Boundary

Drafting and revision are local. The authenticated `Approve & Post` button is
the only outward-write authorization. It approves that exact queue row and
dispatches it through the visible-browser executor. One approval, one post.

## Live Status (read on demand)

You MAY read these vault files when the user asks who has been contacted, who to
target next, or for the live state of the outreach loop. Read them on demand;
do not assume their contents:

- `vault/memory/docs/LINKEDIN-OUTREACH-TRACKER.md` — live touched/pending log
  (never double-touch; golden-hour follow-up rule).
- `vault/memory/docs/LINKEDIN-NETWORK-TARGETS.md` — the static research target
  list per lane.
