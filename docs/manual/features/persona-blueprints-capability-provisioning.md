# Persona Blueprints And Capability Provisioning

Status: Compiler, atomic provisioner, preserve-first migration inventory,
creation-surface parity, explicit reviewed reconcile, and six-axis readiness
truth implemented. Live Discord tool-turn acceptance remains a separate
approval- and transport-gated operation.

## What It Does

A persona blueprint is the one-shot contract for creating a useful,
scoped Homie employee. It describes identity, domain capabilities, channel
intent, and scheduled authority. The compiler produces a reviewable plan
before any profile or shared routing state changes.

The framework distinguishes four capability classes:

| Class | Meaning |
| --- | --- |
| `safe-core` | Recall/search, skill reads, and private planning |
| `domain-pack` | Role-specific research, repository, and integration requirements |
| `operator-exec` | Explicit shell, process, broad file, and write authority |
| `scheduled-study` | Narrow model-only scheduled cognition with no interactive tools |

## Current Operator Truth

The pure compiler, capability-class split, and Python provisioning mechanism
exist. Provisioning is a two-step preview/apply contract:

1. `preview_provision(...)` reads current physical state, compiles the plan,
   and returns secret-free plan/state hashes.
2. `apply_provision(...)` re-reads under locks and refuses unless both hashes
   still match.

Apply stages a full new profile or reconciles only compiler-owned files,
updates Discord binding intent through a strict shared-store path, and emits
private receipts. A newly provisioned binding is written with `enabled: false`;
the runtime neither watches nor resolves it until a later, explicitly approved
activation flips that flag. Reconcile preserves an existing active binding.
Provisioning does not start bots, install schedulers, invoke a provider, or
take external action. CLI blueprint creation may create the ordinary local
wrapper alias around the atomic apply; service-manager installation is refused
during that transaction.

Every creation surface now uses the same Python-owned adapter:

- `thehomie profile create <name>` defaults to `general-specialist`, which
  compiles to non-empty `safe_core`;
- `thehomie profile blueprint list|show|plan|apply|readiness` exposes the
  catalog, preview/apply hashes, typed receipt, and physical provisioning
  state;
- `thehomie profile blueprint reconcile-plan|reconcile` exposes the existing
  provisioner's explicit reconcile mode without bypassing its locks, rollback,
  or receipts;
- `POST /api/agents/preview` returns the canonical plan plus plan/state hashes;
- `POST /api/agents` applies that exact preview and returns a typed
  provisioning receipt;
- `GET /api/agents/templates` returns the same catalog used by the CLI;
- the Hono route remains a transparent proxy, and the dashboard wizard sends
  display name, template, role, model, domain, and optional Discord channel
  intent instead of dropping them.

The dashboard wizard never grants `operator_exec`. CLI callers must supply the
explicit `--operator-exec` flag.

Existing profiles are inventoried through a separate read-only migration
analyzer. `migrate` never writes. Reconcile is a two-command CAS flow:

```powershell
thehomie profile blueprint reconcile-plan <persona> `
  --template <template> --channel <discord-channel-id> --json

thehomie profile blueprint reconcile <persona> `
  --template <template> --channel <discord-channel-id> `
  --preview-hash <reviewed-plan-sha256> `
  --state-hash <reviewed-state-sha256> `
  --approve-reconcile --json
