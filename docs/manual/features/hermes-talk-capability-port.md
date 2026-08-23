# Hermes Talk Capability-Kernel Port

## Purpose

`hermes-talk` is a sibling
implementation of realtime voice orchestration for Hermes Agent. It is not a
TaskChad OS runtime adapter and its source is not vendored here. This page
captures the reusable capability-plugin lessons from the v1.7.0 kernel so a
Hermes plugin can adopt the same safety properties through Hermes-owned host
APIs.

The target is behavioral parity, not shared internals: strict declarations,
truthful readiness, least authority, transactional activation, durable
receipts, and fail-closed recovery.

## Current relationship

| Concern | TaskChad OS v1.7.0 | `hermes-talk` today |
|---|---|---|
| Host boundary | Lane router plus persona tool dispatcher | Hermes `register(ctx)`, `PluginContext`, and `dispatch_tool` |
| Manifest | Closed `manifestVersion: 2` capability manifest | Hermes `plugin.yaml` manifest v1 |
| Capability truth | Route-aware readiness and plugin snapshot | Bounded `talk_capabilities` catalog, in-process first then authenticated API server |
| Lifecycle | Transactional enable/load and disable/unload at turn boundaries | Hermes process-start plugin load; update requires gateway restart |
| Authority | Persona grants, tool scope, elevation, and write approval remain outside the plugin | Operator authority ledger and Hermes tool dispatch remain outside Talk |
| Recovery | Journaled intent/result receipts and deterministic restart recovery | Doctor/status receipts and honest old-code-until-restart behavior |

The table is a mapping, not an equivalence claim. In particular,
`hermes-talk` does not yet implement TaskChad OS's hot lifecycle kernel.

## Invariants to port

1. **Discover before import.** Parse a bounded, closed declaration before
   loading plugin code. Reject unknown keys, unsupported versions, path
   escapes, duplicate contributions, dependency cycles, and replacement
   ambiguity.
2. **Freeze what was accepted.** Validation and execution must use the same
   captured artifact bytes or an equivalent content-addressed package
   identity. A successful preflight over one file followed by execution of
   different bytes is not acceptance.
3. **Separate reach from authority.** A plugin may make a capability
   reachable, but it must never grant the persona or voice session permission
   to use it. Hermes tool policy, operator approval, and Talk's authority
   ledger stay authoritative.
4. **Publish atomically.** Build and validate a candidate snapshot away from
   active turns. Publish it only at a host-owned turn boundary. Readers see
   either the old complete snapshot or the new complete snapshot.
5. **Unload in reverse dependency order.** Every accepted contribution needs
   a disposer. A disposer failure must be visible and move the plugin to an
   honest `restart_required` state when cleanup cannot be proved.
6. **Journal transitions.** Persist bounded, redacted intent and result
   receipts around lifecycle changes. Recovery must distinguish committed,
   rolled-back, and uncertain transitions without replaying side effects.
7. **Keep operator truth lane-specific.** Registration, configuration,
   runtime activation, and live execution are separate receipts. Files on disk
   do not prove the running Hermes gateway loaded them.

## Hermes adaptation boundary

Port the contracts through Hermes surfaces instead of copying TaskChad OS
modules:

- Extend or layer beside Hermes `plugin.yaml`; do not teach Hermes to parse a
  YourProduct-specific file by filename alone.
- Resolve contributions through `PluginContext` and Hermes's own registries.
- Keep `talk_capabilities` read-only and bounded. Catalog presence is never an
  authorization decision.
- Treat gateway restart as the safe first lifecycle implementation. Hot reload
  should ship only after Hermes exposes a turn-boundary publication seam and
  disposer ownership can be proven.
- Keep Realtime session tools frozen for the life of a minted session. A
  plugin change becomes available on a new voice session unless the protocol
  explicitly supports a schema renegotiation receipt.

## Recommended delivery slices

1. **Contract and fixtures:** define a closed Hermes capability declaration,
   import-free parser, size/depth/path bounds, and hostile fixtures.
2. **Read-only discovery:** expose accepted/rejected candidates through doctor
   and JSON status without loading code.
3. **Cold activation:** load accepted contributions only at gateway start;
   prove registry ownership, authority separation, and restart truth.
4. **Receipts and recovery:** add redacted lifecycle receipts and deterministic
   handling of interrupted updates.
5. **Optional hot lifecycle:** only after Hermes owns a safe turn boundary,
   immutable snapshots, reverse disposal, and `restart_required` degradation.

## Acceptance gate

A Hermes port is not complete until tests discriminate these failures:

- malformed or oversized declarations never import code;
- path/symlink escapes and artifact drift are rejected;
- duplicate ownership and dependency cycles fail deterministically;
- a plugin cannot widen tool grants or bypass Talk write approval;
- concurrent reads never observe a partial registry;
- failed activation leaves the old snapshot active;
- failed disposal reports `restart_required` and does not claim unload;
- interrupted lifecycle receipts recover without replaying external actions;
- doctor distinguishes installed, enabled, loaded, running, and live-proven;
- public packaging excludes credentials, journals, logs, runtime state, and
  private manifests.

## References

- [Capability Plugin Kernel](capability-plugin-kernel.md) — canonical YourProduct
  OS v1.7.0 contract and verification commands.
- `hermes-talk` — Hermes voice plugin and its operating manual.
- `docs/CAPABILITY-KERNEL-PORT.md` in the `hermes-talk` repository — the
  Hermes-owned implementation checklist.
