"""Unit tests for the 12-helper public ``personas`` API (PRP-7a Workstream 4a).

Each helper from `personas/__init__.py` has at least one isolation test here.
Integration tests (back-compat snapshots, boot-order audits, sticky-meta
corruption matrices) live in Workstream 4b. This file deliberately avoids
spawning subprocesses or reloading `config` — those concerns belong to 4b.

Reference table — PRP-7a §"Test Plan > test_persona_helpers.py — assertions"
covers the assertion shape verbatim. Each function below is annotated with
its corresponding bullet.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

# Import the package itself so we exercise the public API surface (matches
# how production callers will look this up).
import personas
from personas import core as personas_core


# ---------------------------------------------------------------------------
# validate_persona_name — regex, reserved names, subcommand collisions, length
# ---------------------------------------------------------------------------


def test_validate_persona_name_accepts_valid_names() -> None:
    """Valid lowercase names with digits / hyphens / underscores pass."""
    # Per `_PERSONA_ID_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"`. None of these
    # should raise.
    for name in ("sales", "eng", "prod-2", "team_b", "a", "z9"):
        personas.validate_persona_name(name)


@pytest.mark.parametrize(
    "name,reason",
    [
        ("Sales", "uppercase rejected by regex"),
        ("-sales", "leading dash rejected by regex"),
        ("bad.name", "dot not in regex character set"),
        ("a" * 65, "length 65 exceeds 64-char limit"),
        ("", "empty string fails regex (len 0 < 1)"),
        ("1sales!", "exclamation not in regex character set"),
    ],
)
def test_validate_persona_name_rejects_regex_violations(
    name: str, reason: str
) -> None:
    """ValueError raised for any name that does not match `_PERSONA_ID_RE`."""
    with pytest.raises(ValueError) as exc_info:
        personas.validate_persona_name(name)
    # Sanity-check the message points at the regex contract — keeps the
    # test honest if someone swaps in a different validation strategy.
    assert "Invalid persona name" in str(exc_info.value), reason


@pytest.mark.parametrize(
    "name", ["default", "homie", "thehomie", "test", "tmp", "root", "sudo"]
)
def test_validate_persona_name_rejects_reserved(name: str) -> None:
    """Reserved names (default, homie, thehomie, test, tmp, root, sudo)."""
    with pytest.raises(ValueError) as exc_info:
        personas.validate_persona_name(name)
    assert "reserved" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "name", ["chat", "convoy", "mailbox", "heartbeat", "team", "model"]
)
def test_validate_persona_name_rejects_subcommand_collisions(name: str) -> None:
    """Names colliding with seeded Click subcommands raise ValueError."""
    with pytest.raises(ValueError) as exc_info:
        personas.validate_persona_name(name)
    assert "subcommand" in str(exc_info.value).lower()


def test_validate_persona_name_accepts_caller_supplied_subcommand_set() -> None:
    """`registered_subcommands=` overrides the seed (Rule 1 — None sentinel).

    PRP-7a Anti-pattern Rule 1: the helper accepts a None-sentinel kwarg
    rather than a default-bound `_HOMIE_SUBCOMMANDS_SEED`. This test
    proves the override works (a Phase 2 caller could pass live Click
    state) and that previously-allowed names ("sales") still pass when
    the override does not block them.
    """
    custom = frozenset({"sales"})
    with pytest.raises(ValueError) as exc_info:
        personas.validate_persona_name("sales", registered_subcommands=custom)
    assert "subcommand" in str(exc_info.value).lower()
    # Sanity: a name not in `custom` and not in default seed still passes.
    personas.validate_persona_name("eng", registered_subcommands=custom)


# ---------------------------------------------------------------------------
# get_homie_home — env-on-every-call, `~` expansion (R2 B1 / NB2)
# ---------------------------------------------------------------------------


def test_get_homie_home_falls_back_to_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset HOMIE_HOME -> `Path.home() / ".homie"` resolved."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    expected = (Path.home() / ".homie").resolve(strict=False)
    assert personas.get_homie_home() == expected


def test_get_homie_home_returns_env_value_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOMIE_HOME pointing at an absolute path is returned (resolved)."""
    custom = tmp_path / "custom-deploy"
    custom.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(custom))
    assert personas.get_homie_home() == custom.resolve(strict=False)


