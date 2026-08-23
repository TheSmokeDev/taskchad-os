# Capability Plugin Kernel

Status: Runtime foundation implemented; disabled fixture only, no production boot wiring
Owner: YourAgent
Last updated: 2026-08-22

## What It Does

The capability plugin kernel discovers trusted local v2 plugin manifests without importing
their code, rejects conflicts before effects can run, and applies enable or disable requests
as transactions at an explicit turn boundary. Each new turn receives an immutable generic
contribution snapshot. A snapshot already held by an in-flight turn remains intact after a
later boundary changes the live registry.

Issue #530 proves only the lifecycle and generic binding contract. It does not connect real
tools, toolsets, skills, MCP servers, commands, chat boot, profiles, Talk, Cabinet, or the
Dashboard to the kernel.

## Operator Entry Points

- Chat/Telegram: None in this slice; legacy command and intent extensions remain unchanged.
- CLI: None in this slice.
- Dashboard: None in this slice.
- API: Python callers may use `CapabilityPluginKernel`, but production boot does not create
  one yet.

## Manifest Versions

The package filename remains `extension.json`, but the marker is load-bearing:

- An unmarked manifest is a legacy v1 command/intent extension and stays on the existing
  chat-owned `ExtensionManager` path.
- A capability manifest has the integer `manifestVersion: 2` and is parsed as a strict,
  closed object. Missing, extra, or mistyped fields reject that candidate before import.
- A marked version other than 2 is invalid. It is not silently treated as legacy.

A v2 manifest contains:

| Field | Contract |
|---|---|
| `id` | Bounded lowercase dotted/kebab identifier |
| `name`, `version`, `description` | Bounded strings; assignment/URL credentials and high-confidence standalone Bearer, JWT, OpenAI, GitHub, Slack, Google, AWS, Stripe, and Hugging Face token shapes are rejected without echoing the value; version uses semantic-version syntax and receives the same credential scan |
| `source` | `bundled`, `operator_global`, `project`, or `python_entry_point`; must match the physical source |
| `entrypoint` | Relative `module:function`; traversal, absolute paths, drive prefixes, empty functions, and ambiguous file-plus-package targets are denied |
| `requirements` | Closed `coreVersion`, environment variable names, and plugin dependency IDs; never values |
| `contributions` | Nonempty closed declarations with unique `id`, typed `type`, and acyclic `dependsOn` edges; dependency ordering is iterative so a bounded but deep manifest cannot escape as `RecursionError` |
| `enabledByDefault` | Boolean desired-state default |
| `replaces` | `null` or exact prior plugin ID, `contractVersion`, and canonical `contractFingerprint` |
| `contractVersion` | Positive integer included in the canonical contribution-contract fingerprint |
| `export` | `public` or `private`; private manifests are omitted from public catalog serialization |

## Trusted Discovery And Conflicts

Discovery precedence is deterministic:

1. bundled framework paths;
2. operator-global paths following the existing Homie extension convention;
3. project-local paths only when the caller explicitly opts in;
4. locally installed Python entry points in `thehomie.capability_plugins`, only when their
   exact entry-point name is approved.

Filesystem discovery reads `extension.json` plus bounded Python source bytes without executing
them. Python entry-point discovery reads `thehomie-capability.json` and the approved
distribution's bounded Python artifacts through package metadata; it never calls
`EntryPoint.load()`. Each source file is capped at 1 MiB, each candidate at 256 files and 8 MiB
total. Import happens only after an accepted enable request reaches a turn boundary. Hostile
installed-metadata access is isolated per candidate without terminating discovery or poisoning
unrelated approved entry points.

All candidates are collected before lifecycle work. A later source cannot silently shadow a
prior plugin or contribution. The later candidate is isolated as failed unless it explicitly
names the exact active plugin in `replaces` and both candidates match the declared canonical
SHA-256 fingerprint over contract version, contribution IDs/types/dependency edges, and
plugin-dependency edges. Catalog activity is keyed by path-redacted candidate provenance, so
same-ID replacement records cannot both appear active. Unrelated valid candidates remain
available.

## Desired State, Effective State, And Boundaries

Enable and disable requests first append a durable typed request receipt. Only after that
append succeeds does desired state change or a boundary operation enter the queue.

- Enable request: desired `enabled`; effective state remains `unloaded` until the next
  `apply_turn_boundary()`.
