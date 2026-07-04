"""Persistent registry of delivery targets that are confirmed unreachable.

When a messaging platform reports that a target chat is permanently gone — a
deleted group (``Forbidden: the group chat was deleted``), a bot kicked/blocked,
or a deactivated user — re-sending to it on every cron tick or every fan-out
delivery wastes a send attempt against the platform's flood-control envelope and
spams the logs.  This registry lets the delivery layer short-circuit a target it
has already proven dead, while staying self-healing: any successful send to that
target clears the flag, so a user who re-adds the bot (or restores the chat)
recovers automatically with no manual cleanup.

Scope is deliberately narrow.  Only *whole-chat* deaths are recorded — the
``forbidden`` and chat-level ``not_found`` (``chat not found``) error kinds.
Thread/topic-level ``not_found`` is NOT recorded here: the adapters already
self-heal that by retrying without ``reply_to`` (see the Telegram adapter's
reply-target-deleted path), and a deleted topic does not mean the parent chat is
dead.

The store is a small JSON file under ``config.STATE_DIR``.  Reads/writes are
best-effort: a corrupt or unwritable file degrades to an in-memory-only registry
rather than raising on the delivery path.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Error kinds (from classify_send_error) that mean the *whole chat* is
# unreachable, not a transient or thread-level problem.
_DEAD_ERROR_KINDS = frozenset({"forbidden", "not_found"})


def _default_path() -> Path:
    """State-file path resolved at call time (Rule 1 — never bind at import)."""
    import config  # lazy: scripts/ is the package root; STATE_DIR is persona-resolved

    return config.STATE_DIR / "dead_targets.json"


def _normalize(platform: str, chat_id: str) -> str:
    """Canonical key for a (platform, chat_id) pair."""
    return f"{str(platform).strip().lower()}:{str(chat_id).strip()}"


class DeadTargetRegistry:
    """Thread-safe, persistent set of confirmed-dead delivery targets.

    Keyed on ``platform:chat_id``.  Stores the reason and a timestamp for
    observability.  Self-healing: :meth:`clear` (called on a successful send)
    removes the flag.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._dead: dict[str, dict[str, object]] = {}
        self._path = path if path is not None else _default_path()
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text())
                if isinstance(raw, dict):
                    # Only keep well-shaped entries.
                    self._dead = {
                        k: v for k, v in raw.items() if isinstance(v, dict)
                    }
        except (OSError, ValueError) as exc:
            logger.debug("dead_targets: could not load %s (%s) — starting empty",
                         self._path, exc)
            self._dead = {}

    def _flush_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._dead, indent=2))
            tmp.replace(self._path)
        except OSError as exc:
            # Best-effort: keep the in-memory state, don't break delivery.
            logger.debug("dead_targets: could not persist %s (%s)", self._path, exc)

    # -- public API --------------------------------------------------------

    @staticmethod
    def is_dead_error_kind(error_kind: str | None) -> bool:
        """Return True when ``error_kind`` denotes a permanent whole-chat death."""
        return bool(error_kind) and error_kind in _DEAD_ERROR_KINDS

    def is_dead(self, platform: str, chat_id: str | None) -> bool:
        if not chat_id:
            return False
        with self._lock:
            return _normalize(platform, chat_id) in self._dead

    def mark_dead(self, platform: str, chat_id: str | None,
                  reason: str = "") -> bool:
        """Record a target as confirmed-dead.  Returns True if newly added."""
        if not chat_id:
            return False
        key = _normalize(platform, chat_id)
        with self._lock:
            existed = key in self._dead
            self._dead[key] = {
                "platform": str(platform).strip().lower(),
                "chat_id": str(chat_id),
                "reason": str(reason)[:200],
                "marked_at": time.time(),
            }
            self._flush_locked()
        if not existed:
            logger.info(
                "dead_targets: marked %s as unreachable (%s) — future deliveries "
                "to this target will be skipped until a send succeeds",
                key, reason or "no reason given",
            )
        return not existed

    def clear(self, platform: str, chat_id: str | None) -> bool:
        """Remove a target's dead flag (self-healing).  Returns True if it was set."""
        if not chat_id:
            return False
        key = _normalize(platform, chat_id)
        with self._lock:
            if key in self._dead:
                del self._dead[key]
                self._flush_locked()
                logger.info("dead_targets: cleared %s (delivery succeeded again)", key)
                return True
        return False

    def all_dead(self) -> dict[str, dict[str, object]]:
        """Snapshot of the current dead set (for diagnostics / CLI)."""
        with self._lock:
            return {k: dict(v) for k, v in self._dead.items()}


