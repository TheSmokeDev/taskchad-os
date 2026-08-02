"""Gated Archon dispatch — the default-deny front door for voice-deployed work.

Epic: "Archon as the Execution Spine" (#252), ticket #256.
Architecture: ``PRDs/active/PRD-archon-execution-spine.architecture.md`` (F2,
F3, F5; Boundaries).

``talk_tools`` owns the tool surface and the run lifecycle; ``archon_client``
owns the HTTP transport. This module owns the layer between them — everything
that has to be true BEFORE a workflow is allowed to spend a worktree:

1. **Kill switch** — ``HOMIE_KILLSWITCH_ARCHON_DISPATCH`` (ships ON; the switch
   only ever turns the surface OFF, and a refusal is counted by
   ``security.kill_switches``).
2. **Capability gate** — ``require_integration_action("archon", "dispatch")``.
   Dispatch is deliberately model-initiable, and the declaration says so
   rather than claiming a confirmation this lane cannot prove. The operator's
   rule is tier-by-BLAST-RADIUS: work whose worst case is a worktree and some
   tokens fires on his word with no ceremony; money and outward mutations get
   drafted and then approved through an AUTHED channel where a tap is real.
   A dispatch is the former. The latter is gated where it actually happens —
   the workflow-level ``APPROVE SPEND`` nodes pause the run before any spend
   (see ``docs/manual/features/archon-steering-gates.md``), and routing that
   pause to Telegram/Discord is its own ticket. Anything reached through this
   module is therefore bounded to a clone, a worktree, and tokens.
3. **F2 prompt synthesis** — the ported competence. Archon does
   ``workflowPrompt = synthesizedPrompt ?? originalMessage``
   (``orchestrator-agent.ts:1953``), so a voice turn of "yeah, do that" would
   reach the worker verbatim. The worker starts in a FRESH worktree and never
   sees the conversation, so a brief that only points back at it is worthless.
   :func:`brief_refusal_reason` refuses those deterministically — the model is
   told to restate the task, and the vague string never reaches the client.
4. **Codebase binding** — Archon's conversation→codebase binding drives the
   whole isolation pre-flight, so a dispatch with no resolvable codebase is
   refused rather than sent blind.

Every one of those decisions writes an append-only audit row
(``DATA_DIR/archon_dispatch.jsonl``) — granted or refused — following the
``social/audit.py`` / ``cofounder/notify.py`` shape.

Steering (#259, F4)
-------------------

The same boundary, pointed at a run that is already going. :func:`steer_now`
and :func:`say_now` gate on ``HOMIE_KILLSWITCH_ARCHON_STEER`` + the declared
``archon.steer`` action, write attempt-then-result rows to
``DATA_DIR/archon_steer.jsonl``, and carry the one piece of competence a naive
caller gets wrong: **an approve is not a boolean.** Archon stores the approve
comment as the gate node's captured output, so a bare approve fails every
deterministic ``<gate>-check`` node — the phrase is read from the ledger at act
time (``integrations/archon_approvals``) and sent with it. Neither function
invents a primitive: the five verbs are Archon's own endpoints, and the
natural-language path is Archon's own "any non-slash message on a paused
conversation IS the approval".

Event loop (absolute)
---------------------

:func:`dispatch_now` is BLOCKING and raises if it finds a running event loop in
its thread. The 2026-07-13 wedge came from a long external call made on the
bot's loop; the rule here is structural, not a comment. Callers run it from a
``talk_runs`` worker thread (or ``asyncio.to_thread``) and reply immediately
with the ``WORK_STARTED`` receipt.

Anti-pattern compliance
-----------------------

* **Rule 1** — every knob (``ARCHON_CODEBASE_ID``, the brief floors, the audit
  path) is a ``None`` sentinel resolved inside the function body at CALL time.
* **Rule 2** — :func:`resolve_codebase_id` reads PHYSICAL state: the registered
  codebase rows in ``archon.db`` (``mode=ro``) matched against the real repo
  root on disk, including the ``.git`` worktree pointer. It does not trust a
  cached id or a config claim about which repo this is.
* **Rule 3** — ``kill_switches``, ``capabilities`` and ``archon_client`` are
  reached through module attributes, so a monkeypatch propagates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from integrations import archon_client, capabilities
from security import kill_switches

logger = logging.getLogger(__name__)

#: Operator kill switch. Ships ON — ``HOMIE_KILLSWITCH_ARCHON_DISPATCH=disabled``
#: is the only way to turn the surface off, and the refusal is counted.
KILL_SWITCH = "archon_dispatch"

#: Operator kill switch for the STEERING surface (#259). Ships ON, same as the
#: dispatch switch, and separate from it on purpose: the operator may want to
#: stop the Homie deploying new work without losing the ability to cancel work
#: already in flight.
STEER_KILL_SWITCH = "archon_steer"

#: The declared capability actions this module gates on.
INTEGRATION = "archon"
ACTION = "dispatch"
STEER_ACTION = "steer"

#: The steering verbs, mirroring Archon's own ``manage-run-tool.ts``
#: discriminator minus ``start`` (deploying is :func:`require_dispatch_allowed`'s
#: job) — these are exactly the five that map to an HTTP endpoint.
STEER_ACTIONS = frozenset({"approve", "reject", "resume", "cancel", "abandon"})

#: The subset that destroys in-flight work or refuses a gate. ``manage_run``
#: previews these and acts only on an explicit confirm — Archon's own tool
#: shape, and the framework's announce-then-act voice contract.
DESTRUCTIVE_STEER_ACTIONS = frozenset({"reject", "cancel", "abandon"})

#: Spoken result per action. Kept beside the verbs so a new action cannot ship
#: with a generic "done" that tells the operator nothing about what changed.
_STEER_SUCCESS_TEXT = {
    "approve": "Approved run {run_id} — it is moving again.",
    "reject": "Rejected the gate on run {run_id}.",
    "resume": "Resumed run {run_id}.",
    "cancel": "Cancelled run {run_id}. Whatever it had already written is still on disk.",
    "abandon": "Abandoned run {run_id}.",
}

#: Archon's platform conversation ids (``web-<ts>-<rand>``). Same shape
#: ``archon_client`` validates; checked here too so a caller bug is a
#: ``ValueError`` at the seam rather than a transport error later.
_CONVERSATION_ID_RE = re.compile(r"^[\w-]+$")

#: Archon run ids as they appear in the ledger.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

#: Correlation-key prefix. Predates this ticket as ``talk:<run_id>`` on the
#: convoy row's ``paperclip_issue_id`` external ref; the Archon ids are
#: APPENDED so existing ``talk:``-prefixed consumers keep matching.
CORRELATION_PREFIX = "talk"

#: Ids allowed inside a correlation ref. ``:`` is the field separator, so a
#: token carrying one would make the ref ambiguous — Archon ids are hex or
#: ``[\w-]+`` in practice, and anything else is refused rather than encoded.
_REF_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

#: Same tightness for an operator-supplied codebase id: it is interpolated
#: into a JSON body sent to a local service, and it arrives from env.
_CODEBASE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_DEFAULT_MIN_BRIEF_CHARS = 40
_DEFAULT_MIN_BRIEF_WORDS = 6

#: Phrases that point BACK at the conversation. Archon's own orchestrator
#: prompt names this class explicitly ("do NOT use vague references like 'do
#: what we discussed' or 'yes, go ahead'"), and it is exactly what a voice turn
#: produces. Stripped before the content count, so a brief made only of these
#: scores zero real words no matter how many syllables it has.
_REFERENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bwhat\s+(?:we|you|i)\s+(?:just\s+)?"
        r"(?:discussed|talked\s+about|said|mentioned|agreed(?:\s+on)?)\b",
        r"\bas\s+(?:we\s+)?(?:discussed|agreed|mentioned|said|above|planned)\b",
        r"\blike\s+(?:i|we|you)\s+said\b",
        r"\bper\s+(?:our|the)\s+(?:conversation|chat|discussion)\b",
        r"\bthe\s+(?:thing|one|plan|idea|stuff)\s+(?:we|you|i)\b",
        r"\b(?:do|run|build|make|fire|start|ship|kick\s+off)\s+"
        r"(?:that|this|it|the\s+thing)\b",
        r"\b(?:that|this)\s+(?:thing|one)\b",
        r"\bsame\s+as\s+(?:before|last\s+time)\b",
        r"\bthe\s+usual\b",
        r"\byou\s+know\s+what\s+(?:i|we)\s+mean\b",
        r"\bgo\s+ahead\s+with\s+(?:that|it|this)\b",
    )
)

#: Words that carry no task content. Deliberately conservative: it holds
#: assent, filler and pointers — never a verb or noun that could BE the task.
#: "build", "audit", "migrate" are not here; "do", "make", "go" are.
_FILLER_WORDS = frozenset(
    {
        "a", "about", "actually", "again", "ahead", "alright", "also", "an",
        "and", "any", "are", "as", "at", "be", "bro", "but", "by", "can",
        "cool", "could", "de", "do", "dude", "for", "from", "go", "gonna",
        "good", "happen", "have", "he", "her", "here", "him", "his", "homie",
        "i", "if", "im", "in", "into", "is", "it", "its", "just", "let",
        "lets", "like", "make", "man", "me", "my", "no", "not", "now", "of",
        "off", "ok", "okay", "on", "one", "or", "our", "out", "over", "please",
        "really", "right", "she", "should", "so", "some", "sure", "that",
        "the", "their", "them", "then", "there", "these", "they", "thing",
        "things", "this", "those", "to", "too", "uh", "um", "up", "us", "very",
        "was", "we", "well", "were", "what", "when", "will", "with", "would",
        "yea", "yeah", "yep", "yes", "you", "your", "yup",
    }
)

_WORD_RE = re.compile(r"\w[\w'./-]*", re.UNICODE)


# ---------------------------------------------------------------------------
# Errors — every message here is spoken to the operator verbatim
# ---------------------------------------------------------------------------


class ArchonDispatchError(Exception):
    """A gated Archon dispatch could not proceed. The message is speakable."""


class ArchonDispatchRefusedError(ArchonDispatchError):
    """Refused by the kill switch, the capability policy, or a contract check.

    Distinct from :class:`ArchonDispatchError` (which also covers a transport
    failure) so a caller can tell "we decided not to" from "Archon broke".
    """


# ---------------------------------------------------------------------------
# Config resolvers — Rule 1: resolved at CALL time, never bound as a default
# ---------------------------------------------------------------------------


def brief_floors() -> tuple[int, int]:
    """Return ``(min_chars, min_words)`` for a self-contained brief.

    Env: ``TALK_ARCHON_MIN_BRIEF_CHARS`` (40), ``TALK_ARCHON_MIN_BRIEF_WORDS``
    (6). A non-numeric or negative value falls back to the default with a
    receipt — a fat-fingered ``.env`` must not silently disable the F2 gate.
    """

    def _floor(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            logger.warning("talk_archon: ignoring %s=%r (not an int)", name, raw)
            return default
        if value <= 0:
            # Zero is not a smaller floor — it is the gate switched off, and
            # the F2 contract says a fat-fingered .env must never silently
            # disable it (codex R1 major: floors (0, 0) let "yeah do that"
            # through verbatim).
            logger.warning("talk_archon: ignoring %s=%r (must be >= 1)", name, raw)
            return default
        return value

    return (
        _floor("TALK_ARCHON_MIN_BRIEF_CHARS", _DEFAULT_MIN_BRIEF_CHARS),
        _floor("TALK_ARCHON_MIN_BRIEF_WORDS", _DEFAULT_MIN_BRIEF_WORDS),
    )


def archon_db_path(db_path: Path | str | None = None) -> Path:
    """Resolve the Archon ledger path (``TALK_ARCHON_DB``, else ``~/.archon``).

    Mirrors ``talk_tools._archon_settings()['db']`` so both readers agree on
    which ledger they are looking at.
    """

    if db_path is not None:
        return Path(db_path)
    override = (os.environ.get("TALK_ARCHON_DB") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".archon" / "archon.db"


def default_workflow() -> str:
    """Workflow used by the substantial branch of ``delegate_task`` (F5).

    Env: ``TALK_ARCHON_DEFAULT_WORKFLOW``. ``archon-ralph-dag`` is the repo's
    autonomous "implement this idea" DAG, which is what a delegated task the
    model judged substantial actually is.
    """

    return (os.environ.get("TALK_ARCHON_DEFAULT_WORKFLOW") or "").strip() or "archon-ralph-dag"


# ---------------------------------------------------------------------------
# Audit — append-only JSONL, one row per attempt (granted or refused)
# ---------------------------------------------------------------------------


def append_dispatch_audit_record(
    *,
    workflow: str,
    outcome: str,
    caller: str = "",
    brief_preview: str = "",
    conversation_id: str = "",
    conversation_db_id: str = "",
    codebase_id: str = "",
    run_id: int | None = None,
    error: str = "",
    confirm_nonce: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Append one audit row (``social/audit.py`` shape) and return its id.

    ``audit_path`` is a None sentinel resolved at call time to
    ``config.DATA_DIR / "archon_dispatch.jsonl"`` (Rule 1). Raises on an IO
    failure — :func:`_audit` is the best-effort wrapper callers use.
    """

    if audit_path is None:
        import config  # noqa: PLC0415 — lazy: DATA_DIR is test-monkeypatched

        audit_path = config.DATA_DIR / "archon_dispatch.jsonl"
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    preview = " ".join(str(brief_preview or "").split())[:160]
    record = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "integration": INTEGRATION,
        "action": ACTION,
        "workflow": workflow,
        "outcome": outcome,
        "caller": caller,
        "brief_preview": preview,
        "codebase_id": codebase_id,
        "conversation_id": conversation_id,
        "conversation_db_id": conversation_db_id,
        "run_id": run_id,
        "error": error,
        "confirm_nonce": confirm_nonce,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return f"{record['timestamp']}:{workflow}:{outcome}"