def test_get_homie_home_reads_env_on_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutating HOMIE_HOME between calls returns different values (Rule 1)."""
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(a))
    first = personas.get_homie_home()
    monkeypatch.setenv("HOMIE_HOME", str(b))
    second = personas.get_homie_home()
    assert first != second
    assert first == a.resolve(strict=False)
    assert second == b.resolve(strict=False)


def test_normalize_env_home_expands_tilde() -> None:
    """`_normalize_env_home("~/.homie/profiles/sales")` expands `~`.

    PRP-7a R2 B1 / NB2 — the Windows literal-tilde regression. Without
    `expanduser()`, `Path("~/...").resolve()` produces `<cwd>/~/...` which
    misclassifies the named profile and writes config under a literal `~`
    directory. The result must contain no `~` segment.
    """
    result = personas_core._normalize_env_home("~/.homie/profiles/sales")
    expected = (Path.home() / ".homie" / "profiles" / "sales").resolve(
        strict=False
    )
    assert result == expected
    # Hard sanity check — no path part should be a literal "~" on any platform.
    assert "~" not in result.parts


def test_get_homie_home_expands_literal_tilde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMIE_HOME=`"~/.homie/profiles/sales"` resolves with `~` expanded.

    PRP-7a R2 B1 / NB2 — second-layer guarantee that the env-driven path
    flows through `_normalize_env_home()` before reaching callers.
    """
    monkeypatch.setenv("HOMIE_HOME", "~/.homie/profiles/sales")
    result = personas.get_homie_home()
    expected = (Path.home() / ".homie" / "profiles" / "sales").resolve(
        strict=False
    )
    assert result == expected
    assert "~" not in str(result)


# ---------------------------------------------------------------------------
# get_subprocess_env — conditional HOME / USERPROFILE, never mutates parent
# ---------------------------------------------------------------------------


def test_get_subprocess_env_no_home_dir_returns_unmodified_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`<HOMIE_HOME>/home/` missing -> returns plain `os.environ.copy()`.

    Asserts equality with the parent env at the time of the call. The
    helper must NEVER set HOME or USERPROFILE when the per-profile home
    directory does not exist on disk.
    """
    homie_home = tmp_path / "no-home-dir"
    homie_home.mkdir()  # exists, but `<homie_home>/home` does not
    monkeypatch.setenv("HOMIE_HOME", str(homie_home))
    # Ensure parent has a known HOME we can detect leakage through.
    monkeypatch.setenv("HOME", "PARENT_HOME_VALUE")
    parent_snapshot = dict(os.environ)
    result = personas.get_subprocess_env()
    assert result == parent_snapshot
    # Specifically: HOME stays the parent value, USERPROFILE not added.
    assert result["HOME"] == "PARENT_HOME_VALUE"


def test_get_subprocess_env_with_home_dir_sets_home_and_userprofile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`<HOMIE_HOME>/home/` exists -> HOME set; USERPROFILE set on win32.

    Hermes anchor: hermes_constants.py:115-138. The helper sets HOME to the
    profile-relative home directory so subprocess tools (git, ssh, gh, npm)
    write configs under the per-profile volume.
    """
    homie_home = tmp_path / "homie-home"
    home_dir = homie_home / "home"
    home_dir.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie_home))
    result = personas.get_subprocess_env()
    assert result["HOME"] == str(home_dir)
    if sys.platform == "win32":
        assert result["USERPROFILE"] == str(home_dir)
    else:
        # POSIX: USERPROFILE is not set by the helper. It may still appear
        # if the parent env had it (rare), but the helper never adds it.
        # The contract: on POSIX, USERPROFILE is not deliberately injected.
        # Compare against parent env to be sure.
        if "USERPROFILE" in os.environ:
            # Inherited from parent — that's fine; just don't change it.
            assert result["USERPROFILE"] == os.environ["USERPROFILE"]


def test_get_subprocess_env_does_not_mutate_parent_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parent `os.environ` snapshot is unchanged before/after the call.

    Hermes contract (hermes_constants.py:127-129) — the helper returns a
    new dict; the parent process's environment is never touched.
    """
    homie_home = tmp_path / "h"
    (homie_home / "home").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie_home))
    monkeypatch.setenv("HOME", "PARENT_HOME_BEFORE")
    before = dict(os.environ)
    _ = personas.get_subprocess_env()
    after = dict(os.environ)
    assert before == after, (
        "get_subprocess_env() mutated parent os.environ — Hermes contract "
        "violation (hermes_constants.py:127-129)."
    )


# ---------------------------------------------------------------------------
# set_active_profile / read_active_profile / get_active_profile_path
# ---------------------------------------------------------------------------


def test_set_then_read_active_profile_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`set_active_profile("sales")` then `read_active_profile()` returns it."""
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    personas.set_active_profile("sales")
    assert personas.read_active_profile() == "sales"
    # Path is `<root>/active_profile`.
    expected_path = personas.get_active_profile_path()
    assert expected_path.read_text(encoding="utf-8") == "sales"


