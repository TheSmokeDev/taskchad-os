"""Tests for the Postiz Public API client (integrations/postiz_api.py).

All network I/O is mocked via httpx.MockTransport injected through the
``client=`` parameter. Env knobs resolve at call time (Rule 1), so
``monkeypatch.setenv`` takes effect with no module reload.
"""

from __future__ import annotations

import json

import httpx
import pytest

from integrations import postiz_api
from integrations.postiz_api import (
    PostizAuthFailure,
    PostizBadRequest,
    PostizNotConfigured,
    PostizRateLimited,
    PostizUnreachable,
)


@pytest.fixture()
def configured_env(monkeypatch):
    monkeypatch.setenv("POSTIZ_API_URL", "http://postiz.test/api")
    monkeypatch.setenv("POSTIZ_API_KEY", "test-key")


@pytest.fixture()
def unconfigured_env(monkeypatch):
    monkeypatch.delenv("POSTIZ_API_URL", raising=False)
    monkeypatch.delenv("POSTIZ_API_KEY", raising=False)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestNotConfigured:
    def test_get_status_short_circuits_without_network(self, unconfigured_env):
        def handler(request):  # pragma: no cover — must never be reached
            raise AssertionError("network I/O attempted while unconfigured")

        status = postiz_api.get_status(client=_client(handler))
        assert status.configured is False
        assert status.reachable is False

    def test_create_post_raises_not_configured(self, unconfigured_env):
        with pytest.raises(PostizNotConfigured):
            postiz_api.create_post(
                integration_id="i1",
                content="hello",
                settings={"__type": "mastodon"},
            )

    def test_url_without_key_is_unconfigured(self, monkeypatch):
        monkeypatch.setenv("POSTIZ_API_URL", "http://postiz.test/api")
        monkeypatch.delenv("POSTIZ_API_KEY", raising=False)
        status = postiz_api.get_status()
        assert status.configured is False


class TestAuthHeader:
    def test_raw_key_no_bearer_prefix(self, configured_env):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=[])

        postiz_api.list_integrations(client=_client(handler))
        assert seen["auth"] == "test-key"  # RAW key — no "Bearer " prefix

    def test_base_url_normalized_to_public_v1(self, configured_env):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json=[])

        postiz_api.list_integrations(client=_client(handler))
        assert seen["url"] == "http://postiz.test/api/public/v1/integrations"


class TestErrorMapping:
    def test_401_raises_auth_failure(self, configured_env):
        def handler(request):
            return httpx.Response(401, text="unauthorized")

        with pytest.raises(PostizAuthFailure):
            postiz_api.list_integrations(client=_client(handler))

    def test_429_raises_rate_limited(self, configured_env):
        def handler(request):
            return httpx.Response(429, text="slow down")

        with pytest.raises(PostizRateLimited):
            postiz_api.list_integrations(client=_client(handler))

    def test_400_raises_bad_request(self, configured_env):
        def handler(request):
            return httpx.Response(400, text="bad payload")

        with pytest.raises(PostizBadRequest):
            postiz_api.list_integrations(client=_client(handler))

    def test_connect_error_raises_unreachable(self, configured_env):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(PostizUnreachable):
            postiz_api.list_integrations(client=_client(handler))

    def test_errors_carry_friendly_message(self):
        assert PostizUnreachable().friendly_message
        assert PostizAuthFailure().friendly_message
        assert PostizNotConfigured().friendly_message


class TestGetStatus:
    def test_reachable_and_authed(self, configured_env):
        def handler(request):
            return httpx.Response(200, json=[{"id": "i1"}, {"id": "i2"}])

        status = postiz_api.get_status(client=_client(handler))
        assert status.configured and status.reachable and status.auth_ok
        assert status.integrations_count == 2

    def test_401_means_backend_up_auth_bad(self, configured_env):
        def handler(request):
            return httpx.Response(401, text="unauthorized")

        status = postiz_api.get_status(client=_client(handler))
        assert status.reachable is True
        assert status.auth_ok is False

    def test_connect_error_means_unreachable(self, configured_env):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        status = postiz_api.get_status(client=_client(handler))
        assert status.reachable is False
        assert status.auth_ok is False


class TestListIntegrations:
    def test_parses_integration_fields(self, configured_env):
        def handler(request):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "cm4e1",
                        "name": "YourBrand",
                        "identifier": "youtube",
                        "picture": "https://p/x.jpg",
                        "disabled": False,
                        "profile": "YourBrand",
                    }
                ],
            )

        items = postiz_api.list_integrations(client=_client(handler))
        assert len(items) == 1
        assert items[0].id == "cm4e1"
        assert items[0].identifier == "youtube"
        assert items[0].disabled is False


