# PRP-001B: Local Amendment Rollback CLI

**Status:** implementation-ready after PRP-001A
**Scope:** local Click adapter only; one agent

## Goal and boundaries

Expose A's domain service to a local operator. Do not add domain logic, HTTP/auth, dashboard, or chat commands. Depend on the exported records/functions from A.

## Source anchors

- `.claude/chat/cli.py:41-45,89-92`: focused command imports and `main.add_command` registration.
- `.claude/chat/cli_backup.py`: Click output/confirmation precedent.
- `.claude/scripts/config.py::AMENDMENT_LEDGER_FILE` and `MEMORY_DIR`: resolve at command invocation, so test monkeypatches and active profiles work.
- `.claude/scripts/tests/test_cli.py`: `CliRunner` conventions.

## Contract

Create `.claude/chat/cli_amendments.py`, register group `amendments` in `cli.py`:

```text
thehomie amendments list [--proposal-id ID] [--json]
thehomie amendments rollback PROPOSAL_ID --actor ACTOR --reason TEXT [--yes] [--json]
```

`list` calls A's non-healing listing API and renders newest-first. Text output may show confined snapshot paths; JSON emits exactly one array to stdout. `rollback` refuses before domain invocation unless `--yes`; there is no force option. Pass actor/reason to A unchanged (A trims/validates); local CLI has no identity provider. JSON emits exactly one result object to stdout and diagnostics only to stderr.

Exit 0 only for `rollback_completed`, `rollback_reconciled_after_crash`, and idempotent already-restored success. Every refusal, conflict, lock timeout, or failed durability outcome exits 1. Do not duplicate reason-code interpretation beyond presentation/exit status.

## TDD tasks and acceptance

1. Add `.claude/scripts/tests/test_cli_amendments.py`; first fail tests for registration/help and call-time config resolution.
2. Add list text/JSON tests: empty list, filter, deterministic fields, stdout purity.
3. Add rollback tests: required options; no `--yes` means service mock not called; argument forwarding; no `--force`.
4. Parameterize every A reason code and prove exit status/output; ensure paths/content never appear in errors beyond fields returned by A.
5. Implement the smallest adapter and registration.

Acceptance evidence: named tests proving all above; existing CLI tests green; no live profile paths touched (all config points to `tmp_path`).

## Validation (repository root)

```bash
cd .claude/scripts
uv run --extra dev pytest tests/test_cli_amendments.py tests/test_cli.py -q
uv run --extra dev ruff check ../chat/cli_amendments.py ../chat/cli.py tests/test_cli_amendments.py
```

`--extra dev` is required because pytest/ruff are optional dev dependencies in `.claude/scripts/pyproject.toml`.

## Backout

Remove only `amendments` registration and `cli_amendments.py`; retain A's status recognition/domain model so existing `rollback_pending`/`rolled_back` rows cannot be coerced to `pending`. Stop CLI use before rollback. No ledger migration.