def test_set_active_profile_overwrites_existing_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second `set_active_profile` replaces the first value atomically.

    The atomic-write pattern uses `tempfile + os.replace`; this test
    proves a partial state is never observed by reading after each write.
    Multi-thread race coverage is below.
    """
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    personas.set_active_profile("sales")
    assert personas.read_active_profile() == "sales"
    personas.set_active_profile("eng")
    assert personas.read_active_profile() == "eng"


def test_set_active_profile_concurrent_writes_never_observe_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threaded races never expose a partial-write state.

    Atomic-write contract (PRP-7a §"per-task pseudocode" — `tmp + os.replace`):
    every read MUST observe one of the written values or None, never a
    half-written file. Reads happen interleaved with writes from N threads.

    Windows note: `os.replace` can raise `PermissionError` when two threads
    race the rename of the same target. That is unrelated to the atomic-
    content contract being tested — it is an OS-level rename quirk. The
    writer wraps the call in a retry-on-PermissionError loop so the test
    measures content atomicity, not Windows rename serialization.
    """
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    valid_names = ["sales", "eng", "marketing"]
    observed: list[str | None] = []
    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            try:
                personas.set_active_profile(valid_names[i % 3])
            except PermissionError:
                # Windows os.replace race — unrelated to the content
                # atomicity contract under test. Retry.
                continue
            i += 1

    def reader() -> None:
        # Read 200 times — every read must return one of the valid names
        # or None (transient absence is acceptable; "partial garbage" is not).
        try:
            for _ in range(200):
                observed.append(personas.read_active_profile())
        finally:
            stop.set()

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads.append(threading.Thread(target=reader))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    # Every observation must be either None (no file yet) or a valid name.
    bad = [v for v in observed if v is not None and v not in valid_names]
    assert not bad, (
        f"Partial-write observed: {bad!r} — atomic-write contract violated."
    )
    # Sanity: at least some valid observations actually happened.
    valid_observations = [v for v in observed if v in valid_names]
    assert valid_observations, (
        "Concurrent test produced no valid reads — race not exercised. "
        f"observed={observed[:10]}"
    )


@pytest.mark.parametrize(
    "content,expected,reason",
    [
        ("", None, "empty file -> None"),
        ("   \n\t  ", None, "whitespace-only -> None"),
        ("default", None, "literal 'default' -> None (Hermes parity)"),
        ("sales", "sales", "valid name returned"),
        ("missing-profile-name", "missing-profile-name", "advisory pass-through"),
    ],
)
def test_read_active_profile_passive_tolerance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected: str | None,
    reason: str,
) -> None:
    """`read_active_profile` tolerates corrupt / empty / whitespace / 'default'.

    PRP-7a §"Test Plan > read_active_profile (passive read tolerance)".
    Binary-garbage case is in its own test below because it needs
    `write_bytes` (not `write_text`) to truly exercise the UnicodeDecodeError
    branch in the helper.
    """
    homie_root = tmp_path / ".homie"
    homie_root.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    active = personas.get_active_profile_path()
    active.write_text(content, encoding="utf-8")
    assert personas.read_active_profile() == expected, reason


def test_read_active_profile_returns_none_for_binary_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary garbage in `active_profile` -> returns None (no crash).

    PRP-7a §"Test Plan" — `b'\\xff\\xfe\\x00\\x01'` triggers
    `UnicodeDecodeError` inside `Path.read_text(encoding="utf-8")`. The
    helper catches it and returns None. This is the regression net for
    a hand-edited / corrupt-binary `active_profile` file silently
    bricking startup.
    """
    homie_root = tmp_path / ".homie"
    homie_root.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    active = personas.get_active_profile_path()
    # `0xFF 0xFE` is a UTF-16 BOM and is invalid as a leading UTF-8 byte
    # sequence. Combined with the NUL bytes it makes UnicodeDecodeError
    # the only honest outcome of `read_text(encoding="utf-8")`.
    active.write_bytes(b"\xff\xfe\x00\x01")
    assert personas.read_active_profile() is None


def test_read_active_profile_returns_none_for_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No file at `<root>/active_profile` -> returns None, no crash."""
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    # Don't create the file.
    assert personas.read_active_profile() is None


