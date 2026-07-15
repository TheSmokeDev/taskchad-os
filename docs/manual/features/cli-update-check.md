# Safe Framework Updates

Status: Active baseline
Owner: runtime-chat
Last updated: 2026-07-15

## What It Does

The Homie polls the latest stable `taskchad-os` GitHub release and can apply it
through one canonical staged updater. The updater supports clean installs and
clean customized deployment branches without treating a live checkout like a
disposable clone.

Every update uses this order:

1. acquire the exclusive framework-update lock;
2. refuse tracked dirt;
3. fetch the exact stable release tag;
4. compare untracked paths with the release and refuse collisions;
5. hash deployment-only skills, extensions, and integration modules;
6. build the fast-forward or merge in a disposable candidate worktree;
7. reinstall candidate dependencies and run the updater/runtime regression set;
8. create a rollback ref and pre-mutation receipt;
9. move the live checkout to the already-validated candidate;
10. reinstall live dependencies, restart, and verify health when requested;
11. compare protected hashes and write the final receipt;
12. restore the previous revision and dependencies if a post-apply check fails.

The updater never follows arbitrary branch commits. Its only automatic target
is GitHub's latest non-prerelease release.

## Operator Entry Points

### Chat (admin only)

| Command | Behavior |
|---|---|
| `/update` or `/update status` | Current/latest version, deployment mode, schedule, next run, and blockers |
| `/update now` | Acknowledge immediately and launch the detached updater worker |
| `/update auto on` | Install and enable the native daily timer/task |
| `/update auto off` | Disable and remove the native schedule |
| `/update auto status` | Read physical scheduler state |
| `/update history` | Show recent structured receipts |

Mutating update commands refuse slash-command chaining. Status and history are
safe to inspect. Explicit phrases such as “update yourself” and “pull the
latest YourProduct OS” route to `/update now`; ordinary requests such as “update
the manual” do not.

`/watch` remains the video-learning command and is unrelated to updates.

### CLI

```powershell
thehomie update --check
thehomie update --check --json
thehomie update
thehomie update --yes --json
thehomie update --yes --json --scheduled --restart
thehomie auto-update status --json
thehomie auto-update on
thehomie auto-update off
```

`thehomie update` remains interactive by default. `--yes --json` is the quiet,
non-interactive machine contract. The passive once-per-day update banner still
prints to stderr, keeping JSON stdout clean.

## Deployment Modes

| Mode | Update behavior |
|---|---|
| Clean branch | Candidate fast-forwards to the release |
| Customized branch | Candidate creates a merge whose first parent is the current deployment revision |
| Customized branch with conflicts | Blocked; operator resolves the candidate conflict deliberately |
| Tracked dirty checkout | Blocked before fetch/apply |
| Detached checkout | Blocked from automatic mutation |
| Docker | Check-only; container replacement belongs to the orchestrator |

Untracked files are not deleted or committed. If the target release introduces
the same path, the update blocks instead of overwriting it. Protected untracked
files under `.claude/skills/`, `.claude/extensions/`,
`.claude/chat/extensions/`, and `.claude/scripts/integrations/` are SHA-256
compared before and after apply.

## Scheduling

Linux installs a native systemd oneshot service and timer. The timer uses:

```ini
OnCalendar=*-*-* 04:00:00 America/Los_Angeles
Persistent=true
```

The IANA timezone keeps the run at 4 a.m. across Pacific daylight-saving
changes. `Persistent=true` runs a missed update after the machine returns.
System services can set `HOMIE_UPDATE_SYSTEMD_SCOPE=system` and
`HOMIE_UPDATE_USER=<service-user>`; user installs default to a user timer.

Windows installs `TheHomie-AutoUpdate` through Task Scheduler at local 04:00.
Task Scheduler provides missed-start recovery according to the host's task
policy. The host timezone should be Pacific when the configured framework
timezone is `America/Los_Angeles`.

The manual and scheduled paths share the same lock. A scheduled failure can
notify an admin channel when `HOMIE_UPDATE_ADMIN_PLATFORM` and
`HOMIE_UPDATE_ADMIN_CHANNEL` are configured.

## Receipts And Rollback

Receipts are JSONL records under the active profile's state directory. Each
contains:

- current and target versions/tags;
- deployment mode and baseline/candidate/applied revisions;
- blocker and validation command results;
- rollback ref and rollback state;
- protected hashes;
- requester/scheduled metadata and timestamps.

Rollback refs live under `refs/thehomie-update-backups/<receipt-id>`. A failure
after live mutation resets to the exact baseline, reinstalls dependencies, and
restarts/verifies the restored build when those callbacks are available.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Passive release banner | `.claude/chat/update_check.py` |
| Canonical updater and receipts | `.claude/scripts/framework_update.py` |
| Detached worker/restart/notification | `.claude/scripts/update_worker.py` |
| Linux and Windows schedules | `.claude/scripts/update_scheduler.py` |
| CLI | `.claude/chat/cli.py` |
| Chat command and direct routing | `.claude/chat/commands.py`, `.claude/chat/core_handlers.py`, `.claude/chat/router.py` |
| Execution-intent classifier | `.claude/chat/engine.py` |
| Tests | `.claude/scripts/tests/test_framework_update.py`, `test_update_scheduler.py`, `test_update_chat_command.py` |

## Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_framework_update.py tests/test_update_scheduler.py tests/test_update_chat_command.py -q
uv run pytest tests/test_chat_runtime_engine.py tests/test_command_menu.py tests/test_cli.py -q
```

The temporary-Git tests cover clean upgrades, customized merges, conflicts,
tracked dirt, untracked collisions, skill preservation, locking, candidate and
dependency failures, restart/health failures, rollback, and receipt history.
