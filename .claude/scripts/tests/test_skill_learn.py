"""Tests for cognition.skill_learn — source-driven /learn authoring.

Mirrors tests/test_cognition_skills.py style (tmp_path, flat-sys.path imports).
The only LLM seam (cognition.steps.reasoning_step) is monkeypatched — no network
and no provider dependency, which also asserts the model-agnostic contract:
distillation goes through reasoning_step and nothing else.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cognition import skill_learn
from cognition.skills import SkillSpec, write_skill


# --------------------------------------------------------------------------- #
# parse_source
# --------------------------------------------------------------------------- #


def test_parse_source_url():
    src = skill_learn.parse_source("https://docs.example.com/api focus on auth")
    assert src.kind == "url"
    assert src.raw == "https://docs.example.com/api"
    assert "auth" in src.focus


def test_parse_source_conversation():
    for text in ("", "this conversation", "what we just did", "this"):
        assert skill_learn.parse_source(text).kind == "conversation"


def test_parse_source_notes():
    src = skill_learn.parse_source("filing an expense: open portal, attach receipt")
    assert src.kind == "notes"
    assert "expense" in src.raw


def test_parse_source_path(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text("hello", encoding="utf-8")
    src = skill_learn.parse_source(f"{f} --focus parsing")
    assert src.kind == "path"
    assert src.focus == "parsing"


def test_parse_source_quoted_path_with_spaces(tmp_path):
    """#429 codex R4 MAJOR: `/skills link "C:\\Program Files\\linked skill\\...`
    — quotes are the operator saying the WHOLE thing is one path. The
    first-token split used to truncate it and misclassify it."""
    d = tmp_path / "linked skill"
    d.mkdir()
    f = d / "SKILL.md"
    f.write_text("hello", encoding="utf-8")
    src = skill_learn.parse_source(f'"{f}"')
    assert src.kind == "path"
    assert src.raw == str(f)


def test_parse_source_drive_letter_path_with_spaces(monkeypatch):
    """A bare drive-letter path keeps its spaces (Windows-native intake). The
    existence probe is stubbed so the test is platform-independent.

    Non-vacuity: pre-fix this string classified as ``notes`` — the first token
    (`C:\\Program`) failed every path check."""
    target = r"C:\Program Files\linked skill\SKILL.md"
    monkeypatch.setattr(skill_learn, "_looks_like_path", lambda p: p == target)
    src = skill_learn.parse_source(target)
    assert src.kind == "path"
    assert src.raw == target


@pytest.mark.asyncio
async def test_learn_skill_classifies_the_source_off_the_event_loop(
    monkeypatch, tmp_path
):
    """#429 codex R4 MAJOR: parse_source's path classification can probe
    Path.exists() on a network share — a Windows SMB timeout there stalls
    every coroutine on the loop. The classification now hops a thread; a slow
    probe must not delay a concurrent heartbeat's own sleep."""
    import time as _time

    def _slow_parse(args):
        _time.sleep(0.3)
        return skill_learn.LearnSource(kind="notes", raw=args, focus="")

    monkeypatch.setattr(skill_learn, "parse_source", _slow_parse)

    loop = asyncio.get_event_loop()
    start = loop.time()
    first_tick_at = None

    async def _heartbeat():
        nonlocal first_tick_at
        await asyncio.sleep(0.02)
        first_tick_at = loop.time() - start

    heartbeat_task = asyncio.create_task(_heartbeat())
    learn_task = asyncio.create_task(
        skill_learn.learn_skill(
            "note text", transcript="", skills_dir=tmp_path / "skills"
        )
    )
    await learn_task
    await heartbeat_task

    assert first_tick_at is not None
    assert first_tick_at < 0.15, (
        f"heartbeat's 0.02s sleep took {first_tick_at:.3f}s — the loop was "
        "blocked during source classification"
    )


# --------------------------------------------------------------------------- #
# gather_source
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gather_notes_and_conversation():
    notes = skill_learn.LearnSource(kind="notes", raw="do X then Y")
    assert await skill_learn.gather_source(notes) == "do X then Y"

    conv = skill_learn.LearnSource(kind="conversation")
    got = await skill_learn.gather_source(conv, transcript="user: hi\nassistant: yo")
    assert "assistant: yo" in got


