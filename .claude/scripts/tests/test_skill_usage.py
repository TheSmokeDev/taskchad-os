"""Tests for cognition.skill_usage — recurrence telemetry sidecar (WS2 / Rail 3).

Covers:
- recurrence increments + the staged->eligible flip at threshold;
- prune_stale flips a stale staged row to archived (injected old last_seen_at);
- get_usage / mark_state / list_eligible physical-state behavior (Rule 2);
- REAL concurrency (M3/M4/NM1): N threads each calling record_recurrence on the
  SAME name -> final recurrence_count == N, proving the shared file_lock
  serializes the read-modify-write (no lost updates).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from cognition import skill_usage
from cognition.skill_usage import (
    SkillUsage,
    get_usage,
    list_all_usage,
    list_eligible,
    mark_state,
    prune_stale,
    record_persona_assignment,
    record_recurrence,
)

import config


@pytest.fixture
def sidecar_data_dir(tmp_path, monkeypatch):
    """Point the call-time DATA_DIR resolver at a tmp dir (Rule 1 path).

    Because skill_usage resolves DATA_DIR INSIDE each function (never at import),
    monkeypatching config.DATA_DIR is sufficient — no module reload needed. This
    also exercises that the call-time resolution actually works.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data_dir, raising=False)
    return data_dir


def _sidecar(data_dir):
    return data_dir / skill_usage.SIDECAR_FILE_NAME


# === recurrence increment + eligibility flip ===


def test_first_recurrence_creates_staged_row(sidecar_data_dir):
    u = record_recurrence("alpha", threshold=3)
    assert u.name == "alpha"
    assert u.recurrence_count == 1
    assert u.state == "staged"
    assert u.created_at
    assert u.last_seen_at
    # physically persisted
    assert _sidecar(sidecar_data_dir).exists()


def test_recurrence_increments_monotonically(sidecar_data_dir):
    record_recurrence("alpha", threshold=5)
    record_recurrence("alpha", threshold=5)
    u = record_recurrence("alpha", threshold=5)
    assert u.recurrence_count == 3
    assert u.state == "staged"  # below threshold 5


def test_flips_to_eligible_at_threshold(sidecar_data_dir):
    record_recurrence("beta", threshold=3)
    record_recurrence("beta", threshold=3)
    u = record_recurrence("beta", threshold=3)  # 3rd hit == threshold
    assert u.recurrence_count == 3
    assert u.state == "eligible"
    # and it stays eligible / keeps counting beyond threshold
    u2 = record_recurrence("beta", threshold=3)
    assert u2.recurrence_count == 4
    assert u2.state == "eligible"


def test_threshold_resolves_from_config_when_none(sidecar_data_dir, monkeypatch):
    # WS4 adds SKILL_PROMOTE_REUSE_THRESHOLD; simulate it and confirm the
    # None-sentinel call-time resolver honors it.
    monkeypatch.setattr(config, "SKILL_PROMOTE_REUSE_THRESHOLD", 2, raising=False)
    record_recurrence("gamma")
    u = record_recurrence("gamma")  # 2nd hit hits the config threshold of 2
    assert u.recurrence_count == 2
    assert u.state == "eligible"


def test_threshold_default_3_when_config_absent(sidecar_data_dir, monkeypatch):
    # Ensure the fallback (3) is used if config lacks the knob.
    monkeypatch.delattr(config, "SKILL_PROMOTE_REUSE_THRESHOLD", raising=False)
    record_recurrence("delta")
    record_recurrence("delta")
    u = record_recurrence("delta")  # 3rd hit -> default threshold 3
    assert u.recurrence_count == 3
    assert u.state == "eligible"


def test_source_session_and_path_stored(sidecar_data_dir):
    record_recurrence("eps", source_session="telegram:1:2", path="/skills/generated/x/eps")
    u = get_usage("eps")
    assert u is not None
    assert u.source_session == "telegram:1:2"
    assert u.path == "/skills/generated/x/eps"


