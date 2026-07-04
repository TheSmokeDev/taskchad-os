"""Backup Click commands — `thehomie backup|restore|snapshot`.

Hermes v0.18 Tier-1 ports, Phase 3 operator surface:

- ``thehomie backup``            curated zip of vault + runtime DBs + state
- ``thehomie restore``           default-denied restore (needs --yes; --dry-run previews)
- ``thehomie snapshot create``   fast quick-snapshot of live runtime DBs + state JSONs
- ``thehomie snapshot list``     newest-first snapshot listing
- ``thehomie snapshot restore``  default-denied snapshot restore (needs --yes)

This module sits in ``.claude/chat/`` next to ``cli.py`` so that ``sys.path``
setup performed by ``cli.py`` (lines 18-24) is in effect when this module is
imported. It must NOT hardcode ``.claude/...`` paths — all path resolution
lives in ``backup_tool.py`` via ``config.*`` attribute reads at call time.
"""

from __future__ import annotations

import json as json_mod
import sys
from pathlib import Path

import click

__all__ = ["backup", "restore", "snapshot"]


@click.command("backup")
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Output zip path or directory (default: ~/thehomie-backup-<ts>.zip).",
)
@click.option(
    "--include-secrets",
    is_flag=True,
    help="Include the profile .env in the archive (EXCLUDED by default).",
)
@click.option("--json", "json_mode", is_flag=True, help="Emit machine-readable JSON.")
def backup(out_path: Path | None, include_secrets: bool, json_mode: bool) -> None:
    """Create a full backup zip (vault + runtime DBs + state; secrets excluded)."""
    from backup_tool import create_backup

    if include_secrets:
        click.echo(
            "WARNING: --include-secrets bundles the profile .env "
            "(live credentials) into the archive.",
            err=True,
        )
    result = create_backup(
        out_path=out_path, include_secrets=include_secrets, json_out=json_mode
    )
    if result is None:
        click.echo("Backup created nothing (no files found).", err=True)
        sys.exit(1)


@click.command("restore")
@click.argument("archive", type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True, help="Preview the restore plan; writes NOTHING.")
@click.option("--yes", is_flag=True, help="Confirm the destructive restore.")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Skip the 'target already has state' confirmation. "
        "Never bypasses the running-bot guard."
    ),
)
@click.option("--json", "json_mode", is_flag=True, help="Emit machine-readable JSON.")
def restore(archive: Path, dry_run: bool, yes: bool, force: bool, json_mode: bool) -> None:
    """Restore a backup archive (default-denied: needs --yes; --dry-run previews)."""
    from backup_tool import restore_backup

    ok = restore_backup(
        archive, dry_run=dry_run, yes=yes, force=force, json_out=json_mode
    )
    if not ok:
        sys.exit(1)


@click.group("snapshot")
def snapshot() -> None:
    """Quick state snapshots of the live runtime DBs (keep=20 ring)."""


@snapshot.command("create")
@click.option("--label", default=None, help="Optional label appended to the snapshot id.")
@click.option(
    "--keep",
    type=int,
    default=None,
    help="Prune the snapshot ring to keep N entries (default 20).",
)
def snapshot_create(label: str | None, keep: int | None) -> None:
    """Snapshot the live runtime DBs + small state JSONs."""
    from backup_tool import create_quick_snapshot

    snap_id = create_quick_snapshot(label=label, keep=keep)
    if not snap_id:
        click.echo("No state files found to snapshot.", err=True)
        sys.exit(1)
    click.echo(f"Snapshot created: {snap_id}")
    click.echo(f"Restore with: thehomie snapshot restore {snap_id} --yes")


@snapshot.command("list")
@click.option("--limit", type=int, default=20, show_default=True, help="Max snapshots shown.")
@click.option("--json", "json_mode", is_flag=True, help="Emit JSON instead of a table.")
def snapshot_list(limit: int, json_mode: bool) -> None:
    """List quick snapshots, newest first."""
    from backup_tool import _format_size, list_quick_snapshots

    snaps = list_quick_snapshots(limit=limit)
    if json_mode:
        click.echo(json_mod.dumps(snaps, default=str))
        return
    if not snaps:
        click.echo("No snapshots found.")
        return
    for meta in snaps:
        click.echo(
            f"  {meta.get('id', '?')}  files={meta.get('file_count', 0)}  "
            f"size={_format_size(meta.get('total_size', 0))}"
        )


@snapshot.command("restore")
@click.argument("snapshot_id", type=str)
@click.option("--yes", is_flag=True, help="Confirm the destructive snapshot restore.")
def snapshot_restore(snapshot_id: str, yes: bool) -> None:
    """Restore a quick snapshot (default-denied: needs --yes)."""
    from backup_tool import _is_bot_running, restore_quick_snapshot

    if not yes:
        click.echo(
            "Refusing: snapshot restore overwrites the live runtime DBs. "
            "Re-run with --yes to confirm.",
            err=True,
        )
        sys.exit(1)
    if _is_bot_running():
        click.echo(
            "Refusing: the bot is running. Stop it first "
            "(kill the PID in bot.pid), then retry.",
            err=True,
        )
        sys.exit(1)
    if not restore_quick_snapshot(snapshot_id):
        click.echo(f"Snapshot restore failed: {snapshot_id}", err=True)
        sys.exit(1)
    click.echo(f"Snapshot restored: {snapshot_id}")
