"""Workflow authoring (US-013) — the LLM drafts, CODE validates, stamps, writes.

When a decision's action is ``author``, the model's drafted workflow YAML
(carried in ``decision["message"]``) lands here. CODE owns every write:

- :func:`validate_draft` — ``yaml.safe_load`` round-trip plus the required
  keys (``name``, non-empty ``nodes``); anything else is a
  :class:`WorkflowDraftError` and the authoring attempt is a no-op with a
  warning, never a partial file.
- :func:`stamp_workflow` — provider/model from the backend knob
  (``COFOUNDER_WORKFLOW_PROVIDER`` / ``COFOUNDER_WORKFLOW_MODEL``, Rule 1
  call-time) stamped at BOTH the workflow level and every loop-node level.
  Loop nodes ignore per-node provider (reference lesson — the engine reads
  the workflow-level value for them), so stamping both levels makes the knob
  win regardless of which one a given engine version resolves.
- :func:`author_workflow` — writes the stamped draft to
  ``<repo>/.archon/workflows/<name>.yaml`` (name sanitized so a hostile
  ``name:`` can never escape the workflows folder), atomic tmp + os.replace.
- :func:`restamp_workflow` — re-applies the stamp to an already-authored
  file; the pass calls this after every cycle so an LLM edit inside the repo
  can never drift the provider/model away from the knob. Unchanged files are
  left untouched (no rewrite churn).

Every public entrypoint is fail-open (Invariant 6): an invalid draft, an
unreadable file, or a resolver failure degrades to ``None``/``False`` with a
logged warning — never an exception into the pass.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

WORKFLOWS_SUBDIR = Path(".archon") / "workflows"
REQUIRED_KEYS = ("name", "nodes")
LOOP_KEY = "loop"

# Anything outside this set is folded to "-" in the file name; path
# separators can therefore never survive into the write target.
_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkflowDraftError(ValueError):
    """The drafted text is not one valid Archon workflow mapping."""


def validate_draft(draft_text: str) -> dict[str, Any]:
    """Parse and validate a drafted workflow; raises :class:`WorkflowDraftError`.

    The contract is the ``yaml.safe_load`` round-trip plus the required keys:
    a mapping with a non-empty string ``name`` and a non-empty list ``nodes``
    (the write side re-dumps via ``yaml.safe_dump``, which is the other half
    of the round-trip).
    """
    try:
        data = yaml.safe_load(draft_text or "")
    except yaml.YAMLError as exc:
        raise WorkflowDraftError(f"draft is not valid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise WorkflowDraftError("draft is not a YAML mapping")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowDraftError("draft is missing a non-empty 'name'")
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowDraftError("draft is missing a non-empty 'nodes' list")
    return data


def workflow_file_name(name: str) -> str:
    """A drafted ``name:`` folded into one safe file stem.

    Path separators and other unsafe characters become ``-``; leading dots
    and dashes are stripped so a hostile name (``../escape``) can never
    resolve outside the workflows folder or hide as a dotfile. A name with
    nothing left after sanitizing is rejected.
    """
    stem = _NAME_UNSAFE_RE.sub("-", (name or "").strip()).strip(".-")
    if not stem:
        raise WorkflowDraftError(f"workflow name {name!r} has no safe characters")
    return stem


def stamp_workflow(data: dict[str, Any], *, provider: str, model: str) -> dict[str, Any]:
    """Stamp provider/model at the workflow level AND every loop-node level.

    Pure in-place transform. Non-loop nodes keep whatever provider/model the
    draft gave them — only loop nodes (which the engine resolves from the
    workflow level, ignoring per-node provider) get the belt-and-suspenders
    node-level stamp.
    """
    data["provider"] = provider
    data["model"] = model
    nodes = data.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and LOOP_KEY in node:
                node["provider"] = provider
                node["model"] = model
    return data


def author_workflow(
    repo_path: Path | str,
    draft_text: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> Path | None:
    """Validate, stamp, and write one drafted workflow; ``None`` on any failure.

    Writes ``<repo>/.archon/workflows/<name>.yaml`` (name from the draft's own
    ``name:`` key, sanitized). Provider/model default from the backend knob at
    CALL time (Rule 1). Fail-open: an invalid draft or a write failure is a
    logged warning and ``None`` — never an exception, never a partial file
    (atomic tmp + os.replace).
    """
    try:
        provider, model = _resolve_backend(provider, model)
        data = validate_draft(draft_text)
        stamp_workflow(data, provider=provider, model=model)
        target = (
            Path(repo_path)
            / WORKFLOWS_SUBDIR
            / f"{workflow_file_name(str(data['name']))}.yaml"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, _dump(data))
        logger.info(
            "cofounder: authored workflow %s (provider=%s model=%s)",
            target,
            provider,
            model,
        )
        return target
    except WorkflowDraftError as exc:
        logger.warning("cofounder: workflow draft invalid (%s); no-op", exc)
        return None
    except Exception:
        logger.exception("cofounder: workflow authoring failed; no-op")
        return None


def restamp_workflow(
    path: Path | str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> bool:
    """Re-stamp an authored workflow file; True only when drift was overwritten.

    The after-pass drift guard: reloads the file, re-applies the backend-knob
    stamp at both levels, and rewrites ONLY when the stamped content differs
    (a clean file is never churned). Any failure — missing file, unreadable
    YAML, non-workflow content — degrades to ``False`` with a warning.
    """
    try:
        provider, model = _resolve_backend(provider, model)
        target = Path(path)
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            logger.warning(
                "cofounder: %s is not a workflow mapping; re-stamp skipped", target
            )
            return False
        before = _dump(data)
        stamp_workflow(data, provider=provider, model=model)
        after = _dump(data)
        if after == before:
            return False
        _atomic_write(target, after)
        logger.info(
            "cofounder: re-stamped workflow %s (provider=%s model=%s)",
            target,
            provider,
            model,
        )
        return True
    except Exception as exc:
        logger.warning("cofounder: workflow re-stamp failed for %s (%s)", path, exc)
        return False


def _resolve_backend(provider: str | None, model: str | None) -> tuple[str, str]:
    """The backend knob at CALL time (Rule 1) — None sentinels resolve config."""
    if provider is None or model is None:
        import config  # function-local: call-time env + cheap heartbeat import

        settings = config.get_cofounder_settings()
        if provider is None:
            provider = settings.workflow_provider
        if model is None:
            model = settings.workflow_model
    return provider, model


def _dump(data: dict[str, Any]) -> str:
    """One canonical YAML form so drift comparison is deterministic."""
    return yaml.safe_dump(data, sort_keys=False, width=4096)


def _atomic_write(path: Path, text: str) -> None:
    """Write atomically via tmp + os.replace (project_model pattern)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
