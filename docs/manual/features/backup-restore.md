# Backup, Restore & Quick Snapshots

Status: shipped (Phase 3), default-deny restore
Owner: `.claude/scripts/backup_tool.py` + `.claude/chat/cli_backup.py`
Last updated: 2026-07-04

## What It Does

Operator-grade disaster recovery for The Homie's irreplaceable state. One
command produces a curated, portable zip of the vault + runtime DBs + state
dir (a Hermes v0.18 `hermes_cli/backup.py` port); a second restores it behind
an explicit confirmation; a third keeps a fast rolling ring of quick snapshots
of the live runtime DBs for instant rollback before risky operations (schema
changes, reindexes, bulk edits). Live SQLite DBs are copied WAL-safely via
`sqlite3.backup()` — never raw-copied — so a hot `chat.db` lands consistent.

## Operator Entry Points

| Command | What it does | Safety boundary |
|---|---|---|
| `thehomie backup [--out PATH] [--include-secrets] [--json]` | Zip of vault + DB dir + state dir to `~/thehomie-backup-<ts>.zip` (or `--out` file/dir). | Read-only over source state (copies out). Secrets EXCLUDED by default. |
| `thehomie restore <archive> --dry-run` | Prints the full per-entry plan (add / overwrite / db-swap / skip / blocked). | Mutates NOTHING. The safe default. |
| `thehomie restore <archive> --yes [--force] [--json]` | Restores onto the CURRENT profile's roots. | Default-denied without `--yes`. Refuses while the bot PID is alive. `--force` only skips the "target already has state" confirmation — never the liveness guard. |
| `thehomie snapshot create [--label L] [--keep N]` | Quick snapshot of the live runtime DBs + small state JSONs. | Read-only copy-out. Auto-prunes the ring to keep=20 (or `--keep`). |
| `thehomie snapshot list [--limit N] [--json]` | Lists snapshots, newest first. | Read-only. |
| `thehomie snapshot restore <id> --yes` | Rolls the runtime DBs back to a snapshot (atomic swap). | Default-denied without `--yes`. Refuses while the bot is alive. Snapshot id validated (no separators / traversal) before any disk access. |

## What's Included / Excluded

The backup walks CURATED, persona-resolved roots only — never
`config.PROJECT_ROOT` (no `.git`, `node_modules`, codebase, worktrees).
Arcnames use stable logical prefixes, forward-slash only, so an archive taken
on the default profile restores correctly on a named profile and vice-versa:

| Archive prefix | Source root | Contents |
|---|---|---|
| `vault/` | `config.MEMORY_DIR` | The Obsidian vault — the canonical memory substrate. |
| `data/` | `config.DATA_DIR` | Runtime SQLite DBs (`chat.db`, `orchestration.db`, `dashboard.db`, `memory.db`, per-vault indexes). |
| `state/` | `config.STATE_DIR` | Per-machine state JSONs. |
| `secrets/` | `config.ENV_FILE` | The profile `.env` — ONLY with `--include-secrets`. |

Always excluded: SQLite sidecars (`-wal`/`-shm`/`-journal` — the `.db` itself
is a consistent `sqlite3.backup()` snapshot), `.lock`/`.log`/`.bak`/`.pyc`,
`bot.pid`, the embedding model cache (`models/`), `state-snapshots/`,
`backups/`, `__pycache__`, `.git`, `node_modules`, venvs, and symlinks. The
in-progress zip skips itself if `--out` lands inside a walked root.

## Secrets Opt-In

`config.ENV_FILE` holds live credentials, so it ships OUT of the archive by
default — a routine `thehomie backup` is shareable with no credential leak.
`--include-secrets` opts in: the CLI prints a stderr warning up front, the
summary prints "store it securely", and restore applies a best-effort
`chmod 0600` on the landed `.env`.

## Restore Safety

- **Default-deny**: no `--yes` and no `--dry-run` → refusal with guidance,
  exit 1. `--dry-run` prints the plan and writes nothing.
- **Live-bot refusal**: before any write, `config.BOT_PID_FILE` is read and
  the PID liveness-checked (psutil, `os.kill(pid, 0)` fallback). A live bot →
  friendly "stop the bot first" refusal, exit 1. No flag bypasses this.
- **Traversal guard on every entry** (archives are untrusted once they leave
  the machine): absolute arcnames and `..` components are rejected outright,
  then each mapped target passes a `resolve().relative_to(root)` gate — this
  final gate is load-bearing on Windows, where `Path("C:/repo") / "C:/evil"`
  resolves to the absolute right-hand side.
- **Machine-local runtime names are never restored** (`bot.pid`, any
  `.lock`/`.pid` suffix); unknown prefixes are skipped, not guessed.
- **Atomic DB swap**: each `.db` extracts to a temp file in the target
  directory (same filesystem), then `os.replace` over the live path.
- **Existing-state confirmation**: when the restore would overwrite existing
  files, it prompts interactively; `--force` (or `--json` mode refusal) covers
  non-interactive runs.

## Quick Snapshot Rotation

Snapshots live under `config.STATE_DIR/state-snapshots/<utc-ts>[-label]/` with
a `manifest.json` (id, timestamp, files, sizes). The set is deliberately
SMALL and non-regenerable: `chat.db`, `orchestration.db`, `dashboard.db`, plus
the heartbeat/dream/reflection/weekly/flush state JSONs. `memory.db` is NOT
snapshotted (regenerable from the vault via `memory_index.py`; 35MB × 20 is
waste). Every `create` auto-prunes the ring to the newest 20 (override with
`--keep`). Labels are sanitized to `[A-Za-z0-9._-]` so an id can never carry
path separators.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Engine (port + adaptation) | `.claude/scripts/backup_tool.py` |
| CLI wiring | `.claude/chat/cli_backup.py` |
| Registration seam | `.claude/chat/cli.py` (`main.add_command`, the `cli_session` precedent) |
| Path source of truth | `.claude/scripts/config.py` (all roots read as attributes at call time) |
| Tests | `.claude/scripts/tests/test_backup_tool.py` |

## Config And Env

No new config knobs. Every path resolves through `config.*` attribute access
at call time (Rule 1), so profile swaps and `HOMIE_VAULT_DIR` overrides take
effect immediately: `MEMORY_DIR`, `DATA_DIR`, `STATE_DIR`, `ENV_FILE`,
`CHAT_DB_PATH`, `ORCHESTRATION_DB_PATH`, `DASHBOARD_DB_PATH`, `BOT_PID_FILE`.

## How To Run It

```powershell
cd .claude/scripts
uv run thehomie backup                                   # ~/thehomie-backup-<ts>.zip, no secrets
uv run thehomie backup --out E:\backups --include-secrets
uv run thehomie restore E:\backups\thehomie-backup-....zip --dry-run
uv run thehomie restore E:\backups\thehomie-backup-....zip --yes --force
uv run thehomie snapshot create --label pre-reindex
uv run thehomie snapshot list
uv run thehomie snapshot restore 20260704-181530-pre-reindex --yes
```

Stop the bot before any restore — a live `bot.pid` makes both restore paths
refuse by design.

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_backup_tool.py -q
```

## Public Export Status

This feature page is public-framework safe: the tool is clean by construction
(all paths via `config.*`, no secrets, no personal literals). Public export
must still go through `scripts/sanitize.py`.

## Next Slices

- A scheduled pre-update auto-backup hook (Hermes's `create_pre_update_backup`
  pattern) once an update flow exists to anchor it.
- A `/snapshot` chat command surface if operators want rollback from Telegram.