def _slack_error_code(exc: BaseException) -> str:
    """Extract the Slack API error code from a ``SlackApiError`` defensively.

    ``exc.response`` may be a plain dict, a ``SlackResponse`` (dict-like, stores
    the parsed body on ``.data``), or absent — normalize every shape.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    data = getattr(resp, "data", None)  # SlackResponse stores the parsed body here
    if isinstance(data, dict):
        return str(data.get("error", "") or "")
    if isinstance(resp, dict):
        return str(resp.get("error", "") or "")
    getter = getattr(resp, "get", None)
    if callable(getter):
        try:
            return str(getter("error", "") or "")
        except Exception:
            return ""
    return ""


# Whole-chat not-found phrases. The registry records WHOLE-CHAT deaths only, so
# the string fallback must not treat a thread/topic/message/sub-resource
# not-found ("message not found", "thread not found", "topic_deleted",
# "message_id_invalid") as a dead chat — that would suppress all future
# delivery to a reachable origin until a send happens to succeed and clear it.
_CHAT_LEVEL_NOT_FOUND_PHRASES = (
    "chat not found",
    "channel not found",
    "unknown channel",
)


def _is_chat_level_not_found(msg: str) -> bool:
    """True only for WHOLE-CHAT not-found text (already lower-cased).

    Thread/topic/message/sub-resource not-found returns False — a deleted topic
    or a missing message does not mean the parent chat is unreachable.
    """
    return any(phrase in msg for phrase in _CHAT_LEVEL_NOT_FOUND_PHRASES)


def _classify_provider(exc: BaseException) -> str | None:
    """Classify by provider exception TYPE only (no string matching).

    Provider exception types are imported defensively — the SDKs may be absent.
    """
    try:
        from telegram.error import BadRequest as TgBadRequest
        from telegram.error import Forbidden as TgForbidden

        if isinstance(exc, TgForbidden):
            return "forbidden"
        if isinstance(exc, TgBadRequest) and _is_chat_level_not_found(str(exc).lower()):
            return "not_found"
    except ImportError:
        pass
    try:
        from discord.errors import Forbidden as DcForbidden
        from discord.errors import NotFound as DcNotFound

        if isinstance(exc, DcForbidden):
            return "forbidden"
        if isinstance(exc, DcNotFound):
            return "not_found"
    except ImportError:
        pass
    try:
        from slack_sdk.errors import SlackApiError

        if isinstance(exc, SlackApiError):
            err = _slack_error_code(exc)
            if err == "channel_not_found":
                return "not_found"
            if err in {"is_archived", "account_inactive", "not_in_channel",
                       "restricted_action"}:
                return "forbidden"
    except ImportError:
        pass
    return None


def _classify_message(msg: str) -> str | None:
    """Classify by error-message text (last-resort fallback). ``msg`` is already
    lower-cased. Not-found is narrowed to whole-chat phrases (F1)."""
    if any(s in msg for s in ("forbidden", "blocked", "kicked", "deactivated")):
        return "forbidden"
    if _is_chat_level_not_found(msg):
        return "not_found"
    return None


def _error_chain(exc: BaseException, *, max_depth: int = 10) -> list[BaseException]:
    """Ordered [exc, cause-or-context, …] chain. Bounded depth + cycle-safe.

    An adapter often wraps the real provider error in its own delivery error
    (e.g. ``TelegramDeliveryError`` raised ``from telegram.error.Forbidden``);
    the wrapped cause carries the classifiable detail.
    """
    seen: set[int] = set()
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    for _ in range(max_depth):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    return chain


def classify_send_error(exc: BaseException) -> str | None:
    """Classify a send exception as a permanent whole-chat death.

    Returns ``"forbidden"`` / ``"not_found"`` for a permanent whole-chat death,
    or ``None`` for a transient/other error (which is NOT recorded). Walks the
    ``__cause__`` / ``__context__`` chain (bounded, cycle-safe) so a provider
    error wrapped in an adapter's own delivery error still classifies. Provider
    exception TYPES are matched first (across the whole chain), then a
    string-match fallback for wrapped/generic errors.
    """
    chain = _error_chain(exc)
    # Provider exception TYPES first, across the whole chain.
    for e in chain:
        kind = _classify_provider(e)
        if kind is not None:
            return kind
    # String fallback across the chain (covers wrapped/generic errors like a
    # TelegramDeliveryError whose cause carries "bot was blocked" text).
    for e in chain:
        kind = _classify_message(str(e).lower())
        if kind is not None:
            return kind
    return None  # transient / sub-resource — do NOT record (per Hermes doctrine)
