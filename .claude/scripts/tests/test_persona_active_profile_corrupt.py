"""Sticky active_profile corruption + precedence tests (PRP-7a R1 B4 + R2 NB1 + R3 NNB1).

PRP-7a Workstream 4b — covers two contracts that intersect at the
``apply_persona_override()`` shim:

1. **Corruption tolerance (R1 B4 + R3 NNB1).** ``read_active_profile()``
   never raises on corrupt / binary / empty / whitespace content. The
   shim's downstream behavior depends on whether the meta name was
   provided explicitly via ``--profile``/``-p`` (rank 1) or read passively
   from sticky meta (rank 3). Source-split error handling: explicit
   selections hard-fail (``sys.exit(1)``), sticky-meta failures warn +
   fall back. Per PRP-7a R3 NNB1 the matrix splits further:
       - **Passive-corrupt** (empty/whitespace/binary garbage) → shim
         silently falls through (NO warning printed).
       - **Invalid-non-empty** (uppercase / illegal char / >64 chars /
         reserved name) → shim warns + falls back.

2. **Precedence chain (R2 NB1).** When more than one rank tries to claim
   the active profile, the higher rank ALWAYS wins. Rank order:
       1. ``--profile <name>`` / ``-p <name>`` / ``--profile=<name>`` (CLI)
       2. existing ``HOMIE_HOME`` env var
       3. sticky ``~/.homie/active_profile`` meta
       4. physical default (legacy install)

   Rank 2 is the rank-2 short-circuit added by The Homie's deviation from
   Hermes — Hermes goes CLI -> sticky directly, but a parent process or
   orchestrator that has already pinned ``HOMIE_HOME`` MUST not be
   overridden by stale sticky meta. The NB1 failure mode (`stale meta
   wins over explicit env`) is what this test guarantees against.

Test shape: passive-read tests use ``personas.read_active_profile()``
directly; precedence + source-split error tests are subprocess-based so
the boot shim runs in a clean import order with the right env shape.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import personas

# Repo root + scripts dir for subprocess invocations.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "scripts"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_homie_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Build a fake ``~/.homie/`` root and point HOME at it.

    Re-implements the conftest fixture's setup pattern but locally so
    each test can layer custom ``active_profile`` content + profile dirs.
    Returns the homie root path.
    """
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    homie_root = fake_home / ".homie"
    homie_root.mkdir()
    (homie_root / "profiles").mkdir()
    if sys.platform == "win32":
        monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    return homie_root


def _write_active_profile(homie_root: Path, content: bytes | str) -> None:
    """Write *content* to ``<homie_root>/active_profile`` raw.

    Accepts ``bytes`` for binary-garbage cases (encoding-bypass);
    ``str`` content goes through utf-8 encoding.
    """
    target = homie_root / "active_profile"
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _subprocess_run(
    code: str,
    *,
    extra_env: dict[str, str] | None = None,
    drop_env_keys: tuple[str, ...] = ("HOMIE_HOME", "HOMIE_VAULT_DIR"),
    args: list[str] | None = None,
    invoke_shim: bool = True,
) -> subprocess.CompletedProcess:
    """Run ``python -c "<code>"`` in a clean env and return the result.

    Same shape as the back-compat test's helper, but returns the full
    ``CompletedProcess`` so callers can inspect stdout AND stderr AND
    returncode (the corruption-handling tests need all three).

    When *invoke_shim* is True (default), the snippet is wrapped to call
    ``personas.apply_persona_override()`` BEFORE the user code runs.
    Production entry points always call the shim explicitly (see the
    51 ``__main__`` files in ``.claude/scripts/`` etc.). A bare
    ``python -c "import config"`` does NOT invoke any entry point, so
    the shim never fires unless we explicitly add it. Without this
    wrapping, sticky-meta + CLI-flag tests would silently take the
    rank-4 fall-through path because the shim was never called.

    Set *invoke_shim*=False for tests that explicitly want the bare
    config-import path (e.g., the no-shim circular-import smoke).
    """
    env = os.environ.copy()
    for key in drop_env_keys:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)

    if invoke_shim:
        # Wrap *code* with the shim invocation. The shim mutates os.environ
        # and sys.argv before any framework module is imported — exactly the
        # pattern that the 51 entry points implement at module top-level.
        wrapped = (
            "import personas; personas.apply_persona_override();\n"
            + code
        )
    else:
        wrapped = code

    cmd = [sys.executable, "-c", wrapped]
    if args:
        cmd.extend(args)
    return subprocess.run(
        cmd,
        cwd=str(_SCRIPTS_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# read_active_profile() — passive-read tolerance (R1 B4 + R3 NNB1 base)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,content",
    [
        ("binary_garbage", b"\xff\xfe\x00\x01"),
        ("empty", ""),
        ("whitespace_spaces", "   "),
        ("whitespace_mixed", "   \n\t  "),
        ("default_literal", "default"),
    ],
)
def test_read_active_profile_returns_none_for_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    content: bytes | str,
) -> None:
    """``read_active_profile`` MUST never raise on corrupt content.

    PRP-7a R1 B4 + R3 NNB1 base contract: passive-corrupt cases all
    return ``None`` so the shim's rank-3 short-circuit takes the
    "no sticky meta" path silently.
    """
    homie_root = _seed_homie_home(tmp_path, monkeypatch)
    _write_active_profile(homie_root, content)

    result = personas.read_active_profile()
    assert result is None, (
        f"PRP-7a R1 B4 — corrupt active_profile case '{name}' should "
        f"return None, got {result!r}"
    )


