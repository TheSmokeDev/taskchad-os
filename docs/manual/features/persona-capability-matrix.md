# Persona Capability Matrix

## Status

Active baseline for Discord persona channels and Cabinet participant turns.

## Operator Contract

Persona capabilities are delegated by lane. The master secret source remains
`.claude/scripts/.env`; named Homie profile `.env` files are derived artifacts,
not hand-maintained secret sheets.

The local matrix lives at:

```text
.claude/data/persona-capability-matrix.yaml
```

That file contains env key names and skill names only. It must not contain raw
secret values.

## Env Delegation

Env access is grouped by capability, such as:

- `runtime_core`
- `vault_memory`
- `browser_ops`
- `socials_write`
- `sales_ops`
- `customer_ops`
- `finance`
- `search_analytics`
- `mission_control`

Profile env files are generated from the master env with:

```bash
thehomie profile env-sync --all
thehomie profile env-sync --all --write
thehomie profile env-sync socials --json
```

Dry-run output prints key names only. `--write` materializes the derived
profile `.env` files.

## Skill Delegation

Central skills stay under `.claude/skills`. Personas receive a filtered skill
index from the matrix; profile-local `skills/` directories can still hold custom
persona skills.

Default profile remains broad/admin. Specialist personas should receive the
smallest skill set that matches their lane.

## Runtime Behavior

Discord persona channels and Cabinet participant turns build a scoped runtime
environment from the matrix. They preserve base OS process keys like `PATH` and
`USERPROFILE`, then add only the delegated env keys for the active persona.

Generated/unpromoted skills under `generated/` remain excluded from the prompt
index until the normal skill promotion rails approve them.

Persona capability scoping does not bypass the shared chat-router safety gate.
For Discord persona channels, the router still evaluates natural-language
external-action confirmation before dispatching into a lane persona. Pasted
research, website snippets, Google Maps listings, contact-form URLs, scheduling
links, and similar reference material should pass through to the persona as
chat context; direct requests to send, contact, book, post, publish, deploy, or
otherwise mutate live state still require the existing explicit authorization
path.

## Learning Opt-In

Persona learning (the scheduled belief-extraction loop) is controlled by a
per-profile `config.yaml` field, NOT the capability matrix:

```yaml
# <profile>/config.yaml
learning:
  enabled: true   # default: false
```

Enable via `thehomie profile learning enable <name>`. This is separate from
capability scoping — learning controls whether the persona forms beliefs from
its interactions, not which env keys or skills it can access.

See [Persona Learning Loop](persona-learning-loop.md) for full details.
