"""Tests for backup_tool.py — Hermes v0.18 Tier-1 ports, Phase 3.

Every test runs against tmp_path fake profiles with monkeypatched ``config.*``
attributes — the REAL vault/DBs are NEVER touched. DB assertions follow the
PRP's CRITICAL #7: ``sqlite3.backup()`` rewrites page layout, so restored DBs
are verified via ``PRAGMA integrity_check`` + a seeded-row round-trip, never a
byte-compare.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

# Add paths (mirrors tests/test_cli.py)
_CHAT_DIR = str(Path(__file__).parent.parent.parent / "chat")
_SCRIPTS_DIR = str(Path(__file__).parent.parent)
if _CHAT_DIR not in sys.path:
    sys.path.insert(0, _CHAT_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import config  # noqa: E402
from backup_tool import (  # noqa: E402
    _classify_member,
    _safe_copy_db,
    create_backup,
    create_quick_snapshot,
    list_quick_snapshots,
    restore_backup,
    restore_quick_snapshot,
)

CANARY = "unique-canary-text-A\n"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_wal_db(path: Path, rows: tuple[str, ...] = ("alpha",)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(r,) for r in rows])
        conn.commit()
    finally:
        conn.close()


def _db_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY v")]
    finally:
        conn.close()


def _db_integrity_ok(path: Path) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def _build_profile(base: Path, *, seed: bool = True) -> dict:
    vault = base / "vault"
    data = base / "data"
    state = base / "state"
    env_file = base / ".env"
    for d in (vault, data, state):
        d.mkdir(parents=True, exist_ok=True)
    if seed:
        (vault / "SOUL.md").write_text("# Soul\n" + CANARY, encoding="utf-8")
        (vault / "daily").mkdir(exist_ok=True)
        (vault / "daily" / "log.md").write_text("day log\n", encoding="utf-8")
        _make_wal_db(data / "chat.db")
        (state / "heartbeat-state.json").write_text('{"ok": true}', encoding="utf-8")
        env_file.write_text("FAKE_SECRET=abc123\n", encoding="utf-8")
    return {
        "base": base,
        "vault": vault,
        "data": data,
        "state": state,
        "env": env_file,
        "pid": state / "bot.pid",
    }


def _activate(monkeypatch: pytest.MonkeyPatch, p: dict) -> None:
    monkeypatch.setattr(config, "MEMORY_DIR", p["vault"])
    monkeypatch.setattr(config, "DATA_DIR", p["data"])
    monkeypatch.setattr(config, "STATE_DIR", p["state"])
    monkeypatch.setattr(config, "ENV_FILE", p["env"])
    monkeypatch.setattr(config, "PROJECT_ROOT", p["base"])
    monkeypatch.setattr(config, "CHAT_DB_PATH", p["data"] / "chat.db")
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", p["data"] / "orchestration.db")
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", p["data"] / "dashboard.db")
    # BOT_PID_FILE resolves lazily via config.__getattr__ (PEP 562) — patch the
    # module __dict__ with setitem so teardown DELETES the key and the lazy
    # resolver is restored (setattr teardown would freeze a stale resolved
    # value into the module for the rest of the session).
    monkeypatch.setitem(vars(config), "BOT_PID_FILE", p["pid"])


def _tree(base: Path) -> dict[str, bytes]:
    return {
        f.relative_to(base).as_posix(): f.read_bytes()
        for f in base.rglob("*")
        if f.is_file()
    }


@pytest.fixture
def profile_a(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    p = _build_profile(tmp_path / "profA")
    _activate(monkeypatch, p)
    return p


# ── _safe_copy_db (WAL safety + cross-platform URI) ─────────────────────────


def _forbid_copy2_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the shutil.copy2 last-resort inside _safe_copy_db.

    A regression to Hermes's POSIX-only ``f"file:{src}?mode=ro"`` URI form
    would fail the connect on Windows and silently fall through to a torn
    raw copy — which would still round-trip a quiescent test DB and PASS.
    With the fallback forbidden, only the sqlite3.backup() path can succeed,
    so a broken read-only URI fails these tests instead of hiding.
    """

    def _fallback_forbidden(*_a, **_k):
        raise AssertionError(
            "shutil.copy2 fallback reached — the read-only URI failed to connect"
        )

    monkeypatch.setattr("backup_tool.shutil.copy2", _fallback_forbidden)