def test_read_active_profile_returns_string_for_valid_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid name round-trips through ``read_active_profile`` untouched.

    Sanity check — proves the helper isn't overzealously filtering. The
    downstream shim is the layer that decides what to do with a name
    that points at a missing dir or fails validation.
    """
    homie_root = _seed_homie_home(tmp_path, monkeypatch)
    _write_active_profile(homie_root, "missing-profile-name")
    assert personas.read_active_profile() == "missing-profile-name"


def test_read_active_profile_returns_none_when_file_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``active_profile`` file -> ``read_active_profile`` returns None.

    Catches the regression where the helper raises ``FileNotFoundError``
    instead of cleanly returning None. The shim's rank-3 path relies on
    this contract.
    """
    _seed_homie_home(tmp_path, monkeypatch)
    assert personas.read_active_profile() is None


# ---------------------------------------------------------------------------
# R3 NNB1 — passive-corrupt sticky meta -> silent fall-through (no warning)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,content_repr",
    [
        ("empty", '""'),
        ("whitespace_only", '"   \\n\\t  "'),
        ("binary_garbage", "b'\\xff\\xfe\\x00\\x01'"),
    ],
)
def test_passive_corrupt_sticky_meta_is_silent(
    tmp_path: Path,
    name: str,
    content_repr: str,
    legacy_install_paths: dict[str, str],
) -> None:
    """R3 NNB1 — passive-corrupt sticky meta does NOT print a warning.

    PRP-7a R3 NNB1 split: empty / whitespace-only / binary garbage cause
    ``read_active_profile()`` to return ``None`` (covered above), and the
    shim then takes the rank-3 short-circuit (no sticky meta — fall to
    rank 4). No warning should appear on stderr because "no sticky meta"
    is silent — the file simply behaves as absent.
    """
    fake_home = tmp_path / "fake-home"
    homie_root = fake_home / ".homie"
    homie_root.mkdir(parents=True)
    (homie_root / "profiles").mkdir()
    # Write the corrupt content directly. Use bytes for binary, str otherwise.
    target = homie_root / "active_profile"
    if name == "binary_garbage":
        target.write_bytes(b"\xff\xfe\x00\x01")
    elif name == "empty":
        target.write_text("", encoding="utf-8")
    elif name == "whitespace_only":
        target.write_text("   \n\t  ", encoding="utf-8")

    # Run config import in a clean subprocess. HOMIE_HOME is unset so the
    # shim falls through to the sticky-meta path. We point HOME at our
    # fake-home so ``~/.homie/active_profile`` resolves to the seeded file.
    env_pin: dict[str, str] = {"HOME": str(fake_home)}
    if sys.platform == "win32":
        env_pin["USERPROFILE"] = str(fake_home)

    result = _subprocess_run(
        "import config; print(config.MEMORY_DIR)",
        extra_env=env_pin,
    )

    assert result.returncode == 0, (
        f"PRP-7a R3 NNB1 — passive-corrupt '{name}' should produce exit 0, "
        f"got {result.returncode}\nstderr:\n{result.stderr}"
    )
    assert "Warning: invalid active_profile" not in result.stderr, (
        f"PRP-7a R3 NNB1 — passive-corrupt '{name}' should be silent, "
        f"got warning on stderr:\n{result.stderr}"
    )
    # The resolved MEMORY_DIR should be the legacy install path (rank-4
    # fallback) — proves the shim ignored the corrupt sticky entry.
    expected = str(Path(legacy_install_paths["MEMORY_DIR"]).resolve(strict=False))
    actual = str(Path(result.stdout.strip()).resolve(strict=False))
    assert actual == expected, (
        f"PRP-7a R3 NNB1 — passive-corrupt '{name}' should fall back to "
        f"legacy default MEMORY_DIR.\n  expected: {expected}\n"
        f"  actual:   {actual}"
    )