# === get_usage / mark_state (Rule 2 physical state) ===


def test_get_usage_absent_returns_none(sidecar_data_dir):
    assert get_usage("nope") is None


def test_get_usage_reads_physical_sidecar(sidecar_data_dir):
    record_recurrence("zeta", threshold=10)
    u = get_usage("zeta")
    assert u is not None
    assert u.recurrence_count == 1
    assert u.state == "staged"


def test_mark_state_updates_and_stamps_promoted_at(sidecar_data_dir):
    record_recurrence("eta", threshold=1)  # eligible after one hit
    mark_state("eta", "promoted")
    u = get_usage("eta")
    assert u is not None
    assert u.state == "promoted"
    assert u.promoted_at  # stamped on promotion transition


def test_mark_state_absent_is_noop(sidecar_data_dir):
    mark_state("ghost", "archived")  # must not raise / must not create a row
    assert get_usage("ghost") is None


def test_mark_state_rejects_unknown_state(sidecar_data_dir):
    record_recurrence("theta")
    with pytest.raises(ValueError):
        mark_state("theta", "bogus")


# === list_eligible ===


def test_list_eligible_returns_only_eligible_meeting_threshold(sidecar_data_dir):
    # staged (below threshold)
    record_recurrence("low", threshold=3)
    # eligible
    record_recurrence("hi", threshold=2)
    record_recurrence("hi", threshold=2)
    names = {u.name for u in list_eligible(threshold=2)}
    assert names == {"hi"}


def test_list_eligible_reapplies_threshold_against_counter(sidecar_data_dir):
    # Flip to eligible at threshold 2, then query with a HIGHER threshold (5):
    # the physical counter (2) no longer meets 5, so it must be excluded even
    # though stored state == "eligible" (Rule 2 — threshold re-applied to disk).
    record_recurrence("flip", threshold=2)
    record_recurrence("flip", threshold=2)
    assert get_usage("flip").state == "eligible"
    assert list_eligible(threshold=5) == []
    assert {u.name for u in list_eligible(threshold=2)} == {"flip"}


# === prune_stale ===


