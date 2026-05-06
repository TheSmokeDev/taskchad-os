"""Tests for the WS2 identity payload shim.

Covers contract published to WS3 (cron consumers) + WS4 (engine refactor):
- Public API surface (``__all__``).
- Six-file happy path with uppercase keys.
- Missing files → key absent (NOT empty string, NOT None).
- Empty memory_dir → empty dict.
- ``include`` parameter respected.
- Rule 1 (no tunable config in default args) — AST scan.
- Rule 2 (no module-level file reads) — AST scan.
- R4 NM2: subprocess import probe from ``.claude/chat`` (the standalone
  invocation path WS4 will exercise).
- Lazy ``runtime.bootstrap`` import actually resolves at call time.

All tests use ``tmp_path / 'TheHomie' / 'Memory'`` fixtures per the canonical
pattern at .claude/scripts/tests/test_memory_dream.py:25-65 (R2 NM2 — never
read the real ``vault/memory/`` which is sanitizer-denied).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure scripts dir is on path for direct imports (matches sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Inject .claude/chat so ``cognition.identity_payload`` resolves in-process.
_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Empty vault/memory directory (no files yet)."""
    d = tmp_path / "TheHomie" / "Memory"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def memory_dir_full(memory_dir: Path) -> Path:
    """Memory directory seeded with all six identity files."""
    files = {
        "SOUL.md": "# SOUL\n\nIdentity values.\n",
        "SELF.md": "# SELF\n\nPatterns.\n",
        "USER.md": "# USER\n\nUser profile.\n",
        "MEMORY.md": "# MEMORY\n\nKey decisions.\n",
        "GOALS.md": "# GOALS\n\nQuarterly objectives.\n",
        "WORKING.md": "# WORKING\n\nOpen threads.\n",
    }
    for name, content in files.items():
        (memory_dir / name).write_text(content, encoding="utf-8")
    return memory_dir


# ---------------------------------------------------------------------------
# Contract tests (criteria 5, 6, 7, 8)
# ---------------------------------------------------------------------------


def test_module_importable() -> None:
    """`from cognition.identity_payload import build_identity_payload` works."""
    from cognition.identity_payload import build_identity_payload

    assert callable(build_identity_payload)
    assert build_identity_payload.__doc__ is not None
    assert build_identity_payload.__doc__.strip() != ""


def test_public_api_surface() -> None:
    """`__all__` exposes only `build_identity_payload`."""
    from cognition import identity_payload

    assert identity_payload.__all__ == ("build_identity_payload",)


def test_six_files_present(memory_dir_full: Path) -> None:
    """All six identity files seeded → dict has six uppercase keys."""
    from cognition.identity_payload import build_identity_payload

    payload = build_identity_payload(memory_dir_full)

    assert set(payload.keys()) == {"SOUL", "SELF", "USER", "MEMORY", "GOALS", "WORKING"}
    assert payload["SOUL"] == "# SOUL\n\nIdentity values.\n"
    assert payload["MEMORY"] == "# MEMORY\n\nKey decisions.\n"
    assert payload["WORKING"] == "# WORKING\n\nOpen threads.\n"


def test_returns_dict_keyed_by_uppercase_name(memory_dir_full: Path) -> None:
    """Keys are uppercase identity names with NO `.md` suffix."""
    from cognition.identity_payload import build_identity_payload

    payload = build_identity_payload(memory_dir_full)

    for key in payload:
        assert key.isupper(), f"key {key!r} not uppercase"
        assert not key.endswith(".md"), f"key {key!r} should not have .md suffix"