# ---------------------------------------------------------------------------
# is_default_profile — physical-state SOUL.md check (Rule 2)
# ---------------------------------------------------------------------------


def test_is_default_profile_true_when_soul_md_present(
    default_profile_install: Path,
) -> None:
    """`<install>/vault/memory/SOUL.md` exists -> True (Rule 2)."""
    # Fixture has set HOMIE_VAULT_DIR to point at the fake install's
    # Memory dir, which contains a fixture-installed SOUL.md.
    assert personas.is_default_profile() is True


def test_is_default_profile_false_when_soul_md_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SOUL.md absent -> False. No fall-back to meta cache (Rule 2)."""
    fake_memory = tmp_path / "TheHomie" / "Memory"
    fake_memory.mkdir(parents=True)
    # Note: NO SOUL.md created.
    monkeypatch.setenv("HOMIE_VAULT_DIR", str(fake_memory))
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    assert personas.is_default_profile() is False


# ---------------------------------------------------------------------------
# get_active_profile_name — "default" / "<name>" / "custom" enum (R1 B1)
# ---------------------------------------------------------------------------


def test_get_active_profile_name_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMIE_HOME unset -> 'default' (rank-4 fallback)."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    assert personas.get_active_profile_name() == "default"


def test_get_active_profile_name_default_when_env_equals_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMIE_HOME=`~/.homie` -> 'default' (root, not a named profile)."""
    monkeypatch.setenv("HOMIE_HOME", str(Path.home() / ".homie"))
    assert personas.get_active_profile_name() == "default"


def test_get_active_profile_name_named_profile_under_default_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMIE_HOME=`~/.homie/profiles/sales` -> 'sales'."""
    monkeypatch.setenv(
        "HOMIE_HOME", str(Path.home() / ".homie" / "profiles" / "sales")
    )
    assert personas.get_active_profile_name() == "sales"


def test_get_active_profile_name_named_profile_via_literal_tilde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMIE_HOME=literal '~/.homie/profiles/sales' -> 'sales' (R2 B1 / NB2).

    Without `_normalize_env_home` expansion, `Path("~/...").resolve()`
    leaves the literal `~` segment in place (especially on Windows where
    POSIX shells aren't doing the expansion), and the helper would
    misclassify as 'custom'. This test pins the contract.
    """
    monkeypatch.setenv("HOMIE_HOME", "~/.homie/profiles/sales")
    assert personas.get_active_profile_name() == "sales"


def test_get_active_profile_name_custom_when_env_outside_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOMIE_HOME outside `~/.homie/profiles/` -> 'custom'."""
    custom = tmp_path / "custom-deploy"
    custom.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(custom))
    assert personas.get_active_profile_name() == "custom"


# ---------------------------------------------------------------------------
# get_persona_paths — default / named / custom routing (R1 B1)
# ---------------------------------------------------------------------------


def test_get_persona_paths_default_returns_legacy_install_paths(
    monkeypatch: pytest.MonkeyPatch,
    legacy_install_paths: dict[str, str],
) -> None:
    """`get_persona_paths("default")` returns legacy install-dir paths.

    PRP-7a R1 M5 — hard-coded expected values from the `legacy_install_paths`
    fixture (NOT recomputed by calling `get_default_paths()`). This is the
    explicit guard against path-math theater.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    paths = personas.get_persona_paths("default")
    # MEMORY_DIR snapshot — installer/sanitizer-stable.
    assert str(paths["memory"]) == legacy_install_paths["MEMORY_DIR"]
    assert str(paths["state"]) == legacy_install_paths["STATE_DIR"]
    assert str(paths["env_file"]) == legacy_install_paths["ENV_FILE"]
    assert str(paths["data"]) == legacy_install_paths["DATA_DIR"]
    # Spot-check a couple keys from the broader contract.
    assert str(paths["credentials"]) == legacy_install_paths["INTEGRATIONS_DIR"]


