"""Tests for orchestration/suggestions.py — one test per code path.

Covers the persisted proposal store: add/dedup-latch/cap, resolve-by-ref,
dismiss latch, the accept boundary (sync + async, incl. the create_fn-raises
contract), corrupt-file degradation, and the Rule 1 call-time STATE_DIR
resolution (the store writes to the monkeypatched tmp dir, never the install
dir).
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import config  # noqa: E402
from orchestration import suggestions as sg  # noqa: E402

_SPEC = {"persona_id": "default", "prompt": "do a thing", "schedule": "0 8 * * *", "next_run": None}


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point the store at a tmp STATE_DIR (Rule 1 call-time resolution)."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", d)
    return d


def _add(dedup_key="k1", *, title="Thing", source="catalog", spec=None):
    return sg.add_suggestion(
        title=title,
        description="desc",
        source=source,
        job_spec=dict(spec or _SPEC),
        dedup_key=dedup_key,
    )


# --------------------------------------------------------------------------- #
# add_suggestion
# --------------------------------------------------------------------------- #

def test_add_happy_path_returns_record_and_persists(state_dir) -> None:
    rec = _add("k1")
    assert rec is not None
    assert rec["status"] == "pending"
    assert rec["source"] == "catalog"
    assert len(rec["id"]) == 12
    # File written under the monkeypatched STATE_DIR (Rule 1), NOT the install dir.
    store = state_dir / "suggestions.json"
    assert store.exists()
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data["suggestions"][0]["dedup_key"] == "k1"


def test_add_invalid_source_raises(state_dir) -> None:
    with pytest.raises(ValueError, match="unknown suggestion source"):
        _add("k1", source="bogus")


def test_add_requires_title_and_dedup_key(state_dir) -> None:
    with pytest.raises(ValueError, match="title and dedup_key"):
        _add("   ", title="ok")
    with pytest.raises(ValueError, match="title and dedup_key"):
        _add("k1", title="   ")


def test_duplicate_pending_dedup_skipped(state_dir) -> None:
    assert _add("k1") is not None
    assert _add("k1") is None
    assert len(sg.list_pending()) == 1


def test_max_pending_cap_drops_sixth(state_dir) -> None:
    for i in range(sg.MAX_PENDING):
        assert _add(f"k{i}") is not None
    assert _add("k-overflow") is None
    assert len(sg.list_pending()) == sg.MAX_PENDING


# --------------------------------------------------------------------------- #
# dedup latch — dismissed / accepted never re-offered
# --------------------------------------------------------------------------- #

def test_dismissed_dedup_key_never_reoffered(state_dir) -> None:
    rec = _add("k1")
    # dismiss resolves by id (dedup_key is not a get_suggestion ref).
    assert sg.dismiss_suggestion(rec["id"]) is True
    # Re-adding the same dedup_key is refused (latched forever).
    assert _add("k1") is None
    assert sg.list_pending() == []


def test_accepted_dedup_key_never_reoffered(state_dir) -> None:
    rec = _add("k1")
    sg.accept_suggestion(rec["id"], create_fn=lambda spec: {"ok": True})
    assert sg.get_suggestion(rec["id"])["status"] == "accepted"
    # Re-adding the same dedup_key is refused (accepted latches forever).
    assert _add("k1") is None


# --------------------------------------------------------------------------- #
# get_suggestion resolution
# --------------------------------------------------------------------------- #

def test_get_by_id_index_and_title(state_dir) -> None:
    rec = _add("k1", title="Alpha")
    assert sg.get_suggestion(rec["id"])["id"] == rec["id"]
    assert sg.get_suggestion("1")["id"] == rec["id"]          # 1-based pending index
    assert sg.get_suggestion("ALPHA")["id"] == rec["id"]      # case-insensitive title
    assert sg.get_suggestion("nope") is None


# --------------------------------------------------------------------------- #
# dismiss
# --------------------------------------------------------------------------- #

def test_dismiss_latches_status_and_resolved_at(state_dir) -> None:
    rec = _add("k1")
    assert sg.dismiss_suggestion(rec["id"]) is True
    dismissed = sg.get_suggestion(rec["id"])
    assert dismissed["status"] == "dismissed"
    assert "resolved_at" in dismissed
    assert sg.dismiss_suggestion("unknown-ref") is False


# --------------------------------------------------------------------------- #
# accept — sync
# --------------------------------------------------------------------------- #

def test_accept_calls_create_fn_and_flips_status(state_dir) -> None:
    rec = _add("k1")
    seen = {}

    def create(spec):
        seen.update(spec)
        return {"task_id": 7, **spec}

    job = sg.accept_suggestion(rec["id"], create_fn=create)
    assert job["task_id"] == 7
    assert seen["prompt"] == "do a thing"
    assert sg.get_suggestion(rec["id"])["status"] == "accepted"


def test_accept_merges_origin_when_absent(state_dir) -> None:
    rec = _add("k1")
    captured = {}
    sg.accept_suggestion(
        rec["id"],
        create_fn=lambda spec: captured.update(spec) or spec,
        origin={"platform": "telegram", "chat_id": "42"},
    )
    assert captured["origin"] == {"platform": "telegram", "chat_id": "42"}


def test_accept_raising_create_fn_stays_pending_and_propagates(state_dir) -> None:
    rec = _add("k1")

    class BoomError(Exception):
        pass

    def boom(spec):
        raise BoomError("refused")

    with pytest.raises(BoomError):
        sg.accept_suggestion(rec["id"], create_fn=boom)
    # Guard refused server-side -> must NOT latch as accepted.
    assert sg.get_suggestion(rec["id"])["status"] == "pending"


def test_accept_unknown_or_non_pending_returns_none(state_dir) -> None:
    assert sg.accept_suggestion("nope", create_fn=lambda s: s) is None
    rec = _add("k1")
    sg.dismiss_suggestion(rec["id"])
    # Already dismissed -> not pending -> None, create_fn never called.
    called = []
    assert (
        sg.accept_suggestion(
            rec["id"], create_fn=lambda s: called.append(s) or s
        )
        is None
    )
    assert called == []


# --------------------------------------------------------------------------- #
# accept — async twin
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_accept_async_happy_path(state_dir) -> None:
    rec = _add("k1")

    async def create(spec):
        return {"task_id": 11, **spec}

    job = await sg.accept_suggestion_async(rec["id"], create_fn=create)
    assert job["task_id"] == 11
    assert sg.get_suggestion(rec["id"])["status"] == "accepted"


@pytest.mark.asyncio
async def test_accept_async_raising_stays_pending(state_dir) -> None:
    rec = _add("k1")

    class BoomError(Exception):
        pass

    async def boom(spec):
        raise BoomError("refused")

    with pytest.raises(BoomError):
        await sg.accept_suggestion_async(rec["id"], create_fn=boom)
    assert sg.get_suggestion(rec["id"])["status"] == "pending"


# --------------------------------------------------------------------------- #
# clear_resolved
# --------------------------------------------------------------------------- #

def test_clear_resolved_drops_accepted_keeps_dismissed_and_pending(state_dir) -> None:
    a = _add("k-accept")
    d = _add("k-dismiss")
    _add("k-pending")
    sg.accept_suggestion(a["id"], create_fn=lambda s: s)
    sg.dismiss_suggestion(d["id"])
    removed = sg.clear_resolved()
    assert removed == 1
    statuses = {s["dedup_key"]: s["status"] for s in sg.load_suggestions()}
    assert "k-accept" not in statuses           # accepted pruned
    assert statuses["k-dismiss"] == "dismissed"  # retained for dedup latch
    assert statuses["k-pending"] == "pending"


# --------------------------------------------------------------------------- #
# corrupt-file degradation
# --------------------------------------------------------------------------- #

def test_corrupt_store_degrades_to_empty(state_dir) -> None:
    (state_dir / "suggestions.json").write_text("not json{", encoding="utf-8")
    assert sg.load_suggestions() == []
    assert sg.list_pending() == []
    # And a fresh add still works (overwrites the corrupt file).
    assert _add("k1") is not None
    assert len(sg.list_pending()) == 1


def test_malformed_json_shape_degrades_to_empty(state_dir) -> None:
    (state_dir / "suggestions.json").write_text('{"suggestions": "nope"}', encoding="utf-8")
    assert sg.load_suggestions() == []


def test_bare_list_json_is_tolerated(state_dir) -> None:
    # A legacy bare-list file is coerced to the wrapped shape on read.
    rec = {"id": "abc123abc123", "status": "pending", "dedup_key": "k1", "title": "Legacy"}
    (state_dir / "suggestions.json").write_text(json.dumps([rec]), encoding="utf-8")
    assert len(sg.list_pending()) == 1


# --------------------------------------------------------------------------- #
# F1 — concurrent accept must create exactly once (atomic claim)
# --------------------------------------------------------------------------- #

def test_claim_and_resolve_are_token_cas(state_dir) -> None:
    # Claiming a pending row returns a token and flips it to accepting;
    # a second claim on the now-accepting row returns None.
    rec = _add("k1")
    token = sg._claim_for_accept(rec["id"])
    assert token is not None
    assert sg._claim_for_accept(rec["id"]) is None
    raw = json.loads((state_dir / "suggestions.json").read_text(encoding="utf-8"))
    row = raw["suggestions"][0]
    assert row["status"] == "accepting"
    assert row["claim_token"] == token
    assert "accepting_at" in row
    # Resolve with the WRONG token is a no-op (ABA / re-claim safety).
    assert sg._resolve_claim(rec["id"], "wrong-token", to="accepted") is False
    # Resolve with OUR token commits and clears the claim metadata.
    assert sg._resolve_claim(rec["id"], token, to="accepted") is True
    raw = json.loads((state_dir / "suggestions.json").read_text(encoding="utf-8"))
    row = raw["suggestions"][0]
    assert row["status"] == "accepted"
    assert "claim_token" not in row and "accepting_at" not in row


def test_concurrent_accept_creates_exactly_once(state_dir) -> None:
    rec = _add("k1")
    create_calls: list[dict] = []
    calls_lock = threading.Lock()
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def create(spec):
        with calls_lock:
            create_calls.append(spec)
        time.sleep(0.05)  # widen the in-flight window for the loser to hit the claim
        return {"task_id": 1}

    def worker(name):
        barrier.wait()
        results[name] = sg.accept_suggestion(rec["id"], create_fn=create)

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # create_fn ran EXACTLY once; one winner got the job, one loser got None.
    assert len(create_calls) == 1
    assert sum(1 for v in results.values() if v is not None) == 1
    assert sum(1 for v in results.values() if v is None) == 1
    assert sg.get_suggestion(rec["id"])["status"] == "accepted"


@pytest.mark.asyncio
async def test_concurrent_accept_async_creates_exactly_once(state_dir) -> None:
    rec = _add("k1")
    calls: list[dict] = []

    async def create(spec):
        calls.append(spec)
        await asyncio.sleep(0.01)  # yield so the second coroutine interleaves
        return {"task_id": 1}

    results = await asyncio.gather(
        sg.accept_suggestion_async(rec["id"], create_fn=create),
        sg.accept_suggestion_async(rec["id"], create_fn=create),
    )
    assert len(calls) == 1
    assert sum(1 for r in results if r is not None) == 1
    assert sg.get_suggestion(rec["id"])["status"] == "accepted"


def test_accept_failure_rolls_claim_back_to_pending_and_is_retryable(state_dir) -> None:
    rec = _add("k1")

    def boom(spec):
        raise RuntimeError("refused")

    with pytest.raises(RuntimeError):
        sg.accept_suggestion(rec["id"], create_fn=boom)
    # Rolled back from the accepting claim, not stuck in-flight.
    assert sg.get_suggestion(rec["id"])["status"] == "pending"
    # And it can be accepted again (the claim is re-acquirable).
    job = sg.accept_suggestion(rec["id"], create_fn=lambda s: {"task_id": 2})
    assert job == {"task_id": 2}
    assert sg.get_suggestion(rec["id"])["status"] == "accepted"


def test_dismiss_during_accept_is_not_overwritten(state_dir) -> None:
    rec = _add("k1")

    def create(spec):
        # Operator dismisses WHILE the create is in flight (row is "accepting").
        sg.dismiss_suggestion(rec["id"])
        return {"task_id": 1}

    job = sg.accept_suggestion(rec["id"], create_fn=create)
    # create ran (job returned) but the racing dismiss must WIN the record:
    # the accepted commit is CAS against the claim and does not clobber it.
    assert job == {"task_id": 1}
    assert sg.get_suggestion(rec["id"])["status"] == "dismissed"


# --------------------------------------------------------------------------- #
# Stale-claim recovery — a crash between claim and commit/rollback strands the
# row in "accepting"; recovery heals it before any decision read.
# --------------------------------------------------------------------------- #

def _force_accepting(state_dir, sid: str, *, age_seconds: float, token: str = "tok-forced") -> None:
    """Simulate a stranded/live claim: force a row to accepting with a chosen age."""
    store = state_dir / "suggestions.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    ts = (datetime.now() - timedelta(seconds=age_seconds)).isoformat()
    for s in data["suggestions"]:
        if s["id"] == sid:
            s["status"] = "accepting"
            s["accepting_at"] = ts
            s["claim_token"] = token
    store.write_text(json.dumps(data), encoding="utf-8")


def test_claim_is_stale_uses_ttl_boundary() -> None:
    now = datetime.now()
    ttl = sg._ACCEPT_CLAIM_TTL_S
    # Fresh (well under TTL) -> not stale; old (over TTL) -> stale.
    fresh = (now - timedelta(seconds=1)).isoformat()
    old = (now - timedelta(seconds=ttl + 5)).isoformat()
    assert sg._claim_is_stale(fresh, now) is False
    assert sg._claim_is_stale(old, now) is True
    # Missing / unparseable timestamps recover (treated as stale).
    assert sg._claim_is_stale(None, now) is True
    assert sg._claim_is_stale("not-a-timestamp", now) is True
    # TTL must exceed scheduled_api's 10s HTTP client timeout.
    assert ttl > 10.0


def test_stale_accepting_recovers_to_pending_and_is_retryable(state_dir) -> None:
    rec = _add("k1")
    _force_accepting(state_dir, rec["id"], age_seconds=sg._ACCEPT_CLAIM_TTL_S + 30)
    # A decision read heals it: it reappears as pending, not a zombie.
    assert [s["id"] for s in sg.list_pending()] == [rec["id"]]
    assert sg.get_suggestion(rec["id"])["status"] == "pending"
    # And accept now succeeds (claim is re-acquirable after recovery).
    job = sg.accept_suggestion(rec["id"], create_fn=lambda s: {"task_id": 9})
    assert job == {"task_id": 9}
    assert sg.get_suggestion(rec["id"])["status"] == "accepted"


def test_fresh_accepting_stays_hidden_and_non_duplicating(state_dir) -> None:
    rec = _add("k1")
    _force_accepting(state_dir, rec["id"], age_seconds=1)  # fresh, in-flight
    # Fresh claim is NOT recovered: hidden from pending, not accept-able.
    assert sg.list_pending() == []
    assert sg.accept_suggestion(rec["id"], create_fn=lambda s: {"task_id": 1}) is None
    # And re-adding the same dedup_key does not duplicate the in-flight proposal.
    assert _add("k1") is None
    raw = json.loads((state_dir / "suggestions.json").read_text(encoding="utf-8"))
    assert len(raw["suggestions"]) == 1
    assert raw["suggestions"][0]["status"] == "accepting"


def test_dedup_reoffer_after_stale_recovery(state_dir) -> None:
    rec = _add("k1")
    _force_accepting(state_dir, rec["id"], age_seconds=sg._ACCEPT_CLAIM_TTL_S + 30)
    # add_suggestion runs recovery first: the row is now pending again, so the
    # same dedup_key still dedups to None (the recovered row IS the offer) and no
    # duplicate is created.
    assert _add("k1") is None
    pend = sg.list_pending()
    assert len(pend) == 1 and pend[0]["id"] == rec["id"]


@pytest.mark.asyncio
async def test_async_stale_accepting_is_retryable(state_dir) -> None:
    rec = _add("k1")
    _force_accepting(state_dir, rec["id"], age_seconds=sg._ACCEPT_CLAIM_TTL_S + 30)

    async def create(spec):
        return {"task_id": 12}

    job = await sg.accept_suggestion_async(rec["id"], create_fn=create)
    assert job == {"task_id": 12}
    assert sg.get_suggestion(rec["id"])["status"] == "accepted"
