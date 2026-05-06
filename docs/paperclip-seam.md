---
title: Paperclip Integration Seam
status: stable
audience: external integrators
tags: [public, contract, seam]
---

# Paperclip Integration Seam

This document is the contract between The Homie's CLI surface and any
downstream **Paperclip** integration. The Homie is provider-agnostic —
Paperclip is one of several optional execution backends (Local, Paperclip,
Workflow Runner). The seam is defined by:

1. The CLI **quiet-mode JSON envelope** that the framework emits.
2. The **exit-code table** the framework guarantees on CLI exit.
3. The shape of the future **`homie-paperclip-adapter`** Python package
   that consumes both.

No current Paperclip code lives inside The Homie core. This doc is the
contract a `homie-paperclip-adapter` will be built against.

---

## 1. Quiet JSON envelope schema

Returned on stdout (single line, no trailing newline beyond the JSON
itself) when the CLI is invoked with `-Q` / `--quiet`.

The envelope is built in two places that share the **exact same field
order**:

- Success / adapter-error path:
  `.claude/chat/adapters/cli_adapter.py:308-326`
  (see `CLIAdapter.format_final_output`)
- Pre-adapter exception path:
  `.claude/chat/adapters/cli_adapter.py:72-86`
  (see `build_quiet_error_envelope`)

The envelope is **always 11 fields on success**, **always 12 fields on
error** (the same 11 plus `error` appended last). Field insertion order
is the contract — Paperclip parsers can rely on `list(payload.keys())`.

### 1.1 Success / always-present fields (11)

| # | Field              | Type    | Always present | Source citation |
|---|--------------------|---------|----------------|-----------------|
| 1 | `success`          | bool    | yes            | `cli_adapter.py:309` — `not had_error`. `true` on a clean CLI exit, `false` when the adapter or pre-adapter path captured an error. |
| 2 | `response`         | string  | yes            | `cli_adapter.py:310` — final assistant text on success, empty string `""` when `had_error` is true (the human-readable error text moves to the `error` field instead). |
| 3 | `session_id`       | string  | yes            | `cli_adapter.py:311` — runtime session id assigned by the framework, or `""` if no session was created (e.g. pre-adapter failure). |
| 4 | `lane`             | string  | yes            | `cli_adapter.py:312` — runtime lane that produced the response (e.g. `"claude_native"`, `"openai_codex"`, `"openai_compatible"`). `""` on pre-adapter failure. |
| 5 | `provider`         | string  | yes            | `cli_adapter.py:313` — concrete provider id (e.g. `"anthropic"`, `"openai"`). `""` on pre-adapter failure. |
| 6 | `model`            | string  | yes            | `cli_adapter.py:314` — model id used for the final turn. `""` on pre-adapter failure. |
| 7 | `cost_usd`         | float   | yes            | `cli_adapter.py:315` — accumulated cost for the turn. `0.0` if unknown (NOT `null`). |
| 8 | `tool_calls`       | int     | yes            | `cli_adapter.py:316` — count of tool invocations during the turn. `0` if none. |
| 9 | `execution_time_ms`| int     | yes            | `cli_adapter.py:317` — `int((time.monotonic() - self._start_time) * 1000)`. Wall-clock from CLI parse to envelope emit. `0` on pre-adapter failure (`build_quiet_error_envelope` line `cli_adapter.py:81`). |
| 10| `profile`          | string  | yes            | `cli_adapter.py:320` — active persona/profile name resolved via `_resolve_active_profile_name()` (`cli_adapter.py:17-36`). Fail-OPEN to `"unknown"` (NOT `"default"`) so a resolution failure is NOT silently misfiled under the actual default profile. |
| 11| `source`           | string  | yes            | `cli_adapter.py:321-322` — source-tag for the invocation, normalized through `normalize_source` (`.claude/chat/session.py:24-37`). Always one of: `"interactive"`, `"tool"`, `"cron"`, `"hook"`. Unknown / typo'd values fail-OPEN to `"interactive"`. |

### 1.2 Conditional error field (12th)

| # | Field   | Type   | Present when                       | Source citation |
|---|---------|--------|------------------------------------|-----------------|
| 12| `error` | string | `success` is `false`               | `cli_adapter.py:324-325` (adapter-error sub-path) and `cli_adapter.py:84` (pre-adapter sub-path). Human-readable error string. The `response` field is `""` when this is set — the two are mutually exclusive carriers. |