# ---------------------------------------------------------------------------
# R3 NNB1 — invalid-non-empty sticky meta -> warn + fall back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_name,case_label",
    [
        ("Sales", "uppercase regex violation"),
        ("bad.name", "dot illegal in regex character set"),
        ("a" * 80, "length >64 char limit"),
        ("thehomie", "reserved subcommand collision"),
    ],
)
def test_invalid_non_empty_sticky_meta_warns_and_falls_back(
    tmp_path: Path,
    invalid_name: str,
    case_label: str,
    legacy_install_paths: dict[str, str],
) -> None:
    """R3 NNB1 — invalid-non-empty sticky meta warns + falls back.

    PRP-7a R3 NNB1 split (continued): when ``read_active_profile()``
    returns a non-empty string but ``validate_persona_name()`` rejects
    it, the shim's source-split branch catches the ValueError and warns
    + falls back to default. Failure mode this prevents: stale or
    hand-edited sticky meta permanently bricking every startup until
    the file is manually deleted (same class as R1 B4).
    """
    fake_home = tmp_path / "fake-home"
    homie_root = fake_home / ".homie"
    homie_root.mkdir(parents=True)
    (homie_root / "profiles").mkdir()
    (homie_root / "active_profile").write_text(invalid_name, encoding="utf-8")

    env_pin: dict[str, str] = {"HOME": str(fake_home)}
    if sys.platform == "win32":
        env_pin["USERPROFILE"] = str(fake_home)

    result = _subprocess_run(
        "import config; print(config.MEMORY_DIR)",
        extra_env=env_pin,
    )

    assert result.returncode == 0, (
        f"PRP-7a R3 NNB1 — invalid-non-empty sticky '{case_label}' "
        f"should NEVER hard-fail.\n  exit: {result.returncode}\n"
        f"  stderr: {result.stderr}"
    )
    assert "Warning: invalid active_profile" in result.stderr, (
        f"PRP-7a R3 NNB1 — invalid-non-empty sticky '{case_label}' "
        f"should print 'Warning: invalid active_profile'.\n"
        f"  stderr: {result.stderr}"
    )
    # MEMORY_DIR should fall back to the legacy default install path.
    expected = str(Path(legacy_install_paths["MEMORY_DIR"]).resolve(strict=False))
    actual = str(Path(result.stdout.strip()).resolve(strict=False))
    assert actual == expected, (
        f"PRP-7a R3 NNB1 — invalid '{case_label}' should fall back to "
        f"legacy MEMORY_DIR.\n  expected: {expected}\n  actual: {actual}"
    )