class TestSafeCopyDb:
    def test_wal_uri_connects_on_this_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """as_uri()+?mode=ro must connect — copy2 fallback is forbidden, so
        a malformed URI cannot silently pass through the raw-copy path."""
        _forbid_copy2_fallback(monkeypatch)
        db = tmp_path / "src.db"
        _make_wal_db(db)
        dst = tmp_path / "dst.db"
        assert _safe_copy_db(db, dst) is True
        assert _db_integrity_ok(dst)
        assert _db_rows(dst) == ["alpha"]

    def test_wal_copy_with_uncommitted_writer_is_not_torn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """backup() must beat a live writer: only committed rows land.
        copy2 is forbidden here too — a raw copy of a WAL DB would also
        show only committed rows, masking a dead backup() path."""
        _forbid_copy2_fallback(monkeypatch)
        db = tmp_path / "src.db"
        _make_wal_db(db)
        writer = sqlite3.connect(str(db))
        try:
            writer.execute("BEGIN")
            writer.execute("INSERT INTO t (v) VALUES ('uncommitted')")
            dst = tmp_path / "dst.db"
            assert _safe_copy_db(db, dst) is True
            assert _db_integrity_ok(dst)
            rows = _db_rows(dst)
            assert "alpha" in rows
            assert "uncommitted" not in rows
        finally:
            writer.rollback()
            writer.close()


# ── create_backup ────────────────────────────────────────────────────────────


class TestCreateBackup:
    def test_walks_curated_roots_never_project_root(self, profile_a, tmp_path):
        # A codebase-ish file inside PROJECT_ROOT but outside the curated roots
        (profile_a["base"] / "code.py").write_text("x = 1\n", encoding="utf-8")
        out = create_backup(out_path=tmp_path / "b.zip")
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert all(
            n.startswith(("vault/", "data/", "state/", "secrets/")) for n in names
        )
        assert not any("code.py" in n for n in names)

    def test_arcnames_logical_prefixes_forward_slash(self, profile_a, tmp_path):
        out = create_backup(out_path=tmp_path / "b.zip")
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert "vault/SOUL.md" in names
        assert "vault/daily/log.md" in names
        assert "data/chat.db" in names
        assert "state/heartbeat-state.json" in names
        assert not any("\\" in n for n in names)

    def test_exclusions(self, profile_a, tmp_path):
        data, state = profile_a["data"], profile_a["state"]
        (data / "junk.db-wal").write_bytes(b"x")
        (data / "what.log").write_text("log", encoding="utf-8")
        (data / "models").mkdir()
        (data / "models" / "big.bin").write_bytes(b"model")
        (data / "memory.db").touch()
        _make_wal_db(data / "memory.db")
        (state / "locky.lock").write_text("l", encoding="utf-8")
        (state / "state-snapshots" / "old").mkdir(parents=True)
        (state / "state-snapshots" / "old" / "y.json").write_text("{}", encoding="utf-8")
        profile_a["pid"].write_text("12345", encoding="utf-8")

        out = create_backup(out_path=tmp_path / "b.zip")
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert not any(n.endswith(".db-wal") for n in names)
        assert not any(n.endswith(".log") for n in names)
        assert not any(n.endswith(".lock") for n in names)
        assert not any("models/" in n for n in names)
        assert not any("state-snapshots" in n for n in names)
        assert not any(n.endswith("bot.pid") for n in names)
        # memory.db IS in the full backup (full DR), just not in snapshots
        assert "data/memory.db" in names

    def test_secrets_excluded_by_default(self, profile_a, tmp_path):
        out = create_backup(out_path=tmp_path / "b.zip")
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert not any(n.startswith("secrets/") for n in names)

    def test_include_secrets_opt_in_with_warning(self, profile_a, tmp_path, capsys):
        out = create_backup(out_path=tmp_path / "b.zip", include_secrets=True)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            content = zf.read("secrets/.env").decode("utf-8")
        assert "secrets/.env" in names
        assert "FAKE_SECRET=abc123" in content
        assert "store it securely" in capsys.readouterr().out

    def test_out_dir_gets_stamped_zip_name(self, profile_a, tmp_path):
        target_dir = tmp_path / "outdir"
        target_dir.mkdir()
        out = create_backup(out_path=target_dir)
        assert out is not None
        assert out.parent == target_dir.resolve()
        assert out.name.startswith("thehomie-backup-")
        assert out.suffix == ".zip"

    def test_zip_suffix_forced(self, profile_a, tmp_path):
        out = create_backup(out_path=tmp_path / "backup.dat")
        assert out is not None
        assert out.name.endswith(".zip")

    def test_self_reference_skip(self, profile_a, tmp_path):
        # --out landing INSIDE a walked root must not archive itself
        out = create_backup(out_path=profile_a["state"] / "selfref.zip")
        assert out is not None
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert not any(n.endswith("selfref.zip") for n in names)

    def test_nested_state_root_not_duplicated(self, tmp_path, monkeypatch):
        """Default profile layout: STATE_DIR lives INSIDE DATA_DIR. Each file
        must be owned by exactly one logical prefix (no data/state/ dupes)."""
        base = tmp_path / "nested"
        vault = base / "vault"
        data = base / "data"
        state = data / "state"
        for d in (vault, data, state):
            d.mkdir(parents=True)
        (vault / "SOUL.md").write_text(CANARY, encoding="utf-8")
        _make_wal_db(data / "chat.db")
        (state / "heartbeat-state.json").write_text("{}", encoding="utf-8")
        p = {
            "base": base, "vault": vault, "data": data, "state": state,
            "env": base / ".env", "pid": state / "bot.pid",
        }
        _activate(monkeypatch, p)

        out = create_backup(out_path=tmp_path / "b.zip")
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert "state/heartbeat-state.json" in names
        assert not any(n.startswith("data/state/") for n in names)

    def test_empty_profile_returns_none(self, tmp_path, monkeypatch):
        p = _build_profile(tmp_path / "empty", seed=False)
        _activate(monkeypatch, p)
        assert create_backup(out_path=tmp_path / "b.zip") is None


