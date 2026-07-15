"""Chat router connecting platform adapters to the conversation engine.

Uses ExtensionManager for command dispatch instead of hardcoded elif chains.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from pathlib import Path
from datetime import datetime
from typing import Any

import background_tasks
from adapters.base import resolve_progress_capabilities
from commands import get_command_min_role, get_engine_command_description, get_piv_instruction
from discord_channel_bindings import resolve_discord_channel_binding
from discord_persona_runtime import run_discord_persona_channel_turn
from engine import ConversationEngine
from extension_manager import ExtensionManager
from models import OutgoingMessage, Platform
from session import Session
from session_keys import build_session_key, resolve_thread_id

try:
    from imagegen_workflow import (
        build_imagegen_skill_prompt,
        build_persona_imagegen_skill_prompt,
    )

    _IMAGEGEN_AVAILABLE = True
except ImportError:
    _IMAGEGEN_AVAILABLE = False

# gap-4 URL ingest — raw-regex match on the original message text BEFORE any
# command parsing. Routing on parsed[0] == "vault-ingest" would NOT fire because
# vault-ingest is a Skill (not in the router_commands registry); this regex is
# the only path that triggers URL ingest from chat surface.
_VAULT_INGEST_URL_RE = re.compile(
    r"^/vault-ingest\s+(https?://\S+)\s*$", re.IGNORECASE
)
_VAULT_COMMAND_ALIAS_RE = re.compile(
    r"^/(vaults|vault-ops)(?=\s|$)\s*(.*)$", re.IGNORECASE | re.DOTALL
)

# Phase 3 document ingest — matched against IncomingMessage.caption (NOT the
# rendered turn text). Default-deny: only a caption that is EXACTLY the
# command (whitespace-tolerant) triggers ingest of an upload. Prose captions
# mentioning vault-ingest, bare command text sent AFTER an upload, and
# caption-less uploads all fall through to the engine unchanged.
_VAULT_INGEST_DOC_RE = re.compile(r"^/vault-ingest\s*$", re.IGNORECASE)

# Direct operational phrasing for the framework itself.  Keep this anchored
# and named so ordinary requests like "update the document" still reach the
# engine; only explicit self/framework/public-repo requests become `/update now`.
_PUBLIC_REPO_SPOKEN_RE = ("task" + "chad") + r"\s+os"
_FRAMEWORK_UPDATE_NOW_RE = re.compile(
    rf"^\s*(?:please\s+)?(?:"
    rf"(?:pull|install|upgrade|update)\s+(?:the\s+)?(?:latest|lastest|stable)(?:\s+update)?\s+(?:on|for|from)?\s*(?:{_PUBLIC_REPO_SPOKEN_RE}|the\s+homie)"
    rf"|(?:pull|update|upgrade)\s+(?:the\s+)?(?:{_PUBLIC_REPO_SPOKEN_RE}|homie|framework|repo(?:sitory)?)"
    r"|update\s+(?:yourself|my\s+homie|the\s+homie)"
    r")\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _adapter_connect_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("CHAT_ADAPTER_CONNECT_TIMEOUT_SECONDS", "20")))
    except (TypeError, ValueError):
        return 20.0


class _DocumentCompileError(RuntimeError):
    """Document ingest failed AFTER the raw archive landed (Phase 1 honesty
    contract): the reply must name the archived raw file and state that
    concept compilation did not happen — never claim total failure when the
    raw copy exists on disk."""

    def __init__(self, raw_name: str, original: Exception) -> None:
        self.raw_name = raw_name
        self.original = original
        super().__init__(f"{type(original).__name__}: {original}")


_DISPLAY_FILENAME_MAX_CHARS = 80
# Telegram legacy-Markdown sensitive characters — the Telegram adapter sends
# router replies with parse_mode="Markdown" (telegram.py), so an unescaped
# filename could open/close a formatting span or a link inside the reply.
_MARKDOWN_SENSITIVE_RE = re.compile(r"([`*_\[\]])")


def _display_filename(name: Any) -> str:
    """Render an attacker-controlled filename safe for reply text (post-build F3).

    DISPLAY concern only — archive naming is preserve_raw's central
    `_sanitize_archive_name` (storage layer; keep the two separate). Strips
    control chars including CR/LF (a filename must never inject reply lines),
    collapses to a single line, caps length, and escapes legacy-Markdown
    tokens for the adapter's Markdown parse path.
    """
    text = str(name or "")
    text = "".join(ch for ch in text if ord(ch) >= 0x20 and ch != "\x7f")
    text = " ".join(text.split())
    if len(text) > _DISPLAY_FILENAME_MAX_CHARS:
        text = text[: _DISPLAY_FILENAME_MAX_CHARS - 3] + "..."
    text = _MARKDOWN_SENSITIVE_RE.sub(r"\\\1", text)
    return text or "attachment"


DEFAULT_ENGINE_TIMEOUT_SECONDS = 900.0
# Test/legacy override. Normal runtime reads config.CHAT_ENGINE_TIMEOUT_SECONDS
# at call time so /reload can update the guard without restarting the process.
ENGINE_TIMEOUT_SECONDS: float | None = None
# Keep expiring typing indicators alive and recover one transient status-post
# failure before returning to the normal progress cadence.
PROGRESS_UPDATE_SECONDS = 8.0
PROGRESS_RECOVERY_RETRY_SECONDS = 2.0
PROGRESS_IO_TIMEOUT_SECONDS = 2.0
PREFETCH_ONLY_INTENTS = {"browserops"}
VAULT_COMMAND_NAMES = {"vault", "vaults", "vault-ops"}
VAULT_NAME_ALIASES = {
    "second": "thehomie",
    "thehomie": "thehomie",
    "thehomie": "thehomie",
    "coding": "coding-vault",
    "coding-vault": "coding-vault",
}
VAULT_RECALL_MODES = {"auto", "hybrid", "keyword"}
VAULT_OPS_ROUTINES = {
    "orient": "Medium (~1-2 min)",
    "morning": "Medium (~1-2 min)",
    "debrief": "Medium (~2-3 min)",
    "evening": "Medium (~2-3 min)",
    "weekly": "Heavy (~5-10 min)",
    "capture": "Light (~5 sec)",
    "ingest": "Medium (~1-2 min)",
    "compile": "Light-Heavy",
    "research": "Very heavy (~10-30 min)",
    "maintain": "Heavy (~3-5 min)",
    "context": "Medium (~1-2 min)",
    "status": "Light (~30 sec)",
    "think": "Heavy (~5-10 min)",
}


def _progress_tool_label(raw_name: Any) -> str:
    """Turn a runtime tool name into a short, non-sensitive status label."""

    name = re.sub(r"[^A-Za-z0-9_. -]+", "", str(raw_name or "")).strip()[:64]
    folded = name.lower()
    tokens = {token for token in re.split(r"[_. -]+", folded) if token}
    if not tokens:
        return "Using a tool"
    if tokens & {"read", "readfile"}:
        return "Reading files"
    if tokens & {"grep", "glob", "search", "find", "searchfiles"}:
        return "Searching"
    if tokens & {"bash", "terminal", "command", "shell", "exec", "execute"}:
        return "Running a command"
    if tokens & {"write", "edit", "patch", "applypatch"}:
        return "Updating files"
    if tokens & {"browser", "navigate", "click"}:
        return "Checking the browser"
    if tokens & {"api", "http", "integration", "mcp", "request"}:
        return "Using an integration"
    # Unknown runtime names are intentionally not echoed. Tool registries are
    # normally controlled, but a provider/plugin could surface a client name,
    # local path, or command fragment in this field.
    return "Using a tool"


def _render_progress_status(progress: dict[str, Any]) -> str:
    """Render truthful, bounded progress without exposing tool arguments."""

    elapsed = max(0, int(time.time() - float(progress.get("started") or time.time())))
    current_tool = progress.get("current_tool")
    if current_tool:
        label = _progress_tool_label(current_tool)
        prefix = "🔧"
    else:
        raw_status = str(progress.get("status") or "Working")
        label = " ".join(raw_status.split())[:120] or "Working"
        prefix = "⏳"
    status = f"{prefix} {label} — {elapsed}s"
    calls = int(progress.get("tool_calls") or 0)
    if calls:
        status += f" | {calls} tool call{'s' if calls != 1 else ''}"
    return status

_LINKEDIN_PROFILE_MARKERS = (
    "linkedin profile",
    "linked in profile",
    "my linkedin",
    "my linked in",
    "linkedin account",
    "linked in account",
)
_LINKEDIN_PROFILE_OPEN_MARKERS = (
    "open",
    "open up",
    "pull up",
    "bring up",
    "load",
    "go to",
    "show me",
    "take me to",
    "look at",
)


def _engine_timeout_seconds(has_attachments: bool = False) -> float:
    """Return the configured whole-turn engine timeout in seconds.

    Attachment turns (document uploads) get the longer
    CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS budget. The module-level
    ENGINE_TIMEOUT_SECONDS test override keeps ABSOLUTE precedence.
    """

    if ENGINE_TIMEOUT_SECONDS is not None:
        try:
            return max(0.001, float(ENGINE_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            return DEFAULT_ENGINE_TIMEOUT_SECONDS

    if has_attachments:
        try:
            from config import CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS
            return max(0.001, float(CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS))
        except Exception:
            return DEFAULT_ENGINE_TIMEOUT_SECONDS

    try:
        from config import CHAT_ENGINE_TIMEOUT_SECONDS
        return max(0.001, float(CHAT_ENGINE_TIMEOUT_SECONDS))
    except Exception:
        return DEFAULT_ENGINE_TIMEOUT_SECONDS


def _format_seconds(seconds: float) -> str:
    seconds = float(seconds)
    if seconds.is_integer():
        return str(int(seconds))
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def _engine_timeout_message(timeout_seconds: float, attachments: list[Any] | None = None) -> str:
    formatted = _format_seconds(timeout_seconds)
    if attachments:
        names = [_display_filename(getattr(a, "filename", "")) for a in attachments]
        shown = ", ".join(names[:3]) + (f" (+{len(names) - 3} more)" if len(names) > 3 else "")
        plural = len(names) > 1
        return (
            f"I hit the chat runtime timeout after {formatted}s. "
            f"I have not confirmed the uploaded {'files were' if plural else 'file was'} "
            f"processed yet: {shown}. I kept the turn running in the background "
            "and will post the result here if it finishes."
        )
    return (
        f"I hit the chat runtime timeout after {formatted}s before the model "
        "returned. I kept that turn running in the background and will post "
        "the result here if it finishes. You can keep chatting."
    )


def _incoming_display_text(incoming: Any) -> str:
    raw_event = getattr(incoming, "raw_event", None)
    if isinstance(raw_event, dict):
        candidate = raw_event.get("display_text")
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return getattr(incoming, "text", "") or ""


def _linkedin_profile_natural_action(text: str) -> str | None:
    """Map explicit LinkedIn profile browser requests to the safe router command."""

    lowered = " ".join((text or "").lower().split())
    if "linkedin" not in lowered and "linked in" not in lowered:
        return None
    if not any(marker in lowered for marker in _LINKEDIN_PROFILE_MARKERS):
        return None
    if any(marker in lowered for marker in _LINKEDIN_PROFILE_OPEN_MARKERS):
        return "open"
    return None


class ChatRouter:
    """Routes messages between platform adapters and the conversation engine.

    Handles concurrent conversations while preserving per-thread ordering.
    """

    def __init__(self, engine: ConversationEngine, manager: ExtensionManager) -> None:
        self.engine = engine
        self.adapters: dict[Platform, Any] = {}
        self.manager = manager
        self._transcript_reset_commands = {"clear", "reload"}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._pending_bursts: dict[str, list[Any]] = {}
        self._burst_tasks: dict[str, asyncio.Task[Any]] = {}
        self._burst_delay_seconds = 1.2
        self._pending_followup_choices: dict[str, tuple[Any, Any, str]] = {}
        self._turn_choice_counter = 0
        self._background_engine_tasks: set[asyncio.Task[Any]] = set()
        # Homie Mobile M7 — in-flight engine turns by conversation key, so the
        # dashboard /stop endpoint can cancel a running turn mid-flight.
        self._active_turns: dict[str, asyncio.Task[Any]] = {}

    def register(self, adapter: Any) -> None:
        """Register a platform adapter."""
        self.adapters[adapter.platform] = adapter
        print(f"[{datetime.now()}] Registered adapter: {adapter.platform.value}")

    async def run(self) -> None:
        """Connect all adapters and start listening for messages."""
        if not self.adapters:
            print(f"[{datetime.now()}] No adapters registered, nothing to do")
            return

        # Initialize core handler context (engine, adapters, start time)
        try:
            from core_handlers import set_context
            set_context(engine=self.engine, adapters=self.adapters, bot_start_time=datetime.now())
        except ImportError:
            pass  # core_handlers not available — handlers will degrade gracefully

        async def _connect_adapter(platform: Platform, adapter: Any) -> tuple[Platform, str | None]:
            timeout_s = _adapter_connect_timeout_seconds()
            try:
                await asyncio.wait_for(adapter.connect(), timeout=timeout_s)
                return platform, None
            except asyncio.TimeoutError:
                try:
                    await adapter.disconnect()
                except Exception:
                    pass
                return platform, f"timed out after {timeout_s:g}s"
            except Exception as e:
                return platform, str(e)

        # Connect adapters concurrently. A slow or stuck platform must not keep
        # other configured channels, especially Discord, offline.
        connect_results = await asyncio.gather(
            *(
                _connect_adapter(platform, adapter)
                for platform, adapter in list(self.adapters.items())
            )
        )
        for platform, error in connect_results:
            if error:
                print(
                    f"[{datetime.now()}] FATAL: {platform.value} adapter failed to connect: {error}",
                    flush=True,
                )
                del self.adapters[platform]

        if not self.adapters:
            raise RuntimeError("All adapters failed to connect")

        print(f"[{datetime.now()}] All adapters connected")

        # Create a listen task per adapter
        tasks = [asyncio.create_task(self._listen(adapter)) for adapter in self.adapters.values()]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            print(f"[{datetime.now()}] Router shutting down...")

    async def _listen(self, adapter: Any) -> None:
        """Listen for incoming messages from a single adapter.

        Restarts automatically on transient errors with backoff.
        Only gives up after 5 consecutive failures.
        """
        max_retries = 5
        retry_count = 0
        while retry_count < max_retries:
            try:
                async for incoming in adapter.listen():
                    retry_count = 0  # Reset on successful message
                    self._queue_incoming(adapter, incoming)
                # listen() returned without error — generator exhausted (shouldn't happen)
                print(f"[{datetime.now()}] WARNING: {adapter.platform.value} listener exited cleanly — restarting", flush=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                retry_count += 1
                backoff = min(2 ** retry_count, 30)
                print(
                    f"[{datetime.now()}] Listener error ({adapter.platform.value}): {e} "
                    f"— retry {retry_count}/{max_retries} in {backoff}s",
                    flush=True,
                )
                await asyncio.sleep(backoff)
        print(f"[{datetime.now()}] FATAL: {adapter.platform.value} listener gave up after {max_retries} retries", flush=True)

    def _conversation_key(self, incoming: Any) -> str:
        channel = getattr(incoming, "channel", None)
        thread = getattr(incoming, "thread", None)
        user = getattr(incoming, "user", None)
        platform = getattr(incoming, "platform", "")
        platform_value = getattr(platform, "value", str(platform))
        channel_id = getattr(channel, "platform_id", "")
        thread_id = getattr(thread, "thread_id", None) or channel_id
        user_id = getattr(user, "platform_id", "")
        return f"{platform_value}:{channel_id}:{thread_id}:{user_id}"

    @staticmethod
    def _can_coalesce(incoming: Any) -> bool:
        text = (getattr(incoming, "text", "") or "").strip()
        if text.startswith("/") or text.startswith("__button:"):
            return False
        # Phase 3 document ingest (post-build F1): an upload captioned exactly
        # /vault-ingest IS a slash command riding the caption field, so it gets
        # the same bypass as text slash commands and is handled as its own
        # serialized turn. Generic coalescing would breach default-deny in
        # BOTH orderings: merged-first-caption widening consent onto bystander
        # files, or a captionless first message silently dropping the intended
        # ingest.
        if getattr(incoming, "attachments", None) and _VAULT_INGEST_DOC_RE.match(
            (getattr(incoming, "caption", "") or "").strip()
        ):
            return False
        return True

    @staticmethod
    def _is_turn_followup_button(incoming: Any) -> bool:
        text = (getattr(incoming, "text", "") or "").strip()
        return text.startswith("__button:turn_queue:") or text.startswith(
            "__button:turn_steer:"
        )

    @staticmethod
    def _is_immediate_button(incoming: Any) -> bool:
        text = (getattr(incoming, "text", "") or "").strip()
        return ChatRouter._is_turn_followup_button(incoming) or text.startswith(
            "__button:social:"
        ) or text.startswith("__button:linkedin_flow:") or text.startswith(
            "__button:primo_flow:"
        )

    def _retain_task(self, task: "asyncio.Task[Any]") -> None:
        """Keep a strong reference to a fire-and-forget task.

        CPython holds only weak refs to running tasks — an unreferenced task
        can be garbage-collected mid-await and silently never complete.
        """
        self._background_engine_tasks.add(task)
        task.add_done_callback(self._background_engine_tasks.discard)

    def cancel_active_turn(self, key_prefix: str) -> int:
        """Cancel in-flight engine turns whose conversation key starts with prefix.

        Homie Mobile M7 stop control (`POST /api/conversation/{id}/stop`). The
        caller knows platform+channel+thread but not user_id, so this matches by
        prefix over `_conversation_key` entries. Returns turns cancelled.
        """
        count = 0
        for key, task in list(self._active_turns.items()):
            if key.startswith(key_prefix) and not task.done():
                task.cancel()
                count += 1
        return count

    def _queue_incoming(self, adapter: Any, incoming: Any) -> None:
        """Buffer quick conversational bursts, then handle in thread order."""
        if self._is_immediate_button(incoming):
            self._retain_task(asyncio.create_task(self._handle(adapter, incoming)))
            return

        if not self._can_coalesce(incoming):
            self._retain_task(
                asyncio.create_task(self._handle_serialized(adapter, incoming))
            )
            return

        key = self._conversation_key(incoming)
        self._pending_bursts.setdefault(key, []).append(incoming)
        task = self._burst_tasks.get(key)
        if task is None or task.done():
            self._burst_tasks[key] = asyncio.create_task(
                self._flush_burst_after_delay(adapter, key)
            )

    async def _flush_burst_after_delay(self, adapter: Any, key: str) -> None:
        try:
            await asyncio.sleep(self._burst_delay_seconds)
            batch = self._pending_bursts.pop(key, [])
            self._burst_tasks.pop(key, None)
            if not batch:
                return
            incoming = self._merge_incoming_batch(batch) if len(batch) > 1 else batch[0]
            lock = self._thread_locks.get(key)
            if lock and lock.locked():
                await self._offer_turn_followup_choice(adapter, incoming, key)
                return
            await self._handle_serialized(adapter, incoming)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{datetime.now()}] Burst flush failed for {key}: {e}", flush=True)
        finally:
            if self._pending_bursts.get(key) and key not in self._burst_tasks:
                self._burst_tasks[key] = asyncio.create_task(
                    self._flush_burst_after_delay(adapter, key)
                )

    async def _handle_serialized(self, adapter: Any, incoming: Any) -> None:
        key = self._conversation_key(incoming)
        lock = self._thread_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._handle(adapter, incoming)

    @staticmethod
    def _merge_incoming_batch(batch: list[Any]) -> Any:
        first = batch[0]
        parts: list[str] = [
            f"[User sent {len(batch)} messages in quick succession. "
            "Treat them as one turn; later messages may revise or steer earlier ones.]"
        ]
        attachments = []
        message_ids = []
        raw_events = []
        for index, incoming in enumerate(batch, start=1):
            text = (getattr(incoming, "text", "") or "").strip()
            if text:
                parts.append(f"Message {index}:\n{text}")
            incoming_attachments = list(getattr(incoming, "attachments", []) or [])
            if incoming_attachments:
                attachments.extend(incoming_attachments)
            message_id = getattr(incoming, "platform_message_id", None)
            if message_id:
                message_ids.append(str(message_id))
            raw_events.append(getattr(incoming, "raw_event", {}) or {})

        first.text = "\n\n".join(parts)
        first.attachments = attachments
        # Defensive merge invariant (post-build F1): a merged turn must never
        # fabricate upload consent. The caption survives ONLY when every
        # constituent carried the identical non-empty caption; any mix
        # (including empty) merges to "" so a coalesced burst can never
        # satisfy the /vault-ingest caption gate by accident. Telegram album
        # caption propagation (_merge_document_group) is a DIFFERENT layer —
        # one album is one user action — and is intentionally untouched.
        captions = [(getattr(incoming, "caption", "") or "") for incoming in batch]
        if not (captions[0].strip() and all(c == captions[0] for c in captions)):
            first.caption = ""
        if message_ids:
            first.platform_message_id = ",".join(message_ids)
        # A burst containing a voice turn stays voice-origin so the router
        # still skips the placeholder and the adapter voices the final reply.
        first.voice_origin = any(
            bool(getattr(incoming, "voice_origin", False)) for incoming in batch
        )
        first.raw_event = {"coalesced": True, "events": raw_events}
        return first

    async def _offer_turn_followup_choice(
        self,
        adapter: Any,
        incoming: Any,
        key: str,
    ) -> None:
        """Ask the operator how to apply a follow-up sent during an active turn."""
        from models import MessageComponent

        self._turn_choice_counter += 1
        choice_id = f"{int(time.time() * 1000):x}-{self._turn_choice_counter}"
        self._pending_followup_choices[choice_id] = (adapter, incoming, key)
        preview = (getattr(incoming, "text", "") or "").strip().replace("\n", " ")
        if len(preview) > 160:
            preview = preview[:157] + "..."

        await adapter.send(
            OutgoingMessage(
                text=(
                    "I’m still working on the previous turn. How should I apply "
                    f"this follow-up?\n\n`{preview or '[attachment follow-up]'}`"
                ),
                channel=incoming.channel,
                thread=incoming.thread,
                components=[
                    MessageComponent(
                        label="Queue Next",
                        custom_id=f"turn_queue:{choice_id}",
                        style="secondary",
                    ),
                    MessageComponent(
                        label="Steer Current",
                        custom_id=f"turn_steer:{choice_id}",
                        style="primary",
                    ),
                ],
            )
        )

    async def _apply_turn_followup_choice(
        self,
        adapter: Any,
        incoming: Any,
        custom_id: str,
        *,
        mode: str,
    ) -> None:
        choice_id = custom_id.split(":", 1)[1]
        pending = self._pending_followup_choices.pop(choice_id, None)
        if pending is None:
            await adapter.send(
                OutgoingMessage(
                    text="That follow-up choice is no longer active.",
                    channel=incoming.channel,
                    thread=incoming.thread,
                    is_error=True,
                )
            )
            return

        pending_adapter, followup, _key = pending
        if mode == "steer":
            followup.text = (
                "[Steer the in-flight conversation with this follow-up. "
                "If the previous response already shipped, revise it instead "
                "of treating this as an unrelated topic.]\n\n"
                f"{followup.text}"
            )
            reply = (
                "Steer captured. I’ll apply it as a revision right after the "
                "current response finishes."
            )
        else:
            reply = "Queued. I’ll run it as the next turn after the current response."

        self._retain_task(
            asyncio.create_task(self._handle_serialized(pending_adapter, followup))
        )
        await adapter.send(
            OutgoingMessage(
                text=reply,
                channel=incoming.channel,
                thread=incoming.thread,
            )
        )

    def _parse_command(self, text: str) -> tuple[str, str] | None:
        """Return (command, args) if text is a known bot command, else None."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None

        alias_match = _VAULT_COMMAND_ALIAS_RE.match(stripped)
        if alias_match:
            return alias_match.group(1).lower(), alias_match.group(2).strip()

        names = "|".join(
            re.escape(name)
            for name in sorted(self.manager.get_all_command_names(), key=len, reverse=True)
        )
        if not names:
            return None
        m = re.match(rf"^/({names})(?=\s|$)\s*(.*)$", stripped, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).lower(), m.group(2).strip()
        return None

    def _parse_multi_commands(self, text: str) -> list[tuple[str, str]] | None:
        """Parse multiple /commands from a single message (e.g. '/email /gsc /analytics').

        Returns list of (command, args) tuples, or None if <2 commands found.
        """
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None

        command_names = {name.lower() for name in self.manager.get_all_command_names()}
        tokens = stripped.split()
        if len(tokens) < 2:
            return None

        commands: list[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("/") or token.count("/") != 1:
                break
            cmd = token[1:].lower()
            if cmd not in command_names:
                break
            commands.append(cmd)
            index += 1

        if len(commands) < 2:
            return None

        trailing_args = " ".join(tokens[index:]).strip()
        result = [(cmd, "") for cmd in commands[:-1]]
        result.append((commands[-1], trailing_args))
        return result

    async def _handle(self, adapter: Any, incoming: Any) -> None:
        """Handle a single incoming message: post placeholder, run engine, update.

        Wrapped in a top-level try/except so unhandled errors always send an
        error message back to the user instead of silently dying.
        """
        try:
            await self._handle_inner(adapter, incoming)
        except Exception as e:
            # Last resort — never leave the user on read
            print(f"[{datetime.now()}] UNHANDLED error in _handle: {e}")
            try:
                await adapter.send(
                    OutgoingMessage(
                        text=f"Something broke unexpectedly: {type(e).__name__}: {e}",
                        channel=incoming.channel,
                        thread=incoming.thread,
                        is_error=True,
                    )
                )
            except Exception:
                pass  # If even the error message fails, nothing more we can do

    async def _handle_inner(self, adapter: Any, incoming: Any) -> None:
        """Core message handling logic."""
        text = incoming.text or ""
        skip_intent_detection = False
        platform_str = (
            incoming.platform.value
            if isinstance(getattr(incoming, "platform", None), Platform)
            else str(getattr(incoming, "platform", ""))
        )
        channel_id = getattr(getattr(incoming, "channel", None), "platform_id", "")
        thread_value = getattr(getattr(incoming, "thread", None), "thread_id", None)
        thread_id = resolve_thread_id(channel_id, thread_value)
        session_key = build_session_key(platform_str, channel_id, thread_id)

        # --- Button clicks: __button:{custom_id} ---
        if text.startswith("__button:"):
            await self._handle_button(adapter, incoming, text[len("__button:"):])
            return

        # --- Guided /video wizard: a pending wizard consumes typed input
        # stage-gated (pickers are match-only; the input step takes the
        # brief; the vision step takes redo feedback). State set by
        # core_handlers; TTL-bounded; commands always pass through.
        if text and not text.lstrip().startswith("/"):
            import core_handlers as _video_handlers

            if await _video_handlers.try_consume_video_message(adapter, incoming):
                return

            if await _video_handlers.try_consume_linkedin_message(adapter, incoming):
                return

            if await _video_handlers.try_consume_primo_message(adapter, incoming):
                return

        # --- gap-4 URL ingest: /vault-ingest <url> short-circuits to deterministic
        # router-side fetch + archive + compile. Raw-regex match on the message
        # text — does NOT route through _parse_command (vault-ingest is a Skill,
        # not a router command, so parsed[0] would never reach the router_commands
        # registry). Plain text fall-through preserves the existing skill flow
        # for file-path inputs.
        stripped_text = text.strip()
        m = _VAULT_INGEST_URL_RE.match(stripped_text)
        if m:
            url = m.group(1)
            await self._handle_vault_ingest_url(adapter, incoming, url)
            return

        if background_tasks.is_status_probe(text):
            latest_task = background_tasks.latest_for_session(session_key)
            if latest_task:
                reply = background_tasks.render_status_reply(latest_task)
                await adapter.send(
                    OutgoingMessage(
                        text=reply,
                        channel=incoming.channel,
                        thread=incoming.thread,
                    )
                )
                self._persist_router_turn(incoming, reply)
                return

        # --- Phase 3 document ingest: an upload captioned EXACTLY
        # /vault-ingest short-circuits to the deterministic router-side
        # preserve_raw → companion → compile pipeline. Default-deny: the
        # caption must be the bare command (whitespace-tolerant) — prose
        # captions, command text without attachments, and caption-less
        # uploads fall through unchanged. No retroactive state tracking.
        if getattr(incoming, "attachments", None) and _VAULT_INGEST_DOC_RE.match(
            (getattr(incoming, "caption", "") or "").strip()
        ):
            await self._handle_vault_ingest_document(adapter, incoming)
            return

        router_commands = self.manager.get_router_commands()

        if _FRAMEWORK_UPDATE_NOW_RE.match(text) and "update" in router_commands:
            reply = await self.manager.dispatch("update", adapter, incoming, "now")
            if reply is not None:
                await adapter.send(
                    OutgoingMessage(
                        text=reply,
                        channel=incoming.channel,
                        thread=incoming.thread,
                    )
                )
                self._persist_router_turn(incoming, reply)
            return

        # --- Multi-command: /email /gsc /analytics -> chain all ---
        multi = self._parse_multi_commands(text)
        if multi:
            router_cmds = [(cmd, a) for cmd, a in multi if cmd in router_commands]
            if router_cmds:
                replies: list[str] = []
                had_error = False
                for cmd, cmd_args in router_cmds:
                    try:
                        r = await self.manager.dispatch(
                            cmd, adapter, incoming, cmd_args, collect_only=True,
                        )
                        if r:
                            replies.append(f"*/{cmd}*\n{r}")
                    except Exception as e:
                        had_error = True
                        replies.append(f"*/{cmd}*\nError: {e}")
                if replies:
                    combined = "\n\n━━━━━━━━━━━━━━━\n\n".join(replies)
                    await adapter.send(
                        OutgoingMessage(
                            text=combined,
                            channel=incoming.channel,
                            thread=incoming.thread,
                            is_error=had_error,
                        )
                    )
                    if not any(cmd in self._transcript_reset_commands for cmd, _ in router_cmds):
                        self._persist_router_turn(incoming, combined)
                    return

        # --- /file accept|diff <id> — gap-6 conversational compounding ---
        # Intercept here so the engine never sees these subcommands. The
        # base /file slash command remains routed through the engine /
        # extension system; only accept|diff are deterministic Python.
        parsed = self._parse_command(text)
        if parsed and parsed[0] == "file":
            args = parsed[1]
            if args.startswith("accept ") or args.startswith("diff "):
                sub, _, sub_args = args.partition(" ")
                auto_id = sub_args.strip().split()[0] if sub_args else ""
                reply = await self._handle_file_subcommand(sub, auto_id)
                await adapter.send(
                    OutgoingMessage(
                        text=reply,
                        channel=incoming.channel,
                        thread=incoming.thread,
                    )
                )
                self._persist_router_turn(incoming, reply)
                return

        # --- Single command: /email -> handle directly ---
        if parsed:
            command, args = parsed
            if command in VAULT_COMMAND_NAMES:
                user_role = getattr(incoming, "user_role", "admin")
                min_role = get_command_min_role("vault")
                role_level = {"viewer": 0, "operator": 1, "admin": 2}
                if role_level.get(user_role, 0) < role_level.get(min_role, 0):
                    await adapter.send(
                        OutgoingMessage(
                            text=f"Permission denied: /vault requires {min_role} role.",
                            channel=incoming.channel,
                            thread=incoming.thread,
                        )
                    )
                    return
                handled = await self._handle_vault_command(adapter, incoming, command, args)
                if handled:
                    return
                parsed = None
                skip_intent_detection = True

            if not skip_intent_detection and command in router_commands:
                reply = await self.manager.dispatch(command, adapter, incoming, args)
                if reply is not None:
                    await adapter.send(
                        OutgoingMessage(
                            text=reply,
                            channel=incoming.channel,
                            thread=incoming.thread,
                        )
                    )
                    if command not in self._transcript_reset_commands:
                        self._persist_router_turn(incoming, reply)
                return

            if not skip_intent_detection:
                # Role check for engine commands
                user_role = getattr(incoming, "user_role", "admin")
                min_role = get_command_min_role(command)
                role_level = {"viewer": 0, "operator": 1, "admin": 2}
                if role_level.get(user_role, 0) < role_level.get(min_role, 0):
                    await adapter.send(
                        OutgoingMessage(
                            text=f"Permission denied: /{command} requires {min_role} role.",
                            channel=incoming.channel,
                            thread=incoming.thread,
                        )
                    )
                    return

                # Engine command — convert to natural language for the SDK
                piv_content = get_piv_instruction(command, args)
                if piv_content:
                    if isinstance(getattr(incoming, "raw_event", None), dict):
                        incoming.raw_event.setdefault("display_text", text)
                    incoming.text = piv_content
                    incoming.is_piv = True
                    incoming.piv_command = command
                elif command == "clutch":
                    if isinstance(getattr(incoming, "raw_event", None), dict):
                        incoming.raw_event.setdefault("display_text", text)
                    clutch_prompt = (
                        f"Use the Skill tool to invoke the 'clutch' skill with arguments: {args}"
                        if args
                        else "Use the Skill tool to invoke the 'clutch' skill"
                    )
                    incoming.text = clutch_prompt
                elif command == "quote":
                    if isinstance(getattr(incoming, "raw_event", None), dict):
                        incoming.raw_event.setdefault("display_text", text)
                    quote_prompt = (
                        f"Use the Skill tool to invoke the 'turborater-quote' skill with arguments: {args}"
                        if args
                        else "Use the Skill tool to invoke the 'turborater-quote' skill. Ask the user for: full name, vehicle (year make model), zip code, and coverage type (liability or full coverage)."
                    )
                    incoming.text = quote_prompt
                    incoming.is_piv = True
                    incoming.piv_command = "clutch"
                elif command in {"image", "generate-image", "owner-image"}:
                    if not _IMAGEGEN_AVAILABLE:
                        reply = "Image generation isn't included in this build."
                        await adapter.send(
                            OutgoingMessage(
                                text=reply,
                                channel=incoming.channel,
                                thread=incoming.thread,
                            )
                        )
                        self._persist_router_turn(incoming, reply)
                        return
                    if isinstance(getattr(incoming, "raw_event", None), dict):
                        incoming.raw_event.setdefault("display_text", text)
                    if command == "owner-image":
                        incoming.text = build_persona_imagegen_skill_prompt(
                            command,
                            args,
                            "owner-YourBusiness-rep",
                            incoming.attachments,
                        )
                    else:
                        incoming.text = build_imagegen_skill_prompt(
                            command,
                            args,
                            incoming.attachments,
                        )
                    incoming.is_piv = True
                    incoming.piv_command = "imagegen"
                else:
                    if isinstance(getattr(incoming, "raw_event", None), dict):
                        incoming.raw_event.setdefault("display_text", text)
                    desc = get_engine_command_description(command)
                    if args:
                        incoming.text = f"{desc}: {args}"
                    else:
                        incoming.text = desc or command

        # --- Smart intent detection: natural language -> router commands ---
        if not parsed and not skip_intent_detection:
            requires_confirmation = getattr(
                self.manager, "requires_external_action_confirmation", None
            )
            if callable(requires_confirmation) and requires_confirmation(text):
                build_confirmation = getattr(
                    self.manager,
                    "build_external_action_confirmation",
                    None,
                )
                if callable(build_confirmation):
                    reply = build_confirmation(text)
                else:
                    reply = (
                        "That sounds like it may contact a real person or mutate "
                        "a live surface. Reply with a clear direct instruction "
                        "or use the explicit slash command if you want me to proceed."
                    )
                await adapter.send(
                    OutgoingMessage(
                        text=reply,
                        channel=incoming.channel,
                        thread=incoming.thread,
                    )
                )
                self._persist_router_turn(incoming, reply)
                return

            linkedin_profile_action = _linkedin_profile_natural_action(text)
            if linkedin_profile_action and "linkedin_profile" in router_commands:
                user_role = getattr(incoming, "user_role", "admin")
                min_role = get_command_min_role("linkedin_profile")
                role_level = {"viewer": 0, "operator": 1, "admin": 2}
                if role_level.get(user_role, 0) < role_level.get(min_role, 0):
                    await adapter.send(
                        OutgoingMessage(
                            text=(
                                "Permission denied: /linkedin_profile "
                                f"requires {min_role} role."
                            ),
                            channel=incoming.channel,
                            thread=incoming.thread,
                        )
                    )
                    return

                reply = await self.manager.dispatch(
                    "linkedin_profile",
                    adapter,
                    incoming,
                    linkedin_profile_action,
                )
                if reply is not None:
                    await adapter.send(
                        OutgoingMessage(
                            text=reply,
                            channel=incoming.channel,
                            thread=incoming.thread,
                        )
                    )
                    self._persist_router_turn(incoming, reply)
                    return

            intents = self.manager.detect_intents(text)
            if intents:
                intent_parts: list[tuple[str, str]] = []
                for cmd in intents:
                    try:
                        r = await self.manager.dispatch(
                            cmd, adapter, incoming, "", collect_only=True,
                        )
                        if r:
                            intent_parts.append((cmd, f"## /{cmd}\n{r}"))
                    except Exception as e:
                        intent_parts.append((cmd, f"## /{cmd}\nError: {e}"))

                # Drop pure-error results — let them fall through to the engine
                intent_parts = [
                    (cmd, part) for cmd, part in intent_parts
                    if not part.split("\n", 1)[-1].strip().startswith("Error ")
                ]

                if intent_parts:
                    has_prefetch_only = any(cmd in PREFETCH_ONLY_INTENTS for cmd, _ in intent_parts)
                    data_parts = [part for _, part in intent_parts]
                    if self.manager.wants_analysis(text) or has_prefetch_only:
                        incoming.prefetched_context = "\n\n".join(data_parts)
                    else:
                        if len(intent_parts) == 1:
                            reply = data_parts[0].split("\n", 1)[1]
                        else:
                            reply = "\n\n━━━━━━━━━━━━━━━\n\n".join(
                                f"*/{cmd}*\n{p.split(chr(10), 1)[1]}"
                                for cmd, p in intent_parts
                            )
                        await adapter.send(
                            OutgoingMessage(
                                text=reply,
                                channel=incoming.channel,
                                thread=incoming.thread,
                            )
                        )
                        self._persist_router_turn(incoming, reply)
                        return

        discord_persona_binding = (
            resolve_discord_channel_binding(incoming)
            if parsed is None and not skip_intent_detection
            else None
        )

        try:
            print(
                f"[{datetime.now()}] Message from {incoming.user.platform_id} "
                f"in {incoming.channel.platform_id}: {text[:80]}..."
            )
        except UnicodeEncodeError:
            safe_text = text[:80].encode("ascii", "replace").decode()
            print(
                f"[{datetime.now()}] Message from {incoming.user.platform_id} "
                f"in {incoming.channel.platform_id}: {safe_text}..."
            )

        # Post "Thinking..." placeholder.
        # Voice-origin turns skip it: the placeholder send() would consume the
        # adapter's one-shot voice-reply flag and speak "Thinking..." instead
        # of the real answer. With no placeholder, the final delivery below
        # goes through adapter.send(), which carries the voice-reply branch.
        #
        # Progress has independent, redundant lanes: best-effort typing and an
        # editable status message. A transient failure in either one must not
        # disable the other for the remainder of the turn.
        progress_capabilities = resolve_progress_capabilities(adapter)
        progress_allowed = not getattr(incoming, "voice_origin", False)
        status_progress_enabled = bool(
            progress_allowed
            and progress_capabilities.enabled
            and progress_capabilities.editable
        )
        typing_progress_enabled = bool(
            progress_allowed
            and progress_capabilities.enabled
            and progress_capabilities.typing
        )
        progress: dict[str, Any] = {
            "tool_calls": 0,
            "started": time.time(),
            "status": "Homie is reasoning",
        }
        placeholder_id: str | None = None

        async def _refresh_typing() -> None:
            if not typing_progress_enabled:
                return
            send_typing = getattr(adapter, "send_typing", None)
            if not callable(send_typing):
                return
            try:
                await asyncio.wait_for(
                    send_typing(incoming.channel),
                    timeout=PROGRESS_IO_TIMEOUT_SECONDS,
                )
            except Exception:
                # Typing is a redundant UX signal, never a turn-killing path.
                pass

        async def _send_progress_message(text: str) -> str | None:
            try:
                return await asyncio.wait_for(
                    adapter.send(
                        OutgoingMessage(
                            text=text,
                            channel=incoming.channel,
                            thread=incoming.thread,
                        )
                    ),
                    timeout=PROGRESS_IO_TIMEOUT_SECONDS,
                )
            except Exception as e:
                print(f"[{datetime.now()}] Failed to send progress status: {e}")
                return None

        if status_progress_enabled and typing_progress_enabled:
            placeholder_id, _ = await asyncio.gather(
                _send_progress_message("Thinking..."), _refresh_typing()
            )
        elif status_progress_enabled:
            placeholder_id = await _send_progress_message("Thinking...")
        elif typing_progress_enabled:
            await _refresh_typing()

        # Homie Mobile M7 — cockpit adapters (dashboard SSE) expose
        # emit_turn_event; bind it with this turn's target so the engine's
        # live tool telemetry reaches the stream. Other adapters: no-op.
        _turn_emitter = getattr(adapter, "emit_turn_event", None)
        if callable(_turn_emitter):
            _turn_channel, _turn_thread = incoming.channel, incoming.thread

            def _emit_turn_event(ev: dict[str, Any]) -> None:
                try:
                    _turn_emitter(ev, channel=_turn_channel, thread=_turn_thread)
                except Exception:
                    pass

            progress["emit_turn_event"] = _emit_turn_event

        async def _tick_progress() -> None:
            """Refresh typing and maintain one recoverable progress bubble."""
            nonlocal placeholder_id
            quick_recovery_pending = bool(
                status_progress_enabled
                and progress_capabilities.recover_failed_status
                and placeholder_id is None
            )
            while True:
                delay = (
                    PROGRESS_RECOVERY_RETRY_SECONDS
                    if quick_recovery_pending
                    else PROGRESS_UPDATE_SECONDS
                )
                await asyncio.sleep(delay)
                quick_recovery_pending = False
                await _refresh_typing()
                if not status_progress_enabled:
                    continue
                status = _render_progress_status(progress)
                if placeholder_id is None:
                    if progress_capabilities.recover_failed_status:
                        placeholder_id = await _send_progress_message(status)
                    continue
                try:
                    updated_id = await asyncio.wait_for(
                        adapter.update(
                            OutgoingMessage(
                                text=status,
                                channel=incoming.channel,
                                thread=incoming.thread,
                                is_update=True,
                                update_message_id=placeholder_id,
                            )
                        ),
                        timeout=PROGRESS_IO_TIMEOUT_SECONDS,
                    )
                except Exception:
                    updated_id = None
                if updated_id:
                    placeholder_id = updated_id
                    continue
                if progress_capabilities.recover_failed_status:
                    placeholder_id = None
                    quick_recovery_pending = True

        progress_task = (
            asyncio.create_task(_tick_progress())
            if typing_progress_enabled
            or (
                status_progress_enabled
                and (
                    placeholder_id is not None
                    or progress_capabilities.recover_failed_status
                )
            )
            else None
        )

        final_text = ""
        final_is_error = False
        final_footer: str | None = None
        final_components: list[Any] = []
        followup_messages: list[OutgoingMessage] = []
        engine_result_started = False
        foreground_text: str | None = None
        foreground_is_error = False
        background_task_id: str | None = None

        async def _run_engine() -> None:
            nonlocal final_text, final_is_error, final_footer, final_components
            nonlocal engine_result_started
            if discord_persona_binding is not None:
                outgoing = await run_discord_persona_channel_turn(
                    incoming=incoming,
                    binding=discord_persona_binding,
                    session_store=getattr(self.engine, "session_store", None),
                    project_root=getattr(self.engine, "project_root", Path.cwd()),
                    progress=progress,
                )
                final_text = outgoing.text
                engine_result_started = True
                final_is_error = getattr(outgoing, "is_error", False)
                final_footer = getattr(outgoing, "footer", None)
                yielded_components = getattr(outgoing, "components", None) or []
                if yielded_components:
                    final_components = list(yielded_components)
                return

            async for outgoing in self.engine.handle_message(incoming, progress=progress):
                if engine_result_started:
                    followup_messages.append(outgoing)
                    continue
                final_text = outgoing.text
                engine_result_started = True
                final_is_error = getattr(outgoing, "is_error", False)
                # gap-6: capture engine-side footer + components (concept draft).
                # Persistence (_persist_router_turn) keeps using final_text only —
                # footer never enters chat_history.
                final_footer = getattr(outgoing, "footer", None)
                yielded_components = getattr(outgoing, "components", None) or []
                if yielded_components:
                    final_components = list(yielded_components)

        async def _deliver_background_engine_result(task: asyncio.Task[Any]) -> None:
            nonlocal final_text, final_is_error, final_footer, final_components
            try:
                await task
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[{datetime.now()}] Background engine error: {e}", flush=True)
                final_text = f"Background task failed after timeout: {e}"
                final_is_error = True
                final_footer = None
                final_components = []

            if not final_text.strip():
                final_text = "Background task finished, but it had no text response."

            text = final_text
            if not final_is_error:
                text = f"Background task finished:\n\n{final_text}"

            components = self._extract_result_buttons(final_text)
            if final_components:
                components = list(components) + list(final_components)

            try:
                background_message_id = await adapter.send(
                    OutgoingMessage(
                        text=text,
                        channel=incoming.channel,
                        thread=incoming.thread,
                        is_error=final_is_error,
                        components=components,
                        footer=final_footer,
                    )
                )
                background_tasks.update_task(
                    background_task_id,
                    status="failed" if final_is_error else "completed",
                    final_message_id=background_message_id,
                    final_text=final_text,
                    error=final_text if final_is_error else None,
                )
                for followup in followup_messages:
                    await adapter.send(followup)
            except Exception as e:
                background_tasks.update_task(
                    background_task_id,
                    status="delivery_failed",
                    final_text=text,
                    error=str(e),
                )
                print(
                    f"[{datetime.now()}] Failed to deliver background engine result: {e}",
                    flush=True,
                )

        def _track_background_engine_result(task: asyncio.Task[Any]) -> None:
            self._background_engine_tasks.add(task)

            def _on_engine_done(done_task: asyncio.Task[Any]) -> None:
                self._background_engine_tasks.discard(done_task)
                delivery_task = asyncio.create_task(
                    _deliver_background_engine_result(done_task)
                )
                self._background_engine_tasks.add(delivery_task)
                delivery_task.add_done_callback(self._background_engine_tasks.discard)

            task.add_done_callback(_on_engine_done)

        timeout_seconds = _engine_timeout_seconds(
            bool(getattr(incoming, "attachments", None))
        )

        engine_task = asyncio.create_task(_run_engine())
        _turn_key = self._conversation_key(incoming)
        self._active_turns[_turn_key] = engine_task
        try:
            await asyncio.wait_for(asyncio.shield(engine_task), timeout=timeout_seconds)
        except asyncio.CancelledError:
            # Homie Mobile M7 — operator stop: cancel_active_turn() killed the
            # engine task. Its persistence died with it (history correctly shows
            # no reply); deliver a stop marker instead. If the engine task is
            # still alive, WE were cancelled from outside: cancel the shielded
            # engine task before propagating so shutdown cannot orphan work.
            if not engine_task.cancelled():
                engine_task.cancel()
                try:
                    await engine_task
                except asyncio.CancelledError:
                    pass
                raise
            final_text = "⏹️ Stopped."
            final_is_error = False
            emit = progress.get("emit_turn_event")
            if callable(emit):
                emit({"type": "turn_aborted"})
        except asyncio.TimeoutError:
            formatted_timeout = _format_seconds(timeout_seconds)
            print(f"[{datetime.now()}] Engine timed out after {formatted_timeout}s")
            foreground_text = _engine_timeout_message(
                timeout_seconds, getattr(incoming, "attachments", None)
            )
            foreground_is_error = True
            background_task_id = background_tasks.start_task(
                session_key=session_key,
                platform=platform_str,
                channel_id=channel_id,
                thread_id=thread_id,
                message_id=getattr(incoming, "platform_message_id", None),
                user_request=text,
            )
            _track_background_engine_result(engine_task)
        except Exception as e:
            print(f"[{datetime.now()}] Engine error: {e}")
            final_text = f"Sorry, something went wrong: {e}"
            final_is_error = True
        finally:
            if self._active_turns.get(_turn_key) is engine_task:
                self._active_turns.pop(_turn_key, None)
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass

        # Update the placeholder with the final response
        delivered_text = foreground_text if foreground_text is not None else final_text
        delivered_is_error = foreground_is_error if foreground_text is not None else final_is_error
        if not delivered_text.strip():
            delivered_text = "I processed your request but had no text response."

        # Parse <<BLOG_RESULTS>> marker — attach Publish/Skip buttons
        components = self._extract_result_buttons(delivered_text)
        # If the engine attached components (e.g. concept draft Accept/Diff),
        # carry them into the outgoing message alongside any blog buttons.
        if final_components and foreground_text is None:
            components = list(components) + list(final_components)

        final_delivery_ok = False
        final_delivery_id: str | None = None
        try:
            if placeholder_id:
                try:
                    final_delivery_id = await asyncio.wait_for(
                        adapter.update(
                            OutgoingMessage(
                                text=delivered_text,
                                channel=incoming.channel,
                                thread=incoming.thread,
                                is_update=True,
                                update_message_id=placeholder_id,
                                is_error=delivered_is_error,
                                footer=(
                                    final_footer if foreground_text is None else None
                                ),
                            )
                        ),
                        timeout=PROGRESS_IO_TIMEOUT_SECONDS,
                    )
                except Exception as edit_exc:
                    print(
                        f"[{datetime.now()}] Failed to edit final progress status: "
                        f"{edit_exc}",
                        flush=True,
                    )
                    final_delivery_id = None
                if (
                    final_delivery_id is None
                    and progress_capabilities.recover_failed_status
                ):
                    # A recovery-capable adapter promises message-ID truth.
                    # One fresh send prevents a swallowed edit failure from
                    # becoming a silently missing answer.
                    final_delivery_id = await adapter.send(
                        OutgoingMessage(
                            text=delivered_text,
                            channel=incoming.channel,
                            thread=incoming.thread,
                            is_error=delivered_is_error,
                            footer=final_footer if foreground_text is None else None,
                        )
                    )
                    if final_delivery_id is None:
                        raise RuntimeError(
                            "Final fallback send returned no delivery receipt"
                        )
                elif final_delivery_id is None:
                    raise RuntimeError(
                        "Final progress edit returned no delivery receipt"
                    )
            else:
                final_delivery_id = await adapter.send(
                    OutgoingMessage(
                        text=delivered_text,
                        channel=incoming.channel,
                        thread=incoming.thread,
                        is_error=delivered_is_error,
                        components=components,
                        footer=final_footer if foreground_text is None else None,
                    )
                )
                if status_progress_enabled and final_delivery_id is None:
                    raise RuntimeError("Final send returned no delivery receipt")
            final_delivery_ok = True
            print(
                f"[{datetime.now()}] Final response delivered "
                f"platform={incoming.platform.value} "
                f"message_id={final_delivery_id or 'unknown'} "
                f"followups={len(followup_messages)}",
                flush=True,
            )
        except Exception as e:
            print(f"[{datetime.now()}] Failed to deliver final response: {e}", flush=True)
            try:
                await adapter.send(
                    OutgoingMessage(
                        text=(
                            "I generated a response, but delivery failed before it "
                            "could be shown. I suppressed follow-up nudges for this turn."
                        ),
                        channel=incoming.channel,
                        thread=incoming.thread,
                        is_error=True,
                    )
                )
            except Exception as diag_exc:
                print(
                    f"[{datetime.now()}] Failed to send delivery diagnostic: {diag_exc}",
                    flush=True,
                )

        if final_delivery_ok and foreground_text is None:
            try:
                # Buttons can't be added to edits — send as follow-up
                if components:
                    await adapter.send(
                        OutgoingMessage(
                            text="Ready to publish?" if not final_components else "",
                            channel=incoming.channel,
                            thread=incoming.thread,
                            components=components,
                            footer=final_footer if final_components else None,
                        )
                    )
                for followup in followup_messages:
                    await adapter.send(followup)
            except Exception as e:
                print(f"[{datetime.now()}] Failed to send follow-up response: {e}", flush=True)

        if delivered_is_error:
            self._persist_router_turn(incoming, delivered_text)

    async def _handle_vault_command(
        self,
        adapter: Any,
        incoming: Any,
        command: str,
        args: str,
    ) -> bool:
        """Handle the shared /vault command family.

        Returns True when the router fully handled the turn. Returns False for
        /vault ops, after rewriting the incoming message into a vault-ops skill
        prompt so the normal engine path can run it.
        """

        original_text = f"/{command} {args}".strip()
        if isinstance(getattr(incoming, "raw_event", None), dict):
            incoming.raw_event.setdefault("display_text", original_text)

        parsed = self._parse_vault_args(command, args)
        if isinstance(parsed, str):
            await self._send_vault_reply(adapter, incoming, parsed)
            return True

        subcommand, positionals, options = parsed
        vault_name = options["vault"]

        if subcommand in {"help", ""}:
            await self._send_vault_reply(adapter, incoming, self._vault_help_text())
            return True

        if subcommand == "status":
            reply = self._vault_status_reply(vault_name)
            await self._send_vault_reply(adapter, incoming, reply)
            return True

        if subcommand == "db":
            reply = self._vault_db_reply(vault_name)
            await self._send_vault_reply(adapter, incoming, reply)
            return True

        if subcommand in {"search", "context", "contacts"}:
            query = " ".join(positionals).strip()
            if subcommand == "search" and not query:
                await self._send_vault_reply(
                    adapter,
                    incoming,
                    "Usage: `/vault search <query> [--vault thehomie|coding-vault] [--mode auto|hybrid|keyword] [--limit N]`",
                    is_error=True,
                )
                return True
            if subcommand == "context" and not query:
                await self._send_vault_reply(
                    adapter,
                    incoming,
                    "Usage: `/vault context <topic> [--vault thehomie|coding-vault]`",
                    is_error=True,
                )
                return True
            if subcommand == "contacts":
                query = (
                    f"contacts people clients prospects owners phone email {query}".strip()
                    if query
                    else "contacts people clients prospects owners phone email"
                )
            elif subcommand == "context":
                query = f"context briefing decisions open items source notes {query}"
            reply = await self._vault_recall_reply(
                subcommand=subcommand,
                vault_name=vault_name,
                query=query,
                mode=options["mode"],
                limit=options["limit"],
            )
            await self._send_vault_reply(adapter, incoming, reply)
            return True

        if subcommand == "ingest":
            url = options.get("url") or next(
                (token for token in positionals if token.startswith(("http://", "https://"))),
                "",
            )
            memory_dir, _db_path, error = self._resolve_vault(vault_name)
            if error:
                await self._send_vault_reply(adapter, incoming, error, is_error=True)
                return True
            if url:
                await self._handle_vault_ingest_url(
                    adapter,
                    incoming,
                    url,
                    vault_name=vault_name,
                    memory_dir=memory_dir,
                )
                return True
            if getattr(incoming, "attachments", None):
                await self._handle_vault_ingest_document(
                    adapter,
                    incoming,
                    vault_name=vault_name,
                    memory_dir=memory_dir,
                )
                return True
            await self._send_vault_reply(
                adapter,
                incoming,
                "Usage: `/vault ingest <url> [--vault name]` or use Discord `/vault ingest` with an attachment.",
                is_error=True,
            )
            return True

        if subcommand == "ops":
            if not positionals:
                await self._send_vault_reply(
                    adapter,
                    incoming,
                    "Usage: `/vault ops <orient|debrief|weekly|capture|ingest|compile|research|maintain|context|status|think> [args] [--vault name]`",
                    is_error=True,
                )
                return True
            routine = positionals[0].lower()
            routine_args = " ".join(positionals[1:]).strip()
            if routine not in VAULT_OPS_ROUTINES:
                await self._send_vault_reply(
                    adapter,
                    incoming,
                    f"Unknown vault-ops routine `{routine}`. Supported: {', '.join(sorted(VAULT_OPS_ROUTINES))}.",
                    is_error=True,
                )
                return True
            incoming.text = self._build_vault_ops_prompt(vault_name, routine, routine_args)
            incoming.is_piv = True
            incoming.piv_command = "vault-ops"
            return False

        await self._send_vault_reply(
            adapter,
            incoming,
            f"Unknown /vault subcommand `{subcommand}`.\n\n{self._vault_help_text()}",
            is_error=True,
        )
        return True

    async def _send_vault_reply(
        self,
        adapter: Any,
        incoming: Any,
        text: str,
        *,
        is_error: bool = False,
    ) -> None:
        await adapter.send(
            OutgoingMessage(
                text=text,
                channel=incoming.channel,
                thread=incoming.thread,
                is_error=is_error,
            )
        )
        self._persist_router_turn(incoming, text)

    def _parse_vault_args(
        self,
        command: str,
        args: str,
    ) -> tuple[str, list[str], dict[str, Any]] | str:
        if command == "vault-ops":
            args = f"ops {args}".strip()

        if not args.strip():
            return "help", [], {"vault": "thehomie", "mode": "hybrid", "limit": 5, "url": ""}

        try:
            tokens = shlex.split(args)
        except ValueError as e:
            return f"Could not parse /vault arguments: {e}"

        if not tokens:
            return "help", [], {"vault": "thehomie", "mode": "hybrid", "limit": 5, "url": ""}

        subcommand = tokens[0].lower()
        rest = tokens[1:]
        options: dict[str, Any] = {
            "vault": "thehomie",
            "mode": "hybrid",
            "limit": 5,
            "url": "",
        }
        positionals: list[str] = []
        index = 0
        while index < len(rest):
            token = rest[index]
            if token in {"--vault", "--mode", "--limit", "-n", "--url"}:
                if index + 1 >= len(rest):
                    return f"Missing value for `{token}`."
                value = rest[index + 1]
                if token == "--vault":
                    options["vault"] = value
                elif token == "--mode":
                    options["mode"] = value.lower()
                elif token in {"--limit", "-n"}:
                    options["limit"] = value
                elif token == "--url":
                    options["url"] = value
                index += 2
                continue
            if token.startswith("--vault="):
                options["vault"] = token.split("=", 1)[1]
            elif token.startswith("--mode="):
                options["mode"] = token.split("=", 1)[1].lower()
            elif token.startswith("--limit="):
                options["limit"] = token.split("=", 1)[1]
            elif token.startswith("--url="):
                options["url"] = token.split("=", 1)[1]
            else:
                positionals.append(token)
            index += 1

        if options["vault"] == "thehomie" and positionals:
            if subcommand in {"status", "db"}:
                maybe_vault = self._normalize_vault_name(positionals[0])
                if maybe_vault:
                    options["vault"] = maybe_vault
                    positionals = positionals[1:]
            elif subcommand in {"search", "context", "contacts", "ingest", "ops"}:
                maybe_vault = self._normalize_vault_name(positionals[-1])
                if maybe_vault and len(positionals) > 1:
                    options["vault"] = maybe_vault
                    positionals = positionals[:-1]

        normalized = self._normalize_vault_name(options["vault"])
        if not normalized:
            return (
                f"Unknown vault `{options['vault']}`. "
                "Use `thehomie` or `coding-vault`."
            )
        options["vault"] = normalized

        if options["mode"] not in VAULT_RECALL_MODES:
            return "Unknown recall mode. Use `auto`, `hybrid`, or `keyword`."
        try:
            options["limit"] = max(1, min(10, int(options["limit"])))
        except (TypeError, ValueError):
            return "Limit must be a number from 1 to 10."

        return subcommand, positionals, options

    @staticmethod
    def _normalize_vault_name(raw: str) -> str | None:
        key = str(raw or "").strip().lower().replace("_", "-")
        return VAULT_NAME_ALIASES.get(key)

    def _resolve_vault(self, vault_name: str) -> tuple[Path | None, Path, str | None]:
        from config import VAULT_NAMES, resolve_vault

        if vault_name not in VAULT_NAMES:
            return None, Path(""), f"Unknown vault `{vault_name}`."
        memory_dir, db_path = resolve_vault(vault_name)
        if memory_dir is None:
            return (
                None,
                db_path,
                f"Vault `{vault_name}` is not configured on this machine.",
            )
        return Path(memory_dir), db_path, None

    def _vault_status_reply(self, vault_name: str) -> str:
        memory_dir, db_path, error = self._resolve_vault(vault_name)
        configured = memory_dir is not None and error is None
        vault_exists = bool(memory_dir and memory_dir.exists())
        db_exists = db_path.exists()
        db_size = db_path.stat().st_size if db_exists else 0
        return (
            "*Vault Status*\n"
            f"  Vault: `{vault_name}`\n"
            f"  Configured: {'yes' if configured else 'no'}\n"
            f"  Memory dir exists: {'yes' if vault_exists else 'no'}\n"
            f"  Recall DB exists: {'yes' if db_exists else 'no'}\n"
            f"  Recall DB size: {db_size} bytes"
        )

    def _vault_db_reply(self, vault_name: str) -> str:
        memory_dir, db_path, error = self._resolve_vault(vault_name)
        memory_display = str(memory_dir) if memory_dir else "not configured"
        return (
            "*Vault DB*\n"
            f"  Vault: `{vault_name}`\n"
            f"  Memory dir: `{memory_display}`\n"
            f"  DB path: `{db_path}`\n"
            f"  DB exists: {'yes' if db_path.exists() else 'no'}"
            + (f"\n  Note: {error}" if error else "")
        )

    async def _vault_recall_reply(
        self,
        *,
        subcommand: str,
        vault_name: str,
        query: str,
        mode: str,
        limit: int,
    ) -> str:
        memory_dir, _db_path, error = self._resolve_vault(vault_name)
        if error:
            return error
        try:
            from recall_service import SearchMode, recall as recall_memory

            mode_map = {
                "auto": SearchMode.AUTO,
                "hybrid": SearchMode.HYBRID,
                "keyword": SearchMode.KEYWORD,
            }
            response = await recall_memory(
                query=query,
                memory_dir=memory_dir,
                search_mode=mode_map[mode],
                caller=f"vault-command:{subcommand}",
                max_results=limit,
                is_slash_command=False,
            )
        except Exception as e:
            return f"Vault recall failed for `{vault_name}`: {type(e).__name__}: {e}"

        results = list(getattr(response, "results", []) or [])
        title = {
            "search": "Vault Search",
            "context": "Vault Context",
            "contacts": "Vault Contacts",
        }.get(subcommand, "Vault Recall")
        if not results:
            return f"*{title}*\n  Vault: `{vault_name}`\n  Query: `{query}`\n\nNo matches."

        lines = [
            f"*{title}*",
            f"  Vault: `{vault_name}`",
            f"  Mode: `{mode}`",
            f"  Query: `{query}`",
            "",
        ]
        for result in results[:limit]:
            path = getattr(result, "path", "")
            start = getattr(result, "start_line", 0)
            end = getattr(result, "end_line", 0)
            section = getattr(result, "section_title", "") or "match"
            text = " ".join(str(getattr(result, "text", "") or "").split())
            if len(text) > 260:
                text = text[:257].rstrip() + "..."
            loc = f"{path}:{start}-{end}" if start and end else path
            lines.append(f"- `{loc}` — {section}\n  {text}")
        return "\n".join(lines)

    def _build_vault_ops_prompt(self, vault_name: str, routine: str, args: str) -> str:
        effort = VAULT_OPS_ROUTINES.get(routine, "Medium")
        arg_text = args or "(no additional arguments)"
        return (
            "Use the Skill tool to invoke the 'vault-ops' skill.\n"
            f"Command: {routine}\n"
            f"Arguments: {arg_text}\n"
            f"Selected vault: {vault_name}\n"
            f"Approximate effort: {effort}\n\n"
            "Follow the vault-ops routing rules. Prefer the real recall stack "
            "when retrieving context, preserve provenance, and distinguish "
            "indexed recall results from raw markdown reads. If the routine "
            "writes to a vault, only proceed because this explicit /vault ops "
            "command is the user's authorization for that vault routine."
        )

    @staticmethod
    def _vault_help_text() -> str:
        return (
            "*Vault Commands*\n"
            "`/vault status [vault]`\n"
            "`/vault db [vault]`\n"
            "`/vault search <query> [--vault name] [--mode auto|hybrid|keyword] [--limit N]`\n"
            "`/vault context <topic> [--vault name]`\n"
            "`/vault contacts [query] [--vault name]`\n"
            "`/vault ingest <url> [--vault name]`\n"
            "`/vault ops <routine> [args] [--vault name]`\n\n"
            "Vaults: `thehomie`, `coding-vault`."
        )

    async def _handle_file_subcommand(self, sub: str, auto_id: str) -> str:
        """Dispatch /file accept|diff <id> to the concept_drafter module.

        gap-6 — deterministic Python path; no engine round-trip.
        Returns a user-facing reply string. Never raises — surfaces errors
        as text replies so the user sees what went wrong.
        """
        if not auto_id:
            return f"Usage: `/file {sub} <draft-id>` (8-char prefix or full UUID)."
        try:
            from concept_drafter import (
                DraftAmbiguityError,
                accept_draft,
                diff_draft,
            )
            from config import MEMORY_DIR
        except Exception as e:  # noqa: BLE001
            return f"Drafter unavailable: {e}"

        try:
            if sub == "accept":
                result = accept_draft(auto_id, MEMORY_DIR)
            elif sub == "diff":
                result = diff_draft(auto_id, MEMORY_DIR)
            else:
                return f"Unknown subcommand `/file {sub}`."
        except DraftAmbiguityError as e:
            slugs = []
            for p in e.candidates:
                stem = getattr(p, "stem", str(p))
                slugs.append(f"`{stem}`")
            joined = ", ".join(slugs[:5])
            return (
                f"Multiple drafts match `{auto_id}`. Be more specific. "
                f"Candidates: {joined}"
            )
        except Exception as e:  # noqa: BLE001
            return f"Couldn't {sub} draft `{auto_id}`: {e}"

        status = result.get("status", "")
        if status == "not_found":
            return f"No draft matched `{auto_id}`. It may have been swept."
        if status == "error":
            return f"Failed to {sub} draft `{auto_id}`: {result.get('error', 'unknown error')}"
        if sub == "accept":
            path = result.get("path", "")
            connections = result.get("connections", []) or []
            contradictions = result.get("contradictions", []) or []
            lines = [f"Filed draft to `{path}`."]
            if connections:
                lines.append(f"Connections: {len(connections)}")
            if contradictions:
                lines.append(f"Contradictions flagged: {len(contradictions)}")
            return "\n".join(lines)
        # diff
        preview = result.get("preview", "")
        return f"Draft preview (`{auto_id}`):\n\n{preview}"

    async def _handle_vault_ingest_url(
        self,
        adapter: Any,
        incoming: Any,
        url: str,
        *,
        vault_name: str = "thehomie",
        memory_dir: Path | None = None,
    ) -> None:
        """Router-side URL ingest (gap-4).

        Fetch + archive html+md to ``{vault}/raw/clipped/`` then run the entity
        compilation cascade. Reply with concept/connection/contradiction counts.
        Never reaches the engine — fully deterministic.
        """
        show_vault = memory_dir is not None or vault_name != "thehomie"
        try:
            await adapter.send(
                OutgoingMessage(
                    text=(
                        f"Fetching {url} into `{vault_name}`..."
                        if show_vault
                        else f"Fetching {url}..."
                    ),
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        except Exception:
            # Placeholder send is best-effort; if it fails we still try to fetch.
            pass

        try:
            html_path, md_path, content, report = await asyncio.to_thread(
                self._url_ingest_pipeline, url, memory_dir
            )
        except Exception as e:
            await adapter.send(
                OutgoingMessage(
                    text=(
                        f"Couldn't fetch {url}: {type(e).__name__}: {e}. "
                        "Try saving the page as a file and ingesting that instead."
                    ),
                    channel=incoming.channel,
                    thread=incoming.thread,
                    is_error=True,
                )
            )
            return

        title = content.title or md_path.stem
        n_concepts = len(report.pages_created) + len(report.pages_updated)
        n_connections = len(report.connections_created)
        n_contradictions = len(report.contradictions_found)
        reply = (
            f"Ingested '{title}'. "
            f"{n_concepts} concepts, {n_connections} connections, "
            f"{n_contradictions} contradictions. "
            + (
                f"Vault: `{vault_name}`. Raw: `{html_path.name}`, `{md_path.name}`."
                if show_vault
                else f"Raw: `{html_path.name}`, `{md_path.name}`."
            )
        )
        await adapter.send(
            OutgoingMessage(
                text=reply,
                channel=incoming.channel,
                thread=incoming.thread,
            )
        )
        self._persist_router_turn(incoming, reply)

    @staticmethod
    def _url_ingest_pipeline(url: str, memory_dir: Path | None = None):
        """Synchronous fetch + archive + compile pipeline.

        Runs off the event loop (called via ``asyncio.to_thread`` from the
        async handler). Returns ``(html_path, md_path, content, report)``.
        """
        from url_fetch import fetch_and_archive
        from entity_extractor import compile_entities, extract_entities_heuristic
        from config import MEMORY_DIR

        vault_dir = Path(memory_dir) if memory_dir is not None else MEMORY_DIR
        html_path, md_path, content = fetch_and_archive(url, vault_dir)
        md_text = md_path.read_text(encoding="utf-8")
        ents = extract_entities_heuristic(md_text, str(md_path))
        report = compile_entities(ents, str(md_path), vault_dir, vault_dir)
        return html_path, md_path, content, report

    async def _handle_vault_ingest_document(
        self,
        adapter: Any,
        incoming: Any,
        *,
        vault_name: str = "thehomie",
        memory_dir: Path | None = None,
    ) -> None:
        """Router-side document ingest (Phase 3, doc-upload-truthful-reads).

        An upload captioned exactly ``/vault-ingest`` runs the deterministic
        preserve_raw → companion → extract → compile pipeline per supported
        attachment, with a counted confirmation per file. Unsupported files
        get an explicit per-file refusal — no silent skips. Never reaches the
        engine — the engine timeout does not apply. The persisted turn is the
        audit row (default-deny mutation policy).
        """
        from attachment_context import is_supported_document_attachment

        attachments = list(getattr(incoming, "attachments", []) or [])
        flags = [
            is_supported_document_attachment(att.filename or "", att.mimetype)
            for att in attachments
        ]
        show_vault = memory_dir is not None or vault_name != "thehomie"

        if any(flags):
            names = ", ".join(
                _display_filename(att.filename)
                for att, ok in zip(attachments, flags)
                if ok
            )
            try:
                await adapter.send(
                    OutgoingMessage(
                        text=(
                            f"Ingesting {names} into `{vault_name}`..."
                            if show_vault
                            else f"Ingesting {names}..."
                        ),
                        channel=incoming.channel,
                        thread=incoming.thread,
                    )
                )
            except Exception:
                # Placeholder send is best-effort; ingest proceeds regardless.
                pass

        lines: list[str] = []
        had_error = False

        for att, ok in zip(attachments, flags):
            filename = att.filename or "attachment"
            # Display vs storage separation (post-build F3): the pipeline
            # gets the RAW filename (preserve_raw sanitizes centrally for
            # storage); reply text only ever carries the display-safe form.
            display_name = _display_filename(att.filename)
            if not ok:
                # Explicit per-file refusal — no silent skips.
                lines.append(
                    f"Cannot ingest '{display_name}': unsupported document type "
                    "for /vault-ingest. Supported: .txt, .md, .csv, .tsv, "
                    ".pdf, .docx."
                )
                had_error = True
                continue
            try:
                pipeline_args: tuple[Any, ...] = (att.url, filename, att.mimetype)
                if memory_dir is not None:
                    pipeline_args = (*pipeline_args, memory_dir)
                raw_path, report = await asyncio.to_thread(
                    self._document_ingest_pipeline,
                    *pipeline_args,
                )
            except _DocumentCompileError as e:
                # Partial-state honesty: the raw archive landed; only the
                # compile stage failed. Never claim total failure here.
                lines.append(
                    f"Raw file archived as '{_display_filename(e.raw_name)}', "
                    f"but concept compilation FAILED "
                    f"({type(e.original).__name__}). "
                    "No concept pages were created or updated for this file."
                )
                had_error = True
            except Exception as e:
                # Failure at/before preserve_raw (incl. missing/unreadable
                # Attachment.url): nothing reached the vault for this file.
                lines.append(
                    f"Ingest of '{display_name}' FAILED ({type(e).__name__}). "
                    "Nothing was saved to the vault for this file. "
                    "Re-send it with the /vault-ingest caption to retry."
                )
                had_error = True
            else:
                n_concepts = len(report.pages_created) + len(report.pages_updated)
                n_connections = len(report.connections_created)
                n_contradictions = len(report.contradictions_found)
                lines.append(
                    f"Ingested '{display_name}'. "
                    f"{n_concepts} concepts, {n_connections} connections, "
                    f"{n_contradictions} contradictions. "
                    + (
                        f"Vault: `{vault_name}`. Raw: {_display_filename(raw_path.name)}."
                        if show_vault
                        else f"Raw: {_display_filename(raw_path.name)}."
                    )
                )

        reply = "\n".join(lines)
        await adapter.send(
            OutgoingMessage(
                text=reply,
                channel=incoming.channel,
                thread=incoming.thread,
                is_error=had_error,
            )
        )
        self._persist_router_turn(incoming, reply)

    @staticmethod
    def _document_ingest_pipeline(
        file_path: str,
        filename: str,
        mimetype: str | None,
        memory_dir: Path | None = None,
    ) -> tuple[Any, Any]:
        """Synchronous preserve_raw → companion → extract → compile pipeline.

        Orchestration ONLY — ingest logic stays in entity_extractor /
        attachment_context. Runs off the event loop (called via
        ``asyncio.to_thread``). Returns ``(raw_path, report)``.

        Compile-surface contract: the raw archive is NEVER passed to
        ``compile_entities()``. Every uploaded format compiles against a
        generated ``{raw_stem}.ingest.md`` companion carrying valid Homie
        frontmatter — ``compile_entities()`` enforces frontmatter on ``.md``
        sources AND mutates its source via ``update_source_frontmatter()``;
        the companion is the designated mutation surface, the raw archive
        stays byte-identical.
        """
        import os
        from datetime import date
        from pathlib import Path

        from attachment_context import extract_document_text
        from config import MEMORY_DIR
        from entity_extractor import (
            compile_entities,
            extract_entities_heuristic,
            preserve_raw,
        )

        vault_dir = Path(memory_dir) if memory_dir is not None else MEMORY_DIR
        # preserve_raw sanitizes dest_name centrally and already falls back to
        # a date-prefixed destination on collision — no caller-side retry. A
        # FileExistsError that still escapes means nothing new was saved.
        raw_path = preserve_raw(
            Path(file_path), vault_dir, subdir="uploads", dest_name=filename
        )
        try:
            # FULL text — the inline attachment-context caps DO NOT apply.
            text = extract_document_text(raw_path, raw_path.name, mimetype)
            # Companion: ALWAYS generated, every format (.txt/.md/pdf/docx/
            # csv) — ONE naming rule, no per-format branching. An uploaded
            # .md's own frontmatter survives verbatim as BODY text.
            # Written atomically (tmp + os.replace — the living_memory.py
            # pattern) so disk-full/encoding failures cannot leave a partial
            # .ingest.md beside the raw archive.
            compile_path = raw_path.with_name(raw_path.stem + ".ingest.md")
            companion_text = (
                "---\n"
                "tags: [upload, auto-ingested]\n"
                f"date: {date.today().isoformat()}\n"
                f"source: {raw_path.name}\n"
                "related: []\n"
                "---\n\n" + text
            )
            tmp_companion = compile_path.with_name(compile_path.name + ".tmp")
            try:
                tmp_companion.write_text(companion_text, encoding="utf-8")
                os.replace(tmp_companion, compile_path)
            except BaseException:
                try:
                    tmp_companion.unlink()
                except OSError:
                    pass
                raise
            ents = extract_entities_heuristic(text, str(compile_path))
            report = compile_entities(ents, str(compile_path), vault_dir, vault_dir)
        except Exception as exc:
            # Raw landed; the compile stage failed — surface as the
            # partial-state honesty shape, never "nothing was saved".
            raise _DocumentCompileError(raw_path.name, exc) from exc
        return raw_path, report

    def _extract_result_buttons(self, text: str) -> list[Any]:
        """Parse <<BLOG_RESULTS>> or <<QUOTE_RESULTS>> markers and return action buttons.

        Returns empty list if no marker found.
        """
        from models import MessageComponent
        import json

        # Blog results → Publish/Skip buttons
        if "<<BLOG_RESULTS>>" in text:
            try:
                start = text.index("<<BLOG_RESULTS>>") + len("<<BLOG_RESULTS>>")
                end = text.index("<</BLOG_RESULTS>>")
                data = json.loads(text[start:end].strip())
                draft_id = data.get("draft_id", "")
                if draft_id:
                    return [
                        MessageComponent(label="Publish", custom_id=f"blog_publish:{draft_id}", style="success"),
                        MessageComponent(label="Skip", custom_id=f"blog_skip:{draft_id}", style="secondary"),
                    ]
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                print(f"[{datetime.now()}] Failed to parse BLOG_RESULTS: {e}")

        # Quote results — no buttons needed, just let the text through
        # (quote results are self-contained carrier rate displays)

        return []

    async def _handle_button(self, adapter: Any, incoming: Any, custom_id: str) -> None:
        """Handle a button click routed as __button:{custom_id}.

        Supports blog_publish:{draft_id}, blog_skip:{draft_id}, and
        other future button patterns.
        """
        if custom_id.startswith("turn_queue:"):
            await self._apply_turn_followup_choice(
                adapter,
                incoming,
                custom_id,
                mode="queue",
            )
        elif custom_id.startswith("turn_steer:"):
            await self._apply_turn_followup_choice(
                adapter,
                incoming,
                custom_id,
                mode="steer",
            )
        elif custom_id.startswith("blog_publish:"):
            draft_id = custom_id.split(":", 1)[1]
            try:
                # Lazy import — extension may not be available
                from extensions.blog.handlers import handle_publish

                reply = await handle_publish(adapter, incoming, draft_id)
            except ImportError:
                reply = f"Blog extension not loaded. Manually publish at admin.your-business.example.com > Blog (ID: {draft_id})"
            except Exception as e:
                reply = f"Publish failed: {e}"

            await adapter.send(
                OutgoingMessage(
                    text=reply,
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("blog_skip:"):
            draft_id = custom_id.split(":", 1)[1]
            await adapter.send(
                OutgoingMessage(
                    text=f"Skipped. Draft `{draft_id}` saved for later — view at admin.your-business.example.com > Blog.",
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id == "quote_approve":
            # Run the TurboRater quote via PIV
            incoming.text = "Use the Skill tool to invoke the 'turborater-quote' skill with the lead info from our conversation."
            incoming.is_piv = True
            incoming.piv_command = "quote"
            # Re-route through normal engine handling
            await self._handle_inner(adapter, incoming)
        elif custom_id == "quote_cancel":
            await adapter.send(
                OutgoingMessage(
                    text="Quote cancelled.",
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("concept_accept:"):
            auto_id = custom_id.split(":", 1)[1]
            reply = await self._handle_file_subcommand("accept", auto_id)
            await adapter.send(
                OutgoingMessage(
                    text=reply,
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("concept_diff:"):
            auto_id = custom_id.split(":", 1)[1]
            reply = await self._handle_file_subcommand("diff", auto_id)
            await adapter.send(
                OutgoingMessage(
                    text=reply,
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("concept_ignore:"):
            await adapter.send(
                OutgoingMessage(
                    text="Skipped. Draft will sweep itself in 24h.",
                    channel=incoming.channel,
                    thread=incoming.thread,
                )
            )
        elif custom_id.startswith("video_"):
            # Guided /video wizard steps: kind -> input -> style -> voice ->
            # vision gate (video_kind/video_style/video_voice/video_vision).
            # core_handlers owns the flow and the per-channel pending state.
            import core_handlers as _video_handlers

            await _video_handlers.handle_video_button(adapter, incoming, custom_id)
        elif custom_id.startswith("watch:"):
            import core_handlers as _watch_handlers

            await _watch_handlers.handle_watch_button(adapter, incoming, custom_id)
        elif custom_id.startswith("linkedin_flow:"):
            import core_handlers as _linkedin_handlers

            await _linkedin_handlers.handle_linkedin_button(adapter, incoming, custom_id)
        elif custom_id.startswith("primo_flow:"):
            import core_handlers as _primo_handlers

            await _primo_handlers.handle_primo_button(adapter, incoming, custom_id)
        elif custom_id.startswith("social:"):
            await self._handle_social_button(adapter, incoming, custom_id)
        elif custom_id.startswith("cofounder:"):
            await self._handle_cofounder_button(adapter, incoming, custom_id)
        else:
            # Unknown button — log and ignore
            print(f"[{datetime.now()}] Unknown button: {custom_id}")

    async def _handle_social_button(
        self, adapter: Any, incoming: Any, custom_id: str
    ) -> None:
        """Route a social-draft button tap (``social:<action>:<id>``) to the
        existing ``/social`` handler.

        Approve runs approve+dispatch in one go (the auth-gated button tap IS
        the operator approval under the default-deny write doctrine). Reject
        kills the draft. Edit returns the full body for manual copy/tweak.
        """
        # Default-deny: an external write (LinkedIn/Reddit post) may fire ONLY
        # from a genuine, auth-checked button tap. The Telegram adapter verifies
        # allowed_user_ids and stamps raw_event.interaction_type="button" before
        # emitting this. A raw "__button:social:..." typed through any other
        # ingress (CLI / web relay) lacks that marker and is refused — typed text
        # can never synthesize an approval.
        raw_event = getattr(incoming, "raw_event", None) or {}
        if raw_event.get("interaction_type") != "button":
            await adapter.send(
                OutgoingMessage(
                    text="Social actions only run from the draft buttons. Use `/social` to manage the queue.",
                    channel=incoming.channel,
                    thread=incoming.thread,
                    is_error=True,
                )
            )
            return

        parts = custom_id.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await adapter.send(
                OutgoingMessage(
                    text=f"Malformed social action: {custom_id}",
                    channel=incoming.channel,
                    thread=incoming.thread,
                    is_error=True,
                )
            )
            return

        _, action, pid = parts
        import core_handlers

        try:
            if action == "approve":
                approved = await core_handlers.handle_social(
                    adapter, incoming, f"approve {pid}"
                )
                # Dispatch only if the post actually reached 'approved' state —
                # verified against the DB, not by sniffing the reply string.
                if self._social_post_is_approved(pid):
                    reply = await core_handlers.handle_social(
                        adapter, incoming, f"post {pid}"
                    )
                else:
                    reply = approved
            elif action == "reject":
                reply = await core_handlers.handle_social(
                    adapter, incoming, f"reject {pid}"
                )
            elif action == "edit":
                reply = await self._social_edit_reply(pid)
            else:
                reply = f"Unknown social action: {action}"
        except Exception as e:  # noqa: BLE001 — never leave the tap on read
            reply = f"Social action failed: {type(e).__name__}: {e}"

        await adapter.send(
            OutgoingMessage(
                text=reply,
                channel=incoming.channel,
                thread=incoming.thread,
            )
        )

    async def _handle_cofounder_button(
        self, adapter: Any, incoming: Any, custom_id: str
    ) -> None:
        """Route a co-founder notify-card button tap
        (``cofounder:<action>:<slug>``) through the SAME dispatch path as the
        typed ``/cofounder pause|approve`` — ``manager.dispatch`` applies the
        command's role gate and calls the one registered handler, so a button
        (or a typed ``__button:cofounder:...``) can never do more than the
        slash command and no flip logic is duplicated (US-016).
        """
        parts = custom_id.split(":")
        if len(parts) != 3 or not parts[2]:
            await adapter.send(
                OutgoingMessage(
                    text=f"Malformed co-founder action: {custom_id}",
                    channel=incoming.channel,
                    thread=incoming.thread,
                    is_error=True,
                )
            )
            return
        _, action, slug = parts
        if action not in ("pause", "approve"):
            await adapter.send(
                OutgoingMessage(
                    text=f"Unknown co-founder action: {action}",
                    channel=incoming.channel,
                    thread=incoming.thread,
                    is_error=True,
                )
            )
            return

        try:
            reply = await self.manager.dispatch(
                "cofounder", adapter, incoming, f"{action} {slug}"
            )
        except Exception as e:  # noqa: BLE001 — never leave the tap unanswered
            reply = f"Co-founder action failed: {type(e).__name__}: {e}"
        if reply is None:
            reply = "Co-founder command is not available."

        await adapter.send(
            OutgoingMessage(
                text=reply,
                channel=incoming.channel,
                thread=incoming.thread,
            )
        )

    def _social_post_is_approved(self, pid: str) -> bool:
        """True only when post #pid is genuinely in 'approved' state in the DB.
        Robust gate for the approve→dispatch sequence (no reply-string sniffing)."""
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        try:
            from social.service import SocialPostService

            post = SocialPostService().get_post(int(pid))
            return post is not None and post.status == "approved"
        except Exception:  # noqa: BLE001 — fail closed: no approval, no post
            return False

    async def _social_edit_reply(self, pid: str) -> str:
        """Return the full draft body so the operator can copy, edit, and post
        it manually, then clear the queue row."""
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        try:
            from social.service import SocialPostService

            post = SocialPostService().get_post(int(pid))
            if post is None:
                return f"Draft #{pid} not found."
            return (
                f"Edit draft #{pid} — copy, tweak, and post manually, then "
                f"`/social reject {pid}` to clear it from the queue:\n\n{post.body}"
            )
        except Exception as e:  # noqa: BLE001
            return f"Error loading draft #{pid}: {type(e).__name__}: {e}"

    def _persist_router_turn(self, incoming: Any, reply: str) -> None:
        """Persist direct router-path turns into the transcript store."""

        # Living Mind Act 4 (R1 B4): capture the brief-owed marker BEFORE the
        # store bump below closes the away gap — ONE seam covers every
        # persisting router path. Defensive getattr keeps fake engines
        # (tests) green; the engine method is whole-body fail-open.
        note_router_activity = getattr(self.engine, "note_router_activity", None)
        if callable(note_router_activity):
            note_router_activity(incoming)

        store = getattr(self.engine, "session_store", None)
        if store is None or not hasattr(store, "add_message"):
            return
        if not reply.strip():
            return

        platform_str = incoming.platform.value
        channel_id = incoming.channel.platform_id
        thread_id = resolve_thread_id(
            channel_id,
            incoming.thread.thread_id if incoming.thread else None,
        )
        session_id = build_session_key(platform_str, channel_id, thread_id)
        now = datetime.now()
        existing = store.get(platform_str, channel_id, thread_id)

        if existing:
            existing.message_count += 1
            existing.updated_at = now
            self._apply_router_runtime_metadata(incoming, existing)
            store.update(existing)
        else:
            # PRP-7d R1 B2: read source from incoming; set-once on create
            # (the `if existing:` UPDATE branch above MUST NOT touch source).
            message_source = getattr(incoming, "source", "interactive")
            session = Session(
                session_id=session_id,
                agent_session_id="",
                platform=platform_str,
                channel_id=channel_id,
                thread_id=thread_id,
                user_id=incoming.user.platform_id,
                created_at=now,
                updated_at=now,
                message_count=1,
                source=message_source,
            )
            self._apply_router_runtime_metadata(incoming, session)
            store.create(session)

        timestamp = getattr(incoming, "timestamp", now)
        store.add_message(session_id, "user", _incoming_display_text(incoming), timestamp)
        store.add_message(session_id, "assistant", reply, now)

    def _apply_router_runtime_metadata(self, incoming: Any, session: Session) -> None:
        """Keep router-command quiet metadata aligned with current selection."""

        parsed = self._parse_command((getattr(incoming, "text", "") or "").strip())
        if not parsed or parsed[0] not in {"model", "provider", "teamroom", "team"}:
            return
        command, args = parsed
        requested_runtime_lane: str | None = None
        if command in {"teamroom", "team"}:
            if command == "team":
                team_tokens = args.split(maxsplit=1)
                if not team_tokens or team_tokens[0].lower() != "room":
                    return
                args = team_tokens[1] if len(team_tokens) > 1 else ""
            runtime_requested, requested_runtime_lane = self._router_runtime_request(args)
            if not runtime_requested:
                return

        try:
            from runtime.model_control import configured_model_for_provider
            from runtime.selection import provider_display_name, resolve_runtime_selection

            selection = resolve_runtime_selection()
            selected_lane = requested_runtime_lane or selection.lane or "auto"
            session.runtime_lane = selected_lane
            if selected_lane == "claude_native":
                session.runtime_provider = "claude"
                session.runtime_model = configured_model_for_provider("claude") or ""
            else:
                session.runtime_provider = selection.generic_provider or "auto"
                session.runtime_session_id = ""
                session.runtime_model = (
                    configured_model_for_provider(session.runtime_provider)
                    if session.runtime_provider != "auto"
                    else ""
                ) or ""
            session.runtime_profile_key = (
                f"configured-{provider_display_name(session.runtime_provider)}"
                if session.runtime_provider
                else ""
            )
        except Exception as e:
            print(
                f"[{datetime.now()}] Failed to snapshot router runtime metadata: {e}",
                flush=True,
            )

    @staticmethod
    def _router_runtime_request(args: str) -> tuple[bool, str | None]:
        try:
            tokens = shlex.split(args or "")
        except ValueError:
            return False, None
        runtime_requested = False
        runtime_lane: str | None = None
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "--runtime":
                runtime_requested = True
                i += 1
                continue
            if token.strip(".,:;!?()[]{}").lower() in {"call", "calling", "live", "runtime"}:
                runtime_requested = True
                i += 1
                continue
            if token in {"--lane", "--runtime-lane"} and i + 1 < len(tokens):
                runtime_requested = True
                runtime_lane = tokens[i + 1].strip() or None
                i += 2
                continue
            i += 1
        return runtime_requested, runtime_lane

    async def shutdown(self) -> None:
        """Disconnect all adapters gracefully."""
        for adapter in self.adapters.values():
            try:
                await adapter.disconnect()
            except Exception as e:
                print(f"[{datetime.now()}] Error disconnecting {adapter.platform.value}: {e}")
