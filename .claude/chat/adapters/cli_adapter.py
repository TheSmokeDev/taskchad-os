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
    ):
        self._query = query
        self._quiet = quiet
        self._model = model
        self._toolsets = toolsets
        self._resume = resume_session
        self._continue_last = continue_last
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

        if self._query:
            yield IncomingMessage(
                text=self._query,
                user=user,
                channel=channel,
                platform=Platform.CLI,
                timestamp=datetime.now(),
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
                    )
            except (EOFError, KeyboardInterrupt):
                pass

    async def send(self, message: OutgoingMessage) -> str | None:
        """Print response to stdout.

        In quiet mode, capture the final response text and error flag.
        In normal mode, print everything as it arrives.
        """
        if self._quiet:
            self._final_response = message.text
            if getattr(message, "is_error", False):
                self._got_error = True
        else:
            print(message.text, flush=True)
        self._responses.append(message)
        return None

    async def update(self, message: OutgoingMessage) -> None:
        """CLI doesn't support message editing.

        In quiet mode, do NOT capture updates as the final response.
        In normal mode, print the update for streaming-like experience.
        """
        if not self._quiet:
            print(message.text, flush=True)

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
                "provider": session.runtime_provider,
                "model": session.runtime_model,
                "cost_usd": session.total_cost_usd,
                "tool_calls": session.tool_call_count,
            }
        return {"session_id": "", "provider": "", "model": "", "cost_usd": 0.0, "tool_calls": 0}

    def format_final_output(self, session_id: str | None, result: dict) -> str:
        """Format the final output for Paperclip/MC control plane.

        Quiet mode: deterministic JSON with success flag from is_error.
        Normal mode: human-readable footer with session metadata.
        """
        if self._quiet:
            import json as json_mod

            response_text = self._final_response
            had_error = self._got_error
            payload = {
                "success": not had_error,
                "response": response_text if not had_error else "",
                "session_id": session_id or "",
                "provider": result.get("provider", ""),
                "model": result.get("model", ""),
                "cost_usd": result.get("cost_usd", 0.0),
                "tool_calls": result.get("tool_calls", 0),
                "execution_time_ms": int((time.monotonic() - self._start_time) * 1000),
            }
            if had_error:
                payload["error"] = response_text
            return json_mod.dumps(payload)
        else:
            lines = [
                "",
                "---",
                f"session_id: {session_id or 'none'}",
                f"provider: {result.get('provider', 'unknown')}",
                f"model: {result.get('model', 'unknown')}",
                f"cost_usd: {result.get('cost_usd', 0.0):.4f}",
                f"tool_calls: {result.get('tool_calls', 0)}",
            ]
            return "\n".join(lines)