# ── restore traversal guard (unit) ───────────────────────────────────────────


class TestClassifyMember:
    def test_dotdot_blocked(self, profile_a):
        assert _classify_member("data/../../evil.txt") == ("blocked", None)

    def test_absolute_blocked(self, profile_a):
        abs_member = "data/" + ("C:/Windows/evil.txt" if os.name == "nt" else "/etc/evil.txt")
        assert _classify_member(abs_member) == ("blocked", None)

    def test_empty_rel_blocked(self, profile_a):
        assert _classify_member("vault/") == ("blocked", None)

    def test_unknown_prefix_skipped(self, profile_a):
        assert _classify_member("unknown/x.txt") == ("skip-unknown", None)

    def test_runtime_names_skipped(self, profile_a):
        assert _classify_member("state/bot.pid") == ("skip-runtime", None)
        assert _classify_member("state/other.pid") == ("skip-runtime", None)
        assert _classify_member("data/some.lock") == ("skip-runtime", None)

    def test_legit_member_maps_to_profile_root(self, profile_a):
        action, target = _classify_member("vault/ok.md")
        assert action == "add"
        assert target == profile_a["vault"] / "ok.md"

    def test_db_member_is_db_swap(self, profile_a):
        action, target = _classify_member("data/chat.db")
        assert action == "db-swap"
        assert target == profile_a["data"] / "chat.db"


# ── restore_backup ───────────────────────────────────────────────────────────


