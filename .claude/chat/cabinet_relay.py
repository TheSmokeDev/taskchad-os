"""Cabinet → chat relay.

Bridges the (previously dashboard-only) Cabinet multi-persona conversation
back into the originating chat (Discord / Telegram / any adapter). When an
operator runs ``/standup``, ``/discuss``, or ``/cabinet`` from a chat adapter,
the matching handler calls :func:`ensure_relay`, which spawns a background
task that subscribes to the cabinet SSE stream — via the EXISTING
``integrations.cabinet_api.stream_meeting`` client (the same one the dashboard
and the voice subprocess use) — and posts each completed persona turn
(an ``agent_done`` event) back into the originating channel through the
adapter's existing ``send()``.

Design:

* **One relay task per meeting**, deduped by ``meeting_id``. A meeting already
  being relayed is never double-subscribed — this covers ``/cabinet create``
  followed by one or more ``/cabinet send``.
* **Origin captured in the closure.** The originating adapter + channel are
  passed into the task; there is NO global origin registry and NO new
  cross-process state.
* **Fail-open at every seam.** A relay failure (stream error, send error,
  disabled/unreachable API, no running loop) NEVER propagates to the handler
  or crashes the bot. The handler's own confirmation reply is unaffected.
* **Cross-process boundary intact.** This consumes the orchestration API's
  HTTP SSE; it does NOT import ``cabinet.text_orchestrator`` directly
  (matches the Cross-process invariant in ``.claude/sections/02_chat_interface.md``).

Rule 1: relay settings are resolved at call time via
``config.get_cabinet_relay_settings()`` (no import-time binding).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Module-level dedup guard: meeting ids with a live relay task. asyncio is
# single-threaded, so the membership test + add inside ensure_relay is
# race-free (there is no ``await`` between the check and the add).
_active_relays: set[int] = set()

# Per-meeting high-water SSE seq. If a relay task dies mid-meeting (transient
# stream error / API restart) and a later /cabinet send re-spawns a task, the
# new subscription resumes AFTER this seq instead of replaying the server's
# buffered history from 0 and double-posting already-delivered turns. Cleared
# when the meeting ends (no resume possible/needed past that point).
_meeting_high_seq: dict[int, int] = {}

# Lazily-constructed shared dead-target registry. A deleted/blocked origin
# channel is proven dead once (a send raised a permanent whole-chat error) and
# skipped on subsequent fan-outs, self-healing the instant a send succeeds. The
# accessor is fail-open: if the registry can't be built the relay behaves
# exactly as before (no skip, no mark, no clear).
_dead_registry: Any = None


def _get_dead_registry() -> Any:
    """Return the shared DeadTargetRegistry, building it once. Fail-open: on any
    error (import/construct) returns None so relay delivery is never affected."""
    global _dead_registry
    if _dead_registry is not None:
        return _dead_registry
    try:
        from orchestration.dead_targets import DeadTargetRegistry  # scripts/ on chat path

        _dead_registry = DeadTargetRegistry()
    except Exception:  # noqa: BLE001 — fail-open: no registry → relay unchanged
        return None
    return _dead_registry


def _origin_target(origin: Any) -> tuple[str, str] | None:
    """(platform, chat_id) key for the dead-target registry, mirroring
    ``Channel.unified_id`` composition. None when the origin is unusable."""
    try:
        return origin.platform.value, str(origin.platform_id)
    except Exception:  # noqa: BLE001 — malformed origin never breaks relay
        return None


def _note_send_success(origin: Any) -> None:
    """Self-heal: a successful send clears any dead flag on the origin. Fail-open
    — a registry fault must never change ``_safe_send``'s result."""
    try:
        reg = _get_dead_registry()
        target = _origin_target(origin)
        if reg is not None and target is not None:
            reg.clear(*target)
    except Exception:  # noqa: BLE001 — fail-open
        pass


def _note_send_failure(origin: Any, exc: BaseException) -> None:
    """Record the origin as dead only on a permanent whole-chat death
    (forbidden / chat-not-found). Transient errors are NOT recorded. Fail-open
    — a registry fault must never change ``_safe_send``'s result."""
    try:
        from orchestration.dead_targets import DeadTargetRegistry, classify_send_error

        kind = classify_send_error(exc)
        if not DeadTargetRegistry.is_dead_error_kind(kind):
            return
        reg = _get_dead_registry()
        target = _origin_target(origin)
        if reg is not None and target is not None:
            reg.mark_dead(*target, reason=str(exc)[:200])
    except Exception:  # noqa: BLE001 — fail-open
        pass


def _display_label(agent_id: str) -> str:
    """Human label for a persona id (``seo_content`` -> ``Seo Content``)."""
    if not agent_id:
        return "Homie"
    if agent_id == "default":
        return "Main"
    return agent_id.replace("_", " ").replace("-", " ").strip().title() or "Homie"


def ensure_relay(meeting_id: int, adapter: Any, incoming: Any) -> bool:
    """Ensure a relay task is running for ``meeting_id``.

    Returns ``True`` when a relay is active for this meeting after the call
    (either already running, or freshly spawned) — the handler uses this to
    decide whether to promise "the homies will answer here" or fall back to
    the dashboard-URL message. Returns ``False`` when no relay will run
    (disabled, missing adapter/channel, or no running event loop).

    Side-effect-only and fail-open: any problem here is swallowed so the
    calling handler's confirmation reply is never affected.
    """
    try:
        if meeting_id in _active_relays:
            return True

        # Need a sendable adapter + an origin channel to relay into.
        origin = getattr(incoming, "channel", None)
        send = getattr(adapter, "send", None)
        if origin is None or send is None:
            return False

        import config  # lazy: scripts/ is on the chat sys.path
        settings = config.get_cabinet_relay_settings()
        if not settings.enabled:
            return False

        # Spawn under the running loop; reserve the dedup slot FIRST so a
        # second ensure_relay in the same tick can't race a duplicate.
        loop = asyncio.get_running_loop()
        _active_relays.add(meeting_id)
        loop.create_task(
            _relay_meeting(meeting_id, adapter, origin, settings.max_turns)
        )
        return True
    except RuntimeError:
        # No running event loop (e.g. a sync call path) — nothing to relay.
        _active_relays.discard(meeting_id)
        return False
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("cabinet relay spawn failed (meeting %s): %s", meeting_id, exc)
        _active_relays.discard(meeting_id)
        return False


