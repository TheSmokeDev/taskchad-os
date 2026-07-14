"""Browser Homie runner — CAS claim, stale-claim sweep, receipts, spawn seam.

The runner exists so the chat bot NEVER drives a browser on its event loop
(the 2026-07-13 wedge). These tests lock the four load-bearing behaviors:
exactly-one-claimer CAS, the spawn-not-inline handler seam, the receipt on
both outcomes, and the cadence-tick stale-claim sweep.
"""

from __future__ import annotations

import asyncio
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import browser_control  # type: ignore[import-not-found]  # noqa: E402
import browser_workflows  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402
import shared  # noqa: E402
from social import post_executor  # noqa: E402
from social.browser_homie_runner import run_post, run_sweep  # noqa: E402
from social.service import SocialPostService  # noqa: E402


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "queue.db"


@pytest.fixture()
def svc(db_path: Path) -> SocialPostService:
    return SocialPostService(db_path=db_path)


def _approved_post(svc: SocialPostService, channel: str = "linkedin") -> int:
    pid = svc.create_draft(channel=channel, title="t", body="hello world")
    svc.approve_post(pid)
    return pid


# ---------------------------------------------------------------- CAS claim


def test_claim_cas_exactly_one_winner(svc: SocialPostService) -> None:
    pid = _approved_post(svc)
    assert svc.claim_post(pid) is True
    assert svc.claim_post(pid) is False  # double-tap / cron race loses


def test_claim_requires_approved_status(svc: SocialPostService) -> None:
    pid = svc.create_draft(channel="linkedin", title="t", body="b")
    assert svc.claim_post(pid) is False


def test_clear_claim_allows_reclaim(svc: SocialPostService) -> None:
    pid = _approved_post(svc)
    assert svc.claim_post(pid)
    assert svc.clear_claim(pid)
    assert svc.claim_post(pid) is True


# ---------------------------------------------------------------- runner


def test_run_post_claimed_dispatches_and_sends_receipt(
    monkeypatch: pytest.MonkeyPatch, svc: SocialPostService, db_path: Path
) -> None:
    pid = _approved_post(svc)
    assert svc.claim_post(pid)  # spawner claims, runner runs --claimed

    dispatched: list[int] = []

    def fake_dispatch(post_id: int, *, db_path=None) -> bool:
        dispatched.append(post_id)
        SocialPostService(db_path=db_path).mark_posted(post_id, post_url="https://x/1")
        return True

    receipts: list[str] = []
    monkeypatch.setattr(post_executor, "dispatch_post", fake_dispatch)
    import social.notify as notify

    monkeypatch.setattr(notify, "send_text_to_telegram", lambda t: receipts.append(t) or True)

    rc = run_post(pid, claimed=True, db_path=str(db_path))

    assert rc == 0
    assert dispatched == [pid]
    assert len(receipts) == 1 and "https://x/1" in receipts[0]


