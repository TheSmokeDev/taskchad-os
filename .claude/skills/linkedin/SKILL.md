---
name: linkedin
description: Conversational LinkedIn operator for creating and publishing an image-backed post through Homie's approval queue. Use for /linkedin, "make a LinkedIn post", "cook a post with me", "run a LinkedIn post for me", LinkedIn post revisions, image revisions, engagement, connection requests, or LinkedIn growth strategy. For posts, offer Cook Together or Run It for Me and never publish until the operator approves the exact queued copy and image.
---

# LinkedIn

Use the framework-owned social queue and visible-browser writer. Keep operator
voice, brand files, account identity, and targets in runtime configuration or
private memory; never hard-code them into this public skill.

## Post workflow

1. Start the guided `/linkedin` workflow.
2. Offer exactly two creation modes:
   - **Cook Together**: ask for the rough idea, lesson, story, link, transcript,
     or bullets. Generate one draft plus image, then iterate on either.
   - **Run It for Me**: select a configured topic and generate one complete
     draft plus image without another ideation question.
3. Store every result as a normal `draft` queue row. Never bypass the queue.
4. Present the copy and image together. Support:
   - natural-language copy feedback;
   - `image: <direction>` or the Redo Image button;
   - Start Over or Reject;
   - Approve & Post.
5. Treat **Approve & Post** as the only publishing authorization. It approves
   that exact row and dispatches it through the configured channel executor.
6. Verify the platform receipt and return the permalink. A generated draft,
   browser click, or local success message alone is not proof of publication.

## Writing contract

- Preserve true specifics supplied by the operator.
- Never invent metrics, quotes, customers, results, credentials, or experience.
- Demonstrate expertise through evidence rather than labels.
- Avoid corporate filler, engagement bait, hashtags by default, and em/en
  dashes.
- Make the first two lines earn attention without using a canned hook.
- Keep the post as long as the idea requires and stop when it is done.

## Image contract

- Use the channel's configured design file and optional persona pack.
- Generate an image that supports the post rather than repeating the caption.
- Keep identity-sensitive references private and runtime-resolved.
- On revision, update the same editable queue row and show the new image before
  approval.
- If media generation fails, say so clearly; never imply an image is attached.

## Other LinkedIn lanes

- **Engage**: draft substantive comments; require approval before posting.
- **Connect**: one approval for one profile through `/linkedin_connect`; never
  batch invites.
- **Strategy**: use configured pillars and targets; keep all writes approval
  gated.

## Browser boundary

Use the dedicated visible Chrome/CDP session and the framework
`social_write_driver`. Do not use a headless browser, copied cookies, exported
tokens, or a fresh profile. Confirm the logged-in account and preserve the
one-approval, one-write boundary.

Read `docs/linkedin-automation-playbook.md` only when browser-operation details
or growth strategy are needed.