@pytest.mark.asyncio
async def test_gather_path_file_and_dir(tmp_path):
    (tmp_path / "a.md").write_text("alpha doc", encoding="utf-8")
    (tmp_path / "b.py").write_text("print('beta')", encoding="utf-8")
    (tmp_path / "ignore.bin").write_text("xxxx", encoding="utf-8")

    file_src = skill_learn.LearnSource(kind="path", raw=str(tmp_path / "a.md"))
    assert "alpha doc" in await skill_learn.gather_source(file_src)

    dir_src = skill_learn.LearnSource(kind="path", raw=str(tmp_path))
    dir_text = await skill_learn.gather_source(dir_src)
    assert "alpha doc" in dir_text and "beta" in dir_text


@pytest.mark.asyncio
async def test_gather_size_cap(monkeypatch):
    monkeypatch.setattr(skill_learn, "MAX_SOURCE_CHARS", 10)
    notes = skill_learn.LearnSource(kind="notes", raw="x" * 100)
    assert len(await skill_learn.gather_source(notes)) == 10


@pytest.mark.asyncio
async def test_gather_path_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """M4 (#429 round-2 MAJOR): a local-path gather used to call `_read_path`
    directly inline — genuinely blocking disk IO on an `async def` function
    does NOT hand control back to the event loop; it stalls every OTHER
    coroutine (every other chat surface) for its full duration.

    Proven by timing a heartbeat coroutine's OWN ``asyncio.sleep`` against a
    wall-clock reference captured BEFORE either task starts. A single-
    threaded event loop can only resolve that sleep by actually running its
    timer callback — which cannot happen while the loop thread is stuck
    inside a synchronous ``time.sleep`` elsewhere. Off-loop (via
    ``asyncio.to_thread``), the heartbeat's sleep resolves on schedule
    (~0.02s) while the slow read runs in a separate thread; inline, it
    cannot resolve until AFTER the ~0.3s blocking read releases the loop.
    """
    import time as _time

    def _slow_read_path(raw, cwd=None):
        _time.sleep(0.3)
        return "slow content"

    monkeypatch.setattr(skill_learn, "_read_path", _slow_read_path)

    loop = asyncio.get_event_loop()
    start = loop.time()
    first_tick_at = None

    async def _heartbeat():
        nonlocal first_tick_at
        await asyncio.sleep(0.02)
        first_tick_at = loop.time() - start

    src = skill_learn.LearnSource(kind="path", raw=str(tmp_path))
    heartbeat_task = asyncio.create_task(_heartbeat())
    gather_task = asyncio.create_task(skill_learn.gather_source(src))
    text = await gather_task
    await heartbeat_task

    assert text == "slow content"
    assert first_tick_at is not None
    assert first_tick_at < 0.15, (
        f"heartbeat's own 0.02s sleep took {first_tick_at:.3f}s to resolve — "
        "the event loop was blocked during the local-path gather"
    )


# --------------------------------------------------------------------------- #
# _fetch_url — SSRF guard integration (#429 round-2 BLOCKER)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_url_refuses_when_host_resolves_private(monkeypatch):
    """The guard fires BEFORE any request is made — a resolved-private host
    must never reach the transport, regardless of what it would answer."""
    import socket

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    with pytest.raises(ValueError, match="non-public"):
        await skill_learn._fetch_url("https://evil.example.com/doc")


@pytest.mark.asyncio
async def test_fetch_url_refuses_http_scheme():
    with pytest.raises(ValueError, match="https"):
        await skill_learn._fetch_url("http://example.com/doc")


@pytest.mark.asyncio
async def test_fetch_url_reverifies_a_redirect_to_a_private_host(monkeypatch):
    """A validated public origin issuing a redirect to a private address must
    be refused at the redirect hop, not silently followed — this is the
    TOCTOU gap ``follow_redirects=True`` alone would leave open."""
    import socket

    import httpx

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        addr = "127.0.0.1" if host == "internal.example.com" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    def _handler(request: httpx.Request) -> httpx.Response:
        # The URL now carries the PINNED address and the identity rides in the
        # Host header (M2) — so the origin is recognized by Host, and a hop
        # the guard refuses never produces a request at all.
        if request.headers.get("host") == "public.example.com":
            return httpx.Response(
                302, headers={"location": "https://internal.example.com/secret"}
            )
        raise AssertionError("must never reach the redirect target")

    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: _real_async_client(
            *a, transport=httpx.MockTransport(_handler), **k
        ),
    )

    with pytest.raises(ValueError, match="non-public"):
        await skill_learn._fetch_url("https://public.example.com/doc")


