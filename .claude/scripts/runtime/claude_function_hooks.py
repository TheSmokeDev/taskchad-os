"""Optional Claude Mods transport into the Python-owned cognitive lifecycle.

No model call runs in a function hook. Each callback is bound to a host-created,
short-lived context and only records/enqueues work in that persona's service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_EVENTS = frozenset({"session.start", "tool.call", "turn.complete", "prompt.submit"})
_PROBES = {}


def _root():
    from personas import core

    return Path(core.get_default_paths()["state"]) / "cognition-hooks"


def probe(cli_path: str) -> dict:
    """Generate declarations with this actual executable; never infer from version."""
    cli = Path(cli_path)
    try:
        stamp = (str(cli.resolve()), cli.stat().st_mtime_ns, cli.stat().st_size)
    except OSError:
        return {"supported": False, "reason": "cli_unavailable"}
    if stamp in _PROBES:
        return _PROBES[stamp]
    from runtime.subprocess_env import scrub_nested_claude_state

    env = scrub_nested_claude_state()
    env.update(
        CLAUDE_CODE_ENABLE_FUNCTION_HOOKS="1",
        DISABLE_TELEMETRY="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    phase = "mkdir"
    try:
        probe_root = _root() / "probes"
        probe_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="homie-hook-probe-", dir=probe_root, ignore_cleanup_errors=True
        ) as directory:
            args = [
                str(cli),
                "-p",
                "/plugin-types",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--tools",
                "",
                "--no-session-persistence",
                "--max-turns",
                "1",
            ]
            phase = "invoke_cli"
            result = subprocess.run(
                args,
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            declarations = Path(directory) / ".claude/types/claude-code.d.ts"
            phase = "read_types"
            text = declarations.read_text(encoding="utf-8") if declarations.is_file() else ""
            supported = result.returncode == 0 and all(
                marker in text
                for marker in (
                    "'session.start'",
                    "'tool.call'",
                    "'turn.complete'",
                    "'prompt.submit'",
                    "ProcessRunInit",
                    "stdin?: string",
                )
            )
            report = {
                "supported": supported,
                "reason": "verified_types" if supported else "types_unavailable",
                "types_sha256": hashlib.sha256(text.encode()).hexdigest() if text else None,
            }
            phase = "cleanup"
    except (OSError, subprocess.TimeoutExpired) as exc:
        report = {
            "supported": False,
            "reason": "probe_unavailable",
            "error_type": type(exc).__name__,
            "operation": phase,
            "file": Path(getattr(exc, "filename", "") or "").name,
        }
    _PROBES[stamp] = report
    return report


def prepare(request, *, cli_path=None):
    """Return SDK option additions and a receipt; strict model-only stays hookless."""
    receipt = {"adapter": "engine_sdk", "reason": "host_lifecycle", "events": []}
    if request.model_only:
        receipt["reason"] = "model_only_host_callbacks"
        return {}, receipt
    learning = (request.metadata or {}).get("learning") or {}
    if not learning.get("experience_id"):
        receipt["reason"] = "no_host_experience"
        return {}, receipt
    if os.getenv("HOMIE_COGNITION_FUNCTION_HOOKS", "auto").casefold() in {
        "off",
        "false",
        "0",
        "disabled",
    }:
        receipt["reason"] = "mods_disabled"
        return {}, receipt
    if cli_path is None:
        import shutil

        cli_path = os.getenv("HOMIE_CLAUDE_CLI_PATH") or shutil.which("claude")
    if not cli_path:
        receipt["reason"] = "cli_unavailable"
        return {}, receipt
    verified = probe(cli_path)
    if not verified.get("supported"):
        receipt["reason"] = verified["reason"]
        return {}, receipt
    from personas.learning.service import get_learning_service

    persona = (request.metadata or {}).get("persona_id")
    service = get_learning_service(persona)
    experience = service.get_record(learning["experience_id"])
    if not experience or experience["kind"] != "experience" or not service.enabled():
        receipt["reason"] = "unavailable_host_experience"
        return {}, receipt
    context_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
    context = {
        "id": context_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "persona_id": persona,
        "experience_id": experience["id"],
        "origin_key": learning.get("origin_key", experience["id"]),
        "expires_at": time.time() + 3600,
        "context_prepared": True,
    }
    directory = _root()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (context_id + ".json")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(context, handle)
    plugin = Path(__file__).resolve().parents[2] / "plugins/persona-cognition"
    command = Path(__file__).resolve().parents[1] / "persona_cognition_hook.py"
    receipt.update(
        adapter="claude_mods",
        reason="verified_types",
        context_id=context_id,
        types_sha256=verified.get("types_sha256"),
    )
    return {
        "cli_path": str(cli_path),
        "plugins": [{"type": "local", "path": str(plugin)}],
        "env": {
            "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1",
            "HOMIE_COGNITION_HOOK_CONTEXT": context_id,
            "HOMIE_COGNITION_HOOK_TOKEN": token,
            "HOMIE_COGNITION_HOOK_PYTHON": sys.executable,
            "HOMIE_COGNITION_HOOK_COMMAND": str(command),
        },
    }, receipt


def _clean(value):
    from personas.learning.models import is_credential_key
    from security.redact import redact_sensitive_text

    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if not is_credential_key(str(k))}
    if isinstance(value, list):
        return [_clean(x) for x in value[:40]]
    if isinstance(value, str):
        return redact_sensitive_text(value)[:12000]
    return value if value is None or isinstance(value, (bool, int, float)) else str(value)[:300]


def ingest(context_id, token, payload, *, service=None, directory=None):
    """Internal CLI boundary: persona/experience come only from the host context."""
    if not re.fullmatch(r"[a-f0-9]{32}", str(context_id)) or not isinstance(token, str):
        raise ValueError("invalid hook context")
    root = Path(directory) if directory is not None else _root()
    path = root / (context_id + ".json")
    if path.is_symlink() or (
        path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("hook context cannot be a link")
    context = json.loads(path.read_text(encoding="utf-8"))
    if context.get("closed") or context.get("expires_at", 0) <= time.time():
        raise ValueError("expired hook context")
    if not hmac.compare_digest(context["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
        raise ValueError("invalid hook credential")
    if not isinstance(payload, dict) or payload.get("event") not in _EVENTS:
        raise ValueError("unsupported hook event")
    if payload.get("persona_id", context["persona_id"]) != context["persona_id"]:
        raise ValueError("hook persona cannot be changed")
    from personas.learning.service import get_learning_service

    service = service or get_learning_service(context["persona_id"])
    if service.target.persona_id != context["persona_id"]:
        raise ValueError("hook service does not own the context")
    experience = service.get_record(context["experience_id"])
    if not experience or experience["kind"] != "experience":
        raise ValueError("hook experience is unavailable")
    event = payload["event"]
    event_key = str(payload.get("event_id") or event)[:200]
    key = f"mods:{context_id}:{event_key}"
    service.store.event(
        experience["id"],
        "function_hook_started",
        {"event": event, "context_id": context_id},
        key=key + ":started",
    )
    try:
        result = _process_event(service, experience, context, event, key, event_key, payload)
    except Exception as exc:
        service.store.event(
            experience["id"],
            "function_hook_failed",
            {"event": event, "context_id": context_id, "error_type": type(exc).__name__},
            key=key + ":failed",
        )
        raise
    service.store.event(
        experience["id"],
        "function_hook_processed",
        {"event": event, "context_id": context_id, "status": result.get("status", "queued")},
        key=key + ":processed",
    )
    return result


def _process_event(service, experience, context, event, key, event_key, payload):
    from personas.learning.models import content_hash
    from runtime.function_hooks import emit_cognitive_event

    if not service.enabled():
        return {"ok": True, "status": "paused", "context": ""}
    evidence_ids = [experience["id"]]
    if event == "tool.call":
        details = _clean(payload.get("details", {}))
        observation = service.record_observation(
            experience["id"],
            {
                "status": "partial",
                "quality": "direct",
                "evidence": {
                    "kind": "native_tool_result",
                    "details": details,
                    "event_id": event_key,
                    "hash": content_hash(details),
                },
                "domain_outcome_observed": False,
            },
            source_key=key,
        )
        evidence_ids.append(observation["id"])
    if event == "turn.complete":
        return {"ok": True, "status": "host_debrief_pending", "context": ""}
    phase = {"session.start": "reorient", "tool.call": "interpret", "prompt.submit": "reorient"}[
        event
    ]
    cycle = emit_cognitive_event(
        phase,
        context["origin_key"],
        evidence_ids,
        service=service,
        experience_id=experience["id"],
        metadata={"adapter": "claude_mods", "event": event},
    )
    return {"ok": True, "cycle_id": cycle["id"], "context": ""}


def finish(receipt, service=None):
    """Read actual hook coverage; host callbacks remain the recovery path."""
    context_id = receipt.get("context_id")
    if not context_id:
        return receipt
    try:
        path = _root() / (context_id + ".json")
        context = json.loads(path.read_text(encoding="utf-8"))
        from personas.learning.service import get_learning_service

        service = service or get_learning_service(context["persona_id"])
        events = service.store.events(context["experience_id"])
        receipt["events"] = sorted(
            {
                x.get("payload", {}).get("event")
                for x in events
                if x.get("event_type") == "function_hook_processed"
                and x.get("payload", {}).get("context_id") == context_id
                and x.get("payload", {}).get("event")
            }
        )
        required = {"session.start", "prompt.submit", "turn.complete"}
        receipt["missing_events"] = sorted(required - set(receipt["events"]))
        receipt["coverage"] = "observed" if not receipt["missing_events"] else "partial"
        receipt["recovery_adapter"] = "engine_sdk" if receipt["missing_events"] else None
        context["closed"] = True
        path.write_text(json.dumps(context), encoding="utf-8")
    except Exception:
        receipt["coverage"] = "receipt_unavailable"
    return receipt