class TestConnectUrl:
    def test_returns_url(self, configured_env):
        def handler(request):
            assert request.url.path.endswith("/public/v1/social/mastodon")
            return httpx.Response(200, json={"url": "https://oauth.example/x"})

        url = postiz_api.get_connect_url("mastodon", client=_client(handler))
        assert url == "https://oauth.example/x"

    def test_missing_url_raises(self, configured_env):
        def handler(request):
            return httpx.Response(200, json={})

        with pytest.raises(PostizBadRequest):
            postiz_api.get_connect_url("mastodon", client=_client(handler))


class TestUploadFile:
    def test_multipart_upload_returns_media_dict(self, configured_env, tmp_path):
        card = tmp_path / "card.png"
        card.write_bytes(b"\x89PNG-fake-bytes")

        def handler(request):
            assert request.url.path.endswith("/public/v1/upload")
            content_type = request.headers.get("content-type", "")
            assert content_type.startswith("multipart/form-data")
            assert b"PNG-fake-bytes" in request.read()
            return httpx.Response(
                201, json={"id": "m1", "path": "http://postiz.test/uploads/x.png"}
            )

        media = postiz_api.upload_file(str(card), client=_client(handler))
        assert media == {"id": "m1", "path": "http://postiz.test/uploads/x.png"}

    def test_empty_response_raises(self, configured_env, tmp_path):
        card = tmp_path / "card.png"
        card.write_bytes(b"x")

        def handler(request):
            return httpx.Response(201, json={})

        with pytest.raises(PostizBadRequest):
            postiz_api.upload_file(str(card), client=_client(handler))


class TestCreatePost:
    def test_payload_shape_and_response_parse(self, configured_env):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200, json=[{"postId": "pz-42", "integration": "i1"}]
            )

        post_id = postiz_api.create_post(
            integration_id="i1",
            content="Hello fediverse",
            settings={"__type": "mastodon"},
            client=_client(handler),
        )
        assert post_id == "pz-42"

        body = seen["body"]
        assert body["type"] == "now"
        assert body["shortLink"] is False
        assert body["tags"] == []
        assert body["date"].endswith("Z")
        (item,) = body["posts"]
        assert item["integration"] == {"id": "i1"}
        assert item["value"] == [{"content": "Hello fediverse", "image": []}]
        assert item["settings"] == {"__type": "mastodon"}

    def test_media_attached(self, configured_env):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=[{"postId": "p", "integration": "i"}])

        postiz_api.create_post(
            integration_id="i1",
            content="pic",
            settings={"__type": "instagram", "post_type": "post"},
            media=[{"id": "m1", "path": "https://uploads/x.png"}],
            client=_client(handler),
        )
        assert seen["body"]["posts"][0]["value"][0]["image"] == [
            {"id": "m1", "path": "https://uploads/x.png"}
        ]

    def test_scheduled_post_uses_given_date(self, configured_env):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=[{"postId": "p", "integration": "i"}])

        postiz_api.create_post(
            integration_id="i1",
            content="later",
            settings={"__type": "bluesky"},
            post_type="schedule",
            scheduled_at="2026-07-10T10:00:00.000Z",
            client=_client(handler),
        )
        assert seen["body"]["type"] == "schedule"
        assert seen["body"]["date"] == "2026-07-10T10:00:00.000Z"

    def test_unexpected_response_raises(self, configured_env):
        def handler(request):
            return httpx.Response(200, json={"weird": True})

        with pytest.raises(PostizBadRequest):
            postiz_api.create_post(
                integration_id="i1",
                content="x",
                settings={"__type": "mastodon"},
                client=_client(handler),
            )


class TestListPosts:
    def test_window_params_and_parse(self, configured_env):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "posts": [
                        {
                            "id": "pz-42",
                            "state": "PUBLISHED",
                            "releaseURL": "https://mastodon.social/@x/1",
                        }
                    ]
                },
            )

        posts = postiz_api.list_posts(
            "2026-07-01T00:00:00.000Z",
            "2026-07-07T00:00:00.000Z",
            client=_client(handler),
        )
        assert seen["params"] == {
            "startDate": "2026-07-01T00:00:00.000Z",
            "endDate": "2026-07-07T00:00:00.000Z",
        }
        assert posts[0]["state"] == "PUBLISHED"