`error` is **always last**. Both code paths emit it appended to the
locked 11, never interleaved.

### 1.3 Insertion-order contract

Tests assert `list(payload.keys()) == [...]` verbatim. The two emit
sites use literal dict construction in this exact sequence — that
sequence IS the contract. Do NOT reorder. From
`cli_adapter.py:308-323`:

```
success, response, session_id, lane, provider, model, cost_usd,
tool_calls, execution_time_ms, profile, source [, error]
```

---

## 2. Exit-code table

The Homie CLI guarantees these exit codes on the framework boundary.
Paperclip should branch on the exit code to distinguish "framework
broke" from "tool not installed" from "version drift".

| Exit | Meaning                                  | Raised from                           | Source citation |
|------|------------------------------------------|---------------------------------------|-----------------|
| 0    | Success                                  | normal CLI completion                 | `.claude/chat/cli.py:2238-2263` (success branch) |
| 1    | Generic runtime / lifecycle error        | `LifecycleError`, `ValueError`, `FileExistsError`, `FileNotFoundError`, `ArchonConfigShapeError` | `.claude/chat/cli.py:2270-2278`; exception class at `.claude/scripts/personas/archon.py:95-97` |
| 4    | Archon binary not installed / unparseable| `ArchonNotInstalledError`             | `.claude/chat/cli.py:2264-2266`; exception class at `.claude/scripts/personas/archon.py:86-87` |
| 7    | Archon version mismatch (drift detected) | `ArchonVersionMismatchError`          | `.claude/chat/cli.py:2267-2269`; exception class at `.claude/scripts/personas/archon.py:90-92` |

PRD §12.3 is the upstream owner of this table. The catch order at
`cli.py:2226-2278` is **subclass-first** — `ArchonNotInstalledError`
and `ArchonVersionMismatchError` are caught BEFORE the broader
`(ArchonError, LifecycleError, ...)` block, otherwise the broader
catch would swallow the specific exit-code mapping. Paperclip MUST
NOT depend on stderr text — exit codes are the contract; messages
are not.

Reserved / not yet allocated: 2, 3, 5, 6. Treat any other non-zero
exit as a bug in The Homie and surface it verbatim.

---

## 3. Worked example

A complete round-trip: Paperclip renders a ticket, calls into The
Homie's CLI surface, parses the quiet JSON envelope, and acts on it.

### 3.1 Paperclip command line

Paperclip invokes the CLI via the `thehomie chat` entry point with
`--source tool` (so the `source` field in the envelope reflects who
called) and `-Q` to enable the quiet JSON envelope:

```
thehomie chat --source tool -q "Summarize the latest deploy notes." -Q
```

`--source tool` flows through to `normalize_source`
(`.claude/chat/session.py:24-37`); any unknown value falls back to
`"interactive"` rather than crashing.

### 3.2 Successful response — stdout, single line

```json
{"success": true, "response": "Deploy 4.7 added the executor callback ingress and the conductor loop...", "session_id": "cli:tool:7f3a", "lane": "claude_native", "provider": "anthropic", "model": "claude-opus-4-7-1m", "cost_usd": 0.0123, "tool_calls": 0, "execution_time_ms": 4218, "profile": "default", "source": "tool"}
```

Process exit code: `0`.

### 3.3 Adapter-level error — stdout, single line

The same 11 fields, plus `error` appended last. `success` is `false`,
`response` is `""`:

```json
{"success": false, "response": "", "session_id": "cli:tool:7f3a", "lane": "claude_native", "provider": "anthropic", "model": "claude-opus-4-7-1m", "cost_usd": 0.0, "tool_calls": 0, "execution_time_ms": 1287, "profile": "default", "source": "tool", "error": "runtime: provider returned 503 after 3 retries"}
```

Process exit code: `1`.

### 3.4 Pre-adapter error — Archon not installed

When the failure is upstream of `format_final_output` (e.g. profile
resolution or Archon detection during a `--profile foo` invocation),
`build_quiet_error_envelope` (`cli_adapter.py:39-86`) emits a 12-field
envelope with all defaults set to JSON-clean fixed-type values:

```json
{"success": false, "response": "", "session_id": "", "lane": "", "provider": "", "model": "", "cost_usd": 0.0, "tool_calls": 0, "execution_time_ms": 0, "profile": "unknown", "source": "tool", "error": "archon binary not found on PATH"}
```

Process exit code: `4` (`ArchonNotInstalledError` per `cli.py:2264-2266`).