async def _relay_meeting(
    meeting_id: int, adapter: Any, origin: Any, max_turns: int
) -> None:
    """Stream ``meeting_id`` and post each ``agent_done`` turn to ``origin``.

    Stops on the ``meeting_ended`` event, stream EOF, the ``max_turns`` cap, or
    any error. Always clears the dedup guard on exit so a later meeting with
    the same id (or a retry) can be relayed again.
    """
    relayed = 0
    send_failures = 0
    try:
        # Skip a proven-dead origin (the Hermes short-circuit): a prior send
        # raised a permanent whole-chat error, so re-fanning-out here only
        # burns the platform's flood-control envelope. Self-heals on the next
        # successful send (see _note_send_success). Fail-open: a broken
        # registry never blocks a relay.
        reg = _get_dead_registry()
        target = _origin_target(origin)
        if reg is not None and target is not None:
            try:
                if reg.is_dead(*target):
                    logger.info(
                        "cabinet relay skipping proven-dead origin (meeting %s)",
                        meeting_id,
                    )
                    return
            except Exception:  # noqa: BLE001 — fail-open
                pass

        from integrations import cabinet_api  # lazy (inside try so finally always cleans up)

        # Resume after the last seq we already delivered (None on the first
        # subscribe → server replays from 0, which is correct when nothing has
        # been relayed yet). Prevents double-posting on re-subscribe.
        since = _meeting_high_seq.get(meeting_id)
        async for evt in cabinet_api.stream_meeting(meeting_id, since_seq=since):
            if not isinstance(evt, dict):
                continue
            seq = evt.get("seq")
            if isinstance(seq, int) and seq > _meeting_high_seq.get(meeting_id, 0):
                _meeting_high_seq[meeting_id] = seq
            inner = evt.get("event")
            if not isinstance(inner, dict):
                continue
            etype = inner.get("type")
            if etype == "meeting_ended":
                _meeting_high_seq.pop(meeting_id, None)  # meeting over — no resume past end
                break
            # Only completed persona replies are relayed. Operator turns,
            # typing/tool/turn_complete/state/error events are skipped.
            if etype != "agent_done" or inner.get("incomplete"):
                continue
            text = (inner.get("text") or "").strip()
            if not text:
                continue
            label = _display_label(str(inner.get("agentId") or ""))
            if await _safe_send(adapter, origin, f"**{label}:** {text}"):
                relayed += 1
                send_failures = 0
            else:
                # A persistently broken channel rejects every send — stop after
                # a few consecutive failures instead of dropping the whole
                # roster into a silent void (review finding 4).
                send_failures += 1
                if send_failures >= 3:
                    logger.error(
                        "cabinet relay stopping — channel rejected %d consecutive "
                        "sends (meeting %s)", send_failures, meeting_id,
                    )
                    break
            if max_turns > 0 and relayed >= max_turns:
                break
        # Greppable success breadcrumb — "relayed=0" after a standup is the one
        # signal that catches event-contract drift (review finding 3).
        logger.info("cabinet relay finished (meeting %s, relayed=%d)", meeting_id, relayed)
    except Exception:  # noqa: BLE001 — fail-open: never crash the bot
        # logger.exception keeps the traceback so a code-level bug is NOT
        # disguised as a benign "stream ended" (review finding 2).
        logger.exception("cabinet relay aborted (meeting %s, relayed=%d)", meeting_id, relayed)
        # The handler already told the operator "the homies will answer here";
        # if the stream died before ANY turn (e.g. the cabinet API is down), say
        # so in chat with the dashboard fallback instead of leaving them waiting
        # forever (review finding 1 — the promise/reality gap).
        if relayed == 0:
            url = f"http://localhost:3141/cabinet?id={meeting_id}"
            await _safe_send(
                adapter, origin,
                f"⚠️ Couldn't relay the cabinet answers here — watch {url}",
            )
    finally:
        _active_relays.discard(meeting_id)


async def _safe_send(adapter: Any, origin: Any, text: str) -> bool:
    """Send one relayed message; a send failure must not kill the stream.

    Returns True on success, False on failure, so the caller can detect a
    persistently broken channel and stop (review finding 4).
    """
    from models import OutgoingMessage  # lazy: flat chat import

    try:
        result = await adapter.send(OutgoingMessage(text=text, channel=origin))
    except Exception as exc:  # noqa: BLE001 — fail-open per-send
        logger.warning("cabinet relay send failed (channel send raised)")
        _note_send_failure(origin, exc)
        return False
    # A falsy result for NON-EMPTY text means the adapter swallowed a delivery
    # failure and returned no message id (e.g. slack.py catches chat_postMessage
    # errors, prints, and returns None). Treat it as a failure so we never
    # record a false success/clear — there is no exception to classify, so it is
    # a transient miss (no mark_dead), the key invariant being no false clear.
    if text.strip() and not result:
        logger.warning("cabinet relay send failed (adapter returned no message id)")
        return False
    _note_send_success(origin)
    return True
