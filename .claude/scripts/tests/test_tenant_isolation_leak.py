"""Cross-tenant leak harness — the #1 acceptance gate (Tenant Isolation v0 WS1).

Proves the process-per-tenant boundary holds: a secret seeded into tenant A's
vault is NEVER surfaced by a fresh tenant-B process across recall, chat session,
episodes, and ``/working``. The boundary under test is ``config.py``'s import-time
profile singleton (``config.py:40``) — each tenant runs as a NAMED profile under
its own ``~/.homie/profiles/<name>/`` root, so every data root resolves under that
tenant alone.

Design (mirrors test_default_persona_backcompat.py:56-93):
    - Two tmp named profiles ``tenant-a`` / ``tenant-b`` under one tmp root.
    - The secret is seeded ONLY into tenant A across all four surfaces.
    - Each probe runs in a FRESH SUBPROCESS pinned to a tenant's profile (HOME +
      USERPROFILE + HOMIE_HOME), resolving the data root via the config singleton
      INSIDE the subprocess — so the test exercises real import-time resolution,
      not a hand-passed path.

The two mandatory poles:
    1. BLIND  — tenant B's probe MUST report ABSENT on every surface.
    2. NEGATIVE CONTROL — tenant A's OWN probe MUST report FOUND on every surface.
       Without a passing negative control the harness is inert: a probe that
       never finds the secret would "pass" the blind assertion for the wrong
       reason. The negative control is what guarantees this test CAN fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import tenant_fixtures as tf


@pytest.fixture
def two_tenants(tmp_path: Path) -> Path:
    """Stand up tenant-a (seeded) + tenant-b (clean) under one tmp root.

    Returns the tmp root. ``build_tenant_profile`` creates both skeletons;
    ``seed_secret`` plants ``TENANT_A_SECRET`` into tenant A only. Tenant B is
    deliberately left clean (no MEMORY.md / chat.db / episodes / WORKING.md
    content) so any FOUND from tenant B is a real leak, not pre-seeded noise.
    """
    paths_a = tf.build_tenant_profile(tmp_path, "tenant-a")
    tf.build_tenant_profile(tmp_path, "tenant-b")
    tf.seed_secret(paths_a, tf.TENANT_A_SECRET)
    return tmp_path


# ── Sanity gates (prove the harness is not inert) ───────────────────────────


def test_named_profiles_resolve_distinct_roots(two_tenants: Path) -> None:
    """Both tenants resolve as NAMED profiles with DISTINCT memory roots.

    PRP R2 NM2: the env pinning (HOME + USERPROFILE + HOMIE_HOME) must select the
    NAMED profile, not ``"custom"``. If either tenant fell back to ``"custom"``
    or both resolved the same root, the leak harness would prove nothing.
    """
    assert tf.profile_name_subprocess(two_tenants, "tenant-a") == "tenant-a"
    assert tf.profile_name_subprocess(two_tenants, "tenant-b") == "tenant-b"

    root_a = tf.resolve_memory_dir_subprocess(two_tenants, "tenant-a")
    root_b = tf.resolve_memory_dir_subprocess(two_tenants, "tenant-b")
    assert root_a != root_b, (
        "tenant-a and tenant-b resolved the SAME memory root — the harness "
        f"would be inert.\n  a: {root_a}\n  b: {root_b}"
    )
    assert "tenant-a" in root_a
    assert "tenant-b" in root_b


# ── NEGATIVE CONTROL — tenant A sees its own secret (the test CAN fail) ──────


@pytest.mark.parametrize("surface", tf.SURFACES)
def test_negative_control_tenant_a_surfaces_secret(
    two_tenants: Path, surface: str
) -> None:
    """NEGATIVE CONTROL: tenant A's OWN probe MUST surface the secret.

    This is the guarantee that the blind assertion below is meaningful. If this
    fails, the probe mechanism is broken (wrong root, wrong scan) and the blind
    ABSENT result would be a false pass — so a failure here fails the suite.
    """
    verdict = tf.probe_surface_subprocess(two_tenants, "tenant-a", surface)
    assert verdict == "FOUND", (
        f"NEGATIVE CONTROL FAILED — tenant A could not surface its OWN secret on "
        f"surface '{surface}'. The probe is inert; the blind test is meaningless."
    )


# ── BLIND — tenant B is blind to tenant A's secret on every surface ──────────


@pytest.mark.parametrize("surface", tf.SURFACES)
def test_tenant_b_is_blind_to_tenant_a_secret(
    two_tenants: Path, surface: str
) -> None:
    """CRUX: tenant B's probe MUST report ABSENT on every surface.

    A FOUND here is a cross-tenant leak — the exact P0 the PRD calls
    reputation-ending. Paired with the negative control above, an ABSENT verdict
    is a real proof of isolation, not a probe that simply never matches.
    """
    verdict = tf.probe_surface_subprocess(two_tenants, "tenant-b", surface)
    assert verdict == "ABSENT", (
        f"CROSS-TENANT LEAK — tenant B surfaced tenant A's secret on surface "
        f"'{surface}'. The process-per-tenant boundary did not hold."
    )