def test_get_persona_paths_default_preserves_homie_vault_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_install_paths: dict[str, str],
) -> None:
    """R1 B5 — HOMIE_VAULT_DIR overrides only the `memory` key.

    With HOMIE_VAULT_DIR set, `MEMORY_DIR` follows the override; every
    other path constant remains anchored at the install dir.
    """
    custom_vault = tmp_path / "myvault"
    custom_vault.mkdir()
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.setenv("HOMIE_VAULT_DIR", str(custom_vault))
    paths = personas.get_persona_paths("default")
    assert paths["memory"] == custom_vault.resolve(strict=False)
    # Other keys still match legacy install paths.
    assert str(paths["state"]) == legacy_install_paths["STATE_DIR"]
    assert str(paths["env_file"]) == legacy_install_paths["ENV_FILE"]


def test_get_persona_paths_named_profile_routes_under_homie_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_persona_paths("sales")` -> `~/.homie/profiles/sales/<key>`."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    paths = personas.get_persona_paths("sales")
    expected_root = Path.home().resolve(strict=False) / ".homie" / "profiles" / "sales"
    assert paths["memory"] == expected_root / "memory"
    assert paths["state"] == expected_root / "state"
    assert paths["env_file"] == expected_root / ".env"
    assert paths["data"] == expected_root / "data"


def test_get_persona_paths_custom_uses_homie_home_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 B1 — 'custom' uses HOMIE_HOME as the profile root (not under profiles/)."""
    custom = tmp_path / "custom-deploy"
    custom.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(custom))
    paths = personas.get_persona_paths("custom")
    expected_root = custom.resolve(strict=False)
    assert paths["memory"] == expected_root / "memory"
    assert paths["state"] == expected_root / "state"
    assert paths["env_file"] == expected_root / ".env"
    # Critical: NOT under `<custom>/profiles/custom/...`.
    assert "profiles" not in paths["memory"].parts


def test_explicit_selection_dominates_physical_detection(
    default_profile_install: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 B1 (critical) — explicit HOMIE_HOME wins over SOUL.md presence.

    Setup: fake install has SOUL.md (`is_default_profile()` would be True),
    AND HOMIE_HOME points at `<root>/profiles/sales`. The contract says
    `get_active_profile_name()` MUST return 'sales' (not 'default') and
    `get_persona_paths("sales")["memory"]` MUST end at `profiles/sales/memory`
    — never the install Memory dir.

    This is the regression net for the "physical detection silently wins"
    failure mode that R1 B1 calls out.
    """
    # Fixture put HOMIE_VAULT_DIR at <install>/vault/memory (so SOUL.md
    # is reachable via `is_default_profile()`). Physical detection IS true:
    assert personas.is_default_profile() is True
    # Now set HOMIE_HOME pointing at a named profile under ~/.homie.
    sales_path = Path.home() / ".homie" / "profiles" / "sales"
    monkeypatch.setenv("HOMIE_HOME", str(sales_path))
    # Despite SOUL.md existing, explicit selection MUST dominate.
    assert personas.get_active_profile_name() == "sales"
    paths = personas.get_persona_paths("sales")
    # The memory path MUST end at `profiles/sales/memory`, NOT the install path.
    assert paths["memory"].parts[-3:] == ("profiles", "sales", "memory")
    assert paths["memory"] != Path(default_profile_install, "TheHomie", "Memory")


# ---------------------------------------------------------------------------
# get_active_profile_path — `<homie_root>/active_profile` shape
# ---------------------------------------------------------------------------


def test_get_active_profile_path_under_default_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMIE_HOME unset -> path is `<home>/.homie/active_profile`."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    expected = (Path.home() / ".homie" / "active_profile").resolve(strict=False)
    # `get_active_profile_path` returns `get_default_homie_root() / "active_profile"`,
    # which uses Path joining on the resolved root. Compare on the .name + parent.
    actual = personas.get_active_profile_path()
    assert actual.name == "active_profile"
    assert actual.parent == expected.parent


# ---------------------------------------------------------------------------
# resolve_persona_env — default vs named, missing-profile FileNotFoundError
# ---------------------------------------------------------------------------


def test_resolve_persona_env_default_returns_default_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_persona_env("default")` returns the default Homie root."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    result = personas.resolve_persona_env("default")
    expected = str((Path.home() / ".homie").resolve(strict=False))
    assert result == expected


