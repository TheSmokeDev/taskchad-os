# LinkedIn On-The-Fly Workshop

Status: shipped, queue-backed, operator-gated

Owner: chat router (`.claude/chat/`) plus social runtime
(`.claude/scripts/social/`)

## What It Does

`/linkedin` creates an image-backed LinkedIn draft through a guided workflow.
It offers two entry modes:

- **Cook Together** asks for rough source material, creates the first post and
  image, and accepts iterative copy or image direction.
- **Run It for Me** selects a configured topic and creates the complete first
  post and image without another ideation question.

Both modes create a normal `draft` row in `social_post_queue`. Neither mode
publishes. The authenticated **Approve & Post** button approves that exact row
and dispatches it through the existing social executor.

## Operator Flow

```text
/linkedin
  -> Cook Together | Run It for Me
  -> generate queue draft + image
  -> preview copy + image
  -> revise copy | image: <direction> | Redo Image | Start Over | Reject
  -> Approve & Post
  -> social queue approval
  -> configured LinkedIn executor
  -> platform receipt + permalink
```

Typed fallbacks work on buttonless surfaces:

```text
/linkedin cook
<rough idea>

/linkedin cook What I learned repairing a real browser workflow
/linkedin run
/linkedin cancel
```

While a preview is active, a plain reply revises the copy. Prefix visual
feedback with `image:`:

```text
Make the opening more direct and remove the last paragraph.
image: dark editorial control room with one strong focal point
```

## Ownership

| Layer | Owner | Responsibility |
|---|---|---|
| Command and workshop state | `.claude/chat/core_handlers.py` | Mode picker, 15-minute state, preview cards, typed feedback |
| Button routing | `.claude/chat/router.py` | Routes `linkedin_flow:*`; preserves `social:*` as the publish gate |
| Draft operations | `.claude/scripts/social/linkedin_workshop.py` | Create, revise, and regenerate the same editable queue row |
| Copy and media generation | `.claude/scripts/social/content_factory.py` | Voice-aware caption plus configured design/persona image |
| Approval and dispatch | `.claude/scripts/social/service.py`, `post_executor.py` | State transition, integration policy, visible-browser/API write |
| Skill guidance | `.claude/skills/linkedin/SKILL.md` | Reusable model-facing workflow without tenant identity |

## Safety Contract

- Draft and media generation are local operations.
- Only a genuine adapter button event can invoke workshop buttons.
- Only **Approve & Post** can authorize publication.
- The approved row cannot be revised after it leaves `draft` status.
- Revisions must not invent metrics, customers, quotes, results, or experience.
- Brand files, persona references, target accounts, and logged-in identity stay
  runtime-resolved and private.
- LinkedIn writes use the configured visible Chrome/CDP session. No headless
  fallback, copied browser profile, exported cookies, or batch approval.

## Configuration

The workshop reads the standard LinkedIn channel configuration:

```yaml
linkedin:
  voice_profile: your-voice
  design_file: brand_designs/YourBrand.json
  persona_pack: ""
  topic_pool:
    - a real lesson from building the product
```

The generic framework ships without a personal face pack. Operators can add a
private persona pack at runtime; it must not be embedded into the public skill.

## Verification

```powershell
cd .claude/scripts
uv run pytest tests/test_core_handlers_linkedin.py tests/test_linkedin_workshop.py -q
uv run pytest tests/test_social_button_routing.py tests/test_social_pipeline.py -q
uv run thehomie chat -q "/linkedin" -Q
```

For a live Telegram proof, restart the bot so the native command registry and
router code reload, invoke `/linkedin`, generate a disposable draft, and stop
before **Approve & Post** unless a real publication is intended.

## Latest Live Proof

- Date: 2026-07-11
- Surface: Telegram Web through the persistent visible CDP browser
- Result: `/linkedin` rendered **Cook Together**, **Run It for Me**, and
  **Cancel**; Cook Together opened the rough-idea prompt; a typed `cancel`
  cleared the workshop and returned `LinkedIn workshop cancelled.`
- Write boundary: no draft was generated and no external write ran during this
  menu-routing proof.

## Public Export

This page and the implementation are framework-safe. They describe mechanism
only. Tenant voice files, design tokens, persona images, account identity,
queue rows, generated media, and browser state remain private.