def audit_attempt(**fields: Any) -> None:
    """Best-effort audit append — a failed row never blocks the gate outcome.

    Same precedent as ``kill_switches`` and ``cofounder.notify``: the security
    decision matters more than its record, and every swallow leaves a receipt.
    """

    try:
        append_dispatch_audit_record(**fields)
    except Exception as exc:  # noqa: BLE001 — audit is a record, not the action
        logger.warning("talk_archon: audit write failed: %s: %s", type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# F2 — self-contained brief
# ---------------------------------------------------------------------------


def brief_refusal_reason(brief: str) -> str | None:
    """Return a speakable refusal, or ``None`` when the brief stands alone.

    The worker Archon spawns starts in a fresh worktree with no access to this
    conversation, so a brief is only usable if it carries the task itself.
    Two deterministic floors, both cleared before dispatch:

    * referential pointers ("do that", "what we discussed", "the usual") are
      stripped, so a brief made only of them counts zero real words;
    * what remains must clear both a character floor and a content-word floor
      (:func:`brief_floors`).

    Deliberately deterministic: an LLM asked "is this brief good enough?"
    would be the same LLM that just wrote the vague one.
    """

    text = str(brief or "").strip()
    min_chars, min_words = brief_floors()
    if not text:
        return (
            "I need the actual brief before I deploy anything — the Archon "
            "worker never sees this conversation."
        )

    lowered = text.lower()
    # A brief that POINTS BACK is refused on the pointer, at any length. The
    # earlier version only stripped the phrase before counting, so padding
    # carried it through: "Proceed with the plan above exactly as agreed,
    # ensuring all requirements are met" cleared the floors and reached the
    # worker verbatim (codex R4 major). The worker has no conversation, so a
    # reference to one is worthless however many words surround it.
    stripped = lowered
    referential = False
    for pattern in _REFERENTIAL_PATTERNS:
        stripped, hits = pattern.subn(" ", stripped)
        referential = referential or bool(hits)
    if referential:
        return (
            "That brief points back at what we said, and the Archon worker "
            "never sees this conversation — it starts in a fresh worktree "
            "knowing nothing. Restate the task standalone: what to build, "
            "where it lives, and what done looks like."
        )
    content_words = [
        word for word in _WORD_RE.findall(stripped) if word not in _FILLER_WORDS
    ]

    if len(text) < min_chars or len(content_words) < min_words:
        return (
            "That brief is too thin to deploy. The Archon worker starts in a "
            "fresh worktree and never sees this conversation, so pointing back "
            "at what we said gets a worker with no task. Restate the whole "
            "thing standalone — what to build, where it lives, and what done "
            f"looks like (at least {min_chars} characters and {min_words} real "
            "words) — and I'll fire it."
        )
    return None


# ---------------------------------------------------------------------------
# Correlation key — Homie run id <-> Archon conversation ids
# ---------------------------------------------------------------------------


def build_correlation_ref(
    run_id: int,
    conversation_db_id: str,
    conversation_id: str,
) -> str:
    """Build the convoy external ref that joins a Homie run to its Archon work.

    Format::

        talk:<run_id>:archon:<conversation_db_id>:conv:<platform_conversation_id>

    Both ids are carried because they answer different questions and Archon
    hands back both:

    * ``conversation_db_id`` is the JOIN key — a web-dispatched run puts the
      dispatching conversation in ``run.parent_conversation_id``, so this is
      how #257/#258 find the run from a ledger row.
    * ``conversation_id`` is the PLATFORM id — the only thing
      ``POST /api/conversations/{id}/message`` accepts, so it is what #259's
      natural-language steering needs.

    Raises:
        ValueError: non-positive ``run_id`` or an id carrying the ``:``
            separator (which would make the ref ambiguous to parse).
    """

    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise ValueError("run_id must be a positive int")
    for label, token in (
        ("conversation_db_id", conversation_db_id),
        ("conversation_id", conversation_id),
    ):
        if not isinstance(token, str) or not _REF_TOKEN_RE.match(token):
            raise ValueError(f"{label} {token!r} must match [A-Za-z0-9_.-]+")
    return (
        f"{CORRELATION_PREFIX}:{run_id}"
        f":archon:{conversation_db_id}"
        f":conv:{conversation_id}"
    )


def parse_correlation_ref(ref: str | None) -> dict[str, Any] | None:
    """Parse a convoy external ref back into its correlation fields.

    Returns ``None`` for anything that is not a ``talk:`` ref. Tolerates the
    LEGACY ``talk:<run_id>`` form written before this ticket — those rows are
    real and still in the ledger, so they parse with ``None`` Archon ids rather
    than being reported as corrupt.
    """

    if not isinstance(ref, str) or not ref:
        return None
    parts = ref.split(":")
    if len(parts) < 2 or parts[0] != CORRELATION_PREFIX:
        return None
    try:
        run_id = int(parts[1])
    except ValueError:
        return None
    parsed: dict[str, Any] = {
        "run_id": run_id,
        "conversation_db_id": None,
        "conversation_id": None,
    }
    for index in range(2, len(parts) - 1, 2):
        key, value = parts[index], parts[index + 1]
        if key == "archon":
            parsed["conversation_db_id"] = value or None
        elif key == "conv":
            parsed["conversation_id"] = value or None
    return parsed


# ---------------------------------------------------------------------------
# Codebase binding — Rule 2: physical state, not a config claim
# ---------------------------------------------------------------------------


def _normalized(path: Path | str) -> str:
    """Case- and separator-normalized absolute path for comparison."""

    return os.path.normcase(os.path.abspath(str(path)))


def main_repo_root(repo_root: Path | str) -> Path:
    """Resolve the MAIN repo root for ``repo_root``, following a worktree link.

    In a git worktree ``.git`` is a FILE holding ``gitdir: <main>/.git/
    worktrees/<name>``; Archon registers the main checkout, not the worktree,
    so a codebase lookup keyed on the worktree path finds nothing. Reading the
    pointer is pure file IO — no subprocess on a voice path.

    Returns ``repo_root`` unchanged when it is not a worktree, when the pointer
    is unreadable, or when the pointed-at path has no ``.git`` component.
    """

    root = Path(repo_root)
    git_path = root / ".git"
    try:
        if not git_path.is_file():
            return root
        pointer = git_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        logger.warning("talk_archon: unreadable .git pointer at %s: %s", git_path, exc)
        return root
    if not pointer.startswith("gitdir:"):
        return root
    target = Path(pointer.split(":", 1)[1].strip())
    for parent in (target, *target.parents):
        if parent.name == ".git":
            return parent.parent
    return root


def _registered_codebases(db_path: Path) -> list[dict[str, str]]:
    """Read Archon's registered codebases through a write-refusing URI.

    Returns ``[]`` for a missing or locked ledger — the caller turns that into
    a refusal that names ``ARCHON_CODEBASE_ID``, never a blind dispatch.
    """

    if not db_path.exists():
        return []
    columns = ("id", "name", "default_cwd")
    try:
        connection = sqlite3.connect(
            db_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0
        )
        try:
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM remote_agent_codebases"
            ).fetchall()
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 — an unreadable ledger is "unknown"
        logger.warning(
            "talk_archon: codebase read failed: %s: %s", type(exc).__name__, exc
        )
        return []
    return [dict(zip(columns, row)) for row in rows]


