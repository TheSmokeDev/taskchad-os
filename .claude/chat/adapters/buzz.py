"""Native Buzz collaboration adapter.

Inbound uses an authenticated Nostr WebSocket with CLI polling as a durable
degradation path. Outbound, reactions, uploads, and discovery use Block's
official ``buzz`` CLI with argument arrays and stdin content.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from adapters.base import ProgressCapabilities
from buzz_lock import BuzzIdentityLock
from buzz_nostr import (
    make_auth_event,
    normalize_private_key,
    normalize_pubkey,
    npub_from_hex,
    public_key_from_private,
    verify_event,
)
from models import Attachment, Channel, IncomingMessage, OutgoingMessage, Platform, Thread, User

from buzz_config import BuzzSettings, buzz_media_dir, buzz_state_path, get_buzz_settings
from buzz_signals import render_work_receipt
from buzz_state import BuzzStateStore
from buzz_status import write_buzz_status

logger = logging.getLogger(__name__)
_CHAT_KIND = 9
_DM_CREATED_KIND = 41001
_MEMBERSHIP_ADDED_KIND = 44100
_MEMBERSHIP_REMOVED_KIND = 44101
_DISCOVERY_KINDS = [_DM_CREATED_KIND, _MEMBERSHIP_ADDED_KIND, _MEMBERSHIP_REMOVED_KIND]
_FRAME_MAX_BYTES = 2_000_000
_FETCH_LIMIT = 50
_CLI_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 4.0
_WS_RECOVERY_SECONDS = 20.0
_MEDIA_MAX_BYTES = 8 * 1024 * 1024
_HEX_EVENT_ID = re.compile(r"^[0-9a-f]{64}$")


def _json_objects(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _cli_error(stderr: str, returncode: int) -> str:
    text = (stderr or "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("message"):
        return f"{parsed.get('error', 'error')}: {parsed['message']} (exit {returncode})"
    return text[:240] or f"buzz CLI exited {returncode}"


def _resolve_cli_path(configured: str) -> str:
    if configured and configured != "buzz":
        path = Path(configured).expanduser()
        return str(path) if path.is_file() else ""
    found = shutil.which("buzz")
    if found:
        return found
    fallback = Path.home() / "bin" / ("buzz.exe" if os.name == "nt" else "buzz")
    return str(fallback) if fallback.is_file() else ""


def _websocket_url(relay_url: str) -> str:
    parsed = urlsplit(relay_url.strip())
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    if scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("BUZZ_RELAY_URL must use http(s) or ws(s)")
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _looks_like_buzz_dm_name(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized == "dm" or normalized.startswith("group dm (")


class BuzzAdapter:
    """Signed Buzz transport that preserves Homie's router/session/runtime."""

    progress_capabilities = ProgressCapabilities()
    liveness_critical = False

    def __init__(
        self,
        settings: BuzzSettings | None = None,
        *,
        state_path: Path | None = None,
        status_path: Path | None = None,
        lock_root: Path | None = None,
        media_root: Path | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        recovery_interval: float = _WS_RECOVERY_SECONDS,
    ):
        self.settings = settings or get_buzz_settings()
        self.state = BuzzStateStore(state_path or buzz_state_path())
        self.status_path = status_path
        self.lock_root = lock_root
        self.media_root = media_root or buzz_media_dir()
        self.poll_interval = max(0.05, float(poll_interval))
        self.recovery_interval = max(self.poll_interval, float(recovery_interval))
        self.cli_path = _resolve_cli_path(self.settings.cli_path)
        self._private_key = b""
        self._self_pubkey = ""
        self._self_npub = ""
        self._display_name = ""
        self._allowed_pubkeys: set[str] = set()
        self._user_roles: dict[str, str] = {}
        self._channel_names: dict[str, str] = {}
        self._channel_types: dict[str, str] = {}
        self._user_names: dict[str, str] = {}
        self._scope = ""
        self._identity_lock: BuzzIdentityLock | None = None
        self._queue: asyncio.Queue[IncomingMessage | None] = asyncio.Queue()
        self._transport_task: asyncio.Task[None] | None = None
        self._receipt_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Event | None = None
        self._stopping = False
        self._state = "stopped"
        self._active_transport = "none"
        self._last_error: str | None = None
        self._last_event_at: float | None = None
        self._connected_at: float | None = None
        self._last_update_at: float | None = None
        self._last_discovery_at = 0.0
        self._cli_version: str | None = None
        self._cli_compatible: bool | None = None
        self._lock_conflict = False

    @property
    def platform(self) -> Platform:
        return Platform.BUZZ

    def _status(self) -> dict[str, Any]:
        identity = f"{self._self_npub[:12]}…{self._self_npub[-6:]}" if self._self_npub else ""
        return {
            "enabled": self.settings.configured,
            "state": self._state,
            "active_transport": self._active_transport,
            "relay_host": self.settings.relay_host,
            "identity": identity,
            "watched_channel_count": len(self._channel_types),
            "last_event_time": (
                datetime.fromtimestamp(self._last_event_at).astimezone().isoformat()
                if self._last_event_at
                else None
            ),
            "cli_version": self._cli_version,
            "cli_compatible": self._cli_compatible,
            "lock_conflict": self._lock_conflict,
            "last_error": self._last_error,
        }

    def _write_status(self) -> None:
        try:
            write_buzz_status(self._status(), self.status_path)
        except OSError:
            logger.debug("Buzz status write failed", exc_info=True)

    def _set_transport(self, state: str, active: str, error: str | None = None) -> None:
        self._state = state
        self._active_transport = active
        safe_error = error
        if safe_error and self.settings.private_key:
            safe_error = safe_error.replace(self.settings.private_key, "[redacted]")
        self._last_error = safe_error[:240] if safe_error else None
        self._write_status()

    async def _run_cli(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        timeout: float = _CLI_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        if not self.cli_path:
            raise FileNotFoundError("buzz CLI binary not found")
        env = os.environ.copy()
        env["BUZZ_RELAY_URL"] = self.settings.relay_url
        env["BUZZ_PRIVATE_KEY"] = self.settings.private_key
        process = await asyncio.create_subprocess_exec(
            self.cli_path,
            *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input_text.encode("utf-8") if input_text is not None else None),
                timeout=timeout,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, "", "buzz CLI timed out"
        return (
            process.returncode if process.returncode is not None else 4,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def _inspect_cli_version(self) -> None:
        code, stdout, stderr = await self._run_cli(["--version"], timeout=10)
        if code != 0:
            self._cli_version = "unknown"
            self._cli_compatible = None
            self._last_error = f"CLI version unavailable: {_cli_error(stderr, code)}"
            return
        match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)", stdout)
        self._cli_version = match.group(1) if match else stdout.strip()[:80] or "unknown"
        self._cli_compatible = bool(match and match.group(1).startswith("0.5."))
        if self._cli_compatible is False:
            self._last_error = (
                f"Buzz CLI {self._cli_version} is outside the verified 0.5.x contract"
            )

    async def connect(self) -> None:
        missing = self.settings.missing_required()
        if missing:
            self._set_transport("failed", "none", f"missing required config: {', '.join(missing)}")
            raise RuntimeError(self._last_error)
        if not self.cli_path:
            self._set_transport("failed", "none", "buzz CLI binary not found")
            raise RuntimeError("buzz CLI binary not found; set BUZZ_CLI_PATH")
        self._set_transport("connecting", "none")
        try:
            self._private_key = normalize_private_key(self.settings.private_key)
            self._self_pubkey = public_key_from_private(self._private_key)
            self._self_npub = npub_from_hex(self._self_pubkey)
            self._allowed_pubkeys = {
                normalize_pubkey(value) for value in self.settings.allowed_pubkeys
            }
            configured_roles: dict[str, str] = {}
            for value, role in self.settings.pubkey_roles:
                pubkey = normalize_pubkey(value)
                if pubkey not in self._allowed_pubkeys or role not in {
                    "viewer",
                    "operator",
                    "admin",
                }:
                    raise ValueError("invalid Buzz pubkey role mapping")
                configured_roles[pubkey] = role
            self._user_roles = {
                pubkey: configured_roles.get(pubkey, "viewer") for pubkey in self._allowed_pubkeys
            }
        except (TypeError, ValueError) as exc:
            self._set_transport("failed", "none", "invalid Buzz identity configuration")
            raise RuntimeError(self._last_error) from exc
        await self._inspect_cli_version()

        code, stdout, stderr = await self._run_cli(["users", "get"])
        profiles = _json_objects(stdout)
        if code != 0 or not profiles:
            detail = _cli_error(stderr, code) if code else "buzz users get returned no profile"
            self._set_transport("failed", "none", detail)
            raise RuntimeError(detail)
        profile_pubkey = normalize_pubkey(str(profiles[0].get("pubkey") or ""))
        if profile_pubkey != self._self_pubkey:
            self._set_transport("failed", "none", "Buzz CLI profile does not match private key")
            raise RuntimeError(self._last_error)
        self._display_name = str(profiles[0].get("display_name") or "").strip()

        self._identity_lock = BuzzIdentityLock(
            self.settings.relay_url, self._self_pubkey, root=self.lock_root
        )
        if not self._identity_lock.acquire():
            self._lock_conflict = True
            self._set_transport("failed", "none", "Buzz identity already active on this relay")
            raise RuntimeError(self._last_error)

        try:
            await self._discover_channels(seed=True)
            if not self._channel_types:
                raise RuntimeError("no Buzz channels to watch")
            self.state.recover_sending_receipts()
            self._stopping = False
            self._ready = asyncio.Event()
            self._transport_task = asyncio.create_task(self._transport_supervisor())
            if self.settings.signal_channel:
                self._receipt_task = asyncio.create_task(self._receipt_loop())
            await asyncio.wait_for(self._ready.wait(), timeout=25)
            if self._state == "failed":
                raise RuntimeError(self._last_error or "Buzz transport failed")
            self._connected_at = time.time()
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        self._stopping = True
        for task in (self._transport_task, self._receipt_task):
            if task and not task.done():
                task.cancel()
        for task in (self._transport_task, self._receipt_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._transport_task = None
        self._receipt_task = None
        if self._identity_lock:
            self._identity_lock.release()
            self._identity_lock = None
        self._set_transport("stopped", "none")
        await self._queue.put(None)

    async def reconnect(self) -> None:
        await self.disconnect()
        self._queue = asyncio.Queue()
        await self.connect()

    async def probe_liveness(self) -> Any:
        from liveness import ProbeResult

        self._write_status()
        if self._transport_task is None or self._transport_task.done():
            return ProbeResult(False, self._last_error or "Buzz transport supervisor stopped")
        if self._active_transport == "polling":
            return ProbeResult(True, "degraded: CLI polling active; WebSocket recovery pending")
        return ProbeResult(
            self._state == "connected", f"{self._state} via {self._active_transport}"
        )

    async def listen(self) -> AsyncIterator[IncomingMessage]:
        while True:
            message = await self._queue.get()
            if message is None:
                return
            yield message

    async def send(self, message: OutgoingMessage) -> str | None:
        return await self._send_text(
            message.channel.platform_id,
            message.text,
            reply_to=self._reply_target(message.thread),
            attachments=message.attachments,
        )

    async def deliver_scheduled(
        self, text: str, *, attachments: list[Attachment] | None = None
    ) -> str | None:
        """Deliver a cron/scheduled result to this profile's configured home room."""
        missing = self.settings.missing_required()
        if missing:
            raise RuntimeError(f"missing required config: {', '.join(missing)}")
        if not self.settings.home_channel:
            raise RuntimeError("BUZZ_HOME_CHANNEL is required for scheduled delivery")
        return await self._send_text(self.settings.home_channel, text, attachments=attachments)

    async def update(self, message: OutgoingMessage) -> str | None:
        return None

    async def send_typing(self, channel: Channel) -> None:
        return None

    @staticmethod
    def _reply_target(thread: Thread | None) -> str | None:
        if not thread:
            return None
        candidate = thread.parent_message_id or thread.thread_id
        candidate = candidate.lower()
        return candidate if _HEX_EVENT_ID.fullmatch(candidate) else None

    async def _send_text(
        self,
        channel_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> str | None:
        args = ["messages", "send", "--channel", str(channel_id), "--content", "-"]
        if reply_to:
            args.extend(["--reply-to", reply_to])
        body = text
        for attachment in attachments or []:
            candidate = Path(attachment.url or attachment.filename).expanduser()
            if candidate.is_file():
                args.extend(["--file", str(candidate)])
            elif attachment.url:
                body = f"{body}\n{attachment.url}".strip()
        code, stdout, stderr = await self._run_cli(args, input_text=body)
        if code != 0:
            raise RuntimeError(_cli_error(stderr, code))
        rows = _json_objects(stdout)
        return str(rows[0].get("event_id")) if rows and rows[0].get("event_id") else None

    async def _send_reaction(self, event_id: str, emoji: str = "👀") -> bool:
        code, _stdout, _stderr = await self._run_cli(
            ["reactions", "add", "--event", event_id, "--emoji", emoji]
        )
        return code == 0

    async def _discover_channels(self, *, seed: bool) -> None:
        code, stdout, stderr = await self._run_cli(["channels", "list"])
        if code != 0:
            raise RuntimeError(_cli_error(stderr, code))
        listed = _json_objects(stdout)
        self._channel_names.update(
            {
                str(row["channel_id"]): str(row.get("name") or row["channel_id"])
                for row in listed
                if row.get("channel_id")
            }
        )
        watched = list(self.settings.channels) or list(self._channel_names)
        for channel_id in watched:
            # A mutable channel name (including "DM") and ordinary kind-39000
            # metadata are not authorization evidence. Start fail-closed as a
            # room; relay-confirmed kind-41001 discovery upgrades real DMs below.
            await self._add_channel(channel_id, "group", seed=seed)
        await self._discover_dms(seed=seed)
        await self._discover_structured_dm_candidates(listed, watched, seed=seed)
        self._last_discovery_at = time.monotonic()
        self._write_status()

    async def _discover_dms(self, *, seed: bool) -> None:
        code, stdout, _stderr = await self._run_cli(["dms", "list"])
        if code != 0:
            return
        for row in _json_objects(stdout):
            dm_id = str(row.get("dm_id") or "")
            if dm_id:
                self._channel_names[dm_id] = "DM"
                await self._add_channel(dm_id, "dm", seed=seed)

    async def _discover_structured_dm_candidates(
        self,
        listed: list[dict[str, Any]],
        watched: list[str],
        *,
        seed: bool,
    ) -> None:
        """Resolve v0.5.2 DMs when its kind-41001 projection is absent.

        The compact ``channels list`` shape exposes only a mutable name. For
        names used by Buzz's own DM records, ``channels search`` projects the
        relay-only kind-39000 ``t`` tag as ``channel_type``. Classification
        depends solely on that structured type and exact channel UUID; a room
        named ``DM`` therefore remains a room.
        """
        watched_ids = set(watched)
        candidate_names = {
            str(row.get("name") or "").strip()
            for row in listed
            if str(row.get("channel_id") or "") in watched_ids
            and self._channel_types.get(str(row.get("channel_id") or "")) != "dm"
            and _looks_like_buzz_dm_name(str(row.get("name") or ""))
        }
        for name in sorted(candidate_names):
            code, stdout, _stderr = await self._run_cli(
                [
                    "channels",
                    "search",
                    "--query",
                    name,
                    "--exact",
                    "--include-archived",
                ]
            )
            if code != 0:
                continue
            for row in _json_objects(stdout):
                channel_id = str(row.get("channel_id") or "")
                channel_type = str(row.get("channel_type") or "").strip().lower()
                if channel_id in watched_ids and channel_type == "dm":
                    self._channel_names[channel_id] = name
                    await self._add_channel(channel_id, "dm", seed=seed)

    async def _add_channel(self, channel_id: str, chat_type: str, *, seed: bool) -> None:
        self._channel_types[channel_id] = chat_type
        if not seed or self.state.cursor(self._scope_value(), channel_id) is not None:
            return
        code, stdout, _stderr = await self._run_cli(
            ["messages", "get", "--channel", channel_id, "--limit", str(_FETCH_LIMIT)]
        )
        if code != 0:
            self.state.seed_cursor(self._scope_value(), channel_id, int(time.time()), [])
            return
        latest = 0
        at_latest: list[str] = []
        for event in _json_objects(stdout):
            created_at = int(event.get("created_at") or 0)
            event_id = str(event.get("id") or "")
            if created_at > latest:
                latest, at_latest = created_at, [event_id] if event_id else []
            elif created_at == latest and event_id:
                at_latest.append(event_id)
        self.state.seed_cursor(
            self._scope_value(), channel_id, latest or int(time.time()), at_latest
        )

    def _scope_value(self) -> str:
        if not self._scope:
            self._scope = f"{self.settings.relay_url.rstrip('/')}|{self._self_pubkey}"
        return self._scope

    async def _transport_supervisor(self) -> None:
        if self.settings.transport == "poll":
            self._set_transport("connected", "polling")
            if self._ready:
                self._ready.set()
            await self._poll_forever()
            return
        backoff = 1.0
        while not self._stopping:
            try:
                await self._websocket_session()
                raise ConnectionError("Buzz WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if self.settings.transport == "websocket" and self._connected_at is None:
                    self._set_transport("failed", "none", detail)
                    if self._ready:
                        self._ready.set()
                    return
                self._set_transport("degraded", "polling", detail)
                if self._ready:
                    self._ready.set()
                deadline = time.monotonic() + max(self.recovery_interval, backoff)
                while time.monotonic() < deadline and not self._stopping:
                    await self._poll_sweep()
                    await asyncio.sleep(self.poll_interval)
                backoff = min(backoff * 2, 30.0)

    async def _poll_forever(self) -> None:
        count = 0
        while not self._stopping:
            await self._poll_sweep()
            count += 1
            if count % 5 == 0:
                await self._discover_dms(seed=False)
            await asyncio.sleep(self.poll_interval)

    async def _poll_sweep(self) -> None:
        if time.monotonic() - self._last_discovery_at >= 30.0:
            try:
                await self._discover_channels(seed=False)
            except (FileNotFoundError, RuntimeError):
                pass
        for channel_id in list(self._channel_types):
            cursor = self.state.cursor(self._scope_value(), channel_id)
            since = cursor[0] if cursor else int(time.time())
            args = [
                "messages",
                "get",
                "--channel",
                channel_id,
                "--limit",
                str(_FETCH_LIMIT),
                "--since",
                str(since),
            ]
            code, stdout, _stderr = await self._run_cli(args)
            if code != 0:
                continue
            for event in sorted(
                _json_objects(stdout), key=lambda row: int(row.get("created_at") or 0)
            ):
                await self._handle_event(channel_id, event)

    async def _websocket_session(self) -> None:
        import websockets

        websocket_url = _websocket_url(self.settings.relay_url)
        async with websockets.connect(
            websocket_url,
            open_timeout=20,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=_FRAME_MAX_BYTES,
        ) as websocket:
            await self._authenticate(websocket, websocket_url)
            subscriptions: dict[str, str | None] = {}
            for index, channel_id in enumerate(self._channel_types):
                subscription_id = f"homie-buzz-{index}"
                subscriptions[subscription_id] = channel_id
                await self._subscribe_channel(websocket, subscription_id, channel_id)
            membership_id = "homie-buzz-membership"
            subscriptions[membership_id] = None
            await websocket.send(
                json.dumps(
                    [
                        "REQ",
                        membership_id,
                        {
                            "kinds": _DISCOVERY_KINDS,
                            "#p": [self._self_pubkey],
                            "since": int(time.time()) - 1,
                        },
                    ],
                    separators=(",", ":"),
                )
            )
            self._set_transport("connected", "websocket")
            if self._ready:
                self._ready.set()
            async for raw in websocket:
                if isinstance(raw, bytes):
                    if len(raw) > _FRAME_MAX_BYTES:
                        continue
                    try:
                        raw = raw.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        continue
                elif len(raw.encode("utf-8")) > _FRAME_MAX_BYTES:
                    continue
                try:
                    frame = json.loads(raw)
                except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(frame, list) or not frame:
                    continue
                if frame[0] == "EVENT" and len(frame) >= 3 and isinstance(frame[2], dict):
                    subscription_id = str(frame[1])
                    event = frame[2]
                    if subscription_id == membership_id:
                        if verify_event(event):
                            before = set(self._channel_types)
                            await self._discover_channels(seed=False)
                            for channel_id in set(self._channel_types) - before:
                                sub_id = f"homie-buzz-dm-{len(subscriptions)}"
                                subscriptions[sub_id] = channel_id
                                await self._subscribe_channel(websocket, sub_id, channel_id)
                    else:
                        channel_id = subscriptions.get(subscription_id)
                        if channel_id:
                            await self._handle_event(channel_id, event)
                elif frame[0] == "CLOSED":
                    raise ConnectionError(
                        str(frame[-1] if len(frame) > 1 else "subscription closed")
                    )

    async def _authenticate(self, websocket: Any, websocket_url: str) -> None:
        raw = await asyncio.wait_for(websocket.recv(), timeout=20)
        frame = json.loads(raw)
        if not isinstance(frame, list) or len(frame) < 2 or frame[0] != "AUTH":
            raise ConnectionError("Buzz relay did not send NIP-42 AUTH")
        event = make_auth_event(
            relay_url=websocket_url,
            challenge=str(frame[1]),
            private_key=self._private_key,
            created_at=int(time.time()),
        )
        await websocket.send(json.dumps(["AUTH", event], separators=(",", ":")))
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=20)
            response = json.loads(raw)
            if not isinstance(response, list) or not response:
                continue
            if response[0] == "OK" and len(response) >= 4 and response[1] == event["id"]:
                if response[2] is True:
                    return
                raise ConnectionError(f"Buzz AUTH rejected: {response[3]}")
            if response[0] in {"NOTICE", "CLOSED"}:
                raise ConnectionError(f"Buzz AUTH failed: {response[-1]}")

    async def _subscribe_channel(self, websocket: Any, sub_id: str, channel_id: str) -> None:
        cursor = self.state.cursor(self._scope_value(), channel_id)
        since = max((cursor[0] if cursor else int(time.time())) - 1, 0)
        await websocket.send(
            json.dumps(
                ["REQ", sub_id, {"kinds": [_CHAT_KIND], "#h": [channel_id], "since": since}],
                separators=(",", ":"),
            )
        )

    async def _handle_event(self, channel_id: str, event: dict[str, Any]) -> None:
        if not verify_event(event) or int(event.get("kind") or 0) != _CHAT_KIND:
            return
        channel_tags = [
            str(tag[1])
            for tag in event.get("tags", [])
            if isinstance(tag, list) and len(tag) > 1 and tag[0] == "h"
        ]
        if channel_tags != [channel_id]:
            return
        event_id = str(event["id"]).lower()
        pubkey = str(event["pubkey"]).lower()
        created_at = int(event["created_at"])
        content = event.get("content")
        if created_at > int(time.time()) + 300:
            return
        if not isinstance(content, str) or not content.strip() or len(content) > 65_536:
            return
        if not self.state.record_event_if_new(
            self._scope_value(), channel_id, event_id, created_at
        ):
            return
        if pubkey == self._self_pubkey or pubkey not in self._allowed_pubkeys:
            return
        is_dm = self._channel_types.get(channel_id) == "dm"
        if not is_dm and self.settings.require_mention and not self._is_mentioned(content, event):
            return
        text = self._strip_leading_mention(content)
        if not text:
            return
        display_name = await self._resolve_user_name(pubkey)
        incoming = IncomingMessage(
            text=text,
            user=User(platform=Platform.BUZZ, platform_id=pubkey, display_name=display_name),
            channel=Channel(
                platform=Platform.BUZZ,
                platform_id=channel_id,
                name=self._channel_names.get(channel_id, channel_id),
                is_dm=is_dm,
            ),
            platform=Platform.BUZZ,
            thread=self._thread_from_event(event),
            platform_message_id=event_id,
            attachments=await self._materialize_attachments(
                event_id, self._attachments_from_event(event)
            ),
            timestamp=datetime.fromtimestamp(created_at).astimezone(),
            raw_event=event,
            user_role=self._user_roles.get(pubkey, "viewer"),
        )
        self._last_event_at = time.time()
        self._last_update_at = self._last_event_at
        self._write_status()
        await self._send_reaction(event_id)
        await self._queue.put(incoming)

    def _is_mentioned(self, content: str, event: dict[str, Any] | None = None) -> bool:
        lowered = content.lower()
        if self._self_pubkey in lowered or self._self_npub.lower() in lowered:
            return True
        if event and any(
            isinstance(tag, list)
            and len(tag) > 1
            and tag[0] == "p"
            and str(tag[1]).lower() == self._self_pubkey
            for tag in event.get("tags", [])
        ):
            return True
        if self._display_name:
            return bool(
                re.search(rf"(?<!\w)@?{re.escape(self._display_name.lower())}(?!\w)", lowered)
            )
        return False

    def _strip_leading_mention(self, content: str) -> str:
        candidates = [self._self_pubkey, self._self_npub]
        if self._display_name:
            candidates.insert(0, self._display_name)
        values = "|".join(re.escape(value) for value in candidates if value)
        pattern = rf"^(?:@|nostr:)?(?:{values})[\s:,]*"
        return re.sub(pattern, "", content.strip(), count=1, flags=re.IGNORECASE).strip()

    async def _resolve_user_name(self, pubkey: str) -> str:
        if pubkey in self._user_names:
            return self._user_names[pubkey]
        code, stdout, _stderr = await self._run_cli(["users", "get", "--pubkey", pubkey])
        rows = _json_objects(stdout) if code == 0 else []
        name = str(rows[0].get("display_name") or "").strip() if rows else ""
        self._user_names[pubkey] = name or npub_from_hex(pubkey)[:16]
        return self._user_names[pubkey]

    @staticmethod
    def _thread_from_event(event: dict[str, Any]) -> Thread:
        root: str | None = None
        parent: str | None = None
        for tag in event.get("tags", []):
            if not isinstance(tag, list) or len(tag) < 2 or tag[0] != "e":
                continue
            marker = str(tag[3]).lower() if len(tag) > 3 else ""
            if marker == "root":
                root = str(tag[1])
            elif marker == "reply":
                parent = str(tag[1])
            elif parent is None:
                parent = str(tag[1])
        event_id = str(event.get("id") or "")
        return Thread(thread_id=root or parent or event_id, parent_message_id=parent or event_id)

    @staticmethod
    def _attachments_from_event(event: dict[str, Any]) -> list[Attachment]:
        attachments: list[Attachment] = []
        seen_urls: set[str] = set()
        # Buzz 0.5.x emits NIP-92 `imeta` tags on ordinary kind-9 events.
        # The CLI's JSON output is the raw event, so parse protocol metadata
        # rather than relying on a non-standard `attachments` wrapper.
        for tag in event.get("tags", []):
            if len(attachments) >= 10:
                break
            if not isinstance(tag, list) or not tag or tag[0] != "imeta":
                continue
            metadata: dict[str, str] = {}
            for raw in tag[1:]:
                if not isinstance(raw, str):
                    continue
                key, separator, value = raw.partition(" ")
                if separator and key and value:
                    metadata[key] = value
            url = metadata.get("url", "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            mimetype = metadata.get("m") or None
            filename = unquote(Path(urlsplit(url).path).name) or "attachment"
            if not Path(filename).suffix and mimetype:
                filename += mimetypes.guess_extension(mimetype) or ""
            try:
                size = int(metadata["size"]) if "size" in metadata else None
            except ValueError:
                size = None
            attachments.append(
                Attachment(
                    filename=filename,
                    mimetype=mimetype,
                    url=url,
                    size_bytes=size,
                )
            )
        return attachments

    async def _materialize_attachments(
        self, event_id: str, attachments: list[Attachment]
    ) -> list[Attachment]:
        """Download trusted, bounded relay media into profile-owned local state.

        Attachment parsing elsewhere in Homie intentionally accepts local paths,
        not arbitrary remote URLs. Restricting downloads to the configured relay
        host also prevents an authenticated sender from turning Homie into an SSRF
        client. A failed download keeps a metadata-only attachment with no local
        path so the runtime can disclose that the file was unavailable.
        """
        if not attachments:
            return []
        import httpx

        relay_parts = urlsplit(self.settings.relay_url)
        relay_host = (relay_parts.hostname or "").lower()
        relay_port = relay_parts.port
        self.media_root.mkdir(parents=True, exist_ok=True)
        result: list[Attachment] = []
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            for index, attachment in enumerate(attachments):
                parsed = urlsplit(attachment.url or "")
                declared = attachment.size_bytes
                if (
                    parsed.scheme not in {"http", "https"}
                    or not relay_host
                    or (parsed.hostname or "").lower() != relay_host
                    or parsed.port != relay_port
                    or parsed.username is not None
                    or parsed.password is not None
                    or (declared is not None and declared > _MEDIA_MAX_BYTES)
                ):
                    result.append(
                        Attachment(
                            filename=attachment.filename,
                            mimetype=attachment.mimetype,
                            size_bytes=declared,
                        )
                    )
                    continue
                safe_name = self._safe_attachment_filename(attachment.filename)
                suffix = Path(safe_name).suffix[:12]
                digest = hashlib.sha256(
                    f"{event_id}:{index}:{attachment.url}".encode()
                ).hexdigest()[:24]
                local_path = self.media_root / f"{digest}{suffix}"
                try:
                    total = 0
                    async with client.stream("GET", attachment.url or "") as response:
                        response.raise_for_status()
                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > _MEDIA_MAX_BYTES:
                            raise ValueError("Buzz media exceeds parser limit")
                        with local_path.open("wb") as handle:
                            async for chunk in response.aiter_bytes():
                                total += len(chunk)
                                if total > _MEDIA_MAX_BYTES:
                                    raise ValueError("Buzz media exceeds parser limit")
                                handle.write(chunk)
                    result.append(
                        Attachment(
                            filename=safe_name,
                            mimetype=attachment.mimetype,
                            url=str(local_path),
                            size_bytes=total,
                        )
                    )
                except (httpx.HTTPError, OSError, ValueError):
                    try:
                        local_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    result.append(
                        Attachment(
                            filename=safe_name,
                            mimetype=attachment.mimetype,
                            size_bytes=declared,
                        )
                    )
        return result

    @staticmethod
    def _safe_attachment_filename(filename: str) -> str:
        name = Path(filename or "attachment").name
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
        return name[:160] or "attachment"

    async def _receipt_loop(self) -> None:
        while not self._stopping:
            for row in self.state.claim_receipts(limit=20):
                attempts = int(row["attempts"]) + 1
                try:
                    event_id = await self._send_text(
                        self.settings.signal_channel, render_work_receipt(row["payload"])
                    )
                    self.state.mark_receipt_sent(int(row["id"]), event_id)
                except asyncio.CancelledError:
                    self.state.release_receipt(int(row["id"]), "adapter stopped", attempts)
                    raise
                except Exception as exc:
                    self.state.release_receipt(int(row["id"]), str(exc), attempts)
            await asyncio.sleep(2)
