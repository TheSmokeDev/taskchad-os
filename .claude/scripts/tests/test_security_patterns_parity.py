"""PRD-8 Phase 7a (WS2) — security/patterns three-layer parity tests.

Asserts SECRET_PREFIXES is the SOLE source of truth for secret-prefix patterns:
  - sanitize.py imports from security.patterns
  - runtime/subprocess_env.py imports from security.patterns
  - (future Phase 7b) redact.py would import from security.patterns
  - NO consumer redefines SECRET_PREFIXES locally (AST scan)
  - Phase 4 keys (sk_, gsk_, gr_) are present so Phase 4 ships safely

R2 NM3 — count threshold raised from 22 → 27 to match the explicit B2 inventory.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
PATTERNS_FILE = SCRIPTS_DIR / "security" / "patterns.py"


def test_security_patterns_module_exists() -> None:
    """The security/patterns.py file ships and can be imported."""
    assert PATTERNS_FILE.exists()
    from security.patterns import SECRET_PREFIXES  # noqa: F401
    from security.patterns import LEAK_PATTERN_REGEX  # noqa: F401
    from security.patterns import PREFIX_VENDOR_MAP  # noqa: F401


def test_secret_prefixes_is_tuple() -> None:
    """R1 minor — SECRET_PREFIXES MUST be tuple (immutable defense-in-depth)."""
    from security.patterns import SECRET_PREFIXES
    assert isinstance(SECRET_PREFIXES, tuple)


def test_secret_prefixes_count_at_least_27() -> None:
    """R1 B2 — explicit ≥27 prefix inventory."""
    from security.patterns import SECRET_PREFIXES
    assert len(SECRET_PREFIXES) >= 27, (
        f"SECRET_PREFIXES has {len(SECRET_PREFIXES)} entries; "
        f"R1 B2 requires ≥27. Add missing vendor key shapes."
    )


def test_secret_prefixes_length_desc_sorted() -> None:
    """R1 B3 — most-specific prefix evaluated first.

    Length-desc sort means sk-ant- (7 chars) is checked BEFORE sk- (3 chars),
    sk_live_ (8) BEFORE sk_ (3). Without this, sk-ant-xxxxx gets labeled
    'openai' instead of 'anthropic'.
    """
    from security.patterns import SECRET_PREFIXES
    lengths = [len(p) for p in SECRET_PREFIXES]
    assert lengths == sorted(lengths, reverse=True), (
        f"SECRET_PREFIXES not length-desc sorted. Lengths: {lengths}"
    )


def test_phase4_keys_present() -> None:
    """Phase 4 (voice cascade) keys: sk_ (ElevenLabs), gsk_ (Groq), gr_ (Gradium)."""
    from security.patterns import SECRET_PREFIXES
    for prefix in ("sk_", "gsk_", "gr_"):
        assert prefix in SECRET_PREFIXES, (
            f"Phase 4 key prefix '{prefix}' missing from SECRET_PREFIXES — "
            f"Phase 4 will leak keys without this."
        )


def test_stripe_keys_present() -> None:
    """R1 B2 — Stripe live + test variants present."""
    from security.patterns import SECRET_PREFIXES
    for prefix in ("sk_live_", "sk_test_", "rk_live_", "pk_live_"):
        assert prefix in SECRET_PREFIXES


def test_email_provider_keys_present() -> None:
    """R1 B2 — SendGrid + Mailgun + Heroku + Postmark present."""
    from security.patterns import SECRET_PREFIXES
    for prefix in ("SG.", "key-", "HRKU-", "pcp_"):
        assert prefix in SECRET_PREFIXES


def test_anthropic_winning_specificity() -> None:
    """R1 B3 — sk-ant- comes BEFORE sk- in iteration order so anthropic wins."""
    from security.patterns import SECRET_PREFIXES
    sk_ant_idx = SECRET_PREFIXES.index("sk-ant-")
    sk_idx = SECRET_PREFIXES.index("sk-")
    assert sk_ant_idx < sk_idx, (
        "sk-ant- must be evaluated BEFORE sk- (length-desc). Current order "
        f"has sk-ant- at {sk_ant_idx} and sk- at {sk_idx}."
    )


def test_prefix_vendor_map_iteration_order() -> None:
    """PREFIX_VENDOR_MAP keys MUST iterate in SECRET_PREFIXES order (length-desc)."""
    from security.patterns import PREFIX_VENDOR_MAP, SECRET_PREFIXES
    assert tuple(PREFIX_VENDOR_MAP.keys()) == SECRET_PREFIXES


def test_leak_pattern_regex_compiled() -> None:
    """LEAK_PATTERN_REGEX is a tuple of compiled regex, one per SECRET_PREFIXES entry."""
    from security.patterns import LEAK_PATTERN_REGEX, SECRET_PREFIXES
    assert isinstance(LEAK_PATTERN_REGEX, tuple)
    assert len(LEAK_PATTERN_REGEX) == len(SECRET_PREFIXES)
    for pattern in LEAK_PATTERN_REGEX:
        assert isinstance(pattern, re.Pattern)


def test_contains_leak_pattern_helper_works() -> None:
    """contains_leak_pattern returns True for known synthetic key shapes."""
    from security.patterns import contains_leak_pattern
    # Synthetic — never a real key.
    assert contains_leak_pattern("sk_" + "x" * 24)
    assert contains_leak_pattern("gsk_" + "y" * 24)
    assert contains_leak_pattern("ghp_" + "z" * 24)
    assert not contains_leak_pattern("not-a-key")
    assert not contains_leak_pattern("")


def test_arn_prefixes_separate_from_credentials() -> None:
    """R1 minor — ARN_PREFIXES is a SEPARATE catalog from credentials.

    arn:aws: still appears in SECRET_PREFIXES (via aws-arn vendor label) but
    ARN_PREFIXES is a parallel tuple for future ARN-aware redaction logic.
    """
    from security.patterns import ARN_PREFIXES
    assert isinstance(ARN_PREFIXES, tuple)
    assert "arn:aws:" in ARN_PREFIXES


# === Three-layer parity ===


def test_sanitize_imports_secret_prefixes() -> None:
    """scripts/sanitize.py must import LEAK_PATTERN_REGEX or SECRET_PREFIXES."""
    sanitize_path = REPO_ROOT / "scripts" / "sanitize.py"
    text = sanitize_path.read_text(encoding="utf-8")
    # Look for any of the three import shapes — must hit at least one.
    has_import = (
        "from security.patterns import" in text
        and ("LEAK_PATTERN_REGEX" in text or "SECRET_PREFIXES" in text)
    )
    assert has_import, "sanitize.py must import from security.patterns"


def test_subprocess_env_imports_secret_prefixes() -> None:
    """runtime/subprocess_env.py imports SECRET_PREFIXES (proves consumption)."""
    se_path = SCRIPTS_DIR / "runtime" / "subprocess_env.py"
    assert se_path.exists()
    text = se_path.read_text(encoding="utf-8")
    assert "from security.patterns import" in text
    assert "SECRET_PREFIXES" in text


def test_redact_imports_when_present() -> None:
    """If runtime/redact.py exists (Phase 7b), it must also import SECRET_PREFIXES."""
    redact_path = SCRIPTS_DIR / "runtime" / "redact.py"
    if not redact_path.exists():
        pytest.skip("runtime/redact.py not yet shipped (Phase 7b)")
    text = redact_path.read_text(encoding="utf-8")
    assert "from security.patterns import" in text
    assert "SECRET_PREFIXES" in text


def test_no_local_copies_in_consumers() -> None:
    """R2 NM3 — AST scan: no consumer redefines SECRET_PREFIXES locally.

    Walks all .py files under .claude/scripts/ + scripts/ and asserts the
    name `SECRET_PREFIXES` is NEVER bound via Assign/AnnAssign outside
    security/patterns.py. Pure import-only re-exports (no assignment) are
    allowed.
    """
    skip_parts = {
        "__pycache__",
        ".archon",
        ".tmp",
        "worktrees",
        ".worktrees",
        ".codex-worktrees",
        ".refs",
        "_drafts",
        "_archive",
        "_holders",
        ".venv",          # virtualenv — third-party deps, not framework code
        "site-packages",  # belt: never scan installed packages
    }
    seen_assignment_files: list[str] = []
    for root in (SCRIPTS_DIR, REPO_ROOT / "scripts"):
        if not root.exists():
            continue
        for py_file in root.rglob("*.py"):
            # Skip the canonical source.
            if py_file == PATTERNS_FILE:
                continue
            if any(part in skip_parts for part in py_file.parts):
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "SECRET_PREFIXES":
                            seen_assignment_files.append(str(py_file))
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == "SECRET_PREFIXES":
                        seen_assignment_files.append(str(py_file))
    assert not seen_assignment_files, (
        "SECRET_PREFIXES is reassigned in non-canonical files — three-layer "
        f"parity broken. Offenders: {seen_assignment_files}"
    )


# === Module-only re-export discipline (Rule 3) ===


def test_security_init_does_not_export_callables() -> None:
    """R1 B4 — security/__init__.py exports MODULES only, NEVER callables.

    Importing `from security import requireEnabled` must fail; only
    `from security import kill_switches` (module) is allowed.
    """
    from security import kill_switches  # OK — module import
    from security import patterns  # OK — module import
    assert kill_switches is not None
    assert patterns is not None

    # Adversary path: importing the function directly should not work via
    # the security package namespace.
    with pytest.raises(ImportError):
        from security import requireEnabled  # noqa: F401

    with pytest.raises(ImportError):
        from security import KillSwitchDisabled  # noqa: F401