Paperclip can rely on:
- The envelope is parseable JSON.
- `payload["success"] is False` whenever exit != 0.
- `payload["error"]` is always a string when present.
- `cost_usd` is always a float (never `null`), `tool_calls` always int.

---

## 4. `homie-paperclip-adapter` future-package contract

This package does NOT yet exist in The Homie core. The seam exists so a
future `homie-paperclip-adapter` Python package can be authored against
a stable contract. The Homie commits to maintaining the surface below
across minor versions; breaking changes ship a new major.

### 4.1 Surface The Homie commits to

| Surface                                  | Location                                         | Stability |
|------------------------------------------|--------------------------------------------------|-----------|
| Quiet JSON envelope (Section 1)          | `.claude/chat/adapters/cli_adapter.py:287-326`   | stable across minor versions; new fields appended only |
| Pre-adapter envelope helper              | `build_quiet_error_envelope` at `cli_adapter.py:39-86` | stable; same 12-field shape as adapter-error path |
| Exit-code table (Section 2)              | `.claude/chat/cli.py:2219-2278`                  | stable; new codes are additive only |
| `source` enum                            | `SOURCE_VALUES` at `.claude/chat/session.py:20`  | stable; new sources appended only |
| Source normalization                     | `normalize_source` at `.claude/chat/session.py:24-37` | stable; fail-OPEN behavior is the contract |
| Optional `PaperclipExecutor` adapter (in-process, not via CLI) | `.claude/scripts/orchestration/executor.py:129-229` | unstable / stub — DO NOT consume yet; this seam is the wire-protocol contract while that adapter is wired |

### 4.2 Surface the future `homie-paperclip-adapter` package owns

The future package — a separate PyPI / git release, NOT vendored into
The Homie core — is responsible for:

- Spawning `thehomie chat ... -Q --source tool` as a subprocess.
- Reading exactly one line from stdout, parsing it as JSON.
- Mapping the framework exit code (Section 2) to a Paperclip-side error
  enum.
- Translating `payload["response"]` into Paperclip's render layer.
- Re-invoking the CLI on retry / cancel (no streaming back-channel
  exists yet — that is a future contract revision, not part of this
  seam).

The package MUST NOT:

- Reach into `~/.homie/profiles/` directly (use the CLI flags).
- Read or modify the framework SQLite (`.claude/data/chat.db`).
- Import any module under `.claude/scripts/` or `.claude/chat/`. The
  seam is the CLI process boundary, not a Python import boundary.

Quoted from the framework rules in `.claude/sections/01_architecture.md`:

> Reasoning execution now belongs behind `.claude/scripts/runtime/`, not in
> direct provider SDK calls spread across the codebase.
> ...
> Provider identity and auth method are separate concerns.

Paperclip is one consumer of the runtime surface — it consumes via the
CLI process boundary defined in this document, never by reaching past
it.

---

## 5. No-hard-dependency proof

The Homie core has zero direct Python imports of any `paperclip*`
package. Run from the repo root:

```
grep -rn "from paperclip\|import paperclip" .claude/scripts .claude/chat .claude/hooks --include='*.py'
```

Expected output: zero matches. As of the writing of this document, the
command produces no output at all (exit code 1 from grep is "no
matches found"). The only `paperclip` mention in core Python is the
`PaperclipExecutor` class string-name `"paperclip"` at
`.claude/scripts/orchestration/executor.py:158` and the docstring
`'local', 'paperclip'` at `executor.py:43` — neither is an import.
Both are stubs that fail closed when not configured (`is_configured`
returns `False`, `dispatch` returns a `rejected` receipt with error
`"Paperclip executor not configured"` — `executor.py:165-185`).

Re-run the grep before any release to catch a regression where someone
adds a real `import paperclip` to core.

---

## 6. Scope notes

- This document is part of the public surface and ships in the
  sanitized public mirror via the `INCLUDE_FILES` allowlist in
  `scripts/sanitize.py`.
- This document does NOT link to private files (PRDs, AGENTS.md,
  `vault/memory/...`, PRPs). When private context matters, the
  relevant text is quoted in-place above.
- The Mission Control / `mc-profile-contract.md` companion document is
  intentionally NOT shipped as part of this phase — Mission Control is
  being retired in favor of a frontend swap (PRD-8). When that lands,
  a new document will define the GUI-side contract; the wire format
  defined here is unchanged.
