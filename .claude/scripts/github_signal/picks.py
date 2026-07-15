"""Contextual backlog picks — one background-tier LLM call, deterministic fallback.

The whole eligible backlog (~15 tokens/repo) plus an active-work excerpt goes
into a single quality-tier call that returns ``[{full_name, why_now}]``.
Hallucinated names are dropped and topped up from the fallback; on any LLM
failure the picks degrade to most-recently-starred — the digest always writes.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import config as _main_config  # noqa: E402
from runtime.base import RuntimeRequest  # noqa: E402
from runtime.capabilities import TEXT_REASONING  # noqa: E402
from runtime.lane_router import run_with_runtime_lanes  # noqa: E402

_PICKS_PROMPT = """You pick which of the operator's starred GitHub repos to resurface this week.

## Active work (what the operator is doing RIGHT NOW)
{context}

## Starred backlog (oldest star first — format: starred_date | full_name | language | description)
{inventory}

Pick exactly {n} repos from the backlog that are most useful to the active work right now.
Rules:
- full_name must be copied EXACTLY from the backlog list.
- why_now: one line, <=120 chars, a concrete bridge from the active work to the repo
  ("You're wiring X — this gives you Y"). Generic praise is a failure.
- When relevance ties, prefer older stars (more forgotten).
Return ONLY a JSON array: [{{"full_name": "...", "why_now": "..."}}]"""

_FALLBACK_WHY = "(fallback: recent backlog star — contextual matching unavailable this week)"


def _read_capped(path: Path, cap: int, tail: bool = False) -> str:
    """Read a file head/tail-capped; missing/unreadable → empty string."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) <= cap:
        return text
    return text[-cap:] if tail else text[:cap]


def _gather_context() -> str:
    """Active-work excerpt: GOALS + PRP tracker + last 7 daily logs. Fail-open."""
    sections: list[str] = []

    goals = _read_capped(_main_config.GOALS_FILE, 1500)
    if goals:
        sections.append(f"### Goals\n{goals}")

    tracker = _read_capped(
        _main_config.PROJECT_ROOT / "PRPs" / "active" / "TRACKER.md", 2000
    )
    if tracker:
        sections.append(f"### Active PRP tracker\n{tracker}")

    try:
        logs = sorted(_main_config.DAILY_DIR.glob("*.md"), reverse=True)[:7]
    except OSError:
        logs = []
    for log in reversed(logs):
        body = _read_capped(log, 1200, tail=True)
        if body:
            sections.append(f"### Daily log {log.stem}\n{body}")

    return "\n\n".join(sections) if sections else "(no active-work context found)"


def _inventory_block(eligible: list[dict[str, Any]]) -> str:
    """One line per repo, oldest star first (more forgotten = more visible)."""
    ordered = sorted(eligible, key=lambda i: i.get("starred_at") or "")
    lines = []
    for item in ordered:
        starred = str(item.get("starred_at") or "")[:10] or "unknown"
        desc = (item.get("description") or "").strip()[:110]
        lang = item.get("language") or "-"
        lines.append(f"{starred} | {item['full_name']} | {lang} | {desc}")
    return "\n".join(lines)


def _extract_json_array(text: str) -> list | None:
    """Pull a JSON array out of an LLM reply (handles ```json fences / prose)."""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, list) else None
    except Exception:
        pass
    i, j = t.find("["), t.rfind("]")
    if 0 <= i < j:
        try:
            data = json.loads(t[i : j + 1])
            return data if isinstance(data, list) else None
        except Exception:
            return None
    return None


def _fallback_picks(eligible: list[dict[str, Any]], n: int) -> list[dict[str, str]]:
    """Deterministic degradation: most recently starred eligible repos."""
    ordered = sorted(eligible, key=lambda i: i.get("starred_at") or "", reverse=True)
    return [
        {"full_name": item["full_name"], "why_now": _FALLBACK_WHY}
        for item in ordered[:n]
    ]


async def pick_backlog(
    eligible: list[dict[str, Any]],
    n: int,
    max_budget_usd: float | None = None,
    model: str | None = None,
) -> tuple[list[dict[str, str]], bool]:
    """Return (picks, used_llm). Never raises; never returns hallucinated names."""
    if not eligible or n <= 0:
        return [], False
    if max_budget_usd is None:
        from github_signal.config import get_github_signal_settings

        max_budget_usd = get_github_signal_settings().max_budget_usd
    if model is None:
        model = _main_config.get_background_models()["quality"]

    eligible_names = {item["full_name"] for item in eligible}
    n = min(n, len(eligible))

    llm_picks: list[dict[str, str]] = []
    try:
        req = RuntimeRequest(
            prompt=_PICKS_PROMPT.format(
                n=n, context=_gather_context(), inventory=_inventory_block(eligible)
            ),
            cwd=_main_config.PROJECT_ROOT,
            task_name="github_signal_picks",
            capability=TEXT_REASONING,
            max_turns=1,
            max_budget_usd=max_budget_usd,
            allowed_tools=[],
            env={"CLAUDECODE": ""},
            model=model,
        )
        result = await run_with_runtime_lanes(req)
        raw = _extract_json_array(getattr(result, "text", "") or "")
        seen: set[str] = set()
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            full = str(entry.get("full_name", "")).strip()
            if full in eligible_names and full not in seen:
                seen.add(full)
                why = str(entry.get("why_now", "")).strip()[:160]
                llm_picks.append({"full_name": full, "why_now": why})
            if len(llm_picks) >= n:
                break
    except Exception:
        llm_picks = []

    used_llm = bool(llm_picks)
    if len(llm_picks) < n:
        chosen = {p["full_name"] for p in llm_picks}
        for fb in _fallback_picks(
            [i for i in eligible if i["full_name"] not in chosen], n - len(llm_picks)
        ):
            llm_picks.append(fb)
    return llm_picks[:n], used_llm