class TestRestoreBackup:
    def test_round_trip_to_fresh_profile(self, profile_a, tmp_path, monkeypatch):
        archive = create_backup(out_path=tmp_path / "b.zip")
        assert archive is not None

        prof_b = _build_profile(tmp_path / "profB", seed=False)
        _activate(monkeypatch, prof_b)

        assert restore_backup(archive, yes=True) is True
        # Text canary: byte-identical
        assert (prof_b["vault"] / "SOUL.md").read_text(encoding="utf-8") == (
            "# Soul\n" + CANARY
        )
        assert (prof_b["vault"] / "daily" / "log.md").is_file()
        assert (prof_b["state"] / "heartbeat-state.json").read_text(
            encoding="utf-8"
        ) == '{"ok": true}'
        # DB canary: integrity + seeded-row round-trip (NOT a byte-compare)
        restored_db = prof_b["data"] / "chat.db"
        assert restored_db.is_file()
        assert _db_integrity_ok(restored_db)
        assert _db_rows(restored_db) == ["alpha"]

    def test_dry_run_mutates_nothing(self, profile_a, tmp_path, monkeypatch, capsys):
        archive = create_backup(out_path=tmp_path / "b.zip")
        prof_b = _build_profile(tmp_path / "profB", seed=False)
        (prof_b["vault"] / "SOUL.md").write_text("different\n", encoding="utf-8")
        _activate(monkeypatch, prof_b)

        before = _tree(prof_b["base"])
        assert restore_backup(archive, dry_run=True) is True
        assert _tree(prof_b["base"]) == before
        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "vault/SOUL.md" in out

    def test_default_deny_without_yes(self, profile_a, tmp_path, monkeypatch, capsys):
        archive = create_backup(out_path=tmp_path / "b.zip")
        prof_b = _build_profile(tmp_path / "profB", seed=False)
        _activate(monkeypatch, prof_b)

        before = _tree(prof_b["base"])
        assert restore_backup(archive, dry_run=False, yes=False) is False
        assert _tree(prof_b["base"]) == before
        assert "Refusing" in capsys.readouterr().out

    def test_refuses_while_bot_is_alive(self, profile_a, tmp_path, monkeypatch, capsys):
        archive = create_backup(out_path=tmp_path / "b.zip")
        prof_b = _build_profile(tmp_path / "profB", seed=False)
        _activate(monkeypatch, prof_b)
        prof_b["pid"].write_text(str(os.getpid()), encoding="utf-8")

        before = _tree(prof_b["base"])
        assert restore_backup(archive, yes=True) is False
        assert _tree(prof_b["base"]) == before
        assert "bot is running" in capsys.readouterr().out

    def test_garbage_pid_file_proceeds(self, profile_a, tmp_path, monkeypatch):
        archive = create_backup(out_path=tmp_path / "b.zip")
        prof_b = _build_profile(tmp_path / "profB", seed=False)
        _activate(monkeypatch, prof_b)
        prof_b["pid"].write_text("not-a-pid", encoding="utf-8")

        assert restore_backup(archive, yes=True) is True

    def test_dead_pid_proceeds(self, profile_a, tmp_path, monkeypatch):
        archive = create_backup(out_path=tmp_path / "b.zip")
        prof_b = _build_profile(tmp_path / "profB", seed=False)
        _activate(monkeypatch, prof_b)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        prof_b["pid"].write_text(str(proc.pid), encoding="utf-8")

        assert restore_backup(archive, yes=True) is True

    def test_traversal_members_blocked_legit_restored(
        self, profile_a, tmp_path, monkeypatch
    ):
        evil_zip = tmp_path / "evil.zip"
        abs_member = "C:/Windows/evil-abs.txt" if os.name == "nt" else "/etc/evil-abs.txt"
        with zipfile.ZipFile(evil_zip, "w") as zf:
            zf.writestr("data/../../evil.txt", "boom")
            zf.writestr("data/" + abs_member, "boom")
            zf.writestr("unknown/stray.txt", "stray")
            zf.writestr("state/bot.pid", "999")
            zf.writestr("vault/ok.md", "ok")

        prof_b = _build_profile(tmp_path / "profB", seed=False)
        _activate(monkeypatch, prof_b)

        assert restore_backup(evil_zip, yes=True) is True
        assert (prof_b["vault"] / "ok.md").read_text(encoding="utf-8") == "ok"
        # Nothing escaped anywhere under the test sandbox
        assert not list(tmp_path.rglob("evil.txt"))
        assert not list(tmp_path.rglob("evil-abs.txt"))
        assert not list(prof_b["base"].rglob("stray.txt"))
        assert not prof_b["pid"].exists()

    def test_overwrite_confirmation_aborts_without_force(
        self, profile_a, tmp_path, monkeypatch
    ):
        archive = create_backup(out_path=tmp_path / "b.zip")
        prof_b = _build_profile(tmp_path / "profB", seed=True)
        (prof_b["vault"] / "SOUL.md").write_text("keep-me\n", encoding="utf-8")
        _activate(monkeypatch, prof_b)

        def _no_stdin(*_a, **_k):
            raise EOFError

        monkeypatch.setattr("builtins.input", _no_stdin)
        assert restore_backup(archive, yes=True, force=False) is False
        assert (prof_b["vault"] / "SOUL.md").read_text(encoding="utf-8") == "keep-me\n"

    def test_overwrite_confirmation_accepts_yes_answer(
        self, profile_a, tmp_path, monkeypatch
    ):
        archive = create_backup(out_path=tmp_path / "b.zip")
        prof_b = _build_profile(tmp_path / "profB", seed=True)
        (prof_b["vault"] / "SOUL.md").write_text("stale\n", encoding="utf-8")
        _activate(monkeypatch, prof_b)

        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "y")
        assert restore_backup(archive, yes=True, force=False) is True
        assert CANARY in (prof_b["vault"] / "SOUL.md").read_text(encoding="utf-8")

    def test_force_skips_confirmation(self, profile_a, tmp_path, monkeypatch):
        archive = create_backup(out_path=tmp_path / "b.zip")
        prof_b = _build_profile(tmp_path / "profB", seed=True)
        (prof_b["vault"] / "SOUL.md").write_text("stale\n", encoding="utf-8")
        _activate(monkeypatch, prof_b)

        def _boom(*_a, **_k):  # input() must never be reached with --force
            raise AssertionError("input() called despite --force")

        monkeypatch.setattr("builtins.input", _boom)
        assert restore_backup(archive, yes=True, force=True) is True
        assert CANARY in (prof_b["vault"] / "SOUL.md").read_text(encoding="utf-8")

    def test_secrets_archive_restores_env_file(self, profile_a, tmp_path, monkeypatch):
        archive = create_backup(out_path=tmp_path / "b.zip", include_secrets=True)
        prof_c = _build_profile(tmp_path / "profC", seed=False)
        _activate(monkeypatch, prof_c)

        assert restore_backup(archive, yes=True) is True
        assert prof_c["env"].read_text(encoding="utf-8") == "FAKE_SECRET=abc123\n"

    def test_rejects_missing_and_invalid_archives(self, profile_a, tmp_path):
        assert restore_backup(tmp_path / "nope.zip", yes=True) is False
        not_zip = tmp_path / "notzip.zip"
        not_zip.write_text("not a zip", encoding="utf-8")
        assert restore_backup(not_zip, yes=True) is False
        foreign = tmp_path / "foreign.zip"
        with zipfile.ZipFile(foreign, "w") as zf:
            zf.writestr("random/thing.txt", "x")
        assert restore_backup(foreign, yes=True) is False


