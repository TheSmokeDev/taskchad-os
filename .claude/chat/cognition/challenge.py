"""Called-Shots challenge surface — the Homie says the disagreement out loud (T2 #188).

Detection -> evidence -> (maybe) challenge -> stake. Rides the EXISTING Act-3
cognitive pass: detection is deterministic (regex + length floor, NO LLM call),
receipts come from the sanctioned recall entrypoint (Invariant I-3), and the
disagreement JUDGMENT rides the pass's existing single monologue call via an
injected directive + a parsed ``CHALLENGE_VERDICT`` block. Zero new LLM calls.

Mode contract (``CALLED_SHOTS_CHALLENGE_MODE``, default ``"silent"``):
- ``silent`` — the architecture doc's Spike-2 decision rule made real: detected
  candidate shots are RECORDED (``decided_by="open"``, reviewable and voidable
  via the T1 ``void`` outcome) but NO challenge enters the reply. The feature is
  default-ON and accumulating measurable candidates from day one — silent mode
  is the false-positive measurement phase, NOT a default-OFF violation. The
  challenge WIRE arms only via explicit ``CALLED_SHOTS_CHALLENGE_MODE=live``.
  The ARMING BAR is the architecture doc's Spike 2 as written: a replay of
  HISTORICAL operator turns with a measured false-positive rate — the spike
  harness's bundled sample set is a REGRESSION LOCK on the detection patterns,
  not arming evidence.
- ``live`` — when the monologue's verdict says the evidence materially
  disagrees, the challenge block is surfaced into the reply (region
  ``"challenge"`` -> the engine's uncapped prompt-suffix transport) citing >=1
  receipt, and the bet is staked via ``called_shots.record_shot``.

Hard invariants (T1 contract + epic ACs):
- **No bet, no challenge**: a surfaced challenge REQUIRES a successfully
  recorded shot. ``record_shot`` returning ``None`` (bet NOT staked) means the
  reply stays bare — the Homie never claims a bet the ledger doesn't hold.
- **No receipts, no challenge**: a challenge cites >=1 recall receipt (epic
  AC #1); detection without receipts stakes a silent candidate only.
- The soft toggle ``CALLED_SHOTS_ENABLED`` gates this WHOLE autonomous surface;
  the kill-switch is enforced inside the T1 service (caught here, degraded).
- Every helper fails open — a challenge failure yields a bare, correct turn.

In-conversation dedup is PROCESS-LOCAL ephemeral state (per-session position
hashes, capped): a restart resets it, which at worst re-detects a position once.
That is deliberate — the ledger (T1) is the durable record; this cache only
stops same-conversation nagging (epic non-goal: no override-nagging).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict

# Module-attribute import (Rule 3 shape) — called_shots carries its own
# boot-shim; KillSwitchDisabled is re-raised by its entrypoints.
from cognition import called_shots

# --- Staked-position detection (deterministic — no LLM) ---------------------

# First-person stake language: the operator committing to a position/plan,
# not asking, not commanding. Kept intentionally NARROW — the failure mode
# being measured is false-positive nagging, so precision beats recall here.
_STAKE_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bi\s+(?:really\s+)?think\s+\w+.{0,80}\b(?:best|right|only|should|better|way)\b",
    r"\bi(?:'m| am)\s+(?:convinced|certain|sure|positive)\s+(?:that|we|this|it)\b",
    r"\bi\s+believe\s+(?:we|this|it|that|the)\b.{0,80}\b(?:best|right|should|better|way|works)\b",
    r"\bwe\s+should\s+(?:definitely|absolutely|just|go\s+with|use|switch\s+to|drop|double\s+down)\b",
    r"\bi(?:'ve| have)\s+decided\s+(?:to|that|we)\b",
    r"\blet(?:'s| us)\s+go\s+with\b",
    r"\bmy\s+plan\s+is\s+to\b",
    r"\bi(?:'m| am)\s+(?:going\s+to|gonna)\s+\w+.{0,60}\bbecause\b",
    r"\bthe\s+best\s+(?:way|approach|move|play)\s+is\b",
    r"\bi\s+want\s+to\s+(?:go\s+with|use|switch\s+to|bet\s+on)\b",
))

# Cheap topical buckets for the scorecard grouping axis. The T1 service
# normalizes (strip+casefold) whatever we emit; refinement of this vocabulary
# is deliberately deferred (architecture doc: controlled-vocabulary bucketing
# is a later concern). "" is the valid no-domain bucket.
_DOMAIN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pricing", ("price", "pricing", "charge", "cost", "fee", "discount")),
    ("marketing", ("marketing", "ad", "ads", "campaign", "audience", "brand")),
    ("sales", ("sales", "close", "lead", "outreach", "pipeline", "deal")),
    ("seo", ("seo", "geo", "ranking", "search console", "serp", "keyword")),
    ("product", ("feature", "product", "ux", "ui", "launch", "roadmap")),
    ("voice", ("voice", "call", "telephony", "outbound", "dograh", "pipecat")),
    ("infra", ("server", "deploy", "infra", "hosting", "database", "docker")),
    ("hiring", ("hire", "hiring", "contractor", "freelancer", "team member")),
)


# Exclusion guards (R1 M6 — precision over recall; each guard closes a proven
# false-positive class): fenced/inline code, quoted/reported speech,
# hypothetical/conditional leads, embedded questions.
_FENCED_CODE_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_QUOTED_SPAN_RE = re.compile(r'["“][^"“”]*["”]')
_QUOTE_LINE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
# Sentence chunks WITH their trailing terminator attached — splitting on "?"
# would strip the very character the embedded-question guard checks for.
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")
_HYPOTHETICAL_LEAD_RE = re.compile(
    r"^\s*(?:if|what\s+if|unless|suppose|supposing|imagine|assuming|"
    r"hypothetically|say)\b",
    re.IGNORECASE,
)
_REPORTED_SPEECH_RE = re.compile(
    r"\b(?:he|she|they|client|customer|team|boss|[A-Z]\w+)\s+"
    r"(?:said|says|saying|thinks?|believes?|told|wrote|suggested|"
    r"mentioned|claims?)\b",
)


def detect_staked_position(text: str, *, settings=None) -> str | None:
    """Return the staked-position text, or None when the turn doesn't stake one.

    Deterministic-first gate (Rule-1 settings): first-person stake language +
    length floor, matched SENTENCE-WISE over a cleaned view of the message.
    Never fires on: slash commands, trailing questions, interrogative leads,
    fenced/inline code content, quoted spans or quote lines (reported speech),
    sentences with a hypothetical/conditional lead (if / what if / suppose /
    imagine ...), sentences containing an embedded ``?``, or stake language
    preceded by reported-speech attribution (he said / the client thinks ...).
    Returns the trimmed message (capped) as the position — the operator's own
    words ARE the position.
    """
    if settings is None:
        from config import get_called_shots_challenge_settings
        settings = get_called_shots_challenge_settings()
    stripped = (text or "").strip()
    if len(stripped) < settings.min_chars:
        return None
    if stripped.startswith("/"):
        return None
    # A question is an ask, not a stake. (Trailing "?" or interrogative lead.)
    if stripped.endswith("?"):
        return None
    if re.match(r"^(?:what|how|why|when|where|which|who|should\s+i|can\s+you|could\s+you|do\s+you)\b",
                stripped, re.IGNORECASE):
        return None

    # Cleaned view for matching ONLY (the returned position stays the
    # operator's verbatim message): code and quoted content can't stake a bet.
    cleaned = _FENCED_CODE_RE.sub(" ", stripped)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    cleaned = _QUOTE_LINE_RE.sub(" ", cleaned)
    cleaned = _QUOTED_SPAN_RE.sub(" ", cleaned)

    for sentence in _SENTENCE_RE.findall(cleaned):
        sentence = sentence.strip()
        if not sentence:
            continue
        if "?" in sentence:
            continue  # embedded question anywhere in the sentence
        if _HYPOTHETICAL_LEAD_RE.match(sentence):
            continue  # conditional/hypothetical, not a stake
        for pattern in _STAKE_PATTERNS:
            match = pattern.search(sentence)
            if not match:
                continue
            # Reported speech: stake language attributed to someone else
            # anywhere before the match in this sentence.
            if _REPORTED_SPEECH_RE.search(sentence[: match.start()]):
                continue
            return stripped[:1000]
    return None


def classify_domain(text: str) -> str:
    """Cheap keyword bucket for the scorecard axis; "" = the no-domain bucket."""
    lowered = (text or "").lower()
    for domain, keywords in _DOMAIN_KEYWORDS:
        if any(k in lowered for k in keywords):
            return domain
    return ""


# --- In-conversation dedup (process-local, ephemeral by design) -------------

_RECENT_POSITIONS: OrderedDict[str, OrderedDict[str, None]] = OrderedDict()
_MAX_TRACKED_SESSIONS = 64  # outer eviction bound — oldest session drops first


def _position_key(position: str) -> str:
    normalized = re.sub(r"\s+", " ", (position or "").strip().lower())[:200]
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def session_key(message) -> str:
    """Conversation identity for dedup via the CANONICAL session-key helper.

    Uses the same ``session_keys.build_session_key`` construction the engine
    uses (engine's session seam), reading the REAL model fields
    (``Channel.platform_id`` / ``Thread.thread_id`` — reading a nonexistent
    ``.id`` collapsed every chat of a platform into one key and
    cross-suppressed dedup). Degraded/stub messages without identity fields
    fall back to one shared ``"unknown"`` bucket — with no conversation
    identity there is nothing finer to key on (documented, test-visible).
    """
    try:
        from session_keys import build_session_key, resolve_thread_id

        platform = getattr(getattr(message, "platform", None), "value", None)
        channel_id = getattr(getattr(message, "channel", None), "platform_id", None)
        if not platform or not channel_id:
            return "unknown"
        thread_id = resolve_thread_id(
            channel_id,
            getattr(getattr(message, "thread", None), "thread_id", None),
        )
        return build_session_key(str(platform), str(channel_id), thread_id)
    except Exception:
        return "unknown"


def _touch_session(session: str) -> OrderedDict[str, None]:
    cache = _RECENT_POSITIONS.setdefault(session, OrderedDict())
    _RECENT_POSITIONS.move_to_end(session)
    while len(_RECENT_POSITIONS) > _MAX_TRACKED_SESSIONS:
        _RECENT_POSITIONS.popitem(last=False)
    return cache


def seen_position(session: str, position: str, *, settings=None) -> bool:
    return _position_key(position) in _RECENT_POSITIONS.get(session, ())


def mark_position(session: str, position: str, *, settings=None) -> None:
    if settings is None:
        from config import get_called_shots_challenge_settings
        settings = get_called_shots_challenge_settings()
    cache = _touch_session(session)
    cache[_position_key(position)] = None
    while len(cache) > settings.dedup_cache_size:
        cache.popitem(last=False)


# --- Evidence (Invariant I-3: recall_service is the ONE recall door) --------


async def gather_receipts(position: str, *, settings=None) -> list[str]:
    """Top recall hits for the position, formatted as receipt strings.

    Consumes the REAL ``RecallResult`` schema (cognition/recall.py: ``path`` /
    ``start_line`` / ``end_line`` / ``text`` — there is no ``.content`` or
    ``.source``; reading phantom fields turned every real hit into [] and
    dead-ended live mode). Uses ``SearchMode.KEYWORD`` deliberately: the
    keyword leg is pure FTS5 (~50ms) and NEVER enters tier classification, so
    the Tier-1 haiku rerank LLM leg is structurally unreachable from this
    call — receipts must not add an LLM to the hot path. Fail-open to [] — a
    recall failure downgrades a would-be challenge to a silent candidate.
    """
    if settings is None:
        from config import get_called_shots_challenge_settings
        settings = get_called_shots_challenge_settings()
    try:
        import recall_service
        from config import MEMORY_DIR

        response = await recall_service.recall(
            position,
            MEMORY_DIR,
            search_mode=recall_service.SearchMode.KEYWORD,
            caller="called_shots_challenge",
            max_results=settings.max_receipts,
        )
        receipts: list[str] = []
        for result in list(getattr(response, "results", []) or [])[: settings.max_receipts]:
            path = getattr(result, "path", "") or "memory"
            start = getattr(result, "start_line", 0)
            end = getattr(result, "end_line", 0)
            snippet = re.sub(r"\s+", " ", str(getattr(result, "text", "") or "")).strip()[:240]
            if snippet:
                span = f":{start}-{end}" if start or end else ""
                receipts.append(f"{path}{span}: {snippet}")
        return receipts
    except Exception as exc:
        print(f"[challenge] receipts gather failed (non-fatal): {exc!r}", flush=True)
        return []


async def gather_receipts_bounded(ctx: dict, challenge_decision: dict) -> None:
    """Fill ``ctx['receipts']`` under a hard time bound (R1 BLOCKER 2).

    Called ONLY from the pass's FIRED branch — gate-closed turns never gather
    (silent candidates don't need receipts, so a closed turn pays ZERO recall).
    The recall leg gets its own ``asyncio.wait_for`` (``receipts_timeout_s``,
    default 3.0s). Honest composition note (Kimi L1): this bound is
    SEQUENTIAL with — not nested inside — the pass's monologue timeout, so a
    fired turn's worst-case added latency is ``receipts_timeout_s`` PLUS the
    pass budget; each leg is individually hard-walled. Timeout/failure ->
    receipts stay [] -> no directive -> the detection degrades to a silent
    candidate. Fail-open.
    """
    import asyncio

    try:
        ctx["receipts"] = await asyncio.wait_for(
            gather_receipts(ctx["position"], settings=ctx["settings"]),
            timeout=ctx["settings"].receipts_timeout_s,
        )
    except (TimeoutError, Exception) as exc:  # noqa: BLE001 - fail-open seam
        print(f"[challenge] receipts bounded gather failed: {exc!r}", flush=True)
        ctx["receipts"] = []
    challenge_decision["receipts"] = len(ctx["receipts"])


# --- The monologue directive + verdict parse --------------------------------

_VERDICT_RE = re.compile(r"^\s*CHALLENGE_VERDICT:\s*(\{.*\})\s*$", re.MULTILINE)


def build_challenge_directive(position: str, receipts: list[str]) -> str:
    """The prompt extension that rides the pass's EXISTING monologue call."""
    receipt_lines = "\n".join(f"- {r}" for r in receipts)
    return (
        "# Challenge Check (called-shots)\n"
        "The operator just STAKED this position:\n"
        f'"{position}"\n\n'
        "Stored memory/receipts that may bear on it:\n"
        f"{receipt_lines}\n\n"
        "As part of your private thinking, judge whether the receipts "
        "MATERIALLY CONTRADICT the operator's position. Only a real, "
        "evidence-backed disagreement counts — do not manufacture one. "
        "Then end your thought with EXACTLY one line in this form:\n"
        'CHALLENGE_VERDICT: {"challenge": true|false, '
        '"counter_position": "<your one-sentence counter-position>", '
        '"reasoning": "<one or two sentences citing the receipts>"}'
    )


_META_LEAD_RE = re.compile(
    r"^\s*(?:#+\s*|SYSTEM\s*:|ASSISTANT\s*:|USER\s*:|INSTRUCTIONS?\s*:|"
    r"IMPORTANT\s*:|IGNORE\b[^.:]*[.:])\s*",
    re.IGNORECASE,
)
_PATHLIKE_RE = re.compile(r"[\w\-./\\]+\.(?:md|py|txt|json|yaml|yml)\b")


def _sanitize_verdict_text(value: object, cap: int) -> str:
    """LLM-authored field -> one flat, cap-bounded, instruction-stripped line.

    The verdict is adversarial input at an identity seam: newlines could smuggle
    structure into the reply directive; role/instruction leads could smuggle
    prompt-injection meta. Fold whitespace, strip meta leads (iteratively), cap.
    """
    text = re.sub(r"[\r\n]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    for _ in range(4):  # bounded iterations — stacked leads stripped, no spin
        stripped = _META_LEAD_RE.sub("", text)
        if stripped == text:
            break
        text = stripped.strip()
    return text[:cap]


def parse_challenge_verdict(thought: str) -> tuple[str, dict | None]:
    """Extract + strip the CHALLENGE_VERDICT block. Hostile-input safe.

    Returns ``(clean_thought, verdict|None)`` — malformed/absent block ->
    ``(thought, None)`` (fail-open; the LLM-authored block is adversarial input
    at an identity seam, so every field is coerced, sanitized, and capped).
    """
    if not thought:
        return thought, None
    match = _VERDICT_RE.search(thought)
    if not match:
        return thought, None
    clean = (thought[: match.start()] + thought[match.end():]).strip()
    try:
        raw = json.loads(match.group(1))
        if not isinstance(raw, dict):
            return clean, None
        verdict = {
            "challenge": bool(raw.get("challenge") is True),
            "counter_position": _sanitize_verdict_text(
                raw.get("counter_position"), 500,
            ),
            "reasoning": _sanitize_verdict_text(raw.get("reasoning"), 800),
        }
        return clean, verdict
    except (json.JSONDecodeError, TypeError, ValueError):
        return clean, None


def validate_citations(verdict: dict, receipts: list[str]) -> bool:
    """PROVENANCE: every path-like citation in the verdict ⊆ gathered receipts.

    The monologue can only cite evidence it was HANDED — a fabricated source
    (``FAKE.md``) in counter_position/reasoning means the verdict invented
    provenance, and the challenge must degrade to a silent candidate rather
    than surface an invented receipt. Path-less verdicts pass (they cite by
    substance; the reply block only ever renders OUR receipt list).
    """
    receipt_blob = "\n".join(receipts).lower()
    for field in ("counter_position", "reasoning"):
        for token in _PATHLIKE_RE.findall(verdict.get(field, "")):
            if token.lower() not in receipt_blob:
                print(
                    f"[challenge] fabricated citation dropped: {token!r}",
                    flush=True,
                )
                return False
    return True


# --- Staking + surfacing -----------------------------------------------------


def resolve_persona_id() -> str:
    """Active profile name (the owner_id seam) — fail-open to "default"."""
    try:
        import personas
        name = (personas.get_active_profile_name() or "").strip()
        return name or "default"
    except Exception:
        return "default"


def stake_shot(
    persona_id: str,
    domain: str,
    operator_position: str,
    homie_position: str,
    homie_reasoning: str,
    receipts: list[str],
) -> tuple[object | None, str]:
    """Record the bet via T1. Returns ``(shot|None, reason)``.

    ``KillSwitchDisabled`` degrades to ``(None, "kill_switch")`` (operator said
    off — respected, never re-raised into the turn); a contract ``ValueError``
    is a CALLER bug surfaced as ``(None, "contract_error")`` with a receipt;
    a runtime failure inside T1 already returns None -> ``"record_failed"``.
    """
    try:
        shot = called_shots.record_shot(
            persona_id,
            domain,
            operator_position,
            homie_position,
            homie_reasoning=homie_reasoning,
            receipts=receipts,
        )
    except called_shots.kill_switches.KillSwitchDisabled:
        return None, "kill_switch"
    except ValueError as exc:
        print(f"[challenge] record_shot contract error: {exc!r}", flush=True)
        return None, "contract_error"
    if shot is None:
        return None, "record_failed"
    return shot, "staked"


def render_challenge_block(shot, verdict: dict, receipts: list[str]) -> str:
    """The reply-side challenge block (rides the uncapped prompt suffix).

    An INSTRUCTION for the reply model, not operator-visible verbatim text —
    the model voices the disagreement in its own words, citing >=1 receipt.
    Only called with a REAL recorded shot (no bet, no challenge).
    """
    receipt_lines = "\n".join(f"- {r}" for r in receipts)
    return (
        "The operator staked a position this turn and the stored evidence "
        "materially disagrees. You MUST open a clearly separated paragraph of "
        "your reply that respectfully but DIRECTLY challenges the position — "
        "no hedging, no burying it. State your counter-position, cite at "
        "least one receipt below by its substance, and close by noting the "
        f"bet is on the books as called-shot #{shot.id} (domain: "
        f"{shot.domain or 'general'}) — the operator's call is final and "
        "override is theirs to make.\n"
        f"Counter-position: {verdict.get('counter_position', '')}\n"
        f"Reasoning: {verdict.get('reasoning', '')}\n"
        "Receipts:\n"
        f"{receipt_lines}"
    )


# --- Engine-facing weave (self-free — the engine calls these as module
# functions so binding `_maybe_cognitive_pass` alone onto a stub keeps
# working; the cognition slice owns the challenge logic) ----------------------


async def prepare_challenge(message, challenge_decision: dict) -> dict | None:
    """Detect a staked position + gather receipts. Whole-body fail-open.

    Returns a context dict (position/session/receipts/domain/settings) or
    None. The soft toggle ``CALLED_SHOTS_ENABLED`` gates this WHOLE autonomous
    surface (the T1 kill-switch stays the data-plane hard gate, enforced
    inside the service and caught at the stake seam). Never raises.
    """
    try:
        from config import (
            get_called_shots_challenge_settings,
            get_called_shots_settings,
        )

        cset = get_called_shots_challenge_settings()
        challenge_decision["mode"] = cset.mode
        if not get_called_shots_settings().enabled:
            challenge_decision["reason"] = "soft_disabled"
            return None
        position = detect_staked_position(
            getattr(message, "text", ""), settings=cset,
        )
        if position is None:
            challenge_decision["reason"] = "no_position"
            return None
        challenge_decision["detected"] = True
        session = session_key(message)
        if seen_position(session, position, settings=cset):
            challenge_decision["reason"] = "dedup"
            return None
        # NO receipts here (R1 BLOCKER 2): prepare runs pre-gate on every
        # detected turn — receipts (a recall call) are gathered ONLY inside
        # the pass's fired branch via gather_receipts_bounded, so gate-closed
        # turns pay zero recall and the gather rides a hard time bound.
        return {
            "position": position,
            "session": session,
            "receipts": [],
            "domain": classify_domain(position),
            "settings": cset,
        }
    except Exception as exc:
        challenge_decision["reason"] = "error"
        print(f"[challenge] prepare failed (non-blocking): {exc!r}", flush=True)
        return None


def silent_stake(ctx: dict | None, challenge_decision: dict, *, note: str) -> None:
    """Record a detected position as a SILENT candidate shot. Fail-open.

    The silent-candidate path (architecture Spike 2): ``decided_by="open"``,
    empty homie position (disagreement unjudged on this path), receipts
    attached for review, voidable via T1's ``void`` outcome.

    IDEMPOTENT AT THE STAKE SEAM (Kimi gate M1): the ledger measures
    POSITIONS, not retries. Entry no-op when the position is already marked
    (the outer rescue path can re-enter after a successful in-try stake), and
    the position is marked BEFORE the record attempt — so a crash anywhere
    after the mark (including a ``KillSwitchDisabled`` refusal, M2) is
    session-scope suppressed instead of retry-looping, and a mark failure
    aborts BEFORE staking (the re-utterance then stakes exactly once). The
    dedup cache is process-local, so a bot restart naturally retries; the
    kill-switch stays authoritative at the data plane.
    """
    if ctx is None:
        return
    try:
        if seen_position(ctx["session"], ctx["position"], settings=ctx["settings"]):
            # Already handled this position (e.g. rescue re-entry after a
            # successful stake) — preserve a truthful staked/reason state.
            if not challenge_decision.get("staked"):
                challenge_decision["reason"] = "dedup"
            return
        # Mark FIRST — a failure here aborts before any ledger write.
        mark_position(ctx["session"], ctx["position"], settings=ctx["settings"])
        shot, why = stake_shot(
            resolve_persona_id(),
            ctx["domain"],
            ctx["position"],
            "",
            f"silent-candidate ({note}): detection-gate hit; "
            "disagreement unjudged",
            ctx["receipts"],
        )
        if shot is not None:
            # L2: the decision reflects the ledger IMMEDIATELY on success,
            # before any later fallible step can misreport a real bet.
            challenge_decision.update(
                staked=True, shot_id=shot.id, reason="silent_candidate",
            )
            print(
                f"[challenge] silent candidate staked (shot #{shot.id}, {note})",
                flush=True,
            )
        else:
            challenge_decision["reason"] = why
    except Exception as exc:
        challenge_decision["reason"] = "error"
        print(f"[challenge] silent stake failed (non-blocking): {exc!r}", flush=True)


def consume_verdict(
    ctx: dict | None,
    challenge_decision: dict,
    *,
    turn_wm,
    out,
    thought: str,
    directive_sent: bool,
):
    """Consume the live-mode CHALLENGE_VERDICT (or fall back to silent).

    Returns the (possibly rebuilt) ``(out_wm, clean_thought)``. Invariants:
    a ``CHALLENGE_VERDICT`` marker is STRIPPED from the thought on EVERY path
    — including turns where detection never fired (a spontaneous marker from
    the monologue is untrusted noise that must never reach the reply prompt);
    a challenge memory is appended ONLY when a bet was actually recorded (no
    bet, no challenge); fabricated citations (provenance ⊄ gathered receipts)
    degrade the challenge to a silent candidate; a judged no-disagreement
    stakes nothing and marks dedup (the judge spoke — don't re-ask this
    session). Whole-body fail-open.
    """
    try:
        from cognition.working_memory import Memory

        # UNCONDITIONAL marker strip (R1 M4c): parse first, on every path.
        clean_thought, verdict = parse_challenge_verdict(thought)
        marker_present = clean_thought != thought

        def _rebuilt_wm():
            # Rebuild the enriched WM from the ORIGINAL turn_wm so the raw
            # JSON block never renders into the reply prompt.
            if not marker_present:
                return out
            if clean_thought:
                return turn_wm.with_memory(Memory(
                    role="system",
                    content=clean_thought,
                    region="internal",
                    source="cognition",
                ))
            return turn_wm

        if ctx is None:
            # No detection this turn — a spontaneous marker is discarded
            # (stripped from the reply prompt), never acted on.
            return _rebuilt_wm(), (clean_thought if marker_present else thought)

        if not directive_sent:
            # Silent mode, or live without receipts — candidate only. A
            # spontaneous marker is still stripped, its verdict ignored.
            silent_stake(ctx, challenge_decision, note="no_directive")
            return _rebuilt_wm(), (clean_thought if marker_present else thought)

        if verdict is None:
            # Absent/malformed block (hostile-input safe): candidate only.
            silent_stake(ctx, challenge_decision, note="no_verdict")
            return _rebuilt_wm(), (clean_thought if marker_present else thought)

        rebuilt = _rebuilt_wm()

        if verdict["challenge"] and not validate_citations(
            verdict, ctx["receipts"],
        ):
            # Fabricated provenance -> the verdict lied about its evidence.
            # Never surface an invented receipt; keep the candidate.
            silent_stake(ctx, challenge_decision, note="fabricated_citation")
            return rebuilt, clean_thought

        if not verdict["challenge"]:
            challenge_decision["reason"] = "no_disagreement"
            mark_position(ctx["session"], ctx["position"], settings=ctx["settings"])
            return rebuilt, clean_thought

        shot, why = stake_shot(
            resolve_persona_id(),
            ctx["domain"],
            ctx["position"],
            verdict["counter_position"],
            verdict["reasoning"],
            ctx["receipts"],
        )
        if shot is None:
            # No bet, no challenge — the reply stays bare (T1 contract:
            # never claim a bet the ledger doesn't hold).
            challenge_decision["reason"] = why
            print(
                f"[challenge] verdict fired but stake failed ({why}) — "
                "challenge NOT surfaced",
                flush=True,
            )
            return rebuilt, clean_thought

        # L2: the decision reflects the ledger IMMEDIATELY on success — a
        # later mark/append failure must never misreport a bet that exists.
        challenge_decision.update(
            staked=True, shot_id=shot.id, reason="challenged",
        )
        mark_position(ctx["session"], ctx["position"], settings=ctx["settings"])
        rebuilt = rebuilt.with_memory(Memory(
            role="system",
            content=render_challenge_block(shot, verdict, ctx["receipts"]),
            region="challenge",
            source="cognition",
        ))
        challenge_decision["surfaced"] = True
        print(
            f"[challenge] surfaced challenge + staked shot #{shot.id} "
            f"(domain: {shot.domain or 'general'})",
            flush=True,
        )
        return rebuilt, clean_thought
    except Exception as exc:
        challenge_decision["reason"] = "error"
        print(f"[challenge] verdict consume failed (non-blocking): {exc!r}", flush=True)
        return out, thought


__all__ = (
    "detect_staked_position",
    "classify_domain",
    "session_key",
    "seen_position",
    "mark_position",
    "gather_receipts",
    "build_challenge_directive",
    "parse_challenge_verdict",
    "resolve_persona_id",
    "stake_shot",
    "render_challenge_block",
    "prepare_challenge",
    "silent_stake",
    "consume_verdict",
)