def test_run_post_unclaimed_loses_race_is_noop(
    monkeypatch: pytest.MonkeyPatch, svc: SocialPostService, db_path: Path
) -> None:
    pid = _approved_post(svc)
    assert svc.claim_post(pid)  # someone else owns it

    def explode(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("dispatch_post must not run when the claim is lost")

    monkeypatch.setattr(post_executor, "dispatch_post", explode)

    assert run_post(pid, claimed=False, db_path=str(db_path)) == 0


def test_run_post_failure_sends_failure_receipt(
    monkeypatch: pytest.MonkeyPatch, svc: SocialPostService, db_path: Path
) -> None:
    pid = _approved_post(svc)
    assert svc.claim_post(pid)

    def fake_dispatch(post_id: int, *, db_path=None) -> bool:
        SocialPostService(db_path=db_path).mark_failed(post_id, error="composer never hydrated")
        return False

    receipts: list[str] = []
    monkeypatch.setattr(post_executor, "dispatch_post", fake_dispatch)
    import social.notify as notify

    monkeypatch.setattr(notify, "send_text_to_telegram", lambda t: receipts.append(t) or True)

    rc = run_post(pid, claimed=True, db_path=str(db_path))

    assert rc == 1
    assert len(receipts) == 1 and "composer never hydrated" in receipts[0]


# ---------------------------------------------------------------- sweep


def _stale_claim(svc: SocialPostService, pid: int, minutes_ago: int) -> None:
    stamp = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat(timespec="seconds")
    svc.set_post_fields(pid, claimed_at=stamp)


def test_sweep_fails_stale_claim_and_notifies(
    monkeypatch: pytest.MonkeyPatch, svc: SocialPostService, db_path: Path
) -> None:
    pid = _approved_post(svc)
    assert svc.claim_post(pid)
    _stale_claim(svc, pid, minutes_ago=60)

    receipts: list[str] = []
    import social.notify as notify

    monkeypatch.setattr(notify, "send_text_to_telegram", lambda t: receipts.append(t) or True)

    summary = post_executor.sweep_stale_claims(db_path=db_path, ttl_minutes=15)

    assert summary["swept"] == 1
    post = svc.get_post(pid)
    assert post is not None and post.status == "failed"
    assert "runner died mid-flight" in (post.error or "")
    assert len(receipts) == 1 and "never finished" in receipts[0]


def test_sweep_ignores_fresh_claims(svc: SocialPostService, db_path: Path) -> None:
    pid = _approved_post(svc)
    assert svc.claim_post(pid)  # fresh claim, just made

    summary = post_executor.sweep_stale_claims(db_path=db_path, ttl_minutes=15)

    assert summary["swept"] == 0
    post = svc.get_post(pid)
    assert post is not None and post.status == "approved"


def test_run_sweep_cli_delegates(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    called: list[dict] = []
    monkeypatch.setattr(
        post_executor, "sweep_stale_claims", lambda **kw: called.append(kw) or {"swept": 0}
    )
    assert run_sweep(db_path=str(db_path)) == 0
    assert called and called[0]["db_path"] == str(db_path)


# ------------------------------------------------- cadence claims before drive


def test_dispatch_due_posts_skips_already_claimed_rows(
    monkeypatch: pytest.MonkeyPatch, svc: SocialPostService, db_path: Path
) -> None:
    pid = _approved_post(svc)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    svc.schedule_post(pid, past)
    assert svc.claim_post(pid)  # an approve-tap runner already owns it

    def explode(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("cadence must not dispatch a claimed row")

    monkeypatch.setattr(post_executor, "dispatch_post", explode)

    summary = post_executor.dispatch_due_posts(db_path=db_path)

    assert summary["dispatched"] == 0 and summary["failed"] == 0


# ------------------------------------------------- handler seam: spawn, not drive


@pytest.mark.asyncio
async def test_social_post_handler_spawns_runner_not_inline(
    monkeypatch: pytest.MonkeyPatch, svc: SocialPostService, db_path: Path
) -> None:
    import config

    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", db_path)
    pid = _approved_post(svc)

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        shared, "spawn_detached", lambda cmd, **kw: spawned.append(cmd) or 4242
    )
    monkeypatch.setattr(
        post_executor,
        "dispatch_post",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("handler must never dispatch inline on the event loop")
        ),
    )

    reply = await core_handlers.handle_social(None, None, f"post {pid}")

    assert "Browser Homie" in reply and "4242" in reply
    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[1].endswith("browser_homie_runner.py")
    assert argv[2:] == ["--post-id", str(pid), "--claimed"]
    post = svc.get_post(pid)
    assert post is not None and post.claimed_at is not None  # spawner holds the claim


@pytest.mark.asyncio
async def test_social_post_handler_double_tap_is_noop(
    monkeypatch: pytest.MonkeyPatch, svc: SocialPostService, db_path: Path
) -> None:
    import config

    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", db_path)
    pid = _approved_post(svc)
    assert svc.claim_post(pid)  # first tap's claim

    def explode(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("second tap must not spawn a second runner")

    monkeypatch.setattr(shared, "spawn_detached", explode)

    reply = await core_handlers.handle_social(None, None, f"post {pid}")

    assert "already being posted" in reply


@pytest.mark.asyncio
async def test_social_post_handler_clears_claim_on_spawn_failure(
    monkeypatch: pytest.MonkeyPatch, svc: SocialPostService, db_path: Path
) -> None:
    import config

    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", db_path)
    pid = _approved_post(svc)

    def broken_spawn(*_a, **_k):
        raise OSError("interpreter missing")

    monkeypatch.setattr(shared, "spawn_detached", broken_spawn)

    reply = await core_handlers.handle_social(None, None, f"post {pid}")

    assert "could not start" in reply
    post = svc.get_post(pid)
    assert post is not None and post.claimed_at is None  # claim released for retry


# ------------------------------------------------- loop-liveness regressions


@pytest.mark.asyncio
async def test_reddit_comment_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """2026-07-13 wedge class: a slow browser write must not starve the loop."""
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)  # browser_write_lock target
    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 18222)
    monkeypatch.setattr(
        browser_control, "browser_readiness", lambda *, port, target="desktop": {"enabled": True}
    )
    monkeypatch.setattr(
        browser_workflows,
        "require_browser_workflow_permission",
        lambda *_a, **_k: SimpleNamespace(allowed=True, outcome="succeeded", reason="test"),
    )
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_k: None)

    def slow_drive(port: int, thread_url: str, body: str) -> tuple[bool, str]:
        _time.sleep(0.4)  # stands in for the 60s+ agent-browser chain
        return True, "comment submitted"

    monkeypatch.setattr(core_handlers, "_reddit_drive_comment", slow_drive)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        reply = await core_handlers.handle_reddit(
            None,
            None,
            "comment https://www.reddit.com/r/test/comments/abc123/thread/ | hello "
            "| post this comment to reddit now",
        )
    finally:
        ticker_task.cancel()

    assert "Comment posted" in reply
    assert ticks >= 5, f"event loop starved during /reddit comment (ticks={ticks})"


@pytest.mark.asyncio
async def test_social_write_dispatch_runs_off_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_handle_social_write's executor.dispatch must ride to_thread + the lock."""
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    class SlowExecutor:
        def dispatch(self, subtask):
            _time.sleep(0.4)
            return SimpleNamespace(status="completed", metadata={}, error=None)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        receipt = await asyncio.wait_for(
            asyncio.to_thread(
                core_handlers._dispatch_social_write_locked, SlowExecutor(), None
            ),
            timeout=5,
        )
    finally:
        ticker_task.cancel()

    assert receipt.status == "completed"
    assert ticks >= 5, f"event loop starved during social write dispatch (ticks={ticks})"
