"""Path-mapped security tests for the webhook ingress adapter (Phase 4).

One test per distinct code path (PRP Validation Loop map). MagicMock requests
with real HMAC vectors; handlers are called directly — no port is ever bound
and the network is never touched. Config/dormancy tests exercise the Rule-1
call-time resolver via monkeypatch.setenv.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add chat dir to path (flat sys.path convention — matches the launchers)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "chat"))

import webhook_audit  # noqa: E402
from adapters import webhook as webhook_mod  # noqa: E402
from adapters.webhook import INSECURE_NO_AUTH, WebhookAdapter  # noqa: E402
from models import Channel, OutgoingMessage, Platform  # noqa: E402

import config  # noqa: E402
from config import WebhookRoute, WebhookSettings, get_webhook_settings  # noqa: E402

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"


# ── Builders ────────────────────────────────────────────────


def _route(**kw) -> WebhookRoute:
    base = dict(
        name="gh",
        secret="s3cret-hmac-key",
        events=(),
        prompt="",
        deliver="log",
        deliver_extra={},
        deliver_only=False,
        deliver_extra_templated=False,
        enabled=True,
    )
    base.update(kw)
    return WebhookRoute(**base)


def _settings(routes: dict | None = None, **kw) -> WebhookSettings:
    base = dict(
        host="127.0.0.1",
        port=8622,
        allow_non_loopback=False,
        rate_limit=30,
        max_body_bytes=1_048_576,
        idempotency_ttl=3600,
        routes=routes if routes is not None else {},
    )
    base.update(kw)
    return WebhookSettings(**base)


def _adapter(
    routes: dict | None = None,
    adapter_resolver=None,
    audit=None,
    **kw,
) -> WebhookAdapter:
    return WebhookAdapter(
        _settings(routes=routes, **kw),
        adapter_resolver=adapter_resolver,
        audit=audit if audit is not None else MagicMock(),
    )


def _request(
    route: str = "gh",
    body: bytes = b"{}",
    headers: dict | None = None,
    content_length: int | None | object = ...,
    remote: str = "203.0.113.9",
) -> MagicMock:
    req = MagicMock()
    req.match_info = {"route_name": route}
    req.headers = headers or {}
    req.read = AsyncMock(return_value=body)
    req.content_length = len(body) if content_length is ... else content_length
    req.remote = remote
    return req


def _gh_sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _generic_sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


_SVIX_KEY = b"0123456789abcdef0123456789abcdef"
_SVIX_SECRET = "whsec_" + base64.b64encode(_SVIX_KEY).decode()


def _svix_sign(msg_id: str, ts: str, body: bytes, key: bytes = _SVIX_KEY) -> str:
    signed = msg_id.encode() + b"." + ts.encode() + b"." + body
    return "v1," + base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()


def _signed_request(secret: str, body: bytes = b'{"x": 1}', delivery: str = "d-1",
                    route: str = "gh", extra_headers: dict | None = None) -> MagicMock:
    headers = {
        "X-Hub-Signature-256": _gh_sign(secret, body),
        "X-GitHub-Delivery": delivery,
    }
    headers.update(extra_headers or {})
    return _request(route=route, body=body, headers=headers)


class _FakeWeb:
    """aiohttp.web stand-in for connect() tests — no bind, no network."""

    def __init__(self) -> None:
        self.app = MagicMock()
        self.Application = MagicMock(return_value=self.app)
        self.runner = MagicMock()
        self.runner.setup = AsyncMock()
        self.runner.cleanup = AsyncMock()
        self.AppRunner = MagicMock(return_value=self.runner)
        self.site = MagicMock()
        self.site.start = AsyncMock()
        self.TCPSite = MagicMock(return_value=self.site)


def _last_verdict(audit: MagicMock) -> str:
    return audit.call_args[0][2]


# ── Config / dormancy (tests 1-4) ───────────────────────────


def test_settings_unset_routes_dormant(monkeypatch):
    monkeypatch.delenv("WEBHOOK_ROUTES", raising=False)
    assert get_webhook_settings().routes == {}


def test_settings_malformed_json_no_raise(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ROUTES", "{not valid json!!")
    assert get_webhook_settings().routes == {}


def test_settings_non_object_json_dormant(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ROUTES", '["gh"]')
    assert get_webhook_settings().routes == {}


def test_settings_empty_secret_route_dropped(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ROUTES", '{"gh": {"prompt": "x"}}')
    assert get_webhook_settings().routes == {}
    # secret_env naming an UNSET env var is also an empty effective secret
    monkeypatch.delenv("NO_SUCH_SECRET_ENV", raising=False)
    monkeypatch.setenv(
        "WEBHOOK_ROUTES", '{"gh": {"secret_env": "NO_SUCH_SECRET_ENV"}}'
    )
    assert get_webhook_settings().routes == {}


def test_settings_secret_env_resolves_at_call_time(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TEST_SECRET", "from-env-abc")
    monkeypatch.setenv("WEBHOOK_ROUTES", '{"gh": {"secret_env": "WEBHOOK_TEST_SECRET"}}')
    settings = get_webhook_settings()
    assert settings.routes["gh"].secret == "from-env-abc"


def test_settings_insecure_route_dropped_on_non_loopback(monkeypatch):
    monkeypatch.setenv("WEBHOOK_HOST", "0.0.0.0")
    monkeypatch.setenv("WEBHOOK_ROUTES", f'{{"gh": {{"secret": "{INSECURE_NO_AUTH}"}}}}')
    assert get_webhook_settings().routes == {}
    monkeypatch.setenv("WEBHOOK_HOST", "127.0.0.1")
    assert "gh" in get_webhook_settings().routes


def test_settings_deliver_only_requires_real_target(monkeypatch):
    monkeypatch.setenv(
        "WEBHOOK_ROUTES", '{"gh": {"secret": "s", "deliver_only": true}}'
    )
    assert get_webhook_settings().routes == {}


def test_insecure_sentinel_matches_config():
    assert INSECURE_NO_AUTH == config.WEBHOOK_INSECURE_NO_AUTH


@pytest.mark.asyncio
async def test_connect_no_routes_dormant_no_bind(monkeypatch):
    fake = _FakeWeb()
    monkeypatch.setattr(webhook_mod, "web", fake)
    adapter = _adapter(routes={})
    result = await adapter.connect()
    assert result is False
    assert adapter._runner is None
    fake.Application.assert_not_called()
    fake.TCPSite.assert_not_called()


# ── Signature validation (tests 5-13) ───────────────────────


@pytest.mark.asyncio
async def test_github_signature_valid_accepted():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"action": "opened"}'
    resp = await adapter._handle_webhook(_signed_request("s3cret-hmac-key", body))
    assert resp.status == 202
    assert adapter._queue.qsize() == 1
    assert _last_verdict(audit) == "accepted"


@pytest.mark.asyncio
async def test_github_signature_invalid_401():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"action": "opened"}'
    req = _request(headers={
        "X-Hub-Signature-256": _gh_sign("WRONG-secret", body),
        "X-GitHub-Delivery": "d-1",
    }, body=body)
    resp = await adapter._handle_webhook(req)
    assert resp.status == 401
    assert adapter._queue.qsize() == 0
    assert _last_verdict(audit) == "rejected_signature"


@pytest.mark.asyncio
async def test_svix_signature_valid_accepted():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route(secret=_SVIX_SECRET)}, audit=audit)
    body = b'{"event": "billing"}'
    ts = str(int(time.time()))
    req = _request(body=body, headers={
        "svix-id": "msg_1",
        "svix-timestamp": ts,
        "svix-signature": _svix_sign("msg_1", ts, body),
    })
    resp = await adapter._handle_webhook(req)
    assert resp.status == 202
    assert _last_verdict(audit) == "accepted"


@pytest.mark.asyncio
async def test_svix_timestamp_outside_replay_window_401():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route(secret=_SVIX_SECRET)}, audit=audit)
    body = b'{"event": "billing"}'
    ts = str(int(time.time()) - 1000)  # > 300s tolerance
    req = _request(body=body, headers={
        "svix-id": "msg_1",
        "svix-timestamp": ts,
        "svix-signature": _svix_sign("msg_1", ts, body),  # correctly signed, stale
    })
    resp = await adapter._handle_webhook(req)
    assert resp.status == 401
    assert _last_verdict(audit) == "rejected_signature"


@pytest.mark.asyncio
async def test_svix_multi_signature_rotation_accepted():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route(secret=_SVIX_SECRET)}, audit=audit)
    body = b'{"event": "billing"}'
    ts = str(int(time.time()))
    good = _svix_sign("msg_2", ts, body)
    req = _request(body=body, headers={
        "svix-id": "msg_2",
        "svix-timestamp": ts,
        "svix-signature": f"v1,{base64.b64encode(b'bad-old-key-sig').decode()} {good}",
    })
    resp = await adapter._handle_webhook(req)
    assert resp.status == 202


@pytest.mark.asyncio
async def test_generic_signature_valid_and_invalid():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"n": 1}'
    good = _request(body=body, headers={
        "X-Webhook-Signature": _generic_sign("s3cret-hmac-key", body),
        "X-Request-ID": "r-1",
    })
    resp = await adapter._handle_webhook(good)
    assert resp.status == 202
    bad = _request(body=body, headers={
        "X-Webhook-Signature": _generic_sign("nope", body),
        "X-Request-ID": "r-2",
    })
    resp = await adapter._handle_webhook(bad)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_gitlab_token_valid_and_invalid():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"n": 1}'
    good = _request(body=body, headers={
        "X-Gitlab-Token": "s3cret-hmac-key",  # GitLab sends the plain secret
        "X-Request-ID": "gl-1",
    })
    resp = await adapter._handle_webhook(good)
    assert resp.status == 202
    bad = _request(body=body, headers={
        "X-Gitlab-Token": "wrong-token",
        "X-Request-ID": "gl-2",
    })
    resp = await adapter._handle_webhook(bad)
    assert resp.status == 401
    assert _last_verdict(audit) == "rejected_signature"


@pytest.mark.asyncio
async def test_secret_configured_but_no_signature_header_401():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    resp = await adapter._handle_webhook(_request(body=b"{}", headers={}))
    assert resp.status == 401
    assert _last_verdict(audit) == "rejected_signature"


@pytest.mark.asyncio
async def test_missing_secret_403_at_request_time():
    # A directly-constructed empty-secret route (bypassing the config
    # resolver's drop) must fail CLOSED at request time — not only connect().
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route(secret="")}, audit=audit)
    body = b"{}"
    req = _request(body=body, headers={
        "X-Hub-Signature-256": _gh_sign("anything", body),
    })
    resp = await adapter._handle_webhook(req)
    assert resp.status == 403
    assert adapter._queue.qsize() == 0
    assert _last_verdict(audit) == "rejected_missing_secret"


def test_constant_time_comparison_only():
    src = (_CHAT_DIR / "adapters" / "webhook.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in src
    # No direct equality on any signature/expected value — constant-time only.
    sig_names = r"(gh_sig|generic_sig|gl_token|svix_signature|signature_header|expected)"
    assert not re.search(sig_names + r"\s*[!=]=", src)
    assert not re.search(r"[!=]=\s*" + sig_names, src)


# ── Loopback / INSECURE_NO_AUTH (tests 14-16) ───────────────


@pytest.mark.asyncio
async def test_connect_non_loopback_without_optin_refuses(monkeypatch):
    fake = _FakeWeb()
    monkeypatch.setattr(webhook_mod, "web", fake)
    adapter = _adapter(routes={"gh": _route()}, host="0.0.0.0",
                       allow_non_loopback=False)
    with pytest.raises(RuntimeError):
        await adapter.connect()
    assert adapter._runner is None
    fake.TCPSite.assert_not_called()


@pytest.mark.asyncio
async def test_connect_insecure_route_on_non_loopback_refuses(monkeypatch):
    fake = _FakeWeb()
    monkeypatch.setattr(webhook_mod, "web", fake)
    adapter = _adapter(routes={"gh": _route(secret=INSECURE_NO_AUTH)},
                       host="0.0.0.0", allow_non_loopback=True)
    with pytest.raises(RuntimeError):
        await adapter.connect()
    assert adapter._runner is None
    fake.TCPSite.assert_not_called()


@pytest.mark.asyncio
async def test_connect_loopback_insecure_binds_and_skips_signature(monkeypatch):
    real_web = webhook_mod.web
    fake = _FakeWeb()
    monkeypatch.setattr(webhook_mod, "web", fake)
    adapter = _adapter(routes={"gh": _route(secret=INSECURE_NO_AUTH)})
    assert await adapter.connect() is True
    assert adapter._runner is fake.runner
    fake.TCPSite.assert_called_once_with(fake.runner, "127.0.0.1", 8622)
    # client_max_size wired as body-cap layer 2
    assert fake.Application.call_args.kwargs["client_max_size"] == 1_048_576

    # Handler path (fresh adapter, real aiohttp responses): INSECURE_NO_AUTH
    # skips signature validation entirely — unsigned POST is accepted.
    monkeypatch.setattr(webhook_mod, "web", real_web)
    audit = MagicMock()
    handler_adapter = _adapter(routes={"gh": _route(secret=INSECURE_NO_AUTH)},
                               audit=audit)
    resp = await handler_adapter._handle_webhook(
        _request(body=b'{"local": true}', headers={"X-Request-ID": "r-1"})
    )
    assert resp.status == 202
    assert _last_verdict(audit) == "accepted"


# ── Body cap, three layers (tests 17-18) ────────────────────


@pytest.mark.asyncio
async def test_content_length_over_cap_413_before_read():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, max_body_bytes=100)
    adapter._audit = audit
    req = _request(body=b"{}", content_length=200)
    resp = await adapter._handle_webhook(req)
    assert resp.status == 413
    req.read.assert_not_awaited()  # rejected BEFORE the body was read
    assert _last_verdict(audit) == "rejected_body_cap"


@pytest.mark.asyncio
async def test_post_read_over_cap_413_with_spoofed_content_length():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, max_body_bytes=100, audit=audit)
    big = b"x" * 200
    # Content-Length lies (says 10) — the post-read assertion still rejects.
    resp = await adapter._handle_webhook(_request(body=big, content_length=10))
    assert resp.status == 413
    assert _last_verdict(audit) == "rejected_body_cap"
    # Absent Content-Length (chunked transfer) — same rejection.
    resp = await adapter._handle_webhook(_request(body=big, content_length=None))
    assert resp.status == 413


@pytest.mark.asyncio
async def test_aiohttp_layer_over_read_413():
    from aiohttp import web as real_web

    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, max_body_bytes=100, audit=audit)
    req = _request(content_length=None)
    req.read = AsyncMock(
        side_effect=real_web.HTTPRequestEntityTooLarge(max_size=100, actual_size=200)
    )
    resp = await adapter._handle_webhook(req)
    assert resp.status == 413
    assert _last_verdict(audit) == "rejected_body_cap"


@pytest.mark.asyncio
async def test_read_error_400_writes_audit_row():
    # Review F3: a non-413 body-read failure is a rejected ingress event and
    # must leave a paper trail, not a silent 400.
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    req = _request()
    req.read = AsyncMock(side_effect=RuntimeError("connection torn down"))
    resp = await adapter._handle_webhook(req)
    assert resp.status == 400
    assert _last_verdict(audit) == "rejected_read_error"


@pytest.mark.asyncio
async def test_unparseable_body_400_writes_audit_row():
    # Review F3: a SIGNED but undecodable body (invalid UTF-8 — not JSON, not
    # form-decodable) must 400 with an audit row. Also proves the
    # UnicodeDecodeError path from json.loads cannot escape as a 500.
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b"\xff\xfe\xfa garbage \x80"
    req = _request(body=body, headers={
        "X-Webhook-Signature": _generic_sign("s3cret-hmac-key", body),
        "X-Request-ID": "r-parse",
    })
    resp = await adapter._handle_webhook(req)
    assert resp.status == 400
    assert _last_verdict(audit) == "rejected_parse"
    assert adapter._queue.qsize() == 0


# ── Rate limit (test 19) ────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_429_after_limit_auth_checked_first():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, rate_limit=3, audit=audit)
    # Distinct bodies so each POST is a genuinely distinct delivery — the
    # replay key is now the signed body hash, which dedups identical bodies
    # (see the replay-key tests). This test exercises rate-limiting, not
    # idempotency.
    for i in range(3):
        body = json.dumps({"n": i}).encode()
        resp = await adapter._handle_webhook(
            _signed_request("s3cret-hmac-key", body, delivery=f"d-{i}")
        )
        assert resp.status == 202
    over = json.dumps({"n": 99}).encode()
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", over, delivery="d-over")
    )
    assert resp.status == 429
    assert _last_verdict(audit) == "rejected_rate_limit"
    # Auth is checked BEFORE the rate limit: with the window exhausted, an
    # invalid signature still yields 401 (not 429) and burns no window slot.
    bad_body = json.dumps({"n": 100}).encode()
    bad = _request(body=bad_body, headers={
        "X-Hub-Signature-256": _gh_sign("WRONG", bad_body),
    })
    resp = await adapter._handle_webhook(bad)
    assert resp.status == 401


# ── Idempotency (test 20) ───────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_delivery_id_not_reprocessed():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"x": 1}'
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="dup-1")
    )
    assert resp.status == 202
    assert adapter._queue.qsize() == 1
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="dup-1")
    )
    assert resp.status == 200
    assert "duplicate" in resp.text
    assert adapter._queue.qsize() == 1  # no second agent run
    assert _last_verdict(audit) == "duplicate"


@pytest.mark.asyncio
async def test_duplicate_delivery_id_scoped_per_route():
    # Review F1: the replay cache must key on (route, id). The same provider
    # id on two DIFFERENT routes is two independent events — a low-priv route
    # must not be able to suppress a high-value route by colliding ids.
    audit = MagicMock()
    adapter = _adapter(
        routes={"a": _route(name="a"), "b": _route(name="b")}, audit=audit,
    )
    body = b'{"x": 1}'
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="same-id", route="a")
    )
    assert resp.status == 202
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="same-id", route="b")
    )
    assert resp.status == 202  # NOT duplicate — different route
    assert adapter._queue.qsize() == 2
    # Same route + same id is still a duplicate.
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="same-id", route="a")
    )
    assert resp.status == 200
    assert "duplicate" in resp.text
    assert adapter._queue.qsize() == 2


def test_idempotency_cache_bounded_with_fifo_eviction():
    # Review F2: fresh unique ids inside the TTL must NOT grow the cache
    # unbounded — after the TTL prune, oldest entries FIFO-evict to the cap.
    adapter = _adapter(routes={"gh": _route()}, rate_limit=3)
    cap = max(3 * 2, 128)  # 128
    now = time.time()
    for i in range(200):
        assert adapter._record_delivery_id("gh", f"id-{i}", now) is True
    assert len(adapter._seen_deliveries["gh"]) <= cap
    # Recent replay protection still holds after eviction...
    assert adapter._record_delivery_id("gh", "id-199", now) is False
    # ...while the oldest ids were evicted (bounded-cache tradeoff).
    assert "id-0" not in adapter._seen_deliveries["gh"]


# ── Replay-key idempotency (tests 20a-20e) ──────────────────


@pytest.mark.asyncio
async def test_mutated_delivery_id_replay_blocked():
    # Replay F1: the idempotency key is the signed body hash, NOT the
    # attacker-controlled delivery-id header. The same signed body replayed
    # with a DIFFERENT X-GitHub-Delivery must be deduped — a body-only GitHub
    # signature does not cover that header, so keying on it was a replay hole.
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"action": "opened", "number": 7}'
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="orig-id")
    )
    assert resp.status == 202
    assert adapter._queue.qsize() == 1
    # Same signed body, mutated delivery-id header — signature still valid.
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="mutated-id")
    )
    assert resp.status == 200
    assert "duplicate" in resp.text
    assert adapter._queue.qsize() == 1  # NOT re-processed
    assert _last_verdict(audit) == "duplicate"
    # The cache is keyed on the body hash, not either delivery-id header.
    assert hashlib.sha256(body).hexdigest() in adapter._seen_deliveries["gh"]
    assert "orig-id" not in adapter._seen_deliveries["gh"]
    assert "mutated-id" not in adapter._seen_deliveries["gh"]


@pytest.mark.asyncio
async def test_sanitizer_collision_does_not_suppress_distinct_bodies():
    # Replay F2: two DISTINCT deliveries whose raw delivery-ids sanitize to
    # the SAME display string but carry DIFFERENT bodies must BOTH process.
    # The idempotency key is the lossless body hash, so a lossy display-id
    # collision can no longer drop a legitimately distinct delivery.
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    # "id one" and "id/one" both sanitize to "id-one" (space/slash -> "-").
    assert webhook_mod._sanitize_delivery_id("id one") == \
        webhook_mod._sanitize_delivery_id("id/one")
    body_a = b'{"n": 1}'
    body_b = b'{"n": 2}'
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body_a, delivery="id one")
    )
    assert resp.status == 202
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body_b, delivery="id/one")
    )
    assert resp.status == 202  # distinct body -> distinct replay key
    assert adapter._queue.qsize() == 2
    # Both lossless body hashes are recorded (the collision was on display).
    assert hashlib.sha256(body_a).hexdigest() in adapter._seen_deliveries["gh"]
    assert hashlib.sha256(body_b).hexdigest() in adapter._seen_deliveries["gh"]


@pytest.mark.asyncio
async def test_svix_replay_key_uses_signed_tuple():
    # The Svix replay key is the signed (msg_id . timestamp . body) tuple —
    # the svix-id IS covered by the Svix signature, so it stays in the key.
    # Identical tuple replayed -> deduped; a different (validly signed)
    # svix-id -> distinct key -> processed, even with a byte-identical body.
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route(secret=_SVIX_SECRET)}, audit=audit)
    body = b'{"event": "billing"}'
    ts = str(int(time.time()))

    def _svix_req(msg_id: str) -> MagicMock:
        return _request(body=body, headers={
            "svix-id": msg_id,
            "svix-timestamp": ts,
            "svix-signature": _svix_sign(msg_id, ts, body),
        })

    resp = await adapter._handle_webhook(_svix_req("msg_a"))
    assert resp.status == 202
    # Identical svix-id/timestamp/body -> same signed tuple -> deduped.
    resp = await adapter._handle_webhook(_svix_req("msg_a"))
    assert resp.status == 200
    assert "duplicate" in resp.text
    # Different (validly signed) svix-id, SAME body -> distinct tuple -> new.
    resp = await adapter._handle_webhook(_svix_req("msg_b"))
    assert resp.status == 202
    assert adapter._queue.qsize() == 2
    # The key IS the signed tuple hash, not the bare body hash.
    tuple_key = hashlib.sha256(b"msg_a." + ts.encode() + b"." + body).hexdigest()
    assert tuple_key in adapter._seen_deliveries["gh"]
    assert hashlib.sha256(body).hexdigest() not in adapter._seen_deliveries["gh"]


@pytest.mark.asyncio
async def test_byte_identical_body_same_headers_still_deduped():
    # No regression of the core idempotency guarantee: a byte-identical
    # signed body replayed with the SAME headers is still a duplicate.
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"action": "opened"}'
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="same")
    )
    assert resp.status == 202
    assert adapter._queue.qsize() == 1
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="same")
    )
    assert resp.status == 200
    assert "duplicate" in resp.text
    assert adapter._queue.qsize() == 1
    assert _last_verdict(audit) == "duplicate"


@pytest.mark.asyncio
async def test_audit_and_display_id_stay_sanitized_not_body_hash():
    # KEY/DISPLAY separation: the idempotency key is the body hash, but the
    # audit row's delivery_id and the session channel.platform_id remain the
    # sanitized DISPLAY id derived from the delivery-id header.
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"x": 1}'
    raw_id = "delivery{with}braces and spaces"
    expected_display = webhook_mod._sanitize_delivery_id(raw_id)
    req = _request(body=body, headers={
        "X-Hub-Signature-256": _gh_sign("s3cret-hmac-key", body),
        "X-GitHub-Delivery": raw_id,
    })
    resp = await adapter._handle_webhook(req)
    assert resp.status == 202
    # Session key uses the sanitized DISPLAY id, never the body hash.
    msg = adapter._queue.get_nowait()
    assert msg.channel.platform_id == f"webhook:gh:{expected_display}"
    # Audit row delivery_id is the sanitized DISPLAY id.
    assert audit.call_args[0][1] == expected_display
    # The idempotency cache is keyed on the body hash, NOT the display id.
    assert hashlib.sha256(body).hexdigest() in adapter._seen_deliveries["gh"]
    assert expected_display not in adapter._seen_deliveries["gh"]


# ── Event filter ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_filter_ignores_unlisted_event():
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route(events=("pull_request",))}, audit=audit)
    body = b'{"x": 1}'
    resp = await adapter._handle_webhook(_signed_request(
        "s3cret-hmac-key", body, extra_headers={"X-GitHub-Event": "push"},
    ))
    assert resp.status == 200
    assert "ignored" in resp.text
    assert adapter._queue.qsize() == 0
    assert _last_verdict(audit) == "rejected_event_filtered"


# ── Template render (tests 21-25) ───────────────────────────


def test_render_dot_notation_resolves_nested():
    adapter = _adapter(routes={"gh": _route()})
    out = adapter._render_prompt(
        "Review PR: {pull_request.title}",
        {"pull_request": {"title": "Fix the bug"}}, "pull_request", "gh",
    )
    assert out == "Review PR: Fix the bug"


def test_render_raw_token_dumps_payload_truncated():
    adapter = _adapter(routes={"gh": _route()})
    payload = {"blob": "z" * 10_000}
    out = adapter._render_prompt("{__raw__}", payload, "e", "gh")
    assert out == json.dumps(payload, indent=2)[:4000]
    assert len(out) == 4000


def test_render_missing_key_emitted_literally():
    adapter = _adapter(routes={"gh": _route()})
    out = adapter._render_prompt("v={absent.field}", {"present": 1}, "e", "gh")
    assert out == "v={absent.field}"


def test_render_payload_value_braces_not_reresolved():
    # Injection guard: a payload VALUE containing "{admin.token}" must be
    # emitted literally (re.sub single pass), even when the key EXISTS.
    adapter = _adapter(routes={"gh": _route()})
    out = adapter._render_prompt(
        "{comment}",
        {"comment": "{admin.token}", "admin": {"token": "LEAKED"}}, "e", "gh",
    )
    assert out == "{admin.token}"
    assert "LEAKED" not in out


def test_render_empty_template_default_json_dump():
    adapter = _adapter(routes={"gh": _route()})
    out = adapter._render_prompt("", {"a": 1}, "push", "gh")
    assert "Webhook event 'push'" in out
    assert "```json" in out
    assert '"a": 1' in out


# ── Least-privilege agent lane (tests 26-27) ────────────────


@pytest.mark.asyncio
async def test_agent_lane_message_is_least_privileged():
    adapter = _adapter(routes={"gh": _route(prompt="PR: {pull_request.title}")})
    body = json.dumps({"pull_request": {"title": "Add webhook"}}).encode()
    resp = await adapter._handle_webhook(_signed_request(
        "s3cret-hmac-key", body, delivery="d-lp",
        extra_headers={"X-GitHub-Event": "pull_request"},
    ))
    assert resp.status == 202
    msg = adapter._queue.get_nowait()
    assert msg.source == "tool"
    assert msg.user_role == "viewer"
    assert msg.is_piv is False
    assert msg.platform == Platform.WEBHOOK
    assert msg.channel.platform_id == "webhook:gh:d-lp"
    assert "<untrusted-webhook-payload>" in msg.prefetched_context
    assert "PR: Add webhook" in msg.prefetched_context
    # text is the short TRUSTED directive — never the payload
    assert "Add webhook" not in msg.text
    assert "untrusted" in msg.text.lower()


@pytest.mark.asyncio
async def test_delivery_id_sanitized_and_bounded():
    # Non-blocking review item: the delivery id is an attacker-controlled
    # header that flows into session keys (chat.db) and audit rows — it must
    # be charset-sanitized and length-bounded before any use.
    audit = MagicMock()
    adapter = _adapter(routes={"gh": _route()}, audit=audit)
    body = b'{"x": 1}'
    hostile = "evil\nid\x00with spaces{and}braces" + "A" * 500
    req = _request(body=body, headers={
        "X-Hub-Signature-256": _gh_sign("s3cret-hmac-key", body),
        "X-GitHub-Delivery": hostile,
    })
    resp = await adapter._handle_webhook(req)
    assert resp.status == 202
    msg = adapter._queue.get_nowait()
    delivery_part = msg.channel.platform_id.removeprefix("webhook:gh:")
    assert len(delivery_part) <= 128
    assert re.fullmatch(r"[A-Za-z0-9._:\-]+", delivery_part)
    audited_id = audit.call_args[0][1]
    assert audited_id == delivery_part  # audit sees the same sanitized id


def test_source_tool_is_normalize_safe():
    # "tool" survives session.normalize_source unchanged and is != interactive
    # (a new "webhook" value would fail OPEN to "interactive" and count as
    # operator activity / eat the session-opening brief).
    from session import SOURCE_VALUES, normalize_source

    assert "tool" in SOURCE_VALUES
    assert normalize_source("tool") == "tool"
    assert normalize_source("webhook") == "interactive"  # why "tool" is used


def test_engine_forces_no_tools_for_non_telegram_prefetched():
    # Static proof of the engine gate the webhook lane rides (engine.py):
    # prefetched_context on a non-Telegram platform -> allowed_tools = [].
    src = (_CHAT_DIR / "engine.py").read_text(encoding="utf-8")
    gate = re.search(
        r"if message\.prefetched_context and message\.platform != Platform\.TELEGRAM:",
        src,
    )
    assert gate is not None, "engine least-privilege gate missing"
    tail = src[gate.end():gate.end() + 300]
    assert "allowed_tools = []" in tail
    assert "piv_max_turns = 1" in tail


# ── deliver_only lane (tests 28-32) ─────────────────────────


def _target_adapter(result: str | None = "mid-1", exc: Exception | None = None):
    target = MagicMock()
    if exc is not None:
        target.send = AsyncMock(side_effect=exc)
    else:
        target.send = AsyncMock(return_value=result)
    return target


@pytest.mark.asyncio
async def test_deliver_only_pushes_rendered_template_no_engine():
    audit = MagicMock()
    target = _target_adapter()
    adapter = _adapter(
        routes={"gh": _route(
            prompt="Alert: {msg}", deliver="telegram",
            deliver_extra={"chat_id": "123"}, deliver_only=True,
        )},
        adapter_resolver=lambda pv: target if pv == "telegram" else None,
        audit=audit,
    )
    body = json.dumps({"msg": "disk full"}).encode()
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="d-do")
    )
    assert resp.status == 200
    assert "delivered" in resp.text
    assert adapter._queue.qsize() == 0  # engine NOT invoked
    target.send.assert_awaited_once()
    sent = target.send.call_args[0][0]
    assert isinstance(sent, OutgoingMessage)
    assert sent.text == "Alert: disk full"
    assert sent.channel.platform == Platform.TELEGRAM
    assert sent.channel.platform_id == "123"
    assert _last_verdict(audit) == "delivered"


@pytest.mark.asyncio
async def test_deliver_only_permanent_error_marks_dead_502():
    audit = MagicMock()
    reg = MagicMock()
    reg.is_dead.return_value = False
    target = _target_adapter(exc=Exception("Forbidden: the group chat was deleted"))
    adapter = _adapter(
        routes={"gh": _route(deliver="telegram", deliver_extra={"chat_id": "123"},
                             deliver_only=True)},
        adapter_resolver=lambda pv: target,
        audit=audit,
    )
    adapter._dead_registry = reg
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", delivery="d-dead")
    )
    assert resp.status == 502
    reg.mark_dead.assert_called_once()
    assert reg.mark_dead.call_args[0][:2] == ("telegram", "123")
    assert _last_verdict(audit) == "delivery_failed"


@pytest.mark.asyncio
async def test_deliver_only_proven_dead_target_skipped():
    audit = MagicMock()
    reg = MagicMock()
    reg.is_dead.return_value = True
    target = _target_adapter()
    adapter = _adapter(
        routes={"gh": _route(deliver="telegram", deliver_extra={"chat_id": "123"},
                             deliver_only=True)},
        adapter_resolver=lambda pv: target,
        audit=audit,
    )
    adapter._dead_registry = reg
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", delivery="d-skip")
    )
    assert resp.status == 502
    target.send.assert_not_awaited()  # short-circuited, no send attempt
    assert _last_verdict(audit) == "delivery_failed"


@pytest.mark.asyncio
async def test_deliver_extra_not_payload_templated_by_default():
    # Delivery-redirect vector: with deliver_extra_templated=False (default),
    # an attacker-controlled payload field must NOT rewrite the target chat.
    target = _target_adapter()
    adapter = _adapter(
        routes={"gh": _route(
            deliver="telegram", deliver_only=True,
            deliver_extra={"chat_id": "{payload_chat}"},  # operator mistake
            deliver_extra_templated=False,
        )},
        adapter_resolver=lambda pv: target,
    )
    body = json.dumps({"payload_chat": "666-attacker"}).encode()
    await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="d-red")
    )
    sent = target.send.call_args[0][0]
    assert sent.channel.platform_id == "{payload_chat}"  # literal, unrendered
    assert sent.channel.platform_id != "666-attacker"


@pytest.mark.asyncio
async def test_deliver_extra_templated_github_comment_still_validated(monkeypatch):
    run_mock = MagicMock()
    monkeypatch.setattr(webhook_mod.subprocess, "run", run_mock)
    adapter = _adapter(
        routes={"gh": _route(
            deliver="github_comment", deliver_only=True,
            deliver_extra={"repo": "{r}", "pr_number": "{n}"},
            deliver_extra_templated=True,
        )},
    )
    # Templated repo failing the owner/name regex -> rejected, no subprocess.
    body = json.dumps({"r": "evil; rm -rf /", "n": "13"}).encode()
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="d-r1")
    )
    assert resp.status == 502
    run_mock.assert_not_called()
    # Templated pr_number that is not a positive int -> rejected too.
    body = json.dumps({"r": "owner/repo", "n": "abc"}).encode()
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="d-r2")
    )
    assert resp.status == 502
    run_mock.assert_not_called()


@pytest.mark.asyncio
async def test_github_comment_valid_args_no_shell(monkeypatch):
    run_mock = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(webhook_mod.subprocess, "run", run_mock)
    adapter = _adapter(routes={"gh": _route()})
    ok = await adapter._deliver_github_comment(
        "LGTM", {"repo": "owner/repo", "pr_number": "42"},
    )
    assert ok is True
    argv = run_mock.call_args[0][0]
    assert argv == ["gh", "pr", "comment", "42", "--repo", "owner/repo",
                    "--body", "LGTM"]
    assert "shell" not in run_mock.call_args.kwargs  # arg list, never shell


# ── send() response routing (test 33) ───────────────────────


@pytest.mark.asyncio
async def test_send_routes_response_to_configured_target():
    audit = MagicMock()
    target = _target_adapter()
    adapter = _adapter(
        routes={"gh": _route(deliver="discord", deliver_extra={"chat_id": "999"})},
        adapter_resolver=lambda pv: target if pv == "discord" else None,
        audit=audit,
    )
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", delivery="d-send")
    )
    assert resp.status == 202
    result = await adapter.send(OutgoingMessage(
        text="the answer", channel=Channel(Platform.WEBHOOK, "webhook:gh:d-send"),
    ))
    assert result == "ok"
    sent = target.send.call_args[0][0]
    assert sent.text == "the answer"
    assert sent.channel.platform == Platform.DISCORD
    assert sent.channel.platform_id == "999"
    assert _last_verdict(audit) == "delivered"


@pytest.mark.asyncio
async def test_send_unknown_session_falls_back_to_log():
    adapter = _adapter(routes={"gh": _route()})
    result = await adapter.send(OutgoingMessage(
        text="orphan", channel=Channel(Platform.WEBHOOK, "webhook:gh:unknown"),
    ))
    assert result == "ok"  # log deliver type accepts everything


# ── Audit (tests 34-36) ─────────────────────────────────────


def _file_audit(path: Path):
    def _sink(route, delivery_id, verdict, **kw):
        return webhook_audit.append_webhook_audit_record(
            route, delivery_id, verdict, path=path, **kw,
        )
    return _sink


@pytest.mark.asyncio
async def test_verdicts_write_one_jsonl_row_each(tmp_path):
    log = tmp_path / "webhook_actions.jsonl"
    adapter = _adapter(routes={"gh": _route()}, audit=_file_audit(log))
    body = b'{"x": 1}'
    await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="a-1"))       # accepted
    await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", body, delivery="a-1"))       # duplicate
    await adapter._handle_webhook(_request(body=body, headers={
        "X-Hub-Signature-256": _gh_sign("WRONG", body)}))               # rejected_signature
    await adapter._handle_webhook(_request(route="nope", body=body))    # rejected_unknown_route
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["verdict"] for r in rows] == [
        "accepted", "duplicate", "rejected_signature", "rejected_unknown_route",
    ]
    for row in rows:
        assert set(row) >= {"route", "delivery_id", "verdict", "timestamp"}
        assert row["verdict"] in webhook_audit.KNOWN_VERDICTS


@pytest.mark.asyncio
async def test_audit_failure_is_fail_open(tmp_path):
    # (a) the JSONL writer itself: unwritable path -> None, no raise
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bad_path = blocker / "sub" / "audit.jsonl"
    assert webhook_audit.append_webhook_audit_record("gh", "d", "accepted",
                                                     path=bad_path) is None
    # (b) an injected audit hook that raises: handler verdict still stands
    def _exploding_audit(*a, **kw):
        raise RuntimeError("audit sink down")
    adapter = _adapter(routes={"gh": _route()}, audit=_exploding_audit)
    resp = await adapter._handle_webhook(
        _signed_request("s3cret-hmac-key", delivery="d-fo")
    )
    assert resp.status == 202
    assert adapter._queue.qsize() == 1


def test_audit_redacts_secret_shaped_tokens(tmp_path):
    log = tmp_path / "audit.jsonl"
    token = "ghp_" + "A" * 30
    row = webhook_audit.append_webhook_audit_record(
        "gh", "d-1", "delivery_failed", reason=f"send failed with token {token}",
        path=log,
    )
    assert row is not None
    assert "[REDACTED]" in row["reason"]
    assert token not in row["reason"]
    assert token not in log.read_text()


# ── Adapter protocol surface ────────────────────────────────


def test_platform_property():
    assert _adapter().platform == Platform.WEBHOOK