# ── quick snapshots ──────────────────────────────────────────────────────────


class TestQuickSnapshots:
    def test_create_writes_manifest_and_sanitizes_label(self, profile_a):
        snap_id = create_quick_snapshot(label="pre change!")
        assert snap_id is not None
        assert "/" not in snap_id and "\\" not in snap_id and " " not in snap_id
        snap_dir = profile_a["state"] / "state-snapshots" / snap_id
        manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["id"] == snap_id
        assert manifest["file_count"] >= 1
        assert "data/chat.db" in manifest["files"]
        assert "state/heartbeat-state.json" in manifest["files"]

    def test_snapshot_excludes_memory_db(self, profile_a):
        _make_wal_db(profile_a["data"] / "memory.db")
        snap_id = create_quick_snapshot()
        snap_dir = profile_a["state"] / "state-snapshots" / snap_id
        manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
        assert "data/memory.db" not in manifest["files"]

    def test_snapshot_round_trip(self, profile_a):
        chat_db = profile_a["data"] / "chat.db"
        snap_id = create_quick_snapshot(label="pre")
        assert snap_id is not None

        conn = sqlite3.connect(str(chat_db))
        try:
            conn.execute("INSERT INTO t (v) VALUES ('beta')")
            conn.commit()
        finally:
            conn.close()
        assert _db_rows(chat_db) == ["alpha", "beta"]

        assert restore_quick_snapshot(snap_id) is True
        assert _db_integrity_ok(chat_db)
        assert _db_rows(chat_db) == ["alpha"]

    def test_prune_keeps_20_newest(self, profile_a):
        for i in range(22):
            assert create_quick_snapshot(label=f"s{i:02d}") is not None
        root = profile_a["state"] / "state-snapshots"
        dirs = sorted(d.name for d in root.iterdir() if d.is_dir())
        assert len(dirs) == 20
        assert not any(d.endswith("-s00") for d in dirs)
        assert not any(d.endswith("-s01") for d in dirs)
        assert any(d.endswith("-s21") for d in dirs)

    def test_restore_id_validation_no_disk_touch(self, profile_a):
        snap_id = create_quick_snapshot(label="keep")
        root = profile_a["state"] / "state-snapshots"
        before = _tree(root)
        for bad in ("../evil", "a/b", "a\\b", "", ".", ".."):
            assert restore_quick_snapshot(bad) is False
        assert _tree(root) == before
        assert snap_id in {d.name for d in root.iterdir()}

    def test_restore_unknown_or_manifestless_id(self, profile_a):
        assert restore_quick_snapshot("20990101-000000") is False
        bare = profile_a["state"] / "state-snapshots" / "bare-dir"
        bare.mkdir(parents=True)
        assert restore_quick_snapshot("bare-dir") is False

    def test_list_newest_first_with_limit(self, profile_a):
        for i in range(3):
            create_quick_snapshot(label=f"n{i}")
        snaps = list_quick_snapshots()
        ids = [s["id"] for s in snaps]
        assert len(ids) == 3
        assert ids == sorted(ids, reverse=True)
        assert len(list_quick_snapshots(limit=2)) == 2

    def test_create_with_no_targets_returns_none(self, tmp_path, monkeypatch):
        p = _build_profile(tmp_path / "empty", seed=False)
        _activate(monkeypatch, p)
        assert create_quick_snapshot() is None
        # The empty snapshot dir was cleaned up
        root = p["state"] / "state-snapshots"
        assert not root.exists() or not any(root.iterdir())


