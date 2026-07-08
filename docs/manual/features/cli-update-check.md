# CLI Update Check

Status: Active baseline, live-proven
Owner: runtime-chat
Last updated: 2026-07-06

## What It Does

Every `thehomie` CLI invocation silently checks (at most once per 24h, cached)
whether a newer non-prerelease release exists on the public `taskchad-os`
GitHub repo, and prints a one-line "Update available" banner if so — the same
pattern `gh`, `npm`, and `brew` use. Nothing auto-installs. A separate
`thehomie update` command does the actual install, gated behind an
interactive yes/no confirm.

This exists because installs previously had no way to learn they were behind
— the version living in `pyproject.toml` was stale and unread by any code,
and the only public release tag (`v0.1.0-alpha.1`) was marked pre-release, so
GitHub's `/releases/latest` API (the endpoint any update-checker uses) had
nothing to return. Both were fixed as part of shipping this feature.

## Operator Entry Points

- Chat/Telegram: n/a
- CLI: banner on every `thehomie <command>`; `thehomie update` to install;
  `thehomie --version` to see the current version
- Dashboard: n/a
- API: n/a (talks directly to `api.github.com`, not the framework's own API)

## How It Works

1. `get_current_version()` reads `[project].version` out of
   `.claude/scripts/pyproject.toml` via stdlib `tomllib` — the one place the
   version now lives.
2. `check_for_update()` is TTL-cached (`UPDATE_CHECK_MIN_INTERVAL_HOURS`,
   default 24h) using the same state-file pattern the dream/heartbeat
   pipelines already use (`shared.load_state`/`save_state`, wrapped in
   `shared.file_lock` with a short 1s timeout so a lock miss just skips that
   run instead of blocking CLI startup). Within the TTL window it never
   touches the network.
3. When the cache is stale, `get_latest_release_version()` hits
   `https://api.github.com/repos/<UPDATE_CHECK_REPO>/releases/latest` via
   stdlib `urllib.request` (no new dependency) with a short timeout. Any
   failure — network, timeout, bad JSON, 404 — resolves to `None` and the
   whole check fails closed; it can never break a CLI command.
4. `cli.py`'s `main()` Click group callback (the one hook that fires before
   every subcommand) calls `check_for_update()` and prints the banner to
   **stderr only** — this keeps `--json`/`-Q` machine-consumption paths
   (`status --json`, `chat -Q`) byte-clean on stdout, matching how `gh`/`npm`
   do it.
5. `thehomie update` re-checks live (longer timeout, user is waiting on
   purpose), shows an interactive `click.confirm()` prompt, and on yes: `git
   fetch --tags && git checkout v<latest>` (a pinned tag checkout, not a
   floating `git pull`), then re-runs the same install steps `install.sh`
   already does (`uv sync`, `npm install` + `npm run build` in the dashboard
   dirs). It never auto-restarts a running bot process — it prints a reminder
   to run `run_chat.sh` again instead.
6. `scripts/release.sh <version>` is the operator-side companion: bumps
   `pyproject.toml`, commits, and — only when `origin` resolves to the public
   `TheSmokeDev/taskchad-os` repo — tags, pushes, and runs `gh release create`
   as a real (non-prerelease) release. Run from the private source repo it
   only does the version-bump commit and tells you the next step.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Update-check logic | `.claude/chat/update_check.py` |
| CLI wiring + `update` command | `.claude/chat/cli.py` (`main()` group callback, `update()` command) |
| Config | `.claude/scripts/config.py` (`UPDATE_CHECK_STATE_FILE`, `UPDATE_CHECK_MIN_INTERVAL_HOURS`, `UPDATE_CHECK_REPO`) |
| Version source of truth | `.claude/scripts/pyproject.toml` (`[project].version`) |
| Release helper | `scripts/release.sh` |
| Tests | `.claude/scripts/tests/test_update_check.py` |

## Safety Boundaries

- Never auto-installs an update — the banner is passive; `thehomie update`
  requires an explicit interactive yes.
- Never auto-restarts a running bot process after updating — the operator
  restarts manually.
- The banner prints to stderr only, never stdout — machine/JSON consumers of
  the CLI are unaffected.
- Every failure mode (network, timeout, malformed JSON, missing/corrupt state
  file, missing `pyproject.toml`) fails closed to "no update detected" —
  a broken check can never crash or hang a CLI invocation.
- `git fetch`/`checkout` in `thehomie update` only ever targets a tagged
  release (`v<version>`), never a floating branch — reproducible, not
  commit-following.

## How To Run It

```powershell
thehomie --version
thehomie update
```

```bash
# cut a new release (operator-only, from either repo)
bash scripts/release.sh 1.1.0
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_update_check.py -q
```

15 tests cover version parsing, version-tuple comparison, TTL-cache gating
(no second network call inside the interval), network-failure fail-closed
behavior, and the stderr-only banner (Click `CliRunner`, asserting `result.stderr`
vs `result.stdout` stay separate).

## Latest Live Proof

- Date: 2026-07-06
- Surface: live check against the real `v1.0.0` GitHub release, cut as part
  of shipping this feature
- Result: `get_latest_release_version()` correctly resolved `"1.0.0"` from
  the live API; `thehomie --version` reads `1.0.0` dynamically (no longer a
  hardcoded literal); `gh release view v1.0.0 --json isPrerelease` confirmed
  `false`; `https://api.github.com/repos/TheSmokeDev/taskchad-os/releases/latest`
  returned the `v1.0.0` payload.

## Public Export Status

Public-exported (`github.com/TheSmokeDev/taskchad-os`, commit `47809e2`).

## Next Slices

- Surface current/latest version in `thehomie doctor`/`status --json` output.
- CI automation for cutting releases (currently manual via `scripts/release.sh`
  — no GitHub Actions exist in either repo yet).
- Optional prompt to restart the bot automatically after `thehomie update`
  completes, if one is currently running.