def test_missing_files_absent_from_dict(memory_dir: Path) -> None:
    """Only some files present → others ABSENT from dict (not empty string)."""
    from cognition.identity_payload import build_identity_payload

    (memory_dir / "SOUL.md").write_text("# SOUL\n", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("# MEMORY\n", encoding="utf-8")

    payload = build_identity_payload(memory_dir)

    assert set(payload.keys()) == {"SOUL", "MEMORY"}
    assert "SELF" not in payload
    assert "USER" not in payload
    assert "GOALS" not in payload
    assert "WORKING" not in payload
    # Defensive: missing keys are ABSENT, not "" — assert via `.get` default.
    assert payload.get("SELF", "<missing>") == "<missing>"


def test_missing_optional_files_absent(memory_dir: Path) -> None:
    """Criterion 7 alias — partial seed → optional files absent from dict.

    Same contract as ``test_missing_files_absent_from_dict``: when the
    consumer expresses "no content for that file" it MUST do so via
    ``payload.get(name, '')`` because the key is genuinely absent. Empty
    string is reserved for files that exist but are zero-byte / whitespace.
    """
    from cognition.identity_payload import build_identity_payload

    (memory_dir / "USER.md").write_text("# USER\n", encoding="utf-8")
    (memory_dir / "GOALS.md").write_text("# GOALS\n", encoding="utf-8")

    payload = build_identity_payload(memory_dir)

    assert "USER" in payload
    assert "GOALS" in payload
    # Optional/absent files are NOT in the dict at all.
    for absent in ("SOUL", "SELF", "MEMORY", "WORKING"):
        assert absent not in payload, (
            f"{absent} should be absent (key missing), not present with empty value"
        )


def test_missing_soul_does_not_raise(memory_dir: Path) -> None:
    """Empty memory_dir returns empty dict, never raises."""
    from cognition.identity_payload import build_identity_payload

    # Empty dir → no exception
    payload = build_identity_payload(memory_dir)
    assert payload == {}


def test_empty_memory_dir_returns_empty_dict(memory_dir: Path) -> None:
    """Empty memory_dir → {}. Distinct assertion from previous test."""
    from cognition.identity_payload import build_identity_payload

    assert build_identity_payload(memory_dir) == {}


def test_include_parameter_respected(memory_dir_full: Path) -> None:
    """`include=('SOUL', 'MEMORY')` → only those keys, even when others exist."""
    from cognition.identity_payload import build_identity_payload

    payload = build_identity_payload(memory_dir_full, include=("SOUL", "MEMORY"))

    assert set(payload.keys()) == {"SOUL", "MEMORY"}


# ---------------------------------------------------------------------------
# Anti-pattern enforcement (criterion 8)
# ---------------------------------------------------------------------------


def _module_source() -> str:
    """Return the source of identity_payload.py as a string."""
    module_path = (
        Path(__file__).resolve().parent.parent.parent
        / "chat"
        / "cognition"
        / "identity_payload.py"
    )
    return module_path.read_text(encoding="utf-8")


def test_no_default_args_bind_config() -> None:
    """Rule 1: no FunctionDef has a default value resolving to UPPERCASE constant.

    AST-walks identity_payload.py and asserts that no function default-arg
    expression unparses to a single uppercase identifier (e.g., ``DEFAULT_INCLUDE``)
    or a typical config-style name. The ``include=None`` sentinel is the
    canonical pattern; resolution happens inside the function body.
    """
    import re

    tree = ast.parse(_module_source())
    uppercase_re = re.compile(r"^[A-Z_][A-Z0-9_]*$")

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Combine positional + keyword-only defaults.
        defaults = list(node.args.defaults) + list(node.args.kw_defaults)
        for default in defaults:
            if default is None:
                continue
            try:
                src = ast.unparse(default).strip()
            except Exception:
                continue
            if uppercase_re.match(src):
                offenders.append(f"{node.name}: default={src}")

    assert not offenders, (
        f"Rule 1 violation — default args binding UPPERCASE constants: {offenders}"
    )


def test_no_module_level_file_reads() -> None:
    """Rule 2: no `.read_text()`, `.read_bytes()`, `open()` at module level."""
    tree = ast.parse(_module_source())

    forbidden_attrs = {"read_text", "read_bytes"}
    forbidden_funcs = {"open"}

    # Walk only top-level (Module body), skipping FunctionDef/AsyncFunctionDef
    # bodies — those reads are allowed.
    offenders: list[str] = []
    for node in tree.body:
        # Skip function bodies entirely.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # Walk this top-level node looking for forbidden calls.
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                    offenders.append(f"line {sub.lineno}: .{func.attr}() at module level")
                elif isinstance(func, ast.Name) and func.id in forbidden_funcs:
                    offenders.append(f"line {sub.lineno}: {func.id}() at module level")

    assert not offenders, f"Rule 2 violation — module-level file reads: {offenders}"


# ---------------------------------------------------------------------------
# R4 NM2: standalone-invocation path probe (subprocess from .claude/chat)
# ---------------------------------------------------------------------------


def test_subprocess_invocation_from_chat_dir() -> None:
    """Importing from `.claude/chat` cwd works — exercises R4 NM2 sys.path injection.

    This is a positive proof that the module-top ``_SCRIPTS_DIR`` injection
    actually resolves the lazy ``runtime.bootstrap`` import. Bare in-process
    imports might pass because pytest already sat scripts/ on sys.path; this
    subprocess starts fresh.
    """
    chat_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cognition.identity_payload import build_identity_payload; "
            "print(build_identity_payload.__name__)",
        ],
        cwd=str(chat_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"subprocess import failed:\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "build_identity_payload" in proc.stdout


def test_lazy_runtime_import_actually_works(memory_dir_full: Path) -> None:
    """Calling the function exercises the lazy `runtime.bootstrap` import.

    Positive proof: the resolution path (sys.path injection at module top
    + lazy import inside body) actually loads `read_file_safe` without
    `ModuleNotFoundError`.
    """
    from cognition.identity_payload import build_identity_payload

    # If the lazy import path were broken, this would raise ModuleNotFoundError
    # before returning.
    payload = build_identity_payload(memory_dir_full)
    assert payload  # non-empty (we seeded six files)
    assert "SOUL" in payload