def _inject_row(data_dir, usage: SkillUsage):
    """Write a single SkillUsage row directly to the sidecar for setup."""
    import json
    from dataclasses import asdict

    sidecar = _sidecar(data_dir)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({usage.name: asdict(usage)}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_prune_stale_archives_old_staged_row(sidecar_data_dir):
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    _inject_row(
        sidecar_data_dir,
        SkillUsage(name="dusty", created_at=old, last_seen_at=old, state="staged"),
    )
    archived = prune_stale(stale_days=30)
    assert archived == ["dusty"]
    assert get_usage("dusty").state == "archived"


def test_prune_stale_keeps_fresh_staged_row(sidecar_data_dir):
    fresh = datetime.now(UTC).isoformat()
    _inject_row(
        sidecar_data_dir,
        SkillUsage(name="fresh", created_at=fresh, last_seen_at=fresh, state="staged"),
    )
    archived = prune_stale(stale_days=30)
    assert archived == []
    assert get_usage("fresh").state == "staged"


def test_prune_stale_ignores_eligible_and_promoted(sidecar_data_dir):
    old = (datetime.now(UTC) - timedelta(days=99)).isoformat()
    import json
    from dataclasses import asdict

    sidecar = _sidecar(sidecar_data_dir)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    rows = {
        "elig": asdict(SkillUsage(name="elig", created_at=old, last_seen_at=old, state="eligible")),
        "prom": asdict(SkillUsage(name="prom", created_at=old, last_seen_at=old, state="promoted")),
    }
    sidecar.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    archived = prune_stale(stale_days=30)
    assert archived == []
    assert get_usage("elig").state == "eligible"
    assert get_usage("prom").state == "promoted"


def test_prune_stale_uses_created_at_when_last_seen_missing(sidecar_data_dir):
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    _inject_row(
        sidecar_data_dir,
        SkillUsage(name="nolast", created_at=old, last_seen_at="", state="staged"),
    )
    archived = prune_stale(stale_days=30)
    assert archived == ["nolast"]


def test_prune_stale_writes_no_audit_dependency(sidecar_data_dir):
    # NM2: prune_stale flips state only — it must not import/require skill_audit.
    # (Smoke-level proof: it runs with no audit module wired and returns names.)
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    _inject_row(
        sidecar_data_dir,
        SkillUsage(name="silent", created_at=old, last_seen_at=old, state="staged"),
    )
    assert prune_stale(stale_days=30) == ["silent"]


# === explicit sidecar_path override (tests/tooling) ===


def test_explicit_sidecar_path_override(tmp_path):
    # No DATA_DIR monkeypatch — prove the explicit override wins.
    target = tmp_path / "custom_usage.json"
    record_recurrence("over", threshold=1, sidecar_path=target)
    assert target.exists()
    u = get_usage("over", sidecar_path=target)
    assert u is not None and u.recurrence_count == 1


# === corrupt / torn sidecar tolerance ===


def test_corrupt_sidecar_degrades_to_empty(sidecar_data_dir):
    sidecar = _sidecar(sidecar_data_dir)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{not valid json", encoding="utf-8")
    # get_usage tolerates it (empty map), record_recurrence rebuilds cleanly.
    assert get_usage("anything") is None
    u = record_recurrence("rebuilt", threshold=2)
    assert u.recurrence_count == 1


# === REAL concurrency (M3/M4/NM1) — the load-bearing lock test ===


def test_concurrent_record_recurrence_no_lost_updates(sidecar_data_dir):
    """N threads each record recurrence ONCE on the same name.

    If the file_lock did not serialize the read-modify-write, threads would read
    a stale count before writing and the final recurrence_count would be < N
    (lost updates). Asserting == N proves the lock is load-bearing — a purely
    sequential test would NOT exercise this.
    """
    name = "hot"
    n_threads = 40
    # high threshold so the eligibility flip doesn't interfere with the count
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()  # maximize contention — all threads hit the RMW together
            record_recurrence(name, threshold=10_000)
        except BaseException as exc:  # noqa: BLE001 - record for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker errors: {errors!r}"
    final = get_usage(name)
    assert final is not None
    assert final.recurrence_count == n_threads


def test_lock_is_required_for_correctness(sidecar_data_dir, monkeypatch):
    """Lock-spy: with the lock NEUTERED, the same contention loses updates.

    This proves the lock in the previous test is load-bearing (not decorative):
    replace _file_lock with a no-op context manager and the N-thread count drops
    below N (a lost update). If this ever passes WITH == N, the RMW is being
    serialized by something other than the lock and the concurrency guarantee is
    unverified.
    """
    import contextlib

    @contextlib.contextmanager
    def _noop_lock(_path):
        yield

    monkeypatch.setattr(skill_usage, "_file_lock", _noop_lock, raising=False)

    name = "racy"
    n_threads = 40
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()  # all threads enter the unguarded RMW together
        for _ in range(3):
            # Without the lock, two threads racing os.replace on the SAME target
            # either (a) lose an update (stale read -> overwrite) or (b) on
            # Windows collide on os.replace and raise. BOTH outcomes mean the
            # final count is short of the intended total — that's the property
            # under test. Swallow the collision so the proof is the COUNT, not a
            # crashed worker (the positive test above proves the lock prevents
            # both outcomes).
            with contextlib.suppress(Exception):
                record_recurrence(name, threshold=10_000)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = get_usage(name)
    assert final is not None
    # Without the lock, concurrent RMW does NOT reliably land all 120 increments
    # (lost updates and/or Windows os.replace collisions). This is the negative
    # control for test_concurrent_record_recurrence_no_lost_updates above.
    assert final.recurrence_count < n_threads * 3


# === record_persona_assignment / list_all_usage (#429 BLOCKER 3) ===


def test_record_persona_assignment_is_a_noop_when_row_absent(sidecar_data_dir):
    """A hand-authored central skill never has a sidecar row — recording a
    scope for it must not fabricate one (that would newly RESTRICT a skill
    that was never linked through the scoped path at all)."""
    assert record_persona_assignment("never-linked", "sales") is None
    assert get_usage("never-linked") is None


def test_record_persona_assignment_appends_and_dedupes(sidecar_data_dir):
    record_recurrence("acme-crm", threshold=3)

    u1 = record_persona_assignment("acme-crm", "sales")
    assert u1 is not None
    assert u1.assigned_personas == ["sales"]

    u2 = record_persona_assignment("acme-crm", "marketing")
    assert u2.assigned_personas == ["sales", "marketing"]

    # Re-recording the same persona is a no-op, not a duplicate entry.
    u3 = record_persona_assignment("acme-crm", "sales")
    assert u3.assigned_personas == ["sales", "marketing"]

    # Physically persisted (Rule 2) — a fresh read sees the same scope.
    reread = get_usage("acme-crm")
    assert reread is not None
    assert reread.assigned_personas == ["sales", "marketing"]


def test_record_persona_assignment_blank_persona_is_a_noop(sidecar_data_dir):
    record_recurrence("acme-crm", threshold=3)
    assert record_persona_assignment("acme-crm", "   ") is None
    assert get_usage("acme-crm").assigned_personas == []


def test_list_all_usage_returns_the_full_map(sidecar_data_dir):
    record_recurrence("alpha", threshold=3)
    record_recurrence("beta", threshold=3)
    record_persona_assignment("beta", "sales")

    all_usage = list_all_usage()
    assert set(all_usage) == {"alpha", "beta"}
    assert all_usage["beta"].assigned_personas == ["sales"]
    assert all_usage["alpha"].assigned_personas == []


def test_list_all_usage_empty_sidecar(sidecar_data_dir):
    assert list_all_usage() == {}


# === scope rollback + the unrestricted sentinel (#429 design gate B1/B2) ===


def test_remove_persona_assignment_takes_a_scope_back(sidecar_data_dir):
    """A link that recorded a scope and then failed to publish must be able to
    un-record it, or the sidecar goes on claiming a persona was given a skill
    that was rolled back."""
    record_recurrence("acme-crm", threshold=3)
    record_persona_assignment("acme-crm", "sales")
    record_persona_assignment("acme-crm", "marketing")

    usage = skill_usage.remove_persona_assignment("acme-crm", "marketing")

    assert usage.assigned_personas == ["sales"]
    assert get_usage("acme-crm").assigned_personas == ["sales"]
    # Absent persona / absent row are no-ops, not errors.
    assert skill_usage.remove_persona_assignment("acme-crm", "nobody") is not None
    assert skill_usage.remove_persona_assignment("never-linked", "sales") is None


def test_mark_scope_unrestricted_stamps_the_sentinel(sidecar_data_dir):
    record_recurrence("global-skill", threshold=3)

    usage = skill_usage.mark_scope_unrestricted("global-skill")

    assert usage.assigned_personas == [skill_usage.SCOPE_UNRESTRICTED]
    assert get_usage("global-skill").assigned_personas == ["*"]


def test_mark_scope_unrestricted_never_widens_an_existing_scope(sidecar_data_dir):
    """A promote driven BY a linked-skill intake must not turn that intake's
    one-persona scope into "everyone" on its way through."""
    record_recurrence("sales-skill", threshold=3)
    record_persona_assignment("sales-skill", "sales")

    usage = skill_usage.mark_scope_unrestricted("sales-skill")

    assert usage.assigned_personas == ["sales"]


def test_mark_scope_unrestricted_is_a_noop_without_a_row(sidecar_data_dir):
    assert skill_usage.mark_scope_unrestricted("never-staged") is None
