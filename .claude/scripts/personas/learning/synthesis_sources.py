"""Physical legacy inputs and projections for the shared cognitive journal.

The collector never advances a watermark. Offsets are Unicode character offsets
over the exact decoded file, revisions are SHA-256 of that text. Budgeting and
successful consumption belong to synthesis, so an omitted file remains eligible.
Legacy prose is derived context, never an invented direct observation.
"""

from __future__ import annotations

import hashlib
import stat
from datetime import UTC, datetime
from pathlib import Path

from .models import LearningError, content_hash


def text_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _owned_path(root: Path, path: Path) -> Path:
    root = Path(root).absolute()
    path = Path(path).absolute()
    if not path.is_relative_to(root):
        raise LearningError("synthesis source escaped persona memory")
    for node in (path, *path.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise LearningError("synthesis sources cannot traverse links")
    return path


def collect_sources(service, kind: str, *, now=None) -> list[dict]:
    """Read bounded source families; do not exclude old unconsumed excerpts.

    No rolling-time floor is used: partial and deferred episodes must not expire
    while a provider is unavailable. The core's revision/range cursor filters
    already-consumed material and bounds what reaches a model.
    """
    if kind not in {"reflection", "dream"}:
        raise LearningError("unknown synthesis kind")
    from personas.experience import PERSONA_NOTE_DIRS

    root = Path(service.target.memory_dir)
    result = []
    families = (("episodes", "legacy_episode"), ("daily", "legacy_daily")) + tuple(
        (name, "legacy_work_note") for name in PERSONA_NOTE_DIRS
    )
    for directory, source_kind in families:
        folder = _owned_path(root, root / directory)
        if not folder.is_dir():
            continue
        for candidate in sorted(folder.glob("*.md")):
            path = _owned_path(root, candidate)
            try:
                text = path.read_bytes().decode("utf-8")
                modified = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
            except FileNotFoundError:
                continue  # A concurrent removal consumes nothing.
            if not text.strip():
                continue
            if source_kind == "legacy_episode":
                # Consolidated legacy episodes were already closed before the
                # cutover. New revisions reopen through the episode writer.
                from episodes import read_episode_frontmatter

                if read_episode_frontmatter(path).get("status") != "open":
                    continue
            relative = path.relative_to(root.absolute()).as_posix()
            result.append(
                {
                    "ref": f"memory:{service.target.persona_id}:{relative}",
                    "revision": text_revision(text),
                    "kind": source_kind,
                    "text": text,
                    "evidence_ids": [],
                    "source_time": modified,
                    "start": 0,
                    "end": len(text),
                    "complete": True,
                    "metadata": {
                        "relative_path": relative,
                        "persona_id": service.target.persona_id,
                        "derived": True,
                        "evidence_role": "legacy_context_only",
                        "source_length": len(text),
                    },
                }
            )
    # Preserve identity and explicit manual context without treating an edit as
    # independently observed experience or a reason to run another model call.
    for name in ("SOUL.md", "SELF.md", "MEMORY.md", "GOALS.md", "WORKING.md", "HEARTBEAT.md"):
        path = _owned_path(root, root / name)
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8")
        if not text.strip():
            continue
        result.append(
            {
                "ref": f"memory:{service.target.persona_id}:{name}",
                "revision": text_revision(text),
                "kind": "identity_context",
                "text": text,
                "evidence_ids": [],
                "source_time": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                "start": 0,
                "end": len(text),
                "complete": True,
                "metadata": {
                    "relative_path": name,
                    "persona_id": service.target.persona_id,
                    "always_context": True,
                    "evidence_role": "context_only",
                    "source_length": len(text),
                },
            }
        )
    return result


def _covers(text: str, revision: str, manifests: list[dict]) -> bool:
    if text_revision(text) != revision:
        return False
    cursor = 0
    for item in sorted(manifests, key=lambda row: row.get("start", -1)):
        start, end = item.get("start"), item.get("end")
        if type(start) is not int or type(end) is not int:
            return False
        if not 0 <= start <= end <= len(text) or item.get("text") != text[start:end]:
            return False
        if start > cursor:
            return False
        cursor = max(cursor, end)
    return cursor == len(text)


def project_completed_synthesis(service, cycle_id: str, result, manifest: list[dict]) -> dict:
    """Close only fully consumed, physically unchanged dream episode revisions.

    Durable successful manifests from earlier chunks participate in coverage.
    This callback is repeat-safe after a crash between the physical flip and
    the core's projection acknowledgement. Reflection never closes episodes.
    """
    cycle = service.store.get(cycle_id)
    if not cycle or cycle.get("kind") != "synthesis_cycle":
        raise LearningError("projection requires a journal synthesis cycle")
    if cycle.get("synthesis_kind", cycle.get("mode", cycle.get("phase"))) != "dream":
        return {"status": "not_dream", "consolidated": 0}
    consumption = next(
        (
            event["payload"]
            for event in service.store.events(cycle_id)
            if event.get("event_type") == "synthesis_consumed"
        ),
        None,
    )
    if consumption is None or content_hash(consumption.get("manifest")) != content_hash(manifest):
        raise LearningError("episode projection requires its exact durable consumption receipt")
    from episodes import mark_episodes_consolidated

    successful = []
    for row in service.store.all("synthesis_cycle"):
        if row.get("synthesis_kind", row.get("mode", row.get("phase"))) != "dream":
            continue
        for event in service.store.events(row["id"]):
            if event.get("event_type") == "synthesis_consumed":
                successful.extend(event.get("payload", {}).get("manifest", []))
    # The caller invokes us only after its durable success/consumption receipt.
    successful.extend(manifest)
    count = 0
    for item in manifest:
        if item.get("kind") != "legacy_episode":
            continue
        metadata = item.get("metadata", {})
        if metadata.get("persona_id") != service.target.persona_id:
            raise LearningError("episode projection belongs to another persona")
        relative = metadata.get("relative_path", "")
        root = Path(service.target.memory_dir)
        path = _owned_path(root, root / relative)
        if path.parent != root.absolute() / "episodes" or path.suffix != ".md":
            raise LearningError("invalid episode projection path")
        if not path.exists():
            continue
        chunks = [
            row
            for row in successful
            if row.get("ref") == item["ref"] and row.get("revision") == item["revision"]
        ]
        text = path.read_bytes().decode("utf-8")
        if _covers(text, item["revision"], chunks):
            count += mark_episodes_consolidated(
                [path], expected_revisions={str(path): item["revision"]}
            )
    return {"status": "projected", "consolidated": count}


def _debrief_text(result) -> str:
    if isinstance(result, str):
        return result.strip()
    output = result.get("output", result) if isinstance(result, dict) else {}
    summary = str(output.get("conclusion", output.get("summary", ""))).strip()
    understanding = output.get("understanding", [])
    questions = output.get("investigations", [])
    decisions = [str(row.get("content", "")).strip() for row in understanding]
    threads = [str(row.get("question", "")).strip() for row in questions]
    if not summary and not any(decisions) and not any(threads):
        return ""
    return (
        f"## Summary\n{summary}\n\n## Key Decisions\n"
        + "\n".join(f"- {text}" for text in decisions if text)
        + "\n\n## Open Threads\n"
        + "\n".join(f"- {text}" for text in threads if text)
    )


def project_completed_debrief(service, cycle_id: str, result) -> dict:
    """Project one completed debrief into the existing episode/daily readers.

    The immutable cycle identity is the idempotency key in BOTH physical files,
    so a retry after writing either file adds neither a new episode nor a second
    daily entry. Model prose remains explicitly derived, with root evidence IDs.
    """
    cycle = service.store.get(cycle_id)
    if not cycle or cycle.get("kind") != "cognitive_cycle":
        raise LearningError("debrief projection requires a journal cognitive cycle")
    if cycle.get("status") not in {"retained", "completed"}:
        raise LearningError("debrief projection requires completed reasoning")
    text = _debrief_text(result)
    if not text or text == "FLUSH_OK":
        return {"status": "no_change"}
    from episodes import write_episode_from_flush
    from shared import atomic_write_text, file_lock

    stamp = datetime.fromisoformat(cycle["created_at"].replace("Z", "+00:00"))
    identifier = content_hash(cycle_id)
    provenance = cycle.get("trigger_provenance", [])
    surface = provenance[0].get("surface", "code") if provenance else "code"
    if surface not in {"telegram", "discord", "slack", "whatsapp", "web", "cli", "talk"}:
        surface = "code"
    filename = f"session-flush-{surface}-{identifier}-{stamp:%Y%m%d}-{stamp:%H%M%S}.md"
    root = Path(service.target.memory_dir)
    marker = f"<!-- learning-debrief:{identifier} -->"
    references = ", ".join(cycle.get("evidence_ids", []))
    projection = (
        text + f"\n\nDerived from cognitive cycle {cycle_id}; source evidence: {references}.\n"
    )
    status, episode_path = write_episode_from_flush(
        root,
        context_filename=filename,
        response_text=projection,
        now=stamp,
        persona_id=service.target.persona_id,
        projection_id=identifier,
    )
    daily = _owned_path(root, root / "daily" / f"{stamp:%Y-%m-%d}.md")
    daily.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(daily, timeout=5.0):
        previous = daily.read_text(encoding="utf-8") if daily.exists() else f"# {stamp:%Y-%m-%d}\n"
        if marker not in previous:
            atomic_write_text(
                daily, previous.rstrip() + f"\n\n{marker}\n## Session Debrief\n\n{projection}\n"
            )
    return {
        "status": "projected",
        "episode_status": status.value,
        "episode_path": str(episode_path) if episode_path else None,
        "daily_path": str(daily),
        "cycle_id": cycle_id,
    }
