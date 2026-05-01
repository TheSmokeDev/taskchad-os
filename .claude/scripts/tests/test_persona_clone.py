"""PRP-7b WS4 — clone / export / import tests.

Disposition coverage:
    - Hermes-faithful clone deviation (PRD §7.12, §14.25): default `--clone`
      strips `.env` tokens; `carry_secrets=True` opts back in.
    - R1 minor — `_strip_env_secrets` scope (`KEY=value` -> `KEY=`,
      preserves comments + blanks; `export KEY=value` documented as out-of-scope).
    - export_profile strips `.env` and `credentials/` from archive.
    - import_profile rejects multi-root, invalid name, existing dest.
"""
from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from personas.clone import (
    _CLONE_ALL_STRIP,
    _strip_env_secrets,
    clone_profile,
    export_profile,
    import_profile,
)
from personas.lifecycle import create_profile


# ---------------------------------------------------------------------------
# _strip_env_secrets — line-by-line behavior
# ---------------------------------------------------------------------------


def test_strip_env_secrets_simple_kv():
    """KEY=value -> KEY=."""
    assert _strip_env_secrets("KEY=value") == "KEY="


def test_strip_env_secrets_preserves_trailing_newline():
    assert _strip_env_secrets("KEY=value\n") == "KEY=\n"


def test_strip_env_secrets_preserves_comments():
    """Comments are passed through verbatim."""
    inp = "# comment\nKEY=value\n"
    assert _strip_env_secrets(inp) == "# comment\nKEY=\n"


def test_strip_env_secrets_preserves_blank_lines():
    inp = "\nKEY=value\nOTHER=other\n"
    out = _strip_env_secrets(inp)
    assert out == "\nKEY=\nOTHER=\n"


def test_strip_env_secrets_quoted_value():
    """KEY="value" -> KEY= (quotes stripped along with value)."""
    assert _strip_env_secrets('KEY="value"') == "KEY="


def test_strip_env_secrets_export_kv_documented_scope():
    """`export KEY=value` is OUT OF SCOPE — partition-on-= leaves "export KEY"
    as the key portion. Documented limitation; test asserts current behavior.
    """
    out = _strip_env_secrets("export KEY=value")
    # Partition-on-= sees `export KEY=value` as `key=export KEY`, `value=value`.
    # The output preserves the "export KEY=" prefix verbatim with empty value.
    assert out == "export KEY="


def test_strip_env_secrets_empty_value_passthrough():
    """KEY= (already empty) stays KEY=."""
    assert _strip_env_secrets("KEY=") == "KEY="


# ---------------------------------------------------------------------------
# clone_profile — light-clone path (Hermes-faithful)
# ---------------------------------------------------------------------------


def test_clone_profile_strips_env_secrets_by_default(source_profile_with_secrets):
    """Default clone (carry_secrets=False) writes a stripped .env to dest."""
    dst = clone_profile("source", "dest", carry_secrets=False)
    env_text = (dst / ".env").read_text(encoding="utf-8")
    # Original tokens replaced with empty values.
    assert "BOT123" not in env_text
    assert "sk-test" not in env_text
    # Keys preserved.
    assert "TELEGRAM_BOT_TOKEN=" in env_text
    assert "OPENAI_API_KEY=" in env_text
    # Comments preserved.
    assert "# comment line" in env_text


def test_clone_profile_carry_secrets_copies_verbatim(source_profile_with_secrets):
    """carry_secrets=True copies .env verbatim (Hermes-faithful)."""
    src_text = (source_profile_with_secrets / ".env").read_text(encoding="utf-8")
    dst = clone_profile("source", "dest", carry_secrets=True)
    dst_text = (dst / ".env").read_text(encoding="utf-8")
    assert dst_text == src_text


