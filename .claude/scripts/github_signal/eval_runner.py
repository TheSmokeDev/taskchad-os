"""Repo evaluation — shallow clone, read-only analysis, verdict card.

Run detached by the /stars eval handler::

    uv run python -m github_signal.eval_runner <owner/repo>

Degradation ladder (the card ALWAYS ships): oversize or clone failure →
API-only evidence (metadata + raw README endpoint); LLM failure → facts-only
card with verdict "unavailable". Repo code is NEVER executed, installed, or
tested — evidence gathering is file reads only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

# Boot-shim: must run BEFORE any framework imports
from personas import apply_persona_override

apply_persona_override()

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import config as _main_config  # noqa: E402
from config import now_local  # noqa: E402
from runtime.base import RuntimeRequest  # noqa: E402
from runtime.capabilities import TEXT_REASONING  # noqa: E402
from runtime.lane_router import run_with_runtime_lanes  # noqa: E402

from github_signal import state as state_mod  # noqa: E402
from github_signal.config import (  # noqa: E402
    GITHUB_SIGNAL_DIR,
    REPO_EVAL_SANDBOX_DIR,
    get_github_signal_settings,
)
from github_signal.fetch import GITHUB_API_BASE, api_headers  # noqa: E402
from github_signal.picks import _gather_context  # noqa: E402

_README_NAMES = ("README.md", "README.rst", "README.txt", "README")
_MANIFEST_NAMES = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "setup.py",
    "Dockerfile",
    "composer.json",
)
_VALID_RECOMMENDATIONS = {"adopt", "try", "skip"}

_EVAL_PROMPT = """You evaluate one GitHub repository for the operator. Analysis is read-only —
the repo's code was NOT executed.

## Repo facts (from the GitHub API)
{metadata}

## Active work (what the operator is doing RIGHT NOW)
{context}

## Evidence (file tree, README head, manifest excerpts — capped)
{evidence}

Return ONLY a JSON object:
{{"what_it_is": "<=200 chars — what this repo actually is, from evidence not hype",
  "fit_with_active_work": "<=200 chars — concrete bridge to the active work, or 'none'",
  "recommendation": "adopt" | "try" | "skip",
  "why": "<=240 chars — the deciding evidence (recency, deps, scope, maturity)",
  "effort_estimate": "<=80 chars — e.g. '30 min spike', 'weekend integration'"}}