- Successful load boundary: import under a plugin-scoped module name, stage every declared
  registration, require exact declared/registered set equality, atomically publish, then
  advance the monotonic registry generation. The pending command is claimed before boundary
  effects begin, so an interrupt after terminal success cannot replay the same load.
- Plugin-controlled import, `register`, and disposer callbacks execute outside the registry
  lock under a kernel transaction token and epoch. Lifecycle mutation is explicitly refused
  while that transaction is active; state inspection and snapshots remain nonblocking.
- Plugin `SystemExit` and other plugin-controlled `BaseException` failures are isolated into
  truthful `failed` or `restart_required` terminals. An operator `KeyboardInterrupt` is
  propagated after local authority is quarantined; interrupted and unattempted disposers remain
  tracked as residual state, pending work is retired, and cleanup is never retried in-process.
  Import-time interruption remains cancellation even when namespace cleanup separately fails;
  cleanup uncertainty changes the state to restart-required but never swallows Ctrl+C.
- Before import, the kernel semantically evaluates `coreVersion`, verifies every required
  environment name has a nonempty value, and verifies loaded plugin dependencies. Failures
  emit value-free receipts and execute zero plugin code.
- Disable request: desired `disabled`; effective state and current snapshots remain loaded
  until the next boundary.
- A dependency disable is refused without changing desired state while any dependent remains
  loaded, draining, restart-required, registered, loader-owned, or otherwise physically
  undrained. Disable and fully drain dependents first.
- Successful unload boundary: remove new-turn authority and advance the generation once. If
  an older turn lease exists, state becomes `draining`; physical disposal and module cleanup
  wait until the last affected lease releases. Disposers then run in reverse dependency
  order and plugin-owned modules clear only after every disposer returns exactly `True`.

Repeated equivalent requests append typed `no_op` receipts. They do not advance the
generation, republish bindings, or call a disposer twice.

## Transaction And Snapshot Guarantees

`register(registrar)` receives a manifest-scoped staged registrar. `publish(...)` accepts only
a declared contribution ID, the generic value or synchronous callable, a synchronous
disposer, and the exact dependency edges declared by that manifest. Detectable coroutine,
generator, and async-generator contribution or disposer functions reject before publication.
Nothing is visible until all declarations are registered exactly once.

If import or registration fails, staged registrations are disposed in reverse dependency
order and no binding is published. A failed rollback becomes `restart_required` because the
kernel cannot prove zero residual physical effects.

`snapshot()` acquires a kernel-owned, uniquely identified turn lease and freezes the current
generation, plugin ID/version vector, and contribution mapping. Use it as a context manager or
call `close()` when the turn ends; repeated closes return the completed attempt's same receipts.
`snapshot.resolve(<id>)` and
`snapshot.contributions[<id>]` return a lease-bound `ContributionHandle`, never the raw
registered value. Callable contributions preserve ergonomic `handle(...)` invocation. Each
invocation validates the open lease and holds an active execution reference; snapshot close
rejects new calls and waits for active calls before releasing the plugin lease. A cached handle
therefore cannot run after close. Closing from the same thread's active contribution fails
immediately instead of self-deadlocking. Release is an explicit `open -> closing -> closed` or
`release_failed` state machine: concurrent close callers share the same attempt result, a
failed release remains revoked and visible in lease diagnostics, and a later explicit close
can retry without restoring contribution authority. Kernel lease accounting and completed
receipts are accumulated and returned idempotently across that retry, including when one plugin
completed before a later plugin interrupted the first release attempt. If another lifecycle
transaction is active, close returns `snapshot_release_deferred` while retaining the revoked
lease; an explicit retry performs every newly ready drain instead of stranding zero-lease state.
Outcome publication retries through repeated operator interrupts and propagates the first
interrupt only after waiters can observe a terminal release state.

Every callable result is detached while the invocation reference is still active. Only exact,
closed built-in inert values can return, with containers copied into immutable forms. Returned
coroutines, generators, async generators, callables, and arbitrary custom objects are rejected;
deferred values are closed when synchronously feasible and never escape to the caller. Hostile
metadata inspection on callable results or non-callable reads becomes the same stable
non-inert-result failure.
Contribution exceptions, including `SystemExit`, become a stable value-free execution failure;
raw plugin exceptions are not retained as `__cause__` or `__context__`, hostile exception
`__str__` and type-name access are replaced with fixed labels, and an operator
`KeyboardInterrupt` remains interruptible.
Non-callable data is read with `handle.read()` under the same inert-value rules. This is an API
authority boundary, not a claim that arbitrary Python objects can be made revocable against
hostile private-attribute introspection.

