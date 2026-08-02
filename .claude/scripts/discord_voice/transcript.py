"""Rolling transcript writer for the Discord voice sidecar.

The sidecar used to LOG every final transcript line and drop it — a voice
conversation evaporated on leave. This writer is the durable half of the
Discord vault debrief: it appends final rows to a JSONL file that the
MAIN-venv lifecycle picks up (in the orchestration API process, where
``talk_flush.start_session_flush`` lives) at its stop/start/startup
chokepoints. The sidecar itself never flushes — no in-bridge exit hook
survives the lifecycle's ``taskkill /T /F`` on Windows.

STDLIB ONLY, deliberately: the main venv's pytest imports this module
directly (``discord_voice/__init__.py`` imports nothing), so the writer
is testable without the sidecar venv or pytest-asyncio.

File-handle contract (load-bearing on Windows): one ``open(path, "a")``
→ ONE ``write`` of a full JSON line → close, per row, always
synchronously on the bridge event loop. Never keep a persistent handle
(block-buffering + ``taskkill /F`` silently discards ~8KB of buffered
turns, and CPython holds files without FILE_SHARE_DELETE so the
lifecycle's rename/delete would fail). Never move the writes to
aiofiles/to_thread — the in-process rotate-vs-append safety depends on
event-loop synchrony.

File format:
    {"type": "header", "sessionId": "dv-<hex>", "startedAt": ISO,
     "guildId": int|null, "channelId": int|null}
    {"type": "turn", "role": "user"|"assistant", "text": str, "ts": float}
    one optional truncation-marker turn row (write-once)

Names: the live file is exactly the configured path; rotation renames it
to ``<name>.pending-<epoch_ms>`` (UNIQUE — a single static sibling name
clobbers a session on double channel-switch). The lifecycle claims files
by renaming to ``<name>.claimed`` before flushing.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

#: One regex, shared by the writer (mint) and the lifecycle reader
#: (validation) — pin both ends to this constant, never a copy.
SID_RE = re.compile(r"^dv-[0-9a-f]{8,24}$")

MAX_ROW_TEXT = 2_000
MAX_FILE_BYTES = 512_000
TRUNCATION_MARKER = "[transcript truncated at size cap]"


class TranscriptWriter:
    """Fail-open rolling transcript writer. Every method swallows OS/IO
    errors — losing a transcript line must never touch the voice path."""

    def __init__(self, path: str | os.PathLike | None):
        self._path: Path | None = Path(path) if path else None
        self._capped = False
        # Per-SESSION disable (rotation failure): the next start() retries —
        # a process-lifetime disable would silently record nothing for
        # every later session until the sidecar restarts.
        self._session_disabled = False
        self.session_id: str = ""

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def start(self, guild_id: int | None, channel_id: int | None) -> str:
        """Rotate any leftover live file to a unique ``.pending`` sibling,
        then start a fresh file with a typed header. Returns the minted
        session id (empty string when the writer is disabled)."""

        if self._path is None:
            return ""
        self.session_id = f"dv-{uuid.uuid4().hex[:24]}"
        self._capped = False
        self._session_disabled = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists():
                # Collision-proof pending name: wall-clock ms alone can
                # collide across two same-instant rotations, and os.replace
                # would silently vaporize the earlier session.
                pending = self._path.with_name(
                    f"{self._path.name}.pending-{int(time.time() * 1000)}"
                    f"-{uuid.uuid4().hex[:6]}"
                )
                try:
                    os.replace(self._path, pending)
                except OSError:
                    # The predecessor could NOT be isolated. Writing the new
                    # header in "w" mode would TRUNCATE it — destroying the
                    # previous session's transcript. Disable THIS SESSION's
                    # recording instead (the next start() retries — transient
                    # AV/handle locks clear); the predecessor stays intact
                    # for the lifecycle sweep.
                    self._session_disabled = True
                    self.session_id = ""
                    return ""
            header = {
                "type": "header",
                "sessionId": self.session_id,
                "startedAt": datetime.now().isoformat(timespec="seconds"),
                "guildId": guild_id,
                "channelId": channel_id,
            }
            with open(self._path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — fail-open, the voice path rules
            pass
        return self.session_id

    def append(self, role: str, text: str) -> None:
        """Append one FINAL transcript row. Non-final deltas never come
        here (assistant ``.done`` events carry the complete turn)."""

        if self._path is None or self._capped or self._session_disabled:
            return
        if role not in ("user", "assistant"):
            return
        text = str(text or "").strip()
        if not text:
            return
        try:
            if (
                self._path.exists()
                and self._path.stat().st_size >= MAX_FILE_BYTES
            ):
                # Write-once marker: the flag flips before the write so a
                # failed marker write cannot retry forever.
                self._capped = True
                row = {
                    "type": "turn",
                    "role": "assistant",
                    "text": TRUNCATION_MARKER,
                    "ts": time.time(),
                }
            else:
                row = {
                    "type": "turn",
                    "role": role,
                    "text": text[:MAX_ROW_TEXT],
                    "ts": time.time(),
                }
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_ROW_TEXT",
    "SID_RE",
    "TRUNCATION_MARKER",
    "TranscriptWriter",
]
