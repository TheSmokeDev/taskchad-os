"""Durable session-end delivery independent of the learning SQLite stores.

An end hook saves a bounded, redacted replay envelope before consulting the
learning DB. Session deletion can therefore proceed through a DB outage. The
normal learner discovery replays pending envelopes and deletes one only after
its cognitive cycle is durable. Core source keys make crash/retry idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

from .models import LearningError, canonical_json

_LOG = logging.getLogger(__name__)
_LIMIT = 16000
_FULL_LIMIT = 8_000_000
_EXCERPT_LIMIT = 3500
_FIELDS = frozenset(
    {
        "version",
        "persona_id",
        "session_id",
        "surface",
        "reason",
        "transcript",
        "transcript_hash",
        "transcript_truncated",
    }
)
_FILE = re.compile(r"^[0-9a-f]{64}\.json$")


def _hash(value) -> str:
    # Same full-transcript identity as the original lifecycle capture hook.
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _directory(service) -> Path:
    root = Path(os.path.abspath(service.target.state_dir))
    directory = root / "learning-lifecycle-outbox"
    for path in (directory, *directory.parents):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise LearningError("lifecycle outbox cannot traverse links")
    return directory


def _validate(service, payload: dict, *, identifier: str | None = None) -> str:
    if (
        not isinstance(payload, dict)
        or set(payload) != _FIELDS
        or payload["version"] not in {1, 2, 3}
    ):
        raise LearningError("invalid lifecycle replay envelope")
    if payload["persona_id"] != service.target.persona_id:
        raise LearningError("lifecycle replay belongs to another persona")
    for key, limit in (
        ("session_id", 1024),
        ("surface", 100),
        ("reason", 256),
        ("transcript", _FULL_LIMIT if payload["version"] == 3 else _LIMIT),
    ):
        value = payload[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise LearningError(f"invalid lifecycle replay {key}")
    if (
        not isinstance(payload["transcript_truncated"], bool)
        or not isinstance(payload["transcript_hash"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["transcript_hash"])
    ):
        raise LearningError("invalid lifecycle replay provenance")
    expected = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    if identifier is not None and identifier != expected:
        raise LearningError("lifecycle replay content changed")
    return expected


def _read(service, path: Path) -> dict:
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise LearningError("lifecycle outbox entry cannot be a link")
    if not _FILE.fullmatch(path.name) or path.stat().st_size > _FULL_LIMIT * 6 + 10000:
        raise LearningError("invalid lifecycle outbox entry")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate(service, payload, identifier=path.stem)
    return payload


def canonical_session_transcript(transcript: str) -> str:
    """Exclude transport timestamps and private thought blocks from JSONL identity."""
    text = str(transcript).strip().replace("\r\n", "\n")
    try:
        entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (ValueError, TypeError):
        return text
    if not entries or any(not isinstance(entry, dict) for entry in entries):
        return text
    if len(entries) == 1 and entries[0].get("format") == "homie-session-v2":
        entries = entries[0].get("messages", [])
        if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
            raise LearningError("invalid canonical session transcript")
    messages = []
    for entry in entries:
        message = entry.get("message", entry)
        if not isinstance(message, dict) or message.get("role") not in {
            "user",
            "assistant",
            "tool",
        }:
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        messages.append(
            {
                "role": message["role"],
                "content": content.strip(),
                "created_at": entry.get("created_at", entry.get("timestamp")),
                "source_message_id": entry.get("source_message_id", entry.get("uuid")),
                "source_ref": entry.get("source_ref"),
                "source_revision": entry.get("source_revision"),
                "tool_calls": entry.get("tool_calls", []),
            }
        )
    return canonical_json({"format": "homie-session-v2", "messages": messages})


def _make_payload(
    service, *, persona_id: str, session_id: str, surface: str, transcript: str, reason: str
) -> dict:
    from security.redact import redact_sensitive_text

    if persona_id != service.target.persona_id:
        raise LearningError("session debrief service belongs to another persona")
    original = canonical_session_transcript(transcript)
    payload = {
        "version": 3,
        "persona_id": persona_id,
        "session_id": redact_sensitive_text(str(session_id)),
        "surface": redact_sensitive_text(str(surface)),
        "reason": redact_sensitive_text(str(reason)),
        "transcript": redact_sensitive_text(original),
        "transcript_hash": _hash(original),
        "transcript_truncated": False,
    }
    _validate(service, payload)
    return payload


def persist_session_debrief(service, **values) -> Path:
    """Write/fsync an immutable replay record before any learning DB access."""
    payload = _make_payload(service, **values)
    identifier = _validate(service, payload)
    directory = _directory(service)
    directory.mkdir(parents=True, exist_ok=True)
    _directory(service)
    path = directory / f"{identifier}.json"
    if path.exists():
        _read(service, path)
        return path
    descriptor, filename = tempfile.mkstemp(prefix=".outbox-", dir=directory)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(payload))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Both publish-without-overwrite paths leave concurrent hooks with
            # one complete file, never a partially written JSON envelope.
            if os.name == "nt":
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            _read(service, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _source_ranges(text: str) -> list[dict]:
    """Map canonical message identities to literal source ranges before batching."""
    try:
        value = json.loads(text)
    except ValueError:
        return []
    if not isinstance(value, dict) or value.get("format") != "homie-session-v2":
        return []
    ranges, cursor = [], 0
    for message in value.get("messages", []):
        if not isinstance(message, dict):
            continue
        encoded = canonical_json(message)
        start = text.find(encoded, cursor)
        if start < 0:
            raise LearningError("canonical message range is missing")
        cursor = start + len(encoded)
        if message.get("source_ref") and message.get("source_revision"):
            ranges.append(
                {
                    "start": start,
                    "end": cursor,
                    "source_ref": message["source_ref"],
                    "source_revision": message["source_revision"],
                }
            )
    return ranges


def _ingest_chunks(service, payload: dict, binding_key: str) -> dict:
    """All source portions become durable before the outbox is acknowledged."""
    text, revision = payload["transcript"], payload["transcript_hash"]
    origin = f"{payload['session_id']}:{revision}"
    ranges = _source_ranges(text)
    experience = service.capture_experience(
        origin,
        "session_debrief",
        f"Debrief session {payload['session_id']}; original transcript has {len(text)} characters.",
        metadata={
            "capture_scope": "host",
            "session_id": payload["session_id"],
            "transcript_hash": revision,
            "source_chars": len(text),
            "evidence_role": "source_container",
            "source_evidence": [],
        },
    )
    observations = []
    for start in range(0, len(text), _EXCERPT_LIMIT):
        end = min(len(text), start + _EXCERPT_LIMIT)
        sources = [
            {key: row[key] for key in ("source_ref", "source_revision")}
            for row in ranges
            if row["start"] < end and row["end"] > start
        ]
        observation = service.record_observation(
            experience["id"],
            {
                "quality": "direct",
                "status": "partial",
                "domain_outcome_observed": False,
                "evidence": {
                    "kind": "session_transcript_excerpt",
                    "text": text[start:end],
                    "transcript_hash": revision,
                    "source_chars": len(text),
                    "start": start,
                    "end": end,
                    "source_evidence": sources,
                    "evidence_role": "session_source_bundle",
                },
            },
            source_key=f"{revision}:{start}:{end}",
        )
        observations.append(observation["id"])
    cycles = []
    for offset in range(0, len(observations), 4):
        cycle = service.enqueue_cognitive_cycle(
            "reflect",
            f"{origin}:batch:{offset // 4}",
            [experience["id"], *observations[offset : offset + 4]],
            experience_id=experience["id"],
            metadata={
                "host_event": True,
                "transcript_hash": revision,
                "batch_index": offset // 4,
                "batch_count": (len(observations) + 3) // 4,
            },
        )
        cycles.append(cycle["id"])
    receipt = {
        "experience_id": experience["id"],
        "cognitive_cycle_id": cycles[0],
        "cognitive_cycle_ids": cycles,
        "source_chars": len(text),
        "source_fully_retained": True,
        "observation_ids": observations,
    }
    service.store.set_setting(binding_key, receipt)
    return {"status": "queued", **receipt}


def _ingest_payload(service, payload: dict) -> dict:
    _validate(service, payload)
    if not service.enabled():
        return {"status": "disabled"}
    revision = payload["transcript_hash"]
    origin = f"{payload['session_id']}:{revision}"
    binding_key = "session-debrief:" + _hash([payload["session_id"], revision])
    prior = service.store.setting(binding_key)
    provenance = {key: payload[key] for key in ("surface", "reason", "session_id", "version")}
    if prior is not None:
        experience = service._owned(prior["experience_id"], "experience")
        service._owned(prior["cognitive_cycle_id"], "cognitive_cycle")
        service.store.event(
            experience["id"], "lifecycle_trigger", provenance, key=_hash(provenance)
        )
        return {"status": "queued", **prior}
    if payload["version"] == 3 and len(payload["transcript"]) > _EXCERPT_LIMIT:
        return _ingest_chunks(service, payload, binding_key)
    legacy = payload["version"] == 1
    source_evidence = []
    if not legacy:
        try:
            transcript = json.loads(payload["transcript"])
            if isinstance(transcript, dict) and transcript.get("format") == "homie-session-v2":
                source_evidence = [
                    {"source_ref": row["source_ref"], "source_revision": row["source_revision"]}
                    for row in transcript.get("messages", [])
                    if isinstance(row, dict)
                    and row.get("source_ref")
                    and row.get("source_revision")
                ]
        except ValueError:
            pass  # A truncated/plain transcript has unknown individual source identities.
    experience = service.capture_experience(
        origin,
        payload["surface"] if legacy else "session_debrief",
        payload["transcript"],
        metadata={
            "capture_scope": "host",
            **({"reason": payload["reason"]} if legacy else {"session_id": payload["session_id"]}),
            "transcript_hash": revision,
            "transcript_truncated": payload["transcript_truncated"],
            **(
                {"source_evidence": source_evidence, "evidence_role": "session_source_bundle"}
                if not legacy
                else {}
            ),
        },
    )
    observation = service.record_observation(
        experience["id"],
        {
            "quality": "direct",
            "status": "partial",
            "domain_outcome_observed": False,
            "evidence": {
                "kind": "session_transcript",
                "text": payload["transcript"],
                "transcript_hash": revision,
                **(
                    {"source_evidence": source_evidence, "evidence_role": "session_source_bundle"}
                    if not legacy
                    else {}
                ),
                **({"reason": payload["reason"]} if legacy else {}),
            },
        },
        source_key=revision,
    )
    cycle = service.enqueue_cognitive_cycle(
        "reflect",
        origin,
        [experience["id"], observation["id"]],
        experience_id=experience["id"],
        metadata={"surface": payload["surface"], "reason": payload["reason"], "host_event": True},
    )
    receipt = {
        "cognitive_cycle_id": cycle["id"],
        "experience_id": experience["id"],
    }
    service.store.event(experience["id"], "lifecycle_trigger", provenance, key=_hash(provenance))
    service.store.set_setting(binding_key, receipt)
    return {"status": "queued", **receipt}


def deliver_pending(service, path: Path) -> dict:
    """Ingest exactly the saved envelope, retaining it on pause or DB failure."""
    directory = _directory(service)
    if path.parent != directory:
        raise LearningError("lifecycle replay escaped persona outbox")
    payload = _read(service, path)
    try:
        receipt = _ingest_payload(service, payload)
    except Exception as exc:
        _LOG.warning("session debrief retained in lifecycle outbox: %s", type(exc).__name__)
        return {
            "status": "deferred",
            "outbox_id": path.stem,
            "reason": "learning_unavailable",
            "error_type": type(exc).__name__,
        }
    if receipt["status"] != "queued":
        return receipt | {"outbox_id": path.stem}
    # The cycle is committed. A failed acknowledgement removal is safe to retry.
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _LOG.warning("lifecycle outbox acknowledgement retained: %s", type(exc).__name__)
    return receipt


def enqueue_session_debrief(service, **values) -> dict:
    try:
        path = persist_session_debrief(service, **values)
    except OSError:
        # If the separate outbox filesystem is unavailable, a committed DB cycle
        # is still a durable delivery. Only losing both destinations blocks clear.
        try:
            receipt = _ingest_payload(service, _make_payload(service, **values))
            if receipt["status"] == "queued":
                return receipt
        except Exception:
            pass
        raise LearningError("session debrief could not be retained; session must remain") from None
    return deliver_pending(service, path)


def replay_pending(service) -> dict:
    """Replay bounded pending end hooks; malformed entries stay visibly rejected."""
    directory = _directory(service)
    result = {"delivered": 0, "pending": 0, "rejected": 0}
    if not directory.exists():
        return result
    for path in sorted(directory.glob("*.json")):
        try:
            receipt = deliver_pending(service, path)
            result["delivered" if receipt["status"] == "queued" else "pending"] += 1
        except (LearningError, OSError, ValueError) as exc:
            result["rejected"] += 1
            _LOG.warning("lifecycle replay rejected: %s", type(exc).__name__)
    return result