@pytest.mark.asyncio
async def test_fetch_url_extracts_text_from_a_public_response(monkeypatch):
    import socket

    import httpx

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body><p>hello world</p></body></html>")

    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: _real_async_client(
            *a, transport=httpx.MockTransport(_handler), **k
        ),
    )

    text = await skill_learn._fetch_url("https://public.example.com/doc")
    assert "hello world" in text


@pytest.mark.asyncio
async def test_fetch_url_connects_to_the_validated_address_not_a_rebound_one(
    monkeypatch,
):
    """M2 (#429 design gate): the guard must PIN, not merely pre-check.

    A hostile authoritative server can answer the guard's lookup with a public
    address and the CLIENT's lookup with a private one — classic DNS
    rebinding. The resolver here does exactly that: the first resolution is
    public, every one after it is loopback. The fetch must still be addressed
    to the validated public address (and never hand httpx a hostname it would
    resolve a second time), while ``Host``/SNI keep the original name so
    virtual hosting and certificate verification are unaffected.
    """
    import socket

    import httpx

    resolutions: list[str] = []

    def _rebinding_getaddrinfo(host, port, *args, **kwargs):
        addr = "93.184.216.34" if not resolutions else "127.0.0.1"
        resolutions.append(addr)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_getaddrinfo)

    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<html><body><p>pinned</p></body></html>")

    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: _real_async_client(
            *a, transport=httpx.MockTransport(_handler), **k
        ),
    )

    await skill_learn._fetch_url("https://public.example.com/doc")

    (request,) = seen
    assert request.url.host == "93.184.216.34"
    assert request.url.host != "public.example.com"  # nothing left to re-resolve
    assert request.headers.get("host") == "public.example.com"
    assert request.extensions.get("sni_hostname") == "public.example.com"
    # One resolution, ours. A second one would be the attacker's opening.
    assert resolutions == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_fetch_url_resolution_does_not_block_the_event_loop(monkeypatch):
    """#429 codex R3 MAJOR: ``socket.getaddrinfo`` is blocking — run inline on
    the router's async loop, a resolver stall freezes EVERY channel for the
    duration. Proven the same way as the path-gather loop test: race a
    heartbeat's own ``asyncio.sleep`` against a deliberately slow resolver.
    Inline, the heartbeat cannot tick until the resolver returns; off-loop
    (``asyncio.to_thread``) it resolves on schedule.
    """
    import socket
    import time as _time

    import httpx

    def _slow_getaddrinfo(host, port, *args, **kwargs):
        _time.sleep(0.3)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _slow_getaddrinfo)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body><p>ok</p></body></html>")

    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: _real_async_client(
            *a, transport=httpx.MockTransport(_handler), **k
        ),
    )

    loop = asyncio.get_event_loop()
    start = loop.time()
    first_tick_at = None

    async def _heartbeat():
        nonlocal first_tick_at
        await asyncio.sleep(0.02)
        first_tick_at = loop.time() - start

    heartbeat_task = asyncio.create_task(_heartbeat())
    fetch_task = asyncio.create_task(
        skill_learn._fetch_url("https://public.example.com/doc")
    )
    await fetch_task
    await heartbeat_task

    assert first_tick_at is not None
    assert first_tick_at < 0.15, (
        f"heartbeat's own 0.02s sleep took {first_tick_at:.3f}s to resolve — "
        "the event loop was blocked during DNS resolution"
    )


@pytest.mark.asyncio
async def test_fetch_url_enforces_a_total_deadline_across_the_whole_fetch(monkeypatch):
    """#429 codex R3 MAJOR: ``httpx.Timeout`` is PER-OPERATION — a server that
    keeps every single read just inside the read timeout (or, here, a handler
    that simply answers slower than the total budget) must still be cut off by
    a genuine TOTAL deadline covering every hop and the streaming body."""
    import socket

    import httpx

    from security import ssrf

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    monkeypatch.setattr(ssrf, "TOTAL_TIMEOUT_S", 0.2)

    async def _slow_handler(request: httpx.Request) -> httpx.Response:
        # Slower than the TOTAL budget; MockTransport enforces no per-read
        # timeout, so only a real deadline can stop this.
        await asyncio.sleep(0.5)
        return httpx.Response(200, text="<html><body><p>too late</p></body></html>")

    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: _real_async_client(
            *a, transport=httpx.MockTransport(_slow_handler), **k
        ),
    )

    with pytest.raises(ValueError, match="deadline"):
        await skill_learn._fetch_url("https://public.example.com/doc")