```

The template, channel, both hashes, and approval flag are mandatory. Omitted
role/model fields preserve the strict physical config. Reconcile does not
restart a bot, install a scheduler, call a provider, or write to Discord. Run
production reconciliation from the canonical runtime checkout so the
checkout-owned capability matrix, master env, and Discord binding document are
the production stores.

Do not claim a persona is ready because a tool name appears in a toolset.
Readiness must distinguish:

- declared;
- transportable by the selected runtime lane;
- callable through a registered handler;
- configured with required integrations;
- channel-bound;
- scheduler-safe.

The read-only snapshot rebuilds those axes from current state on every call.
It checks the physical blueprint/config/profile and Discord binding, the live
tool registry and toolset owner, selected-lane adapter carriage, direct
integration and env-key presence, and the scheduled model-only request
contract. It never reads the provisioner's readiness receipt as truth and
never serializes credential values.

Each snapshot represents Discord, direct chat, Cabinet text, web, and
scheduled execution separately. Direct-chat readiness means the physical
profile exists and can be selected; the operator's transient active profile is
activity state and does not degrade other personas. Web remains represented
but is `NOT_APPLICABLE` to caller-tool capabilities while that runtime is
text-only. Scheduled study is reported as safe only when its registered
authority guard produces a real `model_only` request with zero tools, MCP
servers, hooks, or setting sources. Interactive tool and integration rows mark
both web and scheduled surfaces `NOT_APPLICABLE`, so those non-target surfaces
cannot make an otherwise ready capability permanently partial.

Selected-lane caller-tool transport is resolved once per inventory collection,
from the fixed runtime package root, through the lane router's public read-only
probe. Integration capability rows discover caller-tool wrappers from live
registry metadata and separately verify handler presence plus persona scope.

`thehomie status --json`, human status, and `thehomie doctor` retain the full
axis and surface vector. Doctor treats `PARTIAL`, `BLOCKED`, and `ERROR`
compiled profiles as attention; it does not turn a single passing axis into a
green persona.

## Compatibility

The old `core` toolset remains a compatibility alias for its prior wide grant,
including terminal and writes. Existing profiles are not silently narrowed.

New blueprints compile to `safe_core` and a domain pack. `operator_exec` is
opt-in. A migration preview preserves existing effective grants and shows the
recommended blueprint separately.

Persona-dispatched `memory_search` and `search_files` calls are bound to the
calling profile's `memory/` root for the duration of that handler call. The
registry marks these handlers as persona-scoped and the dispatcher injects the
calling persona ID under a reserved internal argument. A future private-state
handler that declares the marker but forgets to accept the identity fails
loudly instead of falling through to operator-global state. Non-persona callers
retain the existing default-vault behavior.

## Scheduled Safety

Interactive persona tools and scheduled curriculum tools are different
authority surfaces. Curriculum admission and study continue to use the strict
`model_only` runtime with no generic toolsets or tools. Enabling chat
capabilities must never widen scheduled study.

## Operator Surfaces

The consistent blueprint flow is:

- `thehomie profile blueprint list|show|plan|apply|readiness` is live;
- `thehomie profile blueprint reconcile-plan|reconcile` is live;
- profile creation flags for template, domain, channel, and explicit
  operator-exec are live;
- dashboard create templates and preview are live;
- typed API preview and apply receipts are live;
- status/doctor readiness details are live.

Reconciliation is not live-channel proof. A persona remains `PARTIAL` when its
selected lane cannot carry caller-supplied tools, a declared handler is absent,
or a required integration is unconfigured. A live Discord tool turn requires
separate runtime-write approval and must produce source, persona-attributed
audit, and runtime/session receipts before the channel is called accepted.

`profile blueprint readiness` delegates to the same physical six-axis snapshot
used by status and doctor. The narrower provisioning-state helper remains an
internal receipt diagnostic and is not presented as persona readiness.

## Security Boundaries

- Profile blueprints contain no secret values.
- Derived profile `.env` files remain owned by env-sync.
- Compiled profile env/skill groups are a profile-local overlay; legacy
  profiles continue to use the shared capability matrix unchanged.
- Domain packs cannot inherit operator-exec.
- Broad filesystem reads stay in operator-exec until they are persona-root
  confined.
- Outward actions keep their dedicated approval gates.
- Proposal authority creates internal proposals only.
- Private profile manifests, channel IDs, and readiness receipts never enter
  the public framework export.
- A persona may request one exact out-of-scope tool call through the
  [Persona Capability Elevation](persona-capability-elevation.md) gate. Approval
  never mutates the compiled blueprint or permanent profile scope.

## Verification

```powershell
cd .claude\scripts
uv run pytest tests/test_persona_blueprints.py tests/test_tool_registry.py `
  tests/test_persona_tool_assembly.py tests/test_persona_toolsets.py `
  tests/test_persona_provisioning.py `
  tests/test_persona_blueprint_migration.py `
  tests/test_persona_creation_surfaces.py `
  tests/test_persona_creation_api.py tests/test_persona_readiness.py `
  tests/test_persona_activation.py `
  tests/test_diagnostics.py tests/test_cli.py tests/test_curriculum.py -q
```

## Related Manuals

- [Persona Team](persona-team.md)
- [Persona Capability Matrix](persona-capability-matrix.md)
- [Persona Lifecycle And Files](persona-lifecycle-files.md)
- [Persona Curriculum Engine](persona-curriculum-engine.md)
- [Scheduled Jobs, Settings, And Audit](scheduled-settings-audit.md)