def resolve_codebase_id(
    repo_root: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
) -> str:
    """Resolve the Archon codebase id to dispatch against.

    Rule 1: both arguments are ``None`` sentinels resolved inside the body.
    Rule 2: after the explicit override, resolution reads PHYSICAL state — the
    registered ``remote_agent_codebases`` rows matched against the real repo
    root on disk (and, for a worktree, its main checkout).

    Order:

    1. ``ARCHON_CODEBASE_ID`` — the operator's explicit binding.
    2. a registered codebase whose ``default_cwd`` IS this repo root.
    3. a registered codebase whose ``default_cwd`` is the main checkout this
       worktree points at.

    Raises:
        ArchonDispatchRefusedError: nothing resolved. The message names the env var
            and lists what Archon actually has registered, because a dispatch
            with no codebase binding gets no isolation pre-flight and lands in
            the wrong tree.
    """

    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]
    root = Path(repo_root)
    codebases = _registered_codebases(archon_db_path(db_path))

    override = (os.environ.get("ARCHON_CODEBASE_ID") or "").strip()
    if override:
        if not _CODEBASE_ID_RE.match(override):
            raise ArchonDispatchRefusedError(
                f"ARCHON_CODEBASE_ID={override!r} is not a valid Archon codebase "
                "id (letters, digits, dash, underscore)."
            )
        # Rule 2: the override is a CLAIM, and existence in the registry is not
        # enough to honour it. The row it names must physically point AT this
        # repo (or the main checkout a worktree belongs to) — otherwise a
        # stale or copied environment binding dispatches a clone into a
        # DIFFERENT registered repository and Archon edits the wrong codebase
        # (codex R3 blocker; the earlier existence-only check was the hole).
        #
        # What the override legitimately buys is DISAMBIGUATION: this box has
        # more than one registered row pointing at the same tree, and the
        # operator picks which. It cannot redirect the target.
        if not codebases:
            raise ArchonDispatchRefusedError(
                f"ARCHON_CODEBASE_ID={override!r} cannot be verified: Archon's "
                "codebase registry is unreadable, so I will not dispatch into "
                "an unconfirmed target."
            )
        by_id = {str(row["id"]): row for row in codebases}
        chosen = by_id.get(override)
        if chosen is None:
            known = ", ".join(sorted(r.get("name") or r["id"] for r in codebases))
            raise ArchonDispatchRefusedError(
                f"ARCHON_CODEBASE_ID={override!r} is not registered in Archon. "
                f"Archon currently knows: {known}."
            )
        wanted_roots = {_normalized(root), _normalized(main_repo_root(root))}
        chosen_cwd = (chosen.get("default_cwd") or "").strip()
        # An empty default_cwd must REFUSE, not normalize: os.path.abspath("")
        # resolves to the process cwd, so a blank row certified itself as this
        # repo whenever the process happened to be running here — restoring
        # the wrong-repository dispatch class the check exists to close
        # (codex R4 blocker).
        if not chosen_cwd or _normalized(chosen_cwd) not in wanted_roots:
            raise ArchonDispatchRefusedError(
                f"ARCHON_CODEBASE_ID={override!r} is registered for "
                f"{chosen_cwd or 'no path'}, but this repo is {root}. I will "
                "not dispatch work into a different codebase — unset the "
                "override or point it at a codebase registered for this repo."
            )
        return override
    wanted = [_normalized(root)]
    main_root = main_repo_root(root)
    if _normalized(main_root) not in wanted:
        wanted.append(_normalized(main_root))

    for target in wanted:
        for codebase in codebases:
            cwd = codebase.get("default_cwd") or ""
            if cwd and _normalized(cwd) == target:
                return str(codebase["id"])

    known = ", ".join(sorted(c.get("name") or c["id"] for c in codebases)) or "none"
    raise ArchonDispatchRefusedError(
        f"no Archon codebase is registered for {root}. Set ARCHON_CODEBASE_ID "
        f"in the Homie environment to bind one. Archon currently knows: {known}."
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchGrant:
    """Proof that one dispatch cleared every gate, plus what it resolved to.

    Held by value so the worker thread dispatches exactly what was approved —
    re-reading env inside the worker could dispatch under a policy that no
    longer matches the audited grant.
    """

    workflow: str
    brief: str
    codebase_id: str
    caller: str


def require_dispatch_allowed(
    workflow: str,
    brief: str,
    *,
    caller: str = "talk.run_archon",
    repo_root: Path | str | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> DispatchGrant:
    """Run every gate and return the grant, or raise a speakable refusal.

    Order is authority-first: the operator's kill switch, then the declared
    policy, then the brief contract, then the codebase binding. Each refusal
    writes its own audit row before raising, and a granted dispatch writes one
    too — so the trail answers "what did the Homie try to deploy" whether or
    not it was allowed to.

    There is no confirmation check here, on purpose. The Talk lane cannot
    prove a spoken yes (``talk_api.py`` receives only a model-authored tool
    name and argument dict; the Realtime transcript never reaches this
    process), so a confirmation gate here would assert something unprovable.
    The operator's rule puts the gate where the harm is instead: this path is
    bounded to a worktree and tokens, and spend/outward actions are approved
    through an authed channel at the point they occur.

    Contract ``ValueError`` (blank workflow) is raised BEFORE any gate work:
    it is a caller bug, not a policy event.

    Raises:
        ValueError: blank ``workflow``.
        ArchonDispatchRefusedError: kill switch, policy, vague brief, or no
            codebase.
    """

    workflow = str(workflow or "").strip()
    if not workflow:
        raise ValueError("workflow must be a non-blank string")
    brief = str(brief or "").strip()

    def audit(outcome: str, error: str = "", **extra: Any) -> None:
        audit_attempt(
            workflow=workflow,
            outcome=outcome,
            caller=caller,
            brief_preview=brief,
            error=error,
            audit_path=audit_path,
            **extra,
        )

    try:
        kill_switches.requireEnabled(KILL_SWITCH, caller=caller)
    except kill_switches.KillSwitchDisabled as exc:
        # The house contract is that a kill-switch refusal PROPAGATES with its
        # switch name and status intact, so callers can map it structurally
        # (503 + switch, counted). Converting it to an ordinary dispatch
        # refusal erased that (codex R3 major) — audit, then re-raise as-is.
        audit("refused_killswitch", error=str(exc))
        raise


    try:
        capabilities.require_integration_action(
            INTEGRATION, ACTION, surface="model", caller=caller
        )
    except capabilities.IntegrationPolicyError as exc:
        audit("denied", error=str(exc))
        raise ArchonDispatchRefusedError(
            f"policy will not let me deploy through Archon: {exc}"
        ) from exc

    refusal = brief_refusal_reason(brief)
    if refusal:
        audit("refused_vague_brief", error="brief is not self-contained")
        raise ArchonDispatchRefusedError(refusal)

    try:
        codebase_id = resolve_codebase_id(repo_root, db_path=db_path)
    except ArchonDispatchRefusedError as exc:
        audit("refused_unresolved_codebase", error=str(exc))
        raise

    # A granted dispatch must not outrun its own record. Refusals stay
    # best-effort (a lost refusal row costs nothing — nothing happened), but
    # a grant whose audit row cannot be persisted is refused outright (codex
    # R2 major: the append-only trail is a requirement, not a nicety, and an
    # unrecorded dispatch is indistinguishable from none). With no
    # confirmation gate on this path, the trail IS the accountability.
    try:
        append_dispatch_audit_record(
            workflow=workflow,
            outcome="granted",
            caller=caller,
            brief_preview=brief,
            codebase_id=codebase_id,
            audit_path=audit_path,
        )
    except Exception as exc:  # noqa: BLE001 — no record, no dispatch
        logger.error("talk_archon: refusing grant, audit unwritable: %s", exc)
        raise ArchonDispatchRefusedError(
            "I could not write the dispatch audit record, so I am not "
            f"deploying: {exc}"
        ) from exc
    return DispatchGrant(
        workflow=workflow, brief=brief, codebase_id=codebase_id, caller=caller
    )


# ---------------------------------------------------------------------------
# The dispatch itself — BLOCKING, never on an event loop
# ---------------------------------------------------------------------------


def dispatch_now(
    grant: DispatchGrant,
    *,
    client: Any | None = None,
    audit_path: Path | str | None = None,
    run_id: int | None = None,
) -> archon_client.ArchonDispatch:
    """Send the granted workflow through Archon's orchestrator. BLOCKING.

    F3: this goes through ``archon_client.dispatch_workflow``, which builds the
    conversation-message form Archon's own run endpoint builds. There is no raw
    run POST in Archon, and skipping the orchestrator would skip the whole
    pre-flight (requirement gates before spend, codebase binding, isolation
    resolution, resume-before-fresh).

    Refuses to run on an event loop. This is the 2026-07-13 wedge class made
    structural: a long external call on the bot's loop freezes Telegram,
    Discord, ``/health`` and the liveness supervisor at once. Run it from a
    worker thread and reply with the ``WORK_STARTED`` receipt instead.

    Raises:
        RuntimeError: called from a thread with a running event loop.
        ArchonDispatchError: any transport or protocol failure, carrying the
            client's ``friendly_message`` so the model can speak it.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "talk_archon.dispatch_now is blocking and must never run on an "
            "event loop — dispatch from a worker thread (asyncio.to_thread)."
        )

    try:
        dispatch = asyncio.run(
            archon_client.dispatch_workflow(
                grant.codebase_id, grant.workflow, grant.brief, client=client
            )
        )
    except archon_client.ArchonAPIError as exc:
        message = getattr(exc, "friendly_message", "") or str(exc) or "Archon API error."
        audit_attempt(
            workflow=grant.workflow,
            outcome="failed",
            caller=grant.caller,
            brief_preview=grant.brief,
            codebase_id=grant.codebase_id,
            run_id=run_id,
            error=f"{type(exc).__name__}: {message}",
            audit_path=audit_path,
        )
        raise ArchonDispatchError(message) from exc

    # A typed 2xx receipt is not the same as an accepted run. Archon can
    # answer 200 while refusing the dispatch (queue refusal, requirement
    # gate), and treating that as success announces WORK_STARTED, claims the
    # convoy row, and then polls for hours for a run that never existed
    # (codex R3 major). The receipt has to SAY it was accepted.
    if not (getattr(dispatch, "dispatched", False) and getattr(dispatch, "accepted", False)):
        detail = str(getattr(dispatch, "status", "") or "no status")
        audit_attempt(
            workflow=grant.workflow,
            outcome="refused_by_archon",
            caller=grant.caller,
            brief_preview=grant.brief,
            codebase_id=grant.codebase_id,
            conversation_id=getattr(dispatch, "conversation_id", ""),
            conversation_db_id=getattr(dispatch, "conversation_db_id", ""),
            run_id=run_id,
            error=f"dispatched={getattr(dispatch, 'dispatched', None)} "
            f"accepted={getattr(dispatch, 'accepted', None)} status={detail}",
            audit_path=audit_path,
        )
        raise ArchonDispatchError(
            f"Archon took the request but did not start {grant.workflow} "
            f"(status: {detail}). Nothing is running."
        )

    audit_attempt(
        workflow=grant.workflow,
        outcome="dispatched",
        caller=grant.caller,
        brief_preview=grant.brief,
        codebase_id=grant.codebase_id,
        conversation_id=dispatch.conversation_id,
        conversation_db_id=dispatch.conversation_db_id,
        run_id=run_id,
        audit_path=audit_path,
    )
    return dispatch


# ---------------------------------------------------------------------------
# Steering — correcting a run the Homie already dispatched (#259, F4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SteerOutcome:
    """One steering attempt, with a message meant to be spoken verbatim.

    Everything except a kill-switch refusal and a caller-bug ``ValueError``
    lands here as ``ok=False`` plus speakable text: the voice lane has one
    channel, and an operator asking to cancel a run deserves a sentence rather
    than a stack trace.
    """

    ok: bool
    run_id: str
    action: str
    message: str
    phrase: str = ""


def append_steer_audit_record(
    *,
    action: str,
    outcome: str,
    run_id: str = "",
    conversation_id: str = "",
    caller: str = "",
    note_preview: str = "",
    phrase: str = "",
    error: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Append one steering audit row and return its id.

    A sibling trail to ``archon_dispatch.jsonl``, not a widening of it: a
    dispatch row is keyed by workflow + brief, a steer row by run + action, and
    collapsing the two would make both harder to read. ``audit_path`` is a
    ``None`` sentinel resolved at call time (Rule 1). Raises on IO failure —
    :func:`steer_audit` is the best-effort wrapper.
    """

    if audit_path is None:
        import config  # noqa: PLC0415 — lazy: DATA_DIR is test-monkeypatched

        audit_path = config.DATA_DIR / "archon_steer.jsonl"
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "integration": INTEGRATION,
        "action": STEER_ACTION,
        "steer_action": action,
        "outcome": outcome,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "caller": caller,
        "note_preview": " ".join(str(note_preview or "").split())[:160],
        "phrase": phrase,
        "error": error,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return f"{record['timestamp']}:{action}:{outcome}"


def steer_audit(**fields: Any) -> None:
    """Best-effort steering audit append; every swallow leaves a receipt."""

    try:
        append_steer_audit_record(**fields)
    except Exception as exc:  # noqa: BLE001 — audit is a record, not the action
        logger.warning(
            "talk_archon: steer audit write failed: %s: %s", type(exc).__name__, exc
        )


def _require_off_event_loop(what: str) -> None:
    """Structural guard: the 2026-07-13 wedge class, not a comment.

    A long external call on the bot's loop freezes Telegram, Discord,
    ``/health`` and the liveness supervisor at once.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"talk_archon.{what} is blocking and must never run on an event loop — "
        "call it from a worker thread (asyncio.to_thread)."
    )


def _gate_phrase_for(run_id: str, db_path: Path | str | None) -> str:
    """The phrase the run's gate demands right now; ``""`` when none/unknown."""

    from integrations import archon_approvals  # noqa: PLC0415 — Rule 3 module attr

    return archon_approvals.read_gate_phrase(run_id, db_path=archon_db_path(db_path))


def steer_now(
    run_id: str,
    action: str,
    *,
    note: str | None = None,
    caller: str = "talk.manage_run",
    client: Any | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> SteerOutcome:
    """Answer or interrupt a dispatched run through Archon's gate. BLOCKING.

    F4: the five actions are Archon's own endpoints; this module adds the gate,
    the trail, and the one piece of competence a naive caller gets wrong —
    **the approve comment.** Archon records it as the gate node's captured
    output, and a bare approve defaults it to the literal ``"Approved"``, which
    fails every deterministic ``<gate>-check`` node that greps for
    ``APPROVE SPEND`` / ``APPROVE DEPLOY``. So an approve reads the phrase THIS
    gate demands from the ledger at act time (Rule 2 — physical state, not the
    state a render was minted from) and sends it as the comment. A gate with no
    phrase check still gets a bare approve, which matters: an
    ``interactive_loop`` gate reads any non-empty comment as FEEDBACK and
    iterates instead of finalizing.

    Exactly two audit rows are written on every path — attempt, then result.
    Success is never encoded as the ABSENCE of a row.

    Args:
        run_id: the Archon run id.
        action: one of :data:`STEER_ACTIONS`.
        note: the operator's words. Carried as ``comment`` (approve) or
            ``reason`` (reject). ``resume``/``cancel``/``abandon`` have no body
            field for one, so a note there is recorded in the trail and the
            outcome SAYS it was not sent rather than dropping it silently.

    Raises:
        ValueError: caller bug — unknown action or a malformed run id.
        kill_switches.KillSwitchDisabled: the operator switched the surface off
            — audited, then re-raised as-is (house contract).
    """

    # Contract checks BEFORE the gate and before any try: a caller bug must
    # surface at the call site, not hide inside a fail-open branch.
    if not isinstance(action, str) or action not in STEER_ACTIONS:
        raise ValueError(f"action {action!r} must be one of {sorted(STEER_ACTIONS)}")
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError(f"run_id {run_id!r} must match {_RUN_ID_RE.pattern}")
    _require_off_event_loop("steer_now")

    note = str(note or "").strip() or None

    def audit(outcome: str, *, phrase: str = "", error: str = "") -> None:
        steer_audit(
            action=action,
            outcome=outcome,
            run_id=run_id,
            caller=caller,
            note_preview=note or "",
            phrase=phrase,
            error=error,
            audit_path=audit_path,
        )

    try:
        kill_switches.requireEnabled(STEER_KILL_SWITCH, caller=caller)
    except kill_switches.KillSwitchDisabled as exc:
        audit("refused_killswitch", error=str(exc))
        raise

    try:
        capabilities.require_integration_action(
            INTEGRATION, STEER_ACTION, surface="model", caller=caller
        )
    except capabilities.IntegrationPolicyError as exc:
        audit("denied", error=str(exc))
        return SteerOutcome(
            ok=False,
            run_id=run_id,
            action=action,
            message=f"Policy will not let me {action} an Archon run: {exc}",
        )

    phrase = ""
    unsent_note = False
    if action == "approve":
        phrase = _gate_phrase_for(run_id, db_path)
        if phrase and (not note or phrase not in note.upper()):
            # Both, when the operator gave words: the check node greps for the
            # phrase, and the operator's sentence is what the gate node
            # captures as its output. Neither is worth losing.
            note = f"{note}\n\n{phrase}" if note else phrase
    elif action != "reject" and note:
        # approve/reject are the only endpoints with a body field. Keep the
        # operator's reason in the trail and SAY it did not travel, rather than
        # discarding it behind their back.
        unsent_note = True

    send_note = note if action in ("approve", "reject") else None

    # Accountability BEFORE the mutation. A row written afterwards is a row
    # that does not exist when the disk is full, and an unrecorded cancel of
    # in-flight work cannot be reconstructed from anywhere else.
    try:
        append_steer_audit_record(
            action=action,
            outcome=f"{action}_attempted",
            run_id=run_id,
            caller=caller,
            note_preview=note or "",
            phrase=phrase,
            audit_path=audit_path,
        )
    except Exception as exc:  # noqa: BLE001 — no record, no mutation
        logger.error("talk_archon: refusing steer, audit unwritable: %s", exc)
        return SteerOutcome(
            ok=False,
            run_id=run_id,
            action=action,
            message=(
                f"I could not write the steering audit record, so I did not "
                f"{action} run {run_id}. Nothing was sent to Archon."
            ),
        )

    try:
        result = asyncio.run(
            archon_client.steer(run_id, action, note=send_note, client=client)
        )
    except archon_client.ArchonAPIError as exc:
        message = getattr(exc, "friendly_message", "") or str(exc) or "Archon API error."
        audit("failed", phrase=phrase, error=f"{type(exc).__name__}: {message}")
        return SteerOutcome(ok=False, run_id=run_id, action=action, message=message)
    except Exception as exc:  # noqa: BLE001 — the operator is never left unanswered
        audit("failed", phrase=phrase, error=f"{type(exc).__name__}: {exc}")
        logger.warning("talk_archon: steer %s blew up: %s", action, exc)
        return SteerOutcome(
            ok=False,
            run_id=run_id,
            action=action,
            message=f"Could not reach Archon to {action} run {run_id}: {type(exc).__name__}.",
        )

    if not result.success:
        audit("rejected_by_archon", phrase=phrase, error=result.message)
        return SteerOutcome(
            ok=False,
            run_id=run_id,
            action=action,
            message=(
                result.message or f"Archon did not accept the {action} for run {run_id}."
            ),
        )

    audit(action, phrase=phrase)
    message = _STEER_SUCCESS_TEXT[action].format(run_id=run_id)
    if phrase:
        message += f" I sent the phrase the gate demanded: {phrase}."
    if unsent_note:
        message += (
            f" Archon's {action} endpoint has no field for a reason, so yours is "
            "in the steering log rather than on the run."
        )
    return SteerOutcome(
        ok=True, run_id=run_id, action=action, message=message, phrase=phrase
    )


def say_now(
    conversation_id: str,
    text: str,
    *,
    run_id: str = "",
    caller: str = "talk.manage_run",
    client: Any | None = None,
    audit_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> SteerOutcome:
    """Send the operator's own words to a run's conversation. BLOCKING.

    F4's first primitive: when a run is paused, ANY non-slash message on its
    conversation becomes the approval (``orchestrator-agent.ts:901-1020``), so
    "looks good, ship it" resumes a paused DAG. This is the voice-native path —
    it is also the one with a sharp edge, and the outcome text carries it: a
    conversation reply cannot REJECT. "No, don't" is a non-slash message and
    therefore approves. Only :func:`steer_now` with ``reject`` refuses a gate.

    ``run_id`` is optional but should always be passed when the caller knows it:
    it is what lets the gate's mandatory phrase be appended, and without it the
    approval resumes the DAG only to fail the next check node. A conversation id
    alone cannot resolve the phrase, which is why this is a separate argument
    rather than something derived here.

    Raises:
        ValueError: caller bug — blank text or a malformed conversation id.
        kill_switches.KillSwitchDisabled: audited, then re-raised as-is.
    """

    if not isinstance(conversation_id, str) or not _CONVERSATION_ID_RE.match(
        conversation_id or ""
    ):
        raise ValueError(
            f"conversation_id {conversation_id!r} must match {_CONVERSATION_ID_RE.pattern}"
        )
    text = str(text or "").strip()
    if not text:
        raise ValueError("text must be a non-blank string")
    _require_off_event_loop("say_now")

    # The operator's words are the approval, but they are not what the gate
    # GREPS for. A spend gate demands the verbatim APPROVE SPEND / APPROVE
    # DEPLOY constant, so "looks good, ship it" resumes the DAG and then fails
    # the deterministic <gate>-check node immediately after — the Homie reports
    # the approval landed while the run rejects it. This is the voice-native
    # path the ticket exists for, so it has to carry the phrase like the
    # explicit approve endpoint already does.
    #
    # The phrase is resolved HERE, at act time, from the uncapped ledger read
    # (Rule 2 — physical state, not the render the card was minted from), and
    # only when the caller could name a run. Both strings survive: the check
    # node needs the constant, and Archon captures the operator's sentence as
    # the gate node's output.
    if run_id:
        phrase = _gate_phrase_for(run_id, db_path)
        if phrase and phrase not in text.upper():
            text = f"{text}\n\n{phrase}"

    def audit(outcome: str, error: str = "") -> None:
        steer_audit(
            action="say",
            outcome=outcome,
            conversation_id=conversation_id,
            caller=caller,
            note_preview=text,
            error=error,
            audit_path=audit_path,
        )

    try:
        kill_switches.requireEnabled(STEER_KILL_SWITCH, caller=caller)
    except kill_switches.KillSwitchDisabled as exc:
        audit("refused_killswitch", str(exc))
        raise

    try:
        capabilities.require_integration_action(
            INTEGRATION, STEER_ACTION, surface="model", caller=caller
        )
    except capabilities.IntegrationPolicyError as exc:
        audit("denied", str(exc))
        return SteerOutcome(
            ok=False,
            run_id="",
            action="say",
            message=f"Policy will not let me send that to Archon: {exc}",
        )

    try:
        append_steer_audit_record(
            action="say",
            outcome="say_attempted",
            conversation_id=conversation_id,
            caller=caller,
            note_preview=text,
            audit_path=audit_path,
        )
    except Exception as exc:  # noqa: BLE001 — no record, no mutation
        logger.error("talk_archon: refusing say, audit unwritable: %s", exc)
        return SteerOutcome(
            ok=False,
            run_id="",
            action="say",
            message=(
                "I could not write the steering audit record, so I did not send "
                "that to the run. Nothing reached Archon."
            ),
        )

    try:
        body = asyncio.run(
            archon_client.send_message(conversation_id, text, client=client)
        )
    except archon_client.ArchonAPIError as exc:
        message = getattr(exc, "friendly_message", "") or str(exc) or "Archon API error."
        audit("failed", f"{type(exc).__name__}: {message}")
        return SteerOutcome(ok=False, run_id="", action="say", message=message)
    except Exception as exc:  # noqa: BLE001
        audit("failed", f"{type(exc).__name__}: {exc}")
        logger.warning("talk_archon: say blew up: %s", exc)
        return SteerOutcome(
            ok=False,
            run_id="",
            action="say",
            message=f"Could not reach Archon: {type(exc).__name__}.",
        )

    # Archon answers 200 with {accepted, status}; an unaccepted message never
    # reached handleMessage, so reporting it as delivered would tell the
    # operator their correction landed when it did not.
    if not (isinstance(body, dict) and body.get("accepted")):
        detail = str((body or {}).get("status") or "no status")
        audit("refused_by_archon", f"accepted=False status={detail}")
        return SteerOutcome(
            ok=False,
            run_id="",
            action="say",
            message=(
                f"Archon took the message but did not accept it (status: {detail}), "
                "so the run did not hear it."
            ),
        )

    audit("say")
    return SteerOutcome(
        ok=True,
        run_id="",
        action="say",
        message=(
            "Sent it to the run. If it was sitting at a gate, that message is the "
            "approval — a conversation reply can only approve, never refuse, so "
            "tell me to reject if that is what you meant."
        ),
    )


def run_id_for_conversation(
    conversation_db_id: str,
    *,
    db_path: Path | str | None = None,
) -> str | None:
    """Find the Archon run a dispatch produced, from the ro ledger.

    The join is ``parent_conversation_id`` and NOT ``conversation_id``: for a
    web-dispatched workflow Archon spawns a separate worker conversation and
    puts it in the run's own ``conversation_id``, leaving the dispatching
    conversation in ``parent_conversation_id``. Filtering the other way matches
    nothing, and an empty result reads exactly like "not started yet".

    Returns ``None`` when the run has not been registered yet, or when the
    ledger is missing/locked — the caller keeps polling.
    """

    if not isinstance(conversation_db_id, str) or not conversation_db_id:
        return None
    path = archon_db_path(db_path)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(
            path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0
        )
        try:
            row = connection.execute(
                "SELECT id FROM remote_agent_workflow_runs "
                "WHERE parent_conversation_id = ? ORDER BY started_at DESC LIMIT 1",
                (conversation_db_id,),
            ).fetchone()
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 — a locked ledger is "not yet"
        logger.warning(
            "talk_archon: run lookup failed: %s: %s", type(exc).__name__, exc
        )
        return None
    return str(row[0]) if row else None


__all__ = [
    "ACTION",
    "CORRELATION_PREFIX",
    "DESTRUCTIVE_STEER_ACTIONS",
    "INTEGRATION",
    "KILL_SWITCH",
    "STEER_ACTION",
    "STEER_ACTIONS",
    "STEER_KILL_SWITCH",
    "ArchonDispatchError",
    "ArchonDispatchRefusedError",
    "DispatchGrant",
    "SteerOutcome",
    "append_dispatch_audit_record",
    "append_steer_audit_record",
    "audit_attempt",
    "archon_db_path",
    "brief_floors",
    "brief_refusal_reason",
    "build_correlation_ref",
    "default_workflow",
    "dispatch_now",
    "main_repo_root",
    "parse_correlation_ref",
    "require_dispatch_allowed",
    "resolve_codebase_id",
    "run_id_for_conversation",
    "say_now",
    "steer_audit",
    "steer_now",
]