Rules: recommendation must reflect fit AND maintenance signals (pushed_at, archived).
Generic praise is a failure. If evidence is thin, say so in why and prefer "try" or "skip"."""


def _api_get(url: str, accept: str | None = None, timeout: float = 30.0) -> Any:
    """GET a GitHub API URL; returns parsed JSON (or raw text for raw Accept).
    Returns None on any failure — the eval degrades, it never dies here."""
    req = urllib.request.Request(url)
    for k, v in api_headers().items():
        req.add_header(k, v)
    if accept:
        req.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if accept and "raw" in accept:
        return raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _rmtree(path: Path) -> None:
    """rmtree that survives Windows read-only .git objects."""

    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


def _clone(full_name: str, sandbox: Path, timeout: float = 180.0) -> bool:
    """Shallow clone. Returns False on any failure (evidence degrades)."""
    if sandbox.exists():
        _rmtree(sandbox)
    sandbox.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                "git", "clone", "--depth", "1", "--single-branch",
                f"https://github.com/{full_name}.git", str(sandbox),
            ],
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0 and sandbox.is_dir()
    except (subprocess.TimeoutExpired, OSError):
        return False


def _gather_clone_evidence(sandbox: Path) -> str:
    """File tree + README head + manifest heads. Pure reads — NEVER executes."""
    parts: list[str] = []

    entries: list[str] = []
    for root, dirs, files in os.walk(sandbox):
        dirs[:] = [d for d in dirs if d != ".git"]
        rel_root = Path(root).relative_to(sandbox)
        for name in sorted(dirs) + sorted(files):
            entries.append(str(rel_root / name).replace("\\", "/"))
            if len(entries) >= 400:
                break
        if len(entries) >= 400:
            entries.append("... (tree capped at 400 entries)")
            break
    parts.append("### File tree\n" + "\n".join(entries))

    for name in _README_NAMES:
        readme = sandbox / name
        if readme.is_file():
            try:
                head = readme.read_text(encoding="utf-8", errors="replace")[:4000]
                parts.append(f"### {name} (head)\n{head}")
            except OSError:
                pass
            break

    for name in _MANIFEST_NAMES:
        manifest = sandbox / name
        if manifest.is_file():
            try:
                head = manifest.read_text(encoding="utf-8", errors="replace")[:2000]
                parts.append(f"### {name}\n{head}")
            except OSError:
                pass

    return "\n\n".join(parts)


def _metadata_block(meta: dict[str, Any]) -> str:
    if not meta:
        return "(GitHub API metadata unavailable)"
    fields = (
        ("full_name", meta.get("full_name")),
        ("description", meta.get("description")),
        ("language", meta.get("language")),
        ("stars", meta.get("stargazers_count")),
        ("pushed_at", meta.get("pushed_at")),
        ("archived", meta.get("archived")),
        ("license", (meta.get("license") or {}).get("spdx_id")),
        ("topics", ", ".join(meta.get("topics") or [])),
        ("size_kb", meta.get("size")),
    )
    return "\n".join(f"- {k}: {v}" for k, v in fields if v not in (None, ""))


def _extract_json_object(text: str) -> dict | None:
    """Pull a JSON object out of an LLM reply (fences / prose tolerated)."""
    if not text:
        return None
    import re

    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            data = json.loads(t[i : j + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _validate_verdict(raw: dict | None) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    rec = str(raw.get("recommendation", "")).strip().lower()
    if rec not in _VALID_RECOMMENDATIONS:
        return None
    caps = (
        ("what_it_is", 220),
        ("fit_with_active_work", 220),
        ("why", 260),
        ("effort_estimate", 100),
    )
    verdict = {"recommendation": rec}
    for key, cap in caps:
        verdict[key] = str(raw.get(key, "")).strip()[:cap]
    return verdict


async def _llm_verdict(
    meta_block: str, evidence: str, max_budget_usd: float
) -> dict[str, str] | None:
    try:
        req = RuntimeRequest(
            prompt=_EVAL_PROMPT.format(
                metadata=meta_block, context=_gather_context(), evidence=evidence
            ),
            cwd=_main_config.PROJECT_ROOT,
            task_name="github_signal_eval",
            capability=TEXT_REASONING,
            max_turns=1,
            max_budget_usd=max_budget_usd,
            allowed_tools=[],
            env={"CLAUDECODE": ""},
            model=_main_config.get_background_models()["quality"],
        )
        result = await run_with_runtime_lanes(req)
        return _validate_verdict(
            _extract_json_object(getattr(result, "text", "") or "")
        )
    except Exception:
        return None


def _render_card(
    full_name: str,
    meta: dict[str, Any],
    verdict: dict[str, str] | None,
    clone_skipped: str | None,
    note_path: Path,
) -> str:
    stars = meta.get("stargazers_count", "?")
    lang = meta.get("language") or "-"
    pushed = str(meta.get("pushed_at") or "?")[:10]
    size_mb = round((meta.get("size") or 0) / 1024)
    lines = [
        f"🔬 Repo Eval — {full_name}",
        f"★ {stars} · {lang} · pushed {pushed} · ~{size_mb}MB",
    ]
    if verdict:
        lines += [
            f"What: {verdict['what_it_is']}",
            f"Fit: {verdict['fit_with_active_work']}",
            f"Verdict: {verdict['recommendation'].upper()} — {verdict['why']}",
            f"Effort: {verdict['effort_estimate']}",
        ]
    else:
        lines.append("Verdict: unavailable (LLM failed — facts only)")
    if clone_skipped:
        lines.append(f"(clone skipped: {clone_skipped} — API-only evidence)")
    lines += [
        f"Note: {note_path}",
        f"/stars used {full_name} · /stars snooze {full_name}",
    ]
    return "\n".join(lines)


def _eval_note_path(full_name: str) -> Path:
    owner, repo = full_name.split("/", 1)
    return (
        GITHUB_SIGNAL_DIR / "evals" / f"{owner}__{repo}--{date.today().isoformat()}.md"
    )


def _write_eval_note(
    full_name: str,
    meta_block: str,
    verdict: dict[str, str] | None,
    card: str,
) -> Path:
    path = _eval_note_path(full_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    rec = verdict["recommendation"] if verdict else "unavailable"
    body = (
        f"---\n"
        f"tags: [signal, github, eval, auto-generated]\n"
        f"repo: {full_name}\n"
        f"date: {today}\n"
        f"recommendation: {rec}\n"
        f"---\n\n"
        f"# Repo Eval — {full_name}\n\n"
        f"```\n{card}\n```\n\n"
        f"## Repo facts\n\n{meta_block}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def _notify_both(card: str) -> tuple[bool, bool]:
    tg_sent = False
    dc_sent = False
    try:
        from social import notify as social_notify

        tg_sent = social_notify.send_text_to_telegram(card)
    except Exception:
        tg_sent = False
    try:
        channel_id = get_github_signal_settings().discord_channel_id
        if channel_id:
            from social import notify as social_notify

            dc_sent = social_notify.send_text_to_discord(card, channel_id)
    except Exception:
        dc_sent = False
    return tg_sent, dc_sent


async def run_eval(full_name: str) -> str:
    """Run one evaluation. Returns 'invalid' or 'done' (degraded runs are done)."""
    full_name = full_name.strip()
    if not state_mod.FULL_NAME_RE.fullmatch(full_name):
        print(f"eval_runner: invalid repo name {full_name!r} (want owner/repo)")
        return "invalid"

    settings = get_github_signal_settings()
    print(f"[{now_local()}] Repo eval: {full_name} — fetching API metadata...")
    meta = _api_get(f"{GITHUB_API_BASE}/repos/{full_name}") or {}
    meta_block = _metadata_block(meta)

    clone_skipped: str | None = None
    evidence = ""
    sandbox = REPO_EVAL_SANDBOX_DIR / full_name.replace("/", "__")

    size_mb = (meta.get("size") or 0) / 1024
    if size_mb > settings.eval_max_repo_mb:
        clone_skipped = f"size ~{round(size_mb)}MB > {settings.eval_max_repo_mb}MB"
    else:
        print(f"[{now_local()}] Repo eval: shallow-cloning into {sandbox}...")
        if not _clone(full_name, sandbox):
            clone_skipped = "clone failed"

    if clone_skipped is None:
        evidence = _gather_clone_evidence(sandbox)
    else:
        readme = _api_get(
            f"{GITHUB_API_BASE}/repos/{full_name}/readme",
            accept="application/vnd.github.raw+json",
        )
        evidence = (
            f"### README (via API, head)\n{str(readme)[:4000]}"
            if readme
            else "(no clone, no README — metadata only)"
        )

    print(f"[{now_local()}] Repo eval: running verdict call...")
    verdict = await _llm_verdict(meta_block, evidence, settings.max_budget_usd)

    # Card + durable note ALWAYS ship, whatever degraded above.
    card = _render_card(
        full_name, meta, verdict, clone_skipped, _eval_note_path(full_name)
    )
    note_path = _write_eval_note(full_name, meta_block, verdict, card)
    tg_sent, dc_sent = _notify_both(card)

    try:
        state_mod.record_eval(
            full_name, verdict["recommendation"] if verdict else "unavailable"
        )
    except Exception as exc:
        print(f"[{now_local()}] Repo eval: state record failed (non-fatal): {exc}")

    if sandbox.is_dir() and not settings.eval_keep_clone:
        try:
            _rmtree(sandbox)
        except Exception as exc:
            print(f"[{now_local()}] Repo eval: cleanup failed (non-fatal): {exc}")

    # Scout memory sync (fail-open — persona may not exist)
    try:
        from github_signal.scout_sync import sync_to_scout

        sync_to_scout([note_path])
    except Exception as exc:
        print(f"[{now_local()}] Repo eval: scout sync failed (non-fatal): {exc}")

    print(
        f"[{now_local()}] Repo eval: done — note={note_path}, "
        f"telegram={'sent' if tg_sent else 'FAILED'}, "
        f"discord={'sent' if dc_sent else 'off/failed'}, "
        f"verdict={'llm' if verdict else 'unavailable'}"
    )
    return "done"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m github_signal.eval_runner",
        description="Read-only repo evaluation — clone shallow, assess, card",
    )
    parser.add_argument("full_name", help="owner/repo to evaluate")
    args = parser.parse_args()
    result = asyncio.run(run_eval(args.full_name))
    sys.exit(1 if result == "invalid" else 0)


if __name__ == "__main__":
    main()
