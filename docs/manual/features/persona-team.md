# Persona Team (AI Employee Company)

Status: Active baseline — persona lifecycle, capability scoping, and learning all shipped
Owner: personas slice + chat adapters + memory pipelines
Last updated: 2026-07-04

## What It Does

The Homie runs as a **team of scoped AI employees**, not one assistant wearing
hats. Each persona is an independent Homie with its own brain, its own tools,
its own optional learning loop, and its own operator channel. You stand up a
department — sales, support, finance, outbound — where every member is scoped to
its lane, talks to you in its own channel, and gets sharper over time from its
own conversations.

This page is the operating model that ties the persona slice together. The five
layers below each have their own deep page; this is the map that says how they
compose into a company.

## The Anatomy Of A Persona

A persona is the sum of five layers, each owned by an existing slice:

| Layer | What it gives the persona | Deep page |
|---|---|---|
| **Identity (the brain)** | Its own `SOUL` / `USER` / `MEMORY` / `SELF` files under its profile root — who it is, what it believes, its boundaries | [persona-lifecycle-files](persona-lifecycle-files.md) |
| **Capabilities (tools + secrets)** | A lane-scoped `.env` and skill set delegated from the capability matrix — it can only touch its lane's keys | [persona-capability-matrix](persona-capability-matrix.md) |
| **Learning (getting sharper)** | Opt-in scheduled belief extraction from its OWN attributed turns — the compounding engine | [persona-learning-loop](persona-learning-loop.md) |
| **Channel (where you talk to it)** | A chat channel (e.g. a Discord channel) bound to the persona | [multi-channel-adapters](multi-channel-adapters.md) |
| **Collaboration (meetings)** | Multi-persona Cabinet rooms — standups, discussions, a roster that answers together | [cabinet-rooms](cabinet-rooms.md) |

The default profile stays broad/admin. Specialist personas get the *smallest*
capability set that matches their lane.

## The Lifecycle — Standing Up An Employee

1. **Create the profile** — clones the identity-file skeleton into a new
   profile root:
   ```bash
   thehomie profile create <name> --clone
   ```
2. **Give it a brain** — write its `SOUL.md` under the profile root: identity,
   what it does, the beliefs it holds, and its hard boundaries. This is the
   persona's character, not a prompt.
3. **Scope its capabilities** — add a matrix entry (`env_groups` +
   `skill_groups`) in `.claude/data/persona-capability-matrix.yaml`, then
   materialize its derived, scoped `.env`:
   ```bash
   thehomie profile env-sync <name> --write
   ```
4. **Bind a channel** — map a chat channel id to the persona (for Discord, an
   entry in `discord-channel-bindings.json`) so a message in that channel routes
   to that persona.
5. **Enable learning** (optional) — turn on the belief loop so it learns from
   its conversations:
   ```bash
   thehomie profile learning enable <name>
   ```
6. **It gets sharper** — the scheduled learning tick extracts beliefs from the
   persona's own attributed turns. A brand-new persona forms its first beliefs
   from day one (it does not need a history of daily logs first — see the
   no-logs first-run behavior in [persona-learning-loop](persona-learning-loop.md)).

## The Isolation Invariants (why a team is safe)

A team of employees is only safe if the members can't reach into each other's
drawers. Four invariants enforce that:

- **Capability isolation.** Each persona receives only its lane's env keys and
  skills; everything else is default-denied. A `finance` homie physically cannot
  see `socials_write` credentials — they were never materialized into its `.env`.
- **Belief isolation (the INPUT/OUTPUT split).** Personas *read* their attributed
  turns from the shared install `chat.db`, but *write* beliefs and episodes to
  their OWN profile root. One persona's learning never lands in another's brain,
  or in the operator's.
- **Provenance floor.** Every persona-sourced belief is forced to
  `source='reflection'`. A persona — or a prospect talking to a persona — can
  never mint a sacrosanct `explicit` belief. Only the operator's own verbatim
  turns can do that.
- **Safety gate is not bypassed.** Persona channels still run the router's
  external-action confirmation. Pasted research, listings, and links flow through
  as context; a request to send, post, book, or otherwise mutate live state still
  requires the existing explicit authorization. Discussing an action is never
  authorization to run it.

## Operator Entry Points

- **CLI:** `thehomie profile create|list|show|env-sync|learning`
- **Dashboard:** `/agents` (create, inspect, activate/deactivate, restart,
  file management)
- **Chat:** persona channels (e.g. Discord), `/cabinet` for multi-persona rooms

## Safety Boundaries

- The capability matrix (`persona-capability-matrix.yaml`) holds env **key names
  and skill names only** — never raw secret values.
- Named-profile `.env` files are **derived artifacts** of the master
  `.claude/scripts/.env`, not hand-maintained secret sheets. Re-derive with
  `env-sync`, never edit by hand.
- Learning is **opt-in per profile** (`config.yaml` `learning.enabled`, default
  `false`) and independent of capability scoping.
- Generated/unpromoted skills under `generated/` stay excluded from a persona's
  prompt index until the skill-promotion rails approve them.

## How To Run It

```powershell
cd <repo>\.claude\scripts
uv run thehomie profile create outbound --clone
uv run thehomie profile env-sync outbound --write
uv run thehomie profile learning enable outbound
uv run thehomie profile list
```

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_persona_learning_tick.py tests/test_persona_learning_isolation.py `
  tests/test_persona_reflection_provenance.py tests/test_corpus_persona_exclusion.py -q
```

Run the dashboard agent/component tests when the persona UI changes.

## Public Export Status

Framework core — this generic pattern is public. The operator's **actual roster**
(which specific personas exist, their channel IDs, and each one's playbook/soul
content) is private operator configuration and stays out of the public export.
Verify the public mirror before claiming current export state.

## Next Slices

- Per-persona voice configuration surfaced in the team view.
- A `thehomie profile` one-shot that scaffolds SOUL + matrix entry + channel
  binding together.
- Deeper Cabinet ↔ persona-team integration (roster changes reflected across
  both surfaces from one place).
