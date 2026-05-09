"""CLI adapter — stdin/stdout for single-query and interactive modes.

Implements PlatformAdapter protocol for command-line usage.
Used by `thehomie chat` CLI command and consumed by Paperclip adapter.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import datetime

from models import Channel, IncomingMessage, OutgoingMessage, Platform, User

# Phase 4 (PRD-8) — voice cascade + marker dispatch.
import voice as voice_mod
from voice_markers import parse_send_markers, strip_send_markers


def _resolve_active_profile_name() -> str:
    """Read the active profile name; fail-open to ``"unknown"`` on resolver error.

    PRP-7d Task 14 / R3 NNM1: the fallback is ``"unknown"`` (NOT ``"default"``)
    so a persona-resolution failure is not silently misfiled under the actual
    default profile. Downstream consumers (Paperclip, Mission Control) need to
    distinguish "we could not resolve" from "the default profile was active".

    The ``from personas import get_active_profile_name`` happens INSIDE the
    function body so module-attribute lookup applies — tests that monkeypatch
    ``personas.activity.get_active_profile_name`` (or
    ``personas.get_active_profile_name``) propagate to this helper. A top-level
    import would cache the function object at import time and break that.
    """
    try:
        from personas import get_active_profile_name

        return get_active_profile_name() or "unknown"
    except Exception:
        return "unknown"


def build_quiet_error_envelope(
    exc: BaseException,
    *,
    source: str = "interactive",
) -> str:
    """Build the 12-field locked-order JSON envelope for CLI-level pre-adapter
    exceptions (PRP-7d Task 15 / R1 B6).

    Used by ``chat/cli.py`` when an exception is raised BEFORE
    ``CLIAdapter.format_final_output`` would normally be called (e.g. bad
    config, persona-resolution failure post-parse, engine init failure).

    The 12-field locked order matches ``format_final_output``'s adapter-error
    sub-path so consumers don't need two parsers. ``error`` is ALWAYS last.
    Defaults are JSON-clean fixed-type values (empty strings, ``0`` /
    ``0.0`` for numerics) — no ``null`` for ``cost_usd`` etc.

    ``profile`` calls ``_resolve_active_profile_name()`` which itself
    fails-open to ``"unknown"`` — defends against recursive failures.

    ``source`` is the echo of the parsed ``--source`` flag value so a
    Paperclip-style ``thehomie chat --source tool -q "x" -Q`` that fails
    during engine/config setup is NOT silently misclassified as
    ``"interactive"``. The value is run through ``normalize_source`` (fail-OPEN
    to ``"interactive"`` for unknown values) so a typo or malicious caller
    cannot pollute the envelope.
    """
    import json as json_mod

    # Local import — avoids a top-level import cycle (chat.session imports from
    # chat.* at parse time; this module is also imported by chat.cli at parse).
    from session import normalize_source

    payload = {
        "success": False,
        "response": "",
        "session_id": "",
        "lane": "",
        "provider": "",
        "model": "",
        "cost_usd": 0.0,
        "tool_calls": 0,
        "execution_time_ms": 0,
        "profile": _resolve_active_profile_name(),
        "source": normalize_source(source),
        "error": str(exc),
    }
    return json_mod.dumps(payload)


class CLIAdapter:
    """CLI adapter — stdin/stdout for single-query and interactive modes."""

    def __init__(
        self,
        *,
        query: str | None = None,
        quiet: bool = False,
        model: str | None = None,
        toolsets: str | None = None,
        resume_session: str | None = None,
        continue_last: bool = False,
        source: str = "interactive",
        voice_path: str | None = None,
        voice_out_path: str | None = None,
    ):
        self._query = query
        self._quiet = quiet
        self._model = model
        self._toolsets = toolsets
        self._resume = resume_session
        self._continue_last = continue_last
        # PRD-7 §7.10 / Phase 4 (PRP-7d): session source tag, propagated into
        # every IncomingMessage this adapter yields and surfaced via
        # get_session_info() for the quiet JSON envelope (WS4).
        self.source: str = source
        # PRD-8 Phase 4: voice ingress/egress.
        # voice_path: read from file, transcribe via cascade, use as user message.
        # voice_out_path: write voice.synthesize(reply) to this path.
        self._voice_path: str | None = voice_path
        self._voice_out_path: str | None = voice_out_path
        self._responses: list[OutgoingMessage] = []
        self._final_response: str = ""
        self._got_error: bool = False
        self._start_time = time.monotonic()
        self._channel_id: str = ""
        self._user_id: str = ""

    @property
    def platform(self) -> Platform:
        return Platform.CLI

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def listen(self) -> AsyncIterator[IncomingMessage]:
        """Yield one message (-q mode) or loop on stdin (interactive).

        Session identity model:
        - --resume <id>: Find session by runtime_session_id, reuse its channel_id
        - --continue/-c: Find most recent CLI session, reuse its channel_id
        - New -q query: channel_id = "cli-{uuid4()[:8]}" (unique per invocation)
        - New interactive: channel_id = "cli-interactive-{uuid4()[:8]}"
        """
        import uuid

        from session import get_session_store

        from config import CHAT_DB_PATH

        user = User(Platform.CLI, os.getenv("USER", "cli-user"), os.getenv("USER", "user"))
        store = get_session_store(CHAT_DB_PATH)

        if self._resume:
            sessions = store.list_active(platform="cli")
            match = next(
                (
                    s
                    for s in sessions
                    if s.runtime_session_id == self._resume or self._resume in s.session_id
                ),
                None,
            )
            if match:
                channel_id = match.channel_id
            else:
                channel_id = f"cli-{uuid.uuid4().hex[:8]}"
                if not self._quiet:
                    print(f"Warning: session '{self._resume}' not found, starting new session")
                self._resume = None

        elif self._continue_last:
            sessions = store.list_active(platform="cli")
            if sessions:
                latest = max(sessions, key=lambda s: s.updated_at)
                channel_id = latest.channel_id
            else:
                channel_id = f"cli-{uuid.uuid4().hex[:8]}"
                if not self._quiet:
                    print("No previous CLI session found, starting new session")

        else:
            suffix = uuid.uuid4().hex[:8]
            channel_id = f"cli-{suffix}" if self._query else f"cli-interactive-{suffix}"

        channel = Channel(Platform.CLI, channel_id, is_dm=True)
        self._channel_id = channel_id
        self._user_id = user.platform_id

        # Phase 4: --voice <path> flag — transcribe file as user message.
        # Single-shot; takes precedence over --query if both set.
        if self._voice_path:
            try:
                transcript = await voice_mod.transcribe_audio_file(self._voice_path)
            except Exception as e:
                print(f"[{datetime.now()}] CLI voice transcribe failed: {e}")
                transcript = ""
            if transcript.strip():
                yield IncomingMessage(
                    text=transcript.strip(),
                    user=user,
                    channel=channel,
                    platform=Platform.CLI,
                    timestamp=datetime.now(),
                    source=self.source,
                )
                return

        if self._query:
            yield IncomingMessage(
                text=self._query,
                user=user,
                channel=channel,
                platform=Platform.CLI,
                timestamp=datetime.now(),
                source=self.source,
            )
        else:
            # Interactive mode — readline loop
            try:
                import readline  # noqa: F401
            except ImportError:
                try:
                    import pyreadline3  # noqa: F401
                except ImportError:
                    pass
            try:
                while True:
                    line = input("thehomie> ").strip()
                    if not line:
                        continue
                    if line in ("/quit", "/exit", "exit", "quit"):
                        break
                    yield IncomingMessage(
                        text=line,
                        user=user,
                        channel=channel,
                        platform=Platform.CLI,
                        timestamp=datetime.now(),
                        source=self.source,
                    )
            except (EOFError, KeyboardInterrupt):
                pass

    async def send(self, message: OutgoingMessage) -> str | None:
        """Print response to stdout.

        In quiet mode, capture the final response text and error flag.
        In normal mode, print everything as it arrives.
        Footer (gap-6 concept draft hint) is appended below the body with
        a blank line separator. Only rendered in non-quiet mode so the
        Paperclip JSON contract stays clean.

        Phase 4: parses [SEND_FILE]/[SEND_PHOTO] markers and writes each
        media path to stdout (or copies to the local CWD); writes voice
        synthesis result to --voice-out path when set.
        """
        footer = getattr(message, "footer", None)

        # Phase 4: marker dispatch — emit one line per marker (path-write only).
        markers = parse_send_markers(message.text)
        for m in markers:
            try:
                # CLI dispatch: just acknowledge the path on stdout. Real CLI
                # users can chain with `xargs cp` or similar — keep it simple.
                if not self._quiet:
                    print(f"[CLI media: kind={m.kind} path={m.path}"
                          + (f" caption={m.caption!r}" if m.caption else "")
                          + "]", flush=True)
            except Exception as e:
                print(f"[{datetime.now()}] CLI marker dispatch failed: {e}")

        text = strip_send_markers(message.text)

        # Phase 4: --voice-out flag — synthesize reply audio to file.
        if self._voice_out_path and text:
            try:
                audio = await voice_mod.synthesize(text)
                with open(self._voice_out_path, "wb") as f:
                    f.write(audio)
            except Exception as e:
                print(f"[{datetime.now()}] CLI voice-out failed: {e}")

        if self._quiet:
            self._final_response = text
            if getattr(message, "is_error", False):
                self._got_error = True
        else:
            print(text, flush=True)
            if footer:
                print(f"\n{footer}", flush=True)
        self._responses.append(message)
        return None

    async def update(self, message: OutgoingMessage) -> None:
        """CLI doesn't support message editing.

        In quiet mode, do NOT capture updates as the final response.
        In normal mode, print the update for streaming-like experience.
        """
        footer = getattr(message, "footer", None)
        if not self._quiet:
            print(message.text, flush=True)
            if footer:
                print(f"\n{footer}", flush=True)

    async def send_typing(self, channel: Channel) -> None:
        pass

    def get_session_info(self) -> dict:
        """Retrieve session metadata after _handle() completes.

        Uses the deterministic channel_id set during listen().
        """
        from session import get_session_store

        from config import CHAT_DB_PATH

        store = get_session_store(CHAT_DB_PATH)
        session = store.get("cli", self._channel_id, self._channel_id)
        if session:
            return {
                "session_id": session.runtime_session_id or session.session_id,
                "lane": session.runtime_lane,
                "provider": session.runtime_provider,
                "model": session.runtime_model,
                "cost_usd": session.total_cost_usd,
                "tool_calls": session.tool_call_count,
                "source": session.source,
            }
        return {
            "session_id": "",
            "lane": "",
            "provider": "",
            "model": "",
            "cost_usd": 0.0,
            "tool_calls": 0,
            "source": "interactive",
        }

    def format_final_output(self, session_id: str | None, result: dict) -> str:
        """Format the final output for Paperclip/MC control plane.

        Quiet mode: deterministic JSON with success flag from is_error.
        Normal mode: human-readable footer with session metadata.

        PRP-7d Task 14: success path emits 11 always-present fields in the
        locked order; adapter-error sub-path appends ``error`` as the 12th
        (always last). Locked order:

            success, response, session_id, lane, provider, model, cost_usd,
            tool_calls, execution_time_ms, profile, source [, error]

        Tests assert ``list(payload.keys()) == [...]`` verbatim against this
        order. Insertion order is the contract — do NOT reorder.
        """
        if self._quiet:
            import json as json_mod

            response_text = self._final_response
            had_error = self._got_error
            payload = {
                "success": not had_error,
                "response": response_text if not had_error else "",
                "session_id": session_id or "",
                "lane": result.get("lane", ""),
                "provider": result.get("provider", ""),
                "model": result.get("model", ""),
                "cost_usd": result.get("cost_usd", 0.0),
                "tool_calls": result.get("tool_calls", 0),
                "execution_time_ms": int((time.monotonic() - self._start_time) * 1000),
                # PRP-7d Task 14 — `profile` + `source` always present, in this
                # order, BEFORE the conditional `error` field.
                "profile": _resolve_active_profile_name(),
                "source": result.get("source", getattr(self, "source", "interactive"))
                or "interactive",
            }
            if had_error:
                payload["error"] = response_text
            return json_mod.dumps(payload)
        else:
            lines = [
                "",
                "---",
                f"session_id: {session_id or 'none'}",
                f"lane: {result.get('lane', 'unknown')}",
                f"provider: {result.get('provider', 'unknown')}",
                f"model: {result.get('model', 'unknown')}",
                f"cost_usd: {result.get('cost_usd', 0.0):.4f}",
                f"tool_calls: {result.get('tool_calls', 0)}",
            ]
            return "\n".join(lines)