A disable boundary immediately gives new turns a binding-free generation, but prior-turn
handles, disposers, and modules remain intact until their leases and active invocations drain.
`outstanding_leases()` reports lease ID, generation, plugin IDs, active invocation count,
closing flag, release state/error code, and creation time. `kernel.close(timeout=...)` waits only
for outstanding leases and otherwise fails closed: it retains journal ownership if a lease was
forgotten or loaded, draining, residual, or restart-required physical state remains. Callers
must explicitly disable and drain healthy plugins first; `close()` never silently runs plugin
callbacks. Pending enable/disable, loading, unload-requested, degraded, and every other
non-quiescent lifecycle state also refuse close even when no registration is currently visible.
An interrupted lease wait restores the open kernel state before propagating.
Journal shutdown is a two-phase boundary: the kernel becomes closed only after journal close is
proven. The journal releases ownership by closing the lock-holding descriptor directly; it never
unlocks first. If descriptor close raises, its ownership outcome is unknowable, so the journal
and kernel become permanently fail-closed before the interrupt/error propagates. This prevents
the old kernel from accepting work after another process acquires the released OS lock.

Every filesystem or approved Python-entry-point plugin loads below a unique kernel-owned package
namespace. Dotted entrypoints and package-relative imports work, but lifecycle execution reads
only the immutable source bytes captured during import-free discovery; replacing `plugin.py`
after discovery cannot change the authorized code. The artifact-set SHA-256 participates in
candidate provenance, so recovery rejects a pending claim when any captured source byte or path
changes. A kernel-owned frozen-artifact finder remains installed only for the plugin lifetime,
and plugin-local builtins redirect static, aliased, and `importlib.import_module()` absolute
self-package imports into that same frozen namespace. Preloaded live modules under the original
package name remain untouched and cannot satisfy plugin self-imports. Cleanup then removes the
finder, the root, and every owned submodule after leases drain. `EntryPoint.load()` and live
package files are never execution authority.

## Durable Lifecycle Receipts

The default sink preserves compact logical JSONL at
`config.DATA_DIR/capability_plugin_lifecycle.jsonl`, resolving `config.DATA_DIR` at first use.
Injected relative journal paths are normalized to absolute paths before ownership is acquired,
so later working-directory changes cannot redirect reads or writes away from the locked file.
Because physical plugin state is process-local, one kernel exclusively owns that journal for
its lifetime. Ownership uses a dedicated non-truncating lock file and a byte-0 OS lock, with an
additional in-process owner guard; a second kernel fails before it can recover or execute work.
Interrupted owner acquisition releases the in-process guard before handle cleanup, so even an
interrupted handle close cannot retain a stale owner. Operator interruption dominates a separate
non-interrupting acquisition or initial-recovery cleanup failure and propagates as a fresh,
argument-free interrupt. Explicit clean `kernel.close()` releases
the owner, and the OS releases it at process exit.

Updates read the committed record set, require event IDs contiguous from 1, allocate the next
event ID, write the full
next JSONL image to a same-directory temporary file, flush and `fsync` it, atomically replace
the journal, and sync the parent directory where supported. After any replace-path exception,
the owner re-reads and compares the exact old and expected images: a visible old image reports
failure, while a visible expected image still has unproven directory durability and any
partial/unknown image is also ambiguous. Both ambiguous cases quarantine the plugin as
restart-required so stale pending work cannot execute. `KeyboardInterrupt` follows the same
image reconciliation, quarantines ambiguous authority, and only then propagates. Temporary-file
and parent-directory descriptor cleanup cannot overwrite that interruption: a fresh,
argument-free interrupt wins over a separate close failure, and a visible replacement retains
the interrupted ambiguity marker. Temporary files are opened as file objects in one exclusive
operation, eliminating the raw-descriptor-to-file-object double-close window. Any
unterminated tail is corruption because it could be a truncated terminal receipt; such tails,
committed blank frames, malformed frames, and event-ID gaps all fail closed. Bounded ticket scale
makes this O(n) rewrite acceptable. Tests and future callers can
inject a path, clock, command ID source, or receipt writer.

