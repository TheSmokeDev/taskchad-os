"""AgentRouter — Pipecat FrameProcessor for cabinet voice routing.

VERBATIM PORT of ClaudeClaw ``warroom/router.py:1-266``. Routing precedence
is preserved exactly:

  1. Drop ``InterimTranscriptionFrame`` (Deepgram emits a partial frame per
     phoneme; without this filter every partial fires a separate bridge call).
  2. Broadcast triggers (everyone / all / team / standup / status update /
     status report) → emit ``AgentRouteFrame(agent_id="all", mode="broadcast")``.
  3. Name-prefix detection (``"research, summarize this"``) → emit
     ``AgentRouteFrame(agent_id="research", mode="single")``.
  4. Pinned agent (set by dashboard via :data:`config.PIN_PATH`) → emit
     ``AgentRouteFrame(agent_id=pinned, mode="single")``.
  5. Default fallback → emit ``AgentRouteFrame(agent_id="main", mode="single")``.

The wire string ``"main"`` is preserved verbatim at this boundary (Q4
translation lock). The Homie-side translation to internal id ``"default"``
happens at the persona-config lookup site in :mod:`cabinet.voice.personas`.

Translation Boundary Audit substitutions (per PRP §"Translation Boundary
Audit"):

  * ``/tmp/warroom-pin.json``  -> ``<tempdir>/cabinet-voice-pin.json``
  * ``/tmp/warroom-agents.json`` -> ``<tempdir>/cabinet-roster.json``

PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M1) — the default tmp dir now resolves
through ``tempfile.gettempdir()`` for cross-platform support; operators can
still override via ``CABINET_VOICE_PIN_PATH`` / ``CABINET_VOICE_ROSTER_PATH``.

mtime-based caching, defensive JSON parsing, and fallback semantics are all
preserved verbatim from upstream.

Rule 3 compliance: all log emit sites that touch dynamic args (paths, exc
strings, agent ids) wrap arguments via the late-bound :func:`_redact` helper
so secrets don't leak through stack-trace messages.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Pipecat is an optional Phase 6 install (heavy dependency). Voice subprocess
# is the only consumer; tests mock these classes. Wrap imports so the module
# imports cleanly in CI without pipecat installed (pure-Python tests run).
try:  # pragma: no cover — exercised by integration only.
    from pipecat.frames.frames import (
        DataFrame,
        InterimTranscriptionFrame,
        TextFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    _PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover — pipecat optional dep.
    _PIPECAT_AVAILABLE = False

    # Stub bases so AST scans + dataclass syntax still load without pipecat.
    class FrameProcessor:  # type: ignore[no-redef]
        async def process_frame(self, frame, direction) -> None:  # noqa: D401
            ...

        async def push_frame(self, frame, direction=None) -> None:
            ...

    class FrameDirection:  # type: ignore[no-redef]
        DOWNSTREAM = "DOWNSTREAM"
        UPSTREAM = "UPSTREAM"

    class DataFrame:  # type: ignore[no-redef]
        pass

    class TranscriptionFrame:  # type: ignore[no-redef]
        pass

    class InterimTranscriptionFrame:  # type: ignore[no-redef]
        pass

    class TextFrame:  # type: ignore[no-redef]
        pass


from . import config as voice_config  # noqa: E402

logger = logging.getLogger("cabinet.voice.router")

# PRD-8 Phase 7b — log-message redaction (Rule 3 module-attribute lookup).
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact

# Renamed file-IPC paths per Translation Boundary Audit.
PIN_PATH: Path = voice_config.PIN_PATH
ROSTER_PATH: Path = voice_config.ROSTER_PATH

# Default fallback if the roster file is missing/unreadable. Matches
# upstream's bundled built-in agents so a fresh install still routes
# correctly. Wire ids preserved verbatim.
_DEFAULT_AGENT_NAMES: frozenset[str] = frozenset({"main", "research", "comms", "content", "ops"})

# Module-level mutable set, kept for back-compat with agent_bridge.py which
# imports AGENT_NAMES directly. _refresh_agent_names_from_roster mutates
# this set in place so importers see the live roster.
AGENT_NAMES: set[str] = set(_DEFAULT_AGENT_NAMES)

# Phrases that trigger a broadcast to all agents (verbatim from
# warroom/router.py:54-57).
BROADCAST_TRIGGERS: set[str] = {
    "everyone",
    "all",
    "team",
    "standup",
    "status update",
    "status report",
}

# Common casual prefixes people use before an agent name (verbatim from
# warroom/router.py:60).
_GREETING_PREFIXES = r"(?:hey|yo|ok|okay|alright)?\s*"

# Build a pattern for broadcast triggers (the trigger words are stable, no
# need to make this dynamic).
_broadcast_pattern = re.compile(
    rf"\b({'|'.join(BROADCAST_TRIGGERS)})\b",
    re.IGNORECASE,
)

# Roster mtime cache + lazily-rebuilt agent-prefix regex.
_roster_mtime: float = 0.0
_agent_pattern: Optional[re.Pattern] = None


def _build_agent_pattern(names: set[str]) -> re.Pattern:
    """Verbatim port of warroom/router.py:74-79."""
    safe = sorted((re.escape(n) for n in names if n), key=len, reverse=True)
    return re.compile(
        rf"^\s*{_GREETING_PREFIXES}({'|'.join(safe)})[,:\s]+(.+)",
        re.IGNORECASE | re.DOTALL,
    )


def _refresh_agent_names_from_roster() -> None:
    """Re-read /tmp/cabinet-roster.json if the file's mtime changed.

    Verbatim port of warroom/router.py:82-115. Updates :data:`AGENT_NAMES` in
    place and invalidates the compiled regex. Falls back to last-good values
    on any error.
    """
    global _roster_mtime, _agent_pattern
    try:
        st = os.stat(ROSTER_PATH)
    except (FileNotFoundError, OSError):
        return
    if st.st_mtime == _roster_mtime:
        return
    try:
        with open(ROSTER_PATH, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("roster read failed, keeping cached AGENT_NAMES: %s", _redact(str(exc)))
        return
    if not isinstance(data, list):
        logger.warning("roster JSON is not a list; ignoring")
        return
    new_names = {
        entry["id"]
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    }
    if not new_names:
        return
    # Always include "main" — it's the implicit default route target.
    new_names.add("main")
    if new_names != AGENT_NAMES:
        AGENT_NAMES.clear()
        AGENT_NAMES.update(new_names)
        _agent_pattern = None  # force rebuild on next access
        logger.info("agent roster refreshed: %s", _redact(str(sorted(AGENT_NAMES))))
    _roster_mtime = st.st_mtime


def _get_agent_pattern() -> re.Pattern:
    """Verbatim port of warroom/router.py:118-123."""
    global _agent_pattern
    _refresh_agent_names_from_roster()
    if _agent_pattern is None:
        _agent_pattern = _build_agent_pattern(AGENT_NAMES)
    return _agent_pattern


# Initialize once at import so AGENT_NAMES reflects the on-disk roster
# even before the first utterance arrives. Safe — falls through silently
# if the roster file does not yet exist (e.g. first-run, no meetings yet).
_refresh_agent_names_from_roster()


@dataclass
class AgentRouteFrame(DataFrame):  # type: ignore[misc]
    """Custom frame carrying routing metadata alongside the user message.

    Verbatim port of warroom/router.py:131-141. Inherits from DataFrame so
    it picks up the standard Pipecat frame attributes (id, name, pts,
    metadata). Without this, observers like IdleFrameObserver crash when
    they try to read frame.id.
    """

    agent_id: str = ""
    message: str = ""
    mode: str = "single"  # "single" or "broadcast"


class AgentRouter(FrameProcessor):  # type: ignore[misc]
    """Receives TextFrames from STT, determines routing, and pushes
    AgentRouteFrames downstream to the HomieAgentBridge.

    Verbatim port of warroom/router.py:144-266 (entire class). Routing
    precedence: broadcast trigger → name-prefix → pinned → main.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # mtime-cached read of /tmp/cabinet-voice-pin.json so we don't
        # stat+parse on every single utterance; only re-read when the file
        # changes.
        self._pin_mtime: float = 0.0
        self._pin_agent: Optional[str] = None

    def _get_pinned_agent(self) -> Optional[str]:
        """Return the currently pinned agent id, or None.

        Verbatim port of warroom/router.py:155-192. Reads the pin file only
        when its mtime has changed since the last read; defends against
        non-dict top-level JSON values.
        """
        try:
            st = os.stat(PIN_PATH)
        except FileNotFoundError:
            if self._pin_agent is not None:
                logger.info("pin cleared (file removed)")
            self._pin_mtime = 0.0
            self._pin_agent = None
            return None
        except OSError as exc:
            logger.debug("pin stat failed: %s", _redact(str(exc)))
            return self._pin_agent

        if st.st_mtime != self._pin_mtime:
            self._pin_mtime = st.st_mtime
            try:
                with open(PIN_PATH, "r") as f:
                    data = json.load(f)
                # The pin file is written by the Hono dashboard, but an
                # attacker or a buggy process could drop arbitrary JSON
                # into the pin file. Defend against non-dict top-level values
                # (strings, lists, numbers) that would otherwise crash .get()
                # with AttributeError.
                _refresh_agent_names_from_roster()
                agent = data.get("agent") if isinstance(data, dict) else None
                if isinstance(agent, str) and agent in AGENT_NAMES:
                    if agent != self._pin_agent:
                        logger.info("pin now: %s", _redact(agent))
                    self._pin_agent = agent
                else:
                    self._pin_agent = None
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.debug("pin read failed: %s", _redact(str(exc)))
                self._pin_agent = None

        return self._pin_agent

    async def process_frame(self, frame, direction):
        """Verbatim port of warroom/router.py:194-266 — routing precedence."""
        # CRITICAL: Must call super first so the parent registers StartFrame
        # and initializes the processor's started state. Without this,
        # system frames (StartFrame, EndFrame, MetricsFrame) cause "not
        # received yet" errors.
        await super().process_frame(frame, direction)

        # Drop interim (non-final) transcription frames. Without this filter,
        # each partial triggered a separate Claude SDK call AND each new
        # partial's TTS cancelled the previous one (allow_interruptions=True),
        # which meant users could speak once and rack up 5+ bridge calls
        # while receiving ~zero audio back.
        if isinstance(frame, InterimTranscriptionFrame):
            return

        # Only process final transcriptions for routing. Any other TextFrame
        # subclass passes through unchanged (e.g. TTS-generated TextFrames
        # flowing downstream to the TTS service).
        if direction != FrameDirection.DOWNSTREAM or not isinstance(frame, TranscriptionFrame):
            await self.push_frame(frame, direction)
            return

        text = frame.text.strip()
        if not text:
            return

        # Check for broadcast triggers first.
        if _broadcast_pattern.search(text):
            cleaned = _broadcast_pattern.sub("", text).strip(" ,:")
            message = cleaned if cleaned else text
            route = AgentRouteFrame(
                agent_id="all",
                message=message,
                mode="broadcast",
            )
            await self.push_frame(route)
            return

        # Check for agent name prefix (regex rebuilt lazily when the
        # roster file changes).
        match = _get_agent_pattern().match(text)
        if match:
            agent_id = match.group(1).lower()
            message = match.group(2).strip()
            route = AgentRouteFrame(
                agent_id=agent_id,
                message=message,
                mode="single",
            )
            await self.push_frame(route)
            return

        # Pinned agent (set via dashboard "click-to-pin" UI). Only affects
        # the default route — explicit spoken prefixes and broadcasts above
        # still win.
        pinned = self._get_pinned_agent()
        if pinned:
            route = AgentRouteFrame(
                agent_id=pinned,
                message=text,
                mode="single",
            )
            await self.push_frame(route)
            return

        # Default: route to main agent.
        route = AgentRouteFrame(
            agent_id="main",
            message=text,
            mode="single",
        )
        await self.push_frame(route)


__all__ = [
    "AGENT_NAMES",
    "AgentRouteFrame",
    "AgentRouter",
    "BROADCAST_TRIGGERS",
    "PIN_PATH",
    "ROSTER_PATH",
    "_DEFAULT_AGENT_NAMES",
    "_build_agent_pattern",
    "_refresh_agent_names_from_roster",
]