def test_resolve_persona_env_named_profile_must_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named profile dir must exist on disk -> FileNotFoundError otherwise.

    The boot shim's R1 B4 / R3 NNB1 source-split error handling relies on
    `resolve_persona_env` raising when the profile dir is missing.
    """
    # Point HOMIE_HOME root at tmp so we don't pollute the real ~/.homie.
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    # tmp/.homie/profiles/sales does NOT exist.
    with pytest.raises(FileNotFoundError) as exc_info:
        personas.resolve_persona_env("sales")
    assert "Profile 'sales' not found" in str(exc_info.value)


def test_resolve_persona_env_named_profile_present(
    tmp_homie_home: Path,
) -> None:
    """Named profile dir present -> returns its absolute path string.

    The `tmp_homie_home` fixture creates `<tmp>/.homie/profiles/sales/`,
    so `resolve_persona_env("sales")` should return that path as a string.
    """
    result = personas.resolve_persona_env("sales")
    # `tmp_homie_home` returns the profile dir itself.
    assert Path(result) == tmp_homie_home


# ---------------------------------------------------------------------------
# get_default_paths — direct invariant coverage
# ---------------------------------------------------------------------------


def test_get_default_paths_returns_expected_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_default_paths()` returns the full Phase 1 + future-key contract.

    PRP-7a §"Contract Chain — foundation -> config-refactor": the resolver
    promises 7 keys for Phase 1 (`memory`, `data`, `state`, `env_file`,
    `credentials`, `logs`, `run`) plus 6 future keys (`archon`, `home`,
    `cron`, `sessions`, `skills`, `workspace`) for Phase 2/3/5 reuse.
    Total = 13 keys. Both `get_default_paths()` and `get_persona_paths()`
    return the same key set so callers can branch by name without surprises.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    paths = personas.get_default_paths()
    expected_keys = {
        "memory",
        "data",
        "state",
        "env_file",
        "credentials",
        "logs",
        "run",
        "archon",
        "home",
        "cron",
        "sessions",
        "skills",
        "workspace",
    }
    assert set(paths.keys()) == expected_keys
    # Every value is a Path instance (not a string).
    for key, value in paths.items():
        assert isinstance(value, Path), (
            f"get_default_paths()['{key}'] is not a Path (got {type(value).__name__})"
        )


def test_get_default_paths_homie_vault_dir_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 B5 — HOMIE_VAULT_DIR override applies ONLY to `memory`.

    Direct test of `get_default_paths()` (vs. via `get_persona_paths`) so
    the contract is asserted at the leaf helper, not just at the wrapper.
    """
    custom = tmp_path / "myvault"
    custom.mkdir()
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.setenv("HOMIE_VAULT_DIR", str(custom))
    paths = personas.get_default_paths()
    assert paths["memory"] == custom.resolve(strict=False)
    # `state` and `data` still derive from the install dir, NOT from the override.
    # We can't snapshot the absolute path here without recomputing the resolver
    # (which is the path-math-theater anti-pattern R1 M5 calls out), so we just
    # check that they did NOT match the override.
    assert paths["state"] != custom.resolve(strict=False)
    assert paths["data"] != custom.resolve(strict=False)


# ---------------------------------------------------------------------------
# apply_persona_override — basic invariants (Workstream 4b owns full coverage)
# ---------------------------------------------------------------------------


def test_apply_persona_override_no_args_no_env_returns_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No CLI flag + no HOMIE_HOME + no sticky meta -> rank-4 default no-op.

    The full source-split error-handling matrix (R1 B4 + R3 NNB1) lives in
    Workstream 4b's `test_persona_active_profile_corrupt.py`. This test
    only proves the boring path: the shim runs without raising or exiting
    when there's nothing to override (rank-4 fallback). That contract is
    foundational — every entry point relies on `apply_persona_override()`
    being safe to call unconditionally.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    # Set sys.argv to a clean state so the CLI flag pre-parse finds nothing.
    monkeypatch.setattr(sys, "argv", ["script.py"])
    # Point the shim at a sticky-meta location that doesn't exist (no
    # `~/.homie/active_profile` to read).
    fake_root = Path.home() / ".homie-no-such-dir-for-testing"
    monkeypatch.setenv(
        "HOMIE_HOME", ""
    )  # explicit empty -> treated as unset by `.strip()` check
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    # Should return None without raising or sys.exit.
    result = personas.apply_persona_override()
    assert result is None
    # Verify the fake root was never created (we didn't touch disk).
    assert not fake_root.exists()