# --------------------------------------------------------------------------- #
# distill_to_spec (model-agnostic LLM seam mocked)
# --------------------------------------------------------------------------- #


def _mock_reasoning(parsed):
    async def _fn(context, instruction, output_schema=None, cwd=None):
        return SimpleNamespace(parsed=parsed, output_text="")
    return _fn


@pytest.mark.asyncio
async def test_distill_builds_spec(monkeypatch):
    import cognition.steps as steps

    monkeypatch.setattr(steps, "reasoning_step", _mock_reasoning({
        "name": "acme-auth",
        "description": "x" * 80,  # over-long; must be clamped
        "category": "api",
        "tools_used": ["curl"],
        "trigger_patterns": ["authenticate to acme"],
        "body": "# acme-auth\n\n## Overview\n\nAuth flow.\n",
    }))

    spec = await skill_learn.distill_to_spec("source text", focus="auth")
    assert spec.name == "acme-auth"
    assert spec.category == "api"
    assert len(spec.description) <= 60
    assert spec.body.startswith("# acme-auth")


@pytest.mark.asyncio
async def test_distill_fail_soft(monkeypatch):
    import cognition.steps as steps

    async def _boom(*a, **k):
        raise RuntimeError("lane down")

    monkeypatch.setattr(steps, "reasoning_step", _boom)
    spec = await skill_learn.distill_to_spec("src", focus="deploy staging")
    assert isinstance(spec, SkillSpec)
    assert spec.name  # always yields an inspectable draft
    assert spec.body


# --------------------------------------------------------------------------- #
# learn_skill end-to-end
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_learn_skill_writes_draft_and_scans(tmp_path, monkeypatch):
    import config
    import cognition.steps as steps

    monkeypatch.setattr(config, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(steps, "reasoning_step", _mock_reasoning({
        "name": "expense-filing",
        "description": "File an expense in the portal",
        "category": "ops",
        "tools_used": [],
        "trigger_patterns": ["file an expense"],
        "body": "# expense-filing\n\n## Overview\n\nFile it.\n## Steps\n\n1. Open portal.\n",
    }))

    skills_dir = tmp_path / "skills"
    result = await skill_learn.learn_skill(
        "filing an expense: open portal, attach receipt, submit",
        skills_dir=skills_dir,
    )

    assert result.ok
    draft = skills_dir / "generated" / "ops" / "expense-filing" / "SKILL.md"
    assert draft.exists()
    text = draft.read_text(encoding="utf-8")
    assert "generated: true" in text
    assert "## Steps" in text  # authored body rendered, not the stub
    assert result.verdict in ("safe", "caution", "dangerous")

    # Seeded reuse counter makes the draft promotion-eligible.
    from cognition import skill_usage

    usage = skill_usage.get_usage("expense-filing")
    assert usage is not None and usage.state == "eligible"


@pytest.mark.asyncio
async def test_learn_skill_empty_source_is_friendly(tmp_path):
    result = await skill_learn.learn_skill(
        "this conversation", transcript="", skills_dir=tmp_path / "skills",
    )
    assert not result.ok
    assert "conversation" in result.message.lower()


# --------------------------------------------------------------------------- #
# write_skill body back-compat + traversal guard
# --------------------------------------------------------------------------- #


def test_write_skill_renders_authored_body(tmp_path):
    spec = SkillSpec(
        name="with-body", description="d", category="cat",
        body="# with-body\n\n## Overview\n\nbody here\n",
    )
    path = write_skill(spec, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "## Overview" in text and "body here" in text
    assert "## Workflow Steps" not in text  # stub suppressed when body present


def test_write_skill_stub_when_no_body(tmp_path):
    spec = SkillSpec(
        name="no-body", description="d", category="cat",
        workflow_steps=["step one"], tools_used=["toolx"],
    )
    text = write_skill(spec, tmp_path).read_text(encoding="utf-8")
    assert "## Workflow Steps" in text and "## Tools Required" in text


def test_write_skill_rejects_path_traversal(tmp_path):
    spec = SkillSpec(name="ok", description="d", category="../escape")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)