# ---------------------------------------------------------------------------
# R1 B4 + R3 NNB1 — explicit CLI invalid name -> hard-fail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_name,case_label",
    [
        ("Sales", "uppercase regex violation"),
        ("bad.name", "dot illegal in regex character set"),
        ("thehomie", "reserved subcommand"),
    ],
)
def test_explicit_cli_invalid_name_hard_fails(
    tmp_path: Path,
    invalid_name: str,
    case_label: str,
) -> None:
    """R1 B4 + R3 NNB1 — explicit ``--profile <invalid>`` exits non-zero.

    Source-split contract: rank-1 (CLI) error handling is hard-fail.
    Stale sticky meta warns and falls back; explicit user input is loud.
    """
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()

    env_pin: dict[str, str] = {"HOME": str(fake_home)}
    if sys.platform == "win32":
        env_pin["USERPROFILE"] = str(fake_home)

    # The shim parses sys.argv directly, so we pass the flag via the
    # subprocess argv. ``-c`` snippet still imports config so the boot
    # path runs end-to-end.
    result = _subprocess_run(
        "import config; print(config.MEMORY_DIR)",
        extra_env=env_pin,
        args=["--profile", invalid_name],
    )

    assert result.returncode != 0, (
        f"PRP-7a R1 B4 — explicit --profile '{case_label}' should "
        f"hard-fail (exit non-zero), got exit {result.returncode}.\n"
        f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )
    assert "Error:" in result.stderr, (
        f"PRP-7a R1 B4 — explicit --profile error should print "
        f"'Error: ...' on stderr.\n  stderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# R1 B4 — sticky/missing-dir warn vs explicit/missing-dir fail
# ---------------------------------------------------------------------------


def test_sticky_active_profile_missing_dir_warns_and_falls_back(
    tmp_path: Path,
    legacy_install_paths: dict[str, str],
) -> None:
    """R1 B4 — sticky meta pointing at missing profile dir warns + falls back.

    PRP-7a R1 B4 contract: sticky ``active_profile=missing-name`` with no
    profile dir on disk MUST NOT crash. The shim warns to stderr and
    falls through to the legacy default. Per PRD §14.13: "warn, fall
    through to physical legacy default (NOT crash; NOT silently use
    wrong default)."
    """
    fake_home = tmp_path / "fake-home"
    homie_root = fake_home / ".homie"
    homie_root.mkdir(parents=True)
    (homie_root / "profiles").mkdir()  # but no `missing-profile-name` subdir
    (homie_root / "active_profile").write_text(
        "missing-profile-name", encoding="utf-8"
    )

    env_pin: dict[str, str] = {"HOME": str(fake_home)}
    if sys.platform == "win32":
        env_pin["USERPROFILE"] = str(fake_home)

    result = _subprocess_run(
        "import config; print(config.MEMORY_DIR)",
        extra_env=env_pin,
    )

    assert result.returncode == 0, (
        f"PRP-7a R1 B4 — sticky active_profile pointing at missing dir "
        f"should NEVER hard-fail.\n  exit: {result.returncode}\n"
        f"  stderr: {result.stderr}"
    )
    assert "Warning:" in result.stderr, (
        "PRP-7a R1 B4 — should print 'Warning:' on stderr."
    )
    assert "active_profile points at missing profile" in result.stderr, (
        f"PRP-7a R1 B4 — warning should mention 'active_profile points "
        f"at missing profile'.\n  stderr: {result.stderr}"
    )
    # Fall-through MEMORY_DIR should be the legacy install default.
    expected = str(Path(legacy_install_paths["MEMORY_DIR"]).resolve(strict=False))
    actual = str(Path(result.stdout.strip()).resolve(strict=False))
    assert actual == expected, (
        f"PRP-7a R1 B4 — fall-back MEMORY_DIR drift.\n"
        f"  expected: {expected}\n  actual: {actual}"
    )


def test_explicit_cli_missing_dir_hard_fails(tmp_path: Path) -> None:
    """R1 B4 — explicit ``--profile missing-name`` exits non-zero.

    Source-split contract: rank-1 (CLI) is hard-fail; rank-3 (sticky) is
    warn+fall-back. A user who explicitly typed ``--profile <name>``
    should get a loud error, not a silent fallback that hides the typo.
    """
    fake_home = tmp_path / "fake-home"
    homie_root = fake_home / ".homie"
    homie_root.mkdir(parents=True)
    (homie_root / "profiles").mkdir()  # no `missing-profile-name` subdir

    env_pin: dict[str, str] = {"HOME": str(fake_home)}
    if sys.platform == "win32":
        env_pin["USERPROFILE"] = str(fake_home)

    result = _subprocess_run(
        "import config; print(config.MEMORY_DIR)",
        extra_env=env_pin,
        args=["--profile", "missing-profile-name"],
    )

    assert result.returncode != 0, (
        f"PRP-7a R1 B4 — explicit --profile missing-name should "
        f"hard-fail, got exit {result.returncode}.\n"
        f"  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )
    assert "Error:" in result.stderr, (
        f"PRP-7a R1 B4 — explicit error should print 'Error: ...' on "
        f"stderr.\n  stderr: {result.stderr}"
    )
    assert "missing-profile-name" in result.stderr, (
        f"PRP-7a R1 B4 — error should mention the offending name.\n"
        f"  stderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# R2 NB1 — env beats sticky (rank-2 short-circuit)
# ---------------------------------------------------------------------------


def test_env_homie_home_beats_sticky_active_profile(tmp_path: Path) -> None:
    """R2 NB1 — explicit HOMIE_HOME env beats stale sticky active_profile.

    NB1 failure mode: parent process sets ``HOMIE_HOME=/tmp/custom-deploy``
    for an explicit custom profile, but ``~/.homie/active_profile`` still
    contains ``sales`` (stale meta from a prior session). Without the
    rank-2 short-circuit, sticky meta would overwrite the explicit env
    selection — wrong persona wins. This test pins down the contract.
    """
    fake_home = tmp_path / "fake-home"
    homie_root = fake_home / ".homie"
    homie_root.mkdir(parents=True)
    (homie_root / "profiles").mkdir()
    # Seed sticky meta with a profile name whose dir does NOT exist.
    (homie_root / "active_profile").write_text("sales", encoding="utf-8")

    # Pin HOMIE_HOME to a deliberately non-sales path. The shim should
    # short-circuit at rank 2 and never read sticky meta.
    custom_home = tmp_path / "custom-deploy"
    custom_home.mkdir()

    env_pin: dict[str, str] = {
        "HOME": str(fake_home),
        "HOMIE_HOME": str(custom_home),
    }
    if sys.platform == "win32":
        env_pin["USERPROFILE"] = str(fake_home)

    result = _subprocess_run(
        "import config; print(config.MEMORY_DIR)",
        extra_env=env_pin,
        # Don't drop HOMIE_HOME from env — we want the parent-set value.
        drop_env_keys=("HOMIE_VAULT_DIR",),
    )

    assert result.returncode == 0, (
        f"PRP-7a R2 NB1 — env-beats-sticky should produce exit 0.\n"
        f"  stderr: {result.stderr}"
    )
    # Per the rank-2 short-circuit contract, the resolved MEMORY_DIR
    # should be ``<custom_home>/memory``, NOT ``<homie_root>/profiles/
    # sales/memory`` and NOT the legacy default.
    expected = str((custom_home / "memory").resolve(strict=False))
    actual = str(Path(result.stdout.strip()).resolve(strict=False))
    assert actual == expected, (
        f"PRP-7a R2 NB1 — rank-2 (env) should beat rank-3 (sticky 'sales')."
        f"\n  expected: {expected}\n  actual: {actual}"
    )
    # Crucially: NO warning about sticky meta should appear, because the
    # shim short-circuited at rank 2 and never read the active_profile file.
    assert "active_profile points at missing profile" not in result.stderr, (
        f"PRP-7a R2 NB1 — rank-2 short-circuit must NOT consult sticky "
        f"meta, but a sticky-meta warning appeared:\n{result.stderr}"
    )


def test_cli_profile_flag_beats_env_homie_home(tmp_path: Path) -> None:
    """R2 NB1 — CLI ``--profile`` overrides parent-set HOMIE_HOME.

    Rank-1 always wins over rank-2 short-circuit. The shim parses
    ``--profile sales``, validates it, and calls ``resolve_persona_env``
    which uses the DEPLOYMENT root (``get_default_homie_root()`` —
    HOMIE_HOME-aware on Docker-like deployments). The CLI still wins
    in that ``HOMIE_HOME`` is REWRITTEN to the resolved profile path
    (sales-rooted), not retained as the parent's value.

    Implementation-faithful contract (matches ``personas/boot.py``):
        - With HOMIE_HOME=``<deploy>`` and ``--profile sales``, the shim
          resolves sales under ``<deploy>/profiles/sales`` (this is the
          Docker-volume / containerized-deployment shape — Hermes
          ``hermes_constants.get_default_hermes_root`` :21-58).
        - The PROOF that CLI beats env is that HOMIE_HOME gets rewritten
          to sales-rooted, NOT left at the parent's deploy path. The
          rank-2 short-circuit (which would have left HOMIE_HOME alone)
          DID NOT FIRE.

    The "rank-2 left HOMIE_HOME alone" branch is covered separately by
    ``test_env_homie_home_beats_sticky_active_profile``.
    """
    # HOMIE_HOME is the deployment root. Sales lives WITHIN it.
    deploy_root = tmp_path / "deploy"
    deploy_root.mkdir()
    profiles_root = deploy_root / "profiles"
    profiles_root.mkdir()
    sales_dir = profiles_root / "sales"
    sales_dir.mkdir()

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()

    env_pin: dict[str, str] = {
        "HOME": str(fake_home),
        "HOMIE_HOME": str(deploy_root),
    }
    if sys.platform == "win32":
        env_pin["USERPROFILE"] = str(fake_home)

    result = _subprocess_run(
        "import config; print(config.MEMORY_DIR)",
        extra_env=env_pin,
        drop_env_keys=("HOMIE_VAULT_DIR",),
        args=["--profile", "sales"],
    )

    assert result.returncode == 0, (
        f"PRP-7a R2 NB1 — CLI-beats-env should produce exit 0.\n"
        f"  stderr: {result.stderr}"
    )
    # CLI wins -> HOMIE_HOME is rewritten to sales-rooted, NOT left at
    # deploy_root. MEMORY_DIR resolves to ``<sales>/memory``.
    expected = str((sales_dir / "memory").resolve(strict=False))
    actual = str(Path(result.stdout.strip()).resolve(strict=False))
    assert actual == expected, (
        f"PRP-7a R2 NB1 — CLI ``--profile sales`` should override the "
        f"rank-2 short-circuit and rewrite HOMIE_HOME to "
        f"``<deploy>/profiles/sales/memory``.\n"
        f"  expected: {expected}\n  actual: {actual}\n"
        f"  HOMIE_HOME was: {deploy_root}\n"
        f"  sales_dir was:  {sales_dir}"
    )
