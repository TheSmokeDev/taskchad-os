"""Handler-level tests for /reddit comment + /reddit post (ban-safety).

These go through the real `handle_reddit` parse + gate + readiness path. They
prove the two adversarial-review FIX-REQUIRED items are closed:

  FIX #1 — body-can-auto-approve: a comment/post body whose LAST words are the
           approval phrase, with NO separate trailing confirmation segment, is
           BLOCKED. A correct isolated trailing confirmation segment is ALLOWED.
  FIX #2 — readiness refusal: when the visible Chrome is not ready
           (`readiness["enabled"]` is False), no drive happens and the attempt is
           audited as failed.

Plus the cheap URL/subreddit injection hardening.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHAT_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import browser_control  # noqa: E402
import core_handlers  # noqa: E402


def _patch_reddit(monkeypatch, *, enabled: bool = True):
    """Record audit rows + drives; never touch a real browser.

    Returns (audits, drives) lists. `drives` is appended to ONLY when a
    `_reddit_drive_*` fires — its emptiness proves no write was driven.
    """
    audits: list = []
    drives: list = []

    def _fake_audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(core_handlers, "_audit_browser_action", _fake_audit)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port=None: {
            "enabled": enabled,
            "cdp_port": port or 9222,
            "cdp_reachable": enabled,
        },
    )

    def _fake_comment(port, thread_url, body):
        drives.append(("comment", thread_url, body))
        return True, "comment submitted (fake)"

    def _fake_post(port, subreddit, title, body):
        drives.append(("post", subreddit, title, body))
        return True, "post submitted (fake)"

    monkeypatch.setattr(core_handlers, "_reddit_drive_comment", _fake_comment)
    monkeypatch.setattr(core_handlers, "_reddit_drive_post", _fake_post)
    return audits, drives


# ── FIX #1: body-ends-with-phrase is BLOCKED (comment + post) ───────────────


def test_reddit_comment_body_ENDS_WITH_phrase_is_blocked(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            # Community-voiced draft legitimately ending in the CTA phrase, with
            # NO separate confirmation segment.
            args="comment https://www.reddit.com/r/x/comments/1/ | Totally agree, you should post this comment to reddit now",
        )
    )

    assert "blocked" in reply.lower()
    assert drives == []  # NO comment driven


def test_reddit_post_body_ENDS_WITH_phrase_is_blocked(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="post insurance | A title | Body copy that says post this to reddit now",
        )
    )

    assert "blocked" in reply.lower()
    assert drives == []  # NO post driven


# ── FIX #1: correct isolated confirmation segment is ALLOWED ────────────────


def test_reddit_comment_allows_with_isolated_confirmation_segment(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="comment https://www.reddit.com/r/x/comments/1/ | helpful reply | post this comment to reddit now",
        )
    )

    assert "blocked" not in reply.lower()
    assert len(drives) == 1
    kind, thread_url, body = drives[0]
    assert kind == "comment"
    assert thread_url == "https://www.reddit.com/r/x/comments/1/"
    # the confirmation segment never enters the landed body
    assert "post this comment to reddit now" not in body
    assert body == "helpful reply"


def test_reddit_post_allows_with_isolated_confirmation_segment(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="post insurance | My Title | My body copy | post this to reddit now",
        )
    )

    assert "blocked" not in reply.lower()
    assert len(drives) == 1
    kind, subreddit, title, body = drives[0]
    assert kind == "post"
    assert subreddit == "insurance"
    assert title == "My Title"
    assert "post this to reddit now" not in body
    assert body == "My body copy"


def test_reddit_post_body_with_literal_pipe_is_preserved(monkeypatch) -> None:
    """A body containing a literal '|' is preserved intact — only the final
    confirmation segment is peeled."""
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="post insurance | My Title | line one | line two | post this to reddit now",
        )
    )

    assert "blocked" not in reply.lower()
    assert len(drives) == 1
    _, subreddit, title, body = drives[0]
    assert subreddit == "insurance"
    assert title == "My Title"
    assert body == "line one | line two"  # pipes inside the body kept


def test_reddit_comment_mid_body_phrase_still_blocked(monkeypatch) -> None:
    """The existing mid-body-phrase block must still hold (no separate segment)."""
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="comment https://www.reddit.com/r/x/comments/1/ | I love how 'post this comment to reddit now' reads",
        )
    )

    assert "blocked" in reply.lower()
    assert drives == []


# ── FIX #2: visible-Chrome readiness refusal before driving ─────────────────


def test_reddit_comment_refuses_when_not_ready(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch, enabled=False)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="comment https://www.reddit.com/r/x/comments/1/ | helpful reply | post this comment to reddit now",
        )
    )

    assert "not ready" in reply.lower()
    assert drives == []  # refused BEFORE any drive
    # audited as failed with the readiness reason
    assert any(
        a.get("outcome") == "failed" and "not ready" in (a.get("reason") or "")
        for a in audits
    )


def test_reddit_post_refuses_when_not_ready(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch, enabled=False)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="post insurance | My Title | My body copy | post this to reddit now",
        )
    )

    assert "not ready" in reply.lower()
    assert drives == []
    assert any(
        a.get("outcome") == "failed" and "not ready" in (a.get("reason") or "")
        for a in audits
    )


# ── Cheap hardening: URL / subreddit injection rejection ────────────────────


def test_reddit_comment_rejects_non_reddit_url(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="comment https://evil.example.com/phish | hi | post this comment to reddit now",
        )
    )

    assert "rejected" in reply.lower()
    assert drives == []


def test_reddit_comment_rejects_non_http_url(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="comment file:///C:/secrets.html | hi | post this comment to reddit now",
        )
    )

    assert "rejected" in reply.lower()
    assert drives == []


def test_reddit_post_rejects_subreddit_with_path_injection(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="post x/../../admin | Title | body | post this to reddit now",
        )
    )

    assert "rejected" in reply.lower()
    assert drives == []


def test_reddit_post_rejects_subreddit_with_query_injection(monkeypatch) -> None:
    audits, drives = _patch_reddit(monkeypatch)

    reply = asyncio.run(
        core_handlers.handle_reddit(
            adapter=None,
            incoming=None,
            args="post x/wiki/edit?foo | Title | body | post this to reddit now",
        )
    )

    assert "rejected" in reply.lower()
    assert drives == []


# ── Event-loop-wedge regression (#130): browser_readiness runs off-loop ─────


def test_reddit_status_offloads_browser_readiness(monkeypatch) -> None:
    """Same event-loop-wedge regression as LinkedIn profile status — #130.

    Completion ORDER is the proof: a slow browser_readiness on the loop would
    block the ticker's timer and "readiness" would land first. Off-loop, the
    0.05s ticker finishes before the 0.2s readiness probe returns.
    """
    order: list[str] = []

    def _slow_readiness(*, port=None):
        import time

        time.sleep(0.2)  # the ~9s worst-case CDP chain, scaled down for the test
        order.append("readiness")
        return {"enabled": True, "cdp_port": port or 9222, "cdp_reachable": True}

    monkeypatch.setattr(browser_control, "browser_readiness", _slow_readiness)
    monkeypatch.setattr(
        browser_control, "browser_status", lambda *, port=None: {"enabled": True, "cdp_port": port}
    )
    monkeypatch.setattr(
        browser_control, "format_browser_status", lambda status, label=None: "status-ok"
    )
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **kw: None)

    async def _run() -> None:
        async def _ticker() -> None:
            await asyncio.sleep(0.05)
            order.append("ticked")

        result, _ = await asyncio.gather(
            core_handlers.handle_reddit(adapter=None, incoming=None, args="status"),
            _ticker(),
        )
        assert order == ["ticked", "readiness"]
        assert isinstance(result, str)

    asyncio.run(_run())