# ── CLI smokes (CliRunner over the registered thehomie commands) ────────────


from cli import main as cli_main  # noqa: E402


def _all_output(result) -> str:
    out = result.output
    try:
        err = result.stderr
    except (ValueError, AttributeError):
        err = ""
    return out + (err or "")


class TestCLISmokes:
    def test_help_screens(self):
        from click.testing import CliRunner

        runner = CliRunner()
        for args in (["backup", "--help"], ["restore", "--help"],
                     ["snapshot", "--help"], ["snapshot", "create", "--help"]):
            result = runner.invoke(cli_main, args)
            assert result.exit_code == 0, f"{args}: {result.output}"

    def test_restore_refuses_without_yes_or_dry_run(self, profile_a, tmp_path):
        from click.testing import CliRunner

        archive = create_backup(out_path=tmp_path / "b.zip")
        runner = CliRunner()
        result = runner.invoke(cli_main, ["restore", str(archive)])
        assert result.exit_code == 1
        assert "Refusing" in _all_output(result)

    def test_snapshot_restore_refuses_without_yes(self, profile_a):
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_main, ["snapshot", "restore", "someid"])
        assert result.exit_code == 1
        assert "Refusing" in _all_output(result)

    def test_snapshot_list_json_parses(self, profile_a):
        from click.testing import CliRunner

        snap_id = create_quick_snapshot(label="cli")
        runner = CliRunner()
        result = runner.invoke(cli_main, ["snapshot", "list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert any(s.get("id") == snap_id for s in payload)