def test_clone_profile_copies_memory_files(source_profile_with_secrets):
    """Light-clone copies SOUL/MEMORY/USER from source/memory/."""
    dst = clone_profile("source", "dest")
    assert (dst / "memory" / "SOUL.md").read_text(encoding="utf-8") == "source soul\n"
    assert (dst / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "source memory\n"
    assert (dst / "memory" / "USER.md").read_text(encoding="utf-8") == "source user\n"


def test_clone_profile_full_strips_runtime_state(source_profile_with_secrets):
    """clone(full=True) calls `_copytree_with_strip` which removes runtime files."""
    # Pre-seed source with runtime state files.
    (source_profile_with_secrets / "run").mkdir(exist_ok=True)
    (source_profile_with_secrets / "run" / "bot.pid").write_text("12345")
    (source_profile_with_secrets / "state").mkdir(exist_ok=True)
    (source_profile_with_secrets / "state" / "heartbeat-state.json").write_text("{}")

    dst = clone_profile("source", "dest", full=True)
    assert not (dst / "run" / "bot.pid").exists()
    assert not (dst / "state" / "heartbeat-state.json").exists()


def test_clone_profile_nonexistent_source_raises_file_not_found(empty_homie_root):
    with pytest.raises(FileNotFoundError):
        clone_profile("nonexistent", "dest")


def test_clone_profile_to_default_raises_value_error(
    source_profile_with_secrets,
):
    """Cannot clone TO `default` (built-in profile is not a clone target)."""
    with pytest.raises(ValueError):
        clone_profile("source", "default")


# ---------------------------------------------------------------------------
# export_profile — strips secrets from archive
# ---------------------------------------------------------------------------


def test_export_profile_strips_env_and_credentials(empty_homie_root):
    """export_profile output archive has NO .env or credentials/ members."""
    info = create_profile("sales", no_alias=True)
    profile_dir = info.path
    # Pre-seed .env and credentials/
    (profile_dir / ".env").write_text("SECRET=value\n")
    creds = profile_dir / "credentials"
    creds.mkdir(exist_ok=True)
    (creds / "google_token.json").write_text("token-data")

    archive_path = export_profile("sales")
    assert archive_path.exists()
    assert archive_path.suffix == ".gz"

    with tarfile.open(archive_path, "r:gz") as tf:
        names = [m.name for m in tf.getmembers()]
    # No member named exactly "<name>/.env" or under credentials/.
    assert not any(n.endswith("/.env") for n in names), names
    assert not any("/credentials/" in n for n in names), names


def test_export_profile_default_path_under_homie_exports(empty_homie_root):
    """Default output goes to ~/.homie/exports/<name>-<ts>.tar.gz."""
    create_profile("sales", no_alias=True)
    archive_path = export_profile("sales")
    assert archive_path.parent == empty_homie_root / "exports"
    assert archive_path.name.startswith("sales-")
    assert archive_path.name.endswith(".tar.gz")


# ---------------------------------------------------------------------------
# import_profile
# ---------------------------------------------------------------------------


def test_import_profile_extracts_to_profiles_dir(empty_homie_root):
    """import_profile materializes <homie>/profiles/<name>/ from archive."""
    info = create_profile("sales", no_alias=True)
    archive = export_profile("sales")
    # Now delete and re-import.
    import shutil
    shutil.rmtree(info.path)
    assert not info.path.exists()

    dst = import_profile(str(archive))
    assert dst.exists()
    assert dst.name == "sales"


def test_import_profile_existing_without_force_raises(empty_homie_root):
    info = create_profile("sales", no_alias=True)
    archive = export_profile("sales")
    with pytest.raises(FileExistsError):
        import_profile(str(archive))


def test_import_profile_force_overwrites(empty_homie_root):
    info = create_profile("sales", no_alias=True)
    archive = export_profile("sales")
    # Mutate dest to verify it gets replaced.
    (info.path / "marker.txt").write_text("pre-import")
    dst = import_profile(str(archive), force=True)
    assert dst.exists()
    # Marker file should NOT survive the overwrite.
    assert not (dst / "marker.txt").exists()


def test_import_profile_as_name_renames(empty_homie_root):
    create_profile("sales", no_alias=True)
    archive = export_profile("sales")
    dst = import_profile(str(archive), as_name="renamed")
    assert dst.name == "renamed"
    assert dst.exists()


def test_import_profile_nonexistent_archive_raises_file_not_found(
    empty_homie_root, tmp_path
):
    with pytest.raises(FileNotFoundError):
        import_profile(str(tmp_path / "missing.tar.gz"))


def test_clone_all_strip_constant_lists_runtime_files():
    """`_CLONE_ALL_STRIP` lists the runtime files removed after full clone.

    Smoke test on the constant — easier to add new entries when the list
    has documented coverage. Documented entries:
    """
    # Spot-check critical entries are present.
    assert "run/bot.pid" in _CLONE_ALL_STRIP
    assert ".delete.lock" in _CLONE_ALL_STRIP
    assert "state/heartbeat-state.json" in _CLONE_ALL_STRIP


# ---------------------------------------------------------------------------
# F2 (post-build adversarial review) — default export DOES NOT archive
# the install repo or nested secrets.
# ---------------------------------------------------------------------------


def test_export_default_does_not_archive_install_repo_secrets(
    empty_homie_root, default_profile_install, monkeypatch
):
    """F2 — export_profile("default") MUST NOT recursively copy the
    install repo. Pre-fix it called shutil.copytree on the install root
    with only a top-level .env / credentials/ removal, which let nested
    .env files (e.g. .claude/scripts/.env) through into the archive.

    This test seeds the install layout with several secret-shaped paths
    NESTED inside subdirs that would have been copied verbatim, then
    verifies the produced archive contains NONE of them.
    """
    # default_profile_install builds <tmp>/install/vault/memory/SOUL.md
    # and overrides HOMIE_VAULT_DIR to point at memory there.
    install_root = default_profile_install

    # Build a richer install-dir layout with nested secret-shaped files
    # that would slip past a top-level-only strip.
    secret_paths = [
        install_root / ".claude" / "scripts" / ".env",
        install_root / ".claude" / "scripts" / "integrations"
            / "google_token.json",
        install_root / ".claude" / "scripts" / "integrations"
            / "credentials.json",
        install_root / "deep" / "nested" / "subdir" / ".env",
        install_root / "private.pem",
    ]
    for p in secret_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("FAKE_SECRET_VALUE_DO_NOT_LEAK", encoding="utf-8")

    # Also make sure HOMIE_HOME points at empty_homie_root so
    # ``~/.homie/exports/`` lands in the test tree.
    monkeypatch.setenv("HOMIE_HOME", str(empty_homie_root))

    archive_path = export_profile("default")
    assert archive_path.exists()

    with tarfile.open(archive_path, "r:gz") as tf:
        names = [m.name for m in tf.getmembers()]

    # F2 — no archive member matches any of the seeded secret paths,
    # whether by basename or by path component.
    for n in names:
        lower = n.lower()
        assert not n.endswith("/.env"), (
            f"F2 violation: nested .env in archive: {n}"
        )
        assert not lower.endswith(".pem"), (
            f"F2 violation: pem file in archive: {n}"
        )
        assert "credentials" not in n.split("/"), (
            f"F2 violation: credentials/ dir in archive: {n}"
        )
        assert "integrations" not in n.split("/"), (
            f"F2 violation: integrations/ dir in archive: {n}"
        )
        # Token files matching `*token*.json` — defensive deny pattern.
        if lower.endswith(".json") and "token" in lower:
            raise AssertionError(
                f"F2 violation: token file in archive: {n}"
            )

    # And the staging tree should NEVER include the install repo's
    # ``.git`` either (the recursive copy would have walked it).
    assert not any(".git" in n.split("/") for n in names), (
        f"F2 violation: .git dir in archive (install-repo recursive copy "
        f"slipped through): {[n for n in names if '.git' in n.split('/')]}"
    )


def test_export_default_archives_default_memory_content(
    empty_homie_root, default_profile_install, monkeypatch
):
    """F2 — when the default export DOES land in the archive, it should
    contain the actual default memory content (vault/memory/SOUL.md
    becomes default/memory/SOUL.md).

    This proves the explicit profile-shaped staging tree works for the
    legitimate happy path.
    """
    install_root = default_profile_install
    default_soul = install_root / "TheHomie" / "Memory" / "SOUL.md"
    marker = "F2-MARKER-DEFAULT-EXPORT-CONTENT-9d8acf2b"
    default_soul.write_text(
        f"# Default SOUL\n\n{marker}\n", encoding="utf-8"
    )

    monkeypatch.setenv("HOMIE_HOME", str(empty_homie_root))
    archive_path = export_profile("default")

    with tarfile.open(archive_path, "r:gz") as tf:
        soul_member = None
        for m in tf.getmembers():
            if m.name.endswith("/memory/SOUL.md"):
                soul_member = m
                break
        assert soul_member is not None, (
            f"F2 violation: archive has no memory/SOUL.md member; "
            f"members={[m.name for m in tf.getmembers()][:20]}"
        )
        f = tf.extractfile(soul_member)
        assert f is not None
        soul_bytes = f.read()
    assert marker.encode() in soul_bytes, (
        "F2 violation: default export's memory/SOUL.md does NOT contain "
        "the actual default identity content."
    )


# ---------------------------------------------------------------------------
# F3 (post-build adversarial review) — fail CLOSED on rewrite/unlink failure
# ---------------------------------------------------------------------------


def test_clone_all_fails_closed_when_env_rewrite_raises(
    source_profile_with_secrets, monkeypatch
):
    """F3 — when ``carry_secrets=False`` and the ``.env`` rewrite raises
    OSError (Windows readonly attr / ACL / disk error), the clone MUST
    NOT succeed with the verbatim ``.env`` left on disk. Either the
    rewrite succeeds or the operation raises.

    Pre-fix: the catch was best-effort, leaving the source's literal
    secrets in the destination's ``.env``.
    """
    real_write = Path.write_text

    def faulting_write(self, *args, **kwargs):
        # Fail ONLY on the destination .env rewrite. Allow every other
        # write_text call (e.g. seeding identity files).
        if self.name == ".env" and "profiles/dest" in str(self).replace(
            "\\", "/"
        ):
            raise OSError("simulated readonly .env (F3 fail-closed)")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", faulting_write)

    # F3 contract: rewrite-or-raise. The clone MUST NOT silently
    # succeed leaving secrets in dest/.env.
    with pytest.raises((OSError, RuntimeError)):
        clone_profile("source", "dest", full=True, carry_secrets=False)


def test_export_fails_closed_if_secret_path_slips_through(
    empty_homie_root, monkeypatch
):
    """F3 — if the ignore filter ever lets a secret-shaped path through
    the staging copy, the post-stage scan MUST RAISE before the archive
    is written.

    Test setup: monkeypatch ``_default_export_ignore`` to a no-op so
    ``.env`` and ``credentials/`` survive into the staged tree. Then
    invoke export and assert it raises.
    """
    from personas import clone as clone_mod
    from personas.lifecycle import create_profile

    info = create_profile("sales", no_alias=True)
    profile_dir = info.path
    (profile_dir / ".env").write_text("SECRET=value\n", encoding="utf-8")

    # Defeat the ignore filter so the secret slips into the staged tree.
    def no_op_ignore(_dir, names):
        return []

    monkeypatch.setattr(clone_mod, "_default_export_ignore", no_op_ignore)

    # Post-stage scan must reject before the tarball is produced.
    with pytest.raises(RuntimeError, match="secret-shaped paths"):
        export_profile("sales")
