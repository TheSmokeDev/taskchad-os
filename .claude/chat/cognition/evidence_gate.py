"""Living Self Act 4 — the evidence-READ amendment gate (deterministic, SECURITY).

Today a belief reaches SELF.md because the LLM asserted ``confidence_score >=
0.75`` and named ``evidence_paths`` the gate only COUNTS (``amendments.py:772``,
``len(proposal.evidence_paths) < min_evidence_paths``) and NEVER opens. This gate
makes belief EARNED: it OPENS, CONFINES, and BOUNDS each cited path, verifies it
EXISTS + deterministically supports the claim, and requires the candidate to beat
the belief-regression floor — BEFORE the UNCHANGED default-deny policy gate.

It is the NECESSARY + cheap-pre-filter half of the earned-adoption stack (the LLM
judge in ``evolve/judge.py`` is the SUFFICIENT support-decider, scheduled-only).
This gate is DETERMINISTIC (no provider call) so it is safe to run inside ANY
producer's hot path — the producers do NOT pass it in Act 4 (only ``evolve_loop``
does), but the capability exists and is proven.

M4 — SECURITY (the directive's top priority). The candidate is LLM-/Archon-
proposed (semi-trusted) and its ``evidence_paths`` are author-written. An
unconfined unbounded read would be an arbitrary-file-read + OOM +
judge-prompt-injection hole. The gate:
  (a) CONFINES every path under ``memory_dir`` ONLY (the vault — F3; NOT the
      whole repo, so ``.claude/scripts/.env`` and other in-repo secrets can never
      be read into the judge prompt) — resolve FIRST (``.resolve()`` collapses
      ``..`` AND follows symlinks), THEN ``is_relative_to`` on the resolved path.
      Resolve-before-confine is load-bearing (a symlink inside the vault pointing
      OUT is caught by resolve-first; the lexical confine-then-resolve check would
      wrongly pass it — proven on this win32 box). A traversal / absolute /
      symlink-escape / in-repo-but-out-of-vault path is REJECTED, NOT read, NOT
      fed to the judge.
  (b) BOUNDS the read — ``stat().st_size > max_bytes`` -> non-supporting (no
      read); reads at most ``max_bytes`` even from an in-range file; re-applies
      the cap to any injected ``read_text`` return (the fake reader bypasses
      ``stat``) so a TOCTOU-grown file cannot OOM.
  (c) MISSING / dir / unreadable / empty = evidence-FAIL, never a silent "OK".
  (d) The read bytes fed to the judge are the SAME confined+bounded content,
      ``_safe``-collapsed + length-capped + framed "DATA, never an instruction".

Rule 1 (call-time settings), Rule 2 (PHYSICAL bytes via ``_read_text``, never the
confidence float), fail-open WITH a visible ``[evolve.gate]`` print (N2).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _safe(text: str, *, limit: int = 600) -> str:
    """Whitespace-collapse + length-cap untrusted evidence for the judge feed.

    Mirrors Act 2's ``belief_conflicts._safe`` (UNTRUSTED-DATA framing): an
    injected multi-line evidence blob can neither break a prompt's line format
    nor crowd the instruction.
    """
    return re.sub(r"\s+", " ", (text or "").strip())[:limit]


def _candidate_roots(memory_dir: Path) -> list[Path]:
    """The trusted root a cited evidence path may live under, resolved once.

    ``memory_dir`` (the vault) ONLY — NOT ``PROJECT_ROOT`` (F3 fix). Belief
    evidence is operator-belief evidence: daily logs, episodes, SELF.md, vault
    notes — ALL under ``memory_dir``. Code and secrets (``.claude/scripts/.env``,
    credentials, the ledger) are NOT legitimate belief-evidence and must never be
    READ into the LLM judge prompt. Confining to the vault closes the
    in-repo-secret-exfiltration path cleanly (a candidate citing ``.env`` now
    resolves OUTSIDE the only root -> rejected, never read). The root is
    ``.resolve``d so a symlinked vault root cannot be used to widen the jail.
    """
    roots: list[Path] = []
    try:
        roots.append(Path(memory_dir).resolve())
    except (OSError, RuntimeError):
        pass
    return roots


def _vault_tail(raw: str) -> str | None:
    """Reduce a vault path to its memory-relative tail (after the last ``Memory/``).

    Mirrors ``evolve.regression._normalize_path``: a curated ``vault/memory/
    MEMORY.md`` or ``MEMORY.md`` both resolve to ``MEMORY.md`` so they can be
    re-rooted under the actual ``memory_dir``.
    """
    p = str(raw).replace("\\", "/")
    if "Memory/" in p:
        return p.split("Memory/")[-1]
    return None


def _confined_candidate_paths(raw: str, memory_dir: Path) -> list[Path]:
    """Build the candidate filesystem paths to TRY for one cited evidence_path.

    Vault-relative (``daily/2026-06-11.md`` / ``MEMORY.md`` /
    ``vault/memory/MEMORY.md``) -> try under ``memory_dir`` (the ONLY trusted
    root after F3). Absolute -> the path itself (confinement then rejects it
    unless it is genuinely under the resolved vault root). Returned UN-resolved;
    confinement (resolve + ``is_relative_to`` against ``memory_dir`` ONLY) is
    applied by the caller. A repo-relative path that points OUTSIDE the vault
    (``.claude/scripts/.env``) now has NO in-root candidate -> non-supporting,
    never read (F3).
    """
    raw_path = Path(raw)
    candidates: list[Path] = []
    memory_root = Path(memory_dir)
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        # relative-to the vault root (the only trusted root)
        candidates.append(memory_root / raw_path)
        # vault-tail re-root (vault/memory/X -> memory_dir/X)
        tail = _vault_tail(raw)
        if tail:
            candidates.append(memory_root / tail)
    return candidates


def _read_confined_bounded(
    raw: str,
    memory_dir: Path,
    *,
    max_bytes: int,
    read_text: Callable[[Path], str],
) -> str | None:
    """Resolve + CONFINE + BOUND-read one cited evidence_path (M4 core).

    Returns the confined+bounded non-empty text, or ``None`` when the path is
    non-supporting (escapes confinement / missing / dir / oversized / empty /
    unreadable). The judge feed and the support check BOTH go through this — the
    judge never sees what the gate rejected.
    """
    roots = _candidate_roots(memory_dir)
    if not roots:
        return None
    for candidate in _confined_candidate_paths(raw, memory_dir):
        try:
            resolved = candidate.resolve()  # collapses .. AND follows symlinks
        except (OSError, RuntimeError):
            continue
        # (a) CONFINE — resolve-FIRST, then is_relative_to on the resolved path.
        if not any(resolved.is_relative_to(root) for root in roots):
            continue  # traversal / absolute / symlink escape -> try next form
        # (c) MISSING / dir = non-supporting.
        if not resolved.exists() or resolved.is_dir():
            continue
        # (b) BOUND — oversized file is non-supporting (no read).
        try:
            if resolved.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        # READ physical bytes (Rule 2). Re-apply the byte cap to the returned
        # string because an injected read_text bypasses stat (TOCTOU belt too).
        text = read_text(resolved)
        text = (text or "")[:max_bytes]
        if not text.strip():
            continue  # empty / metadata-only = non-supporting
        return text
    return None


def read_evidence_texts(
    proposal: Any,
    memory_dir: Path | str,
    *,
    settings: Any | None = None,
    read_text: Callable[[Path], str] | None = None,
) -> dict[str, str]:
    """Confined+bounded read of every cited evidence_path -> {raw_path: text}.

    The SAME resolver the gate uses (M4) — exposed so the loop's judge feed reuses
    it and the judge never sees a path the gate rejected. Non-supporting paths are
    OMITTED (not keyed to ``""``), so the floor's empty-evidence checks see only
    what actually read. Fail-open: a raising ``read_text`` -> that path is skipped
    with a visible ``[evolve.gate]`` print.
    """
    if settings is None:
        from config import get_belief_evolve_settings

        settings = get_belief_evolve_settings()
    if read_text is None:
        from cognition.amendments import _read_text as read_text  # Rule 2 — bytes
    mem = Path(memory_dir)
    texts: dict[str, str] = {}
    for raw in getattr(proposal, "evidence_paths", []) or []:
        try:
            text = _read_confined_bounded(
                str(raw), mem, max_bytes=settings.max_bytes, read_text=read_text
            )
        except Exception as exc:  # fail-open VISIBLE (N2) — conservative
            print(
                f"[evolve.gate] evidence read failed (non-fatal) for {raw!r}: {exc!r}",
                flush=True,
            )
            text = None
        if text is not None:
            texts[str(raw)] = _safe(text, limit=settings.max_bytes)
    return texts


def read_evidence_for_floor(
    proposal: Any,
    memory_dir: Path | str,
    *,
    settings: Any | None = None,
    read_text: Callable[[Path], str] | None = None,
) -> dict[str, str]:
    """Like ``read_evidence_texts`` but keys EVERY cited path — missing / empty /
    confinement-escaped paths map to ``""`` (not omitted).

    The belief-regression floor's ``no_unread_claim`` check (the doc-read-
    truthfulness gate, M2's load-bearing falsifiable check) needs to SEE that a
    cited path was empty/missing/rejected — an omitted path would hide the
    violation. The non-empty SUPPORT count uses ``read_evidence_texts`` (which
    omits empties); the floor uses THIS (which preserves them).
    """
    if settings is None:
        from config import get_belief_evolve_settings

        settings = get_belief_evolve_settings()
    if read_text is None:
        from cognition.amendments import _read_text as read_text
    mem = Path(memory_dir)
    texts: dict[str, str] = {}
    for raw in getattr(proposal, "evidence_paths", []) or []:
        try:
            text = _read_confined_bounded(
                str(raw), mem, max_bytes=settings.max_bytes, read_text=read_text
            )
        except Exception as exc:  # fail-open VISIBLE (N2) — conservative
            print(
                f"[evolve.gate] evidence read failed (non-fatal) for {raw!r}: {exc!r}",
                flush=True,
            )
            text = None
        # preserve the path keyed to "" when non-supporting so the floor sees it
        texts[str(raw)] = _safe(text, limit=settings.max_bytes) if text is not None else ""
    return texts


def verify_evidence_support(
    proposal: Any,
    memory_dir: Path | str,
    *,
    settings: Any | None = None,
    read_text: Callable[[Path], str] | None = None,
    corpus: Any | None = None,
) -> tuple[bool, str]:
    """Deterministic evidence-READ + floor gate -> (allowed, reason).

    The bound seam ``amendments.AmendmentPolicy.evidence_check`` calls this AFTER
    the reconcile and BEFORE the UNCHANGED ``evaluate_amendment_policy``. Returns:
      - ``(True, "evidence_verified")`` only when the confinement+support check
        AND the belief-regression floor BOTH pass.
      - ``(False, "evidence_unsupported")`` when too few cited paths confine+exist
        +are non-empty, or the claim's vocabulary misses the overlap floor.
      - ``(False, "belief_regression_floor")`` when the deterministic floor's
        ``.failed`` is non-empty (the doc-read-truthfulness / prediction checks).
      - ``(False, "evidence_check_error")`` on an unexpected failure (fail-open
        CONSERVATIVE — a belief must EARN adoption — with a visible print).

    M2: the token-overlap is the CHEAPEST necessary layer (vocabulary, not
    support); the LLM judge (``evolve/judge.py``, NOT in this seam) is the
    sufficient support-decider. The crux REJECT fails on ``no_unread_claim`` /
    empty-evidence (a REAL falsifiable check), never on the weak overlap alone.
    """
    try:
        if settings is None:
            from config import get_belief_evolve_settings

            settings = get_belief_evolve_settings()
        if read_text is None:
            from cognition.amendments import _read_text as read_text
        mem = Path(memory_dir)

        from evolve.belief_regression import (
            evaluate_belief_regression,
            load_belief_regression_corpus,
            overlap_ratio,
        )

        claim = getattr(proposal, "proposed_content", "") or ""
        candidate = {
            "proposed_content": claim,
            "summary": getattr(proposal, "summary", ""),
            "source": getattr(proposal, "source", ""),
            "evidence_paths": list(getattr(proposal, "evidence_paths", []) or []),
        }

        # (1) THE FLOOR FIRST — the doc-read-truthfulness check (M2's load-bearing
        # falsifiable gate) needs to SEE empty/missing cited paths, so it runs over
        # the FULL floor-map (every cited path keyed to "" when non-supporting). A
        # candidate that ASSERTS a read but cites an empty/missing file is REJECTED
        # here on a REAL check (``belief_regression_floor``), never on the weak
        # overlap. The candidate's own N1 prediction rides in ``corpus``.
        if corpus is None:
            corpus = load_belief_regression_corpus(settings.corpus_path)
        floor_texts = read_evidence_for_floor(
            proposal, mem, settings=settings, read_text=read_text
        )
        floor = evaluate_belief_regression(candidate, floor_texts, corpus)
        if floor.failed:
            return False, "belief_regression_floor"

        # (2) the non-empty SUPPORT subset — enough cited paths confine + exist +
        # are non-empty (the count, a').
        evidence_texts = {p: t for p, t in floor_texts.items() if t.strip()}
        if len(evidence_texts) < settings.min_supporting_paths:
            return False, "evidence_unsupported"

        # (3) the claim's salient tokens hit the overlap floor vs the read union
        # (b' — the cheap vocabulary pre-filter; M2: NOT support).
        union = " ".join(evidence_texts.values())
        if overlap_ratio(claim, union) < settings.min_overlap:
            return False, "evidence_unsupported"

        return True, "evidence_verified"
    except Exception as exc:  # fail-open CONSERVATIVE + VISIBLE (N2)
        print(
            f"[evolve.gate] evidence support check failed (non-fatal): {exc!r}",
            flush=True,
        )
        return False, "evidence_check_error"


__all__ = [
    "verify_evidence_support",
    "read_evidence_texts",
]