Every receipt includes schema version, stable idempotent `command_id`, unique monotonic
`event_id`, request/progress/terminal phase, command and event transition, plugin/version/
source plus a path-redacted full-candidate provenance fingerprint covering the complete manifest,
location, artifact paths, and executable-byte hashes, typed event,
desired/effective/lifecycle state, generation before and after, declared
contribution IDs, outcome, `restart_required`, UTC timestamp, bounded redacted detail, and
per-contribution disposal outcomes. Environment requirements serialize names only.
Recovery validates every committed frame before filtering and accepts only that exact closed
schema, including the journal-owner envelope, with exact strings, integers, booleans, lists,
aware timestamps, bounded redacted detail, valid enums, consistent event/transition/state
semantics, stable per-command identity, and an owner-scoped request/progress/terminal state
machine that forbids records after that owner's terminal. An incomplete request can become
authority only when its complete candidate provenance exactly matches the active manifest and
source location and captured executable artifacts; entrypoint, requirements, contribution
types/dependencies, source paths, code bytes, or other manifest changes therefore invalidate the
old claim. Recovery never coerces, skips, or adopts malformed or stale records. Generated records
are validated before commit, and an unexpected post-commit parse failure revokes pending
authority and quarantines the plugin. Invalid receipt values are discarded before the kernel
raises its stable persistence error, so secret-bearing enum input cannot remain attached through
an exception cause or context.
Credential assignments, URL query credentials, high-confidence standalone credential shapes,
and every nonempty manifest-required environment value are redacted from failure detail;
exact secret bytes are removed before control-character normalization and overlapping values
are removed longest-first so no credential fragment survives. Secret-shaped plugin,
contribution, dependency, replacement, command, journal-owner, and historical receipt identifiers
are rejected before catalog or receipt persistence. Hostile manifest enum and JSON conversion
failures are discarded before the stable error is raised, so raw values cannot remain in an
exception cause or context.
Machine `detail_code` and instance `error_code` values use a bounded lowercase identifier and
fall back to `plugin_error` whenever plugin-controlled input is invalid or secret-bearing.
Successful/request receipts preserve an empty `detail_code` when no diagnostic condition exists.
Only the exact host-owned lifecycle-error type can supply a machine code; hostile subclasses
fall back to a fixed code and still enter cleanup. Propagated operator interrupts are fresh,
argument-free exceptions with no retained plugin cause, context, or traceback-local exception
object; lifecycle cleanup and journal reconciliation retain interruption booleans only.

Idempotent replay is scoped to the current journal-owner lifetime, so a terminal written by a
prior process can never replace the new kernel's local physical outcome. Reusing a command ID
for another plugin or transition remains rejected across history. On kernel reconstruction,
an accepted request without a later terminal is adopted by a new owner request record, restored
into desired/pending state, and completed at the next boundary; completed prior-owner commands
are historical and require a new local request/execution claim even when the command ID is
retried. Recovery is marked complete only after every parse, supersession, adoption, and
terminal write succeeds; a failed recovery attempt remains retryable and cannot be skipped.

Replacing pending work writes the old command's `superseded` terminal and the new request in a
single atomic journal image. A confirmed old image leaves the old in-memory operation
authoritative. A visible new image after any replace-path exception still has unproven directory
durability, so it and every partial/unknown image revoke pending authority and require restart.
Both batch records are validated before commit, and the replacement request carries a durable
supersession marker. A boundary-free cancellation that is already physically satisfied writes
its success terminal immediately. If a marked replacement nevertheless remains incomplete at
recovery, the successor terminalizes every incomplete claim for that plugin as restart-required
and never adopts older or later load authority. The recovered fence terminal remains a durable
historical marker, so a crash between individual terminal writes cannot restore later authority
on another restart. Historical fence authority is event-ordered: it covers incomplete claims
that predate the fence, while a fully terminalized fence cannot poison an independent command
accepted in a later process epoch.
Injected single-receipt writers cannot prove this transaction and therefore refuse
pending-command supersession.

A receipt write failure never returns a successful claim. Before effects, the request fails
and prior physical state is preserved. After effects ran, the operation is
`restart_required`; new snapshots advertise no authority.

## Restart-Required Remediation

`restart_required` means the process cannot prove both physical disposal and durable
lifecycle accounting. Do not claim the plugin is unloaded and do not re-enable it in the same
process. New snapshots already exclude its authority. When production wiring arrives, the
operator must perform a controlled framework restart, reconstruct effective state from
validated manifests, and verify a fresh snapshot before accepting new turns.

## Reversible Bundled Fixture

`.claude/extensions/_capability_fixture` is public-safe and disabled by default. It performs
no network, filesystem, subprocess, credential, browser, provider, or external action. It
publishes two generic in-memory callables; the dependent contribution declares the base
contribution as a dependency. The focused integration test proves:

1. discovery does not import `plugin.py`;
2. enable receipt precedes boundary load;
3. both bindings publish atomically and return deterministic values;
4. a held snapshot remains callable;
5. disable receipt does not mutate that snapshot;
6. the disable boundary advertises zero new-turn authority without disposing the held turn;
7. releasing the lease disposes dependent before base and clears the owned namespace;
8. a new snapshot contains zero fixture bindings; and
9. request, terminal, and draining events exist with monotonic IDs in the injected JSONL file.

The legacy `ExtensionManager` sees the same manifest as disabled metadata with zero commands
or intents, so existing v1 packages retain their current behavior.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Manifest/discovery | `.claude/scripts/runtime/capability_plugin_manifest.py` |
| Lifecycle/snapshots | `.claude/scripts/runtime/capability_plugins.py` |
| Locked receipt journal | `.claude/scripts/runtime/capability_plugin_journal.py` |
| Bundled proof | `.claude/extensions/_capability_fixture/extension.json`, `plugin.py` |
| Tests | `.claude/scripts/tests/test_capability_plugin_manifest.py`, `test_capability_plugins.py` |
| Subprocess lock proof | `.claude/scripts/tests/_holders/hold_capability_journal_owner.py` |
| Legacy compatibility | `.claude/chat/extension_manager.py`, `.claude/scripts/local_extension_loader.py` |

## Safety Boundaries

- Trusted discovery is local and import-free. Remote installation, marketplace discovery,
  signed-package policy, and remote code execution are excluded.
- Manifest and error data are hostile, bounded, and redacted. Secret values must never appear
  in manifests, catalogs, receipts, logs, commits, or public exports.
- Registration is generic in this slice. Real contribution adapters and domain registry
  mutation belong to issue #531.
- Profile equipment/epochs, progressive disclosure, migrations, CLI/API/Dashboard controls,
  production activation, and sanitizer export are later slices.
- Plugin reach never replaces execution authorization or any existing gate.

## How To Test It

From `.claude/scripts`:

```powershell
python -m pytest tests/test_capability_plugin_manifest.py -q
python -m pytest tests/test_capability_plugins.py -q
python -m pytest tests/test_capability_plugin_manifest.py `
  tests/test_capability_plugins.py tests/test_extension_manager.py `
  tests/test_local_extension_loader.py tests/test_tool_registry.py -q
```

Use `uv run` instead of direct `python` in an environment where the repository UV cache is
available. Tests inject temporary receipt paths and do not write operator lifecycle state.

## Rollback

Revert the issue #530 implementation commits. There is no data migration, database schema,
profile mutation, or production activation to undo. Existing legacy extensions remain on the
chat manager throughout. Historical JSONL receipts are audit records and should not be
silently deleted as part of code rollback.

## Latest Proof

- Date: 2026-08-22
- Surface: isolated worktree unit and fixture integration suites
- Result: 177 focused tests and 272 complete #530/legacy tests passed after the branch was
  refreshed onto current `master`. The direct #529 transport/readiness files passed 88 tests
  with one intentional skip, and the mandated PR2 runtime suite passed all 232 tests. The
  earlier persona activation/diagnostics/CLI gate also passed all 97 tests. Ruff, Python 3.12
  compilation, and diff checks passed. This is code proof only; no bot restart, provider call,
  deploy, or production/operator mutation is implied.

## Public Export Status

The approved public release target is TaskChad OS `v1.7.0`. The private repository remains the
source of truth and `scripts/sanitize.py` is the only supported export path. Until the public
commit and pushed `v1.7.0` tag exist, treat this as release metadata rather than publication
proof. The disabled fixture remains inert after export; the release does not activate a plugin
or add production boot wiring.

## Next Slices

- Issue #531: typed adapters for real tool, toolset, skill, MCP, and command contributions.
- Later epic slices: profile equipment/epochs, disclosure, representative migrations,
  operator controls, production activation, and additional release hardening.
