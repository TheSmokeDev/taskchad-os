"""Verify-before-commit model switching for all operator chat surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
from pathlib import Path

from . import opencode_free
from .model_control import (
    apply_runtime_model_choice,
    resolve_runtime_model_choice,
    selected_runtime_model,
)
from .selection import (
    apply_runtime_selection_choice,
    describe_runtime_selection,
    resolve_runtime_selection,
)

_lock = threading.RLock()
_generations: dict[str, int] = {}


class ModelSwitchReply(str):
    """String-compatible router reply with truthful machine failure metadata."""

    def __new__(cls, message: str, *, success: bool, code: str | None = None):
        obj = super().__new__(cls, message)
        obj.is_error = not success
        obj.error_code = code
        return obj


def selection_label(env) -> str:
    selection = resolve_runtime_selection(env)
    return (
        f"{describe_runtime_selection(selection)} "
        f"[model: {selected_runtime_model(selection, env) or 'auto'}]"
    )


def _fingerprint(path: Path | None):
    if path is None or not path.exists():
        return None
    return (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).digest())


def _selection_snapshot(env) -> dict:
    return {
        k: v
        for k, v in env.items()
        if k.startswith("SECOND_BRAIN_")
        and ("MODEL" in k or "PROVIDER" in k or k == "SECOND_BRAIN_RUNTIME_LANE")
    }


def _write_updates(path: Path, updates: dict[str, str | None]) -> None:
    from shared import atomic_write_text

    content = path.read_text(encoding="utf-8") if path.exists() else ""
    for key, value in updates.items():
        pattern = re.compile(rf"^(?:export\s+)?{re.escape(key)}\s*=.*(?:\n|$)", re.M)
        # Values are canonical identifiers, not secrets. Quoting still handles
        # punctuation accepted by other providers without reinterpreting it.
        line = "" if value is None else f"{key}='{value.replace(chr(39), chr(92) + chr(39))}'\n"
        if pattern.search(content):
            content = pattern.sub(lambda _: line, content)
        elif line:
            content = content.rstrip("\n") + ("\n" if content else "") + line
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)


async def switch_runtime_model(raw: str, *, env_path: Path | None = None, environ=None):
    """Only successful probes may commit Free. Never hold a file lock over I/O."""
    env = os.environ if environ is None else environ
    path = Path(env_path).resolve() if env_path is not None else None
    key = str(path) if path is not None else f"process:{id(env)}"
    updates: dict[str, str | None] = {}
    candidate = dict(env)
    try:
        if any(c in raw for c in "\r\n\x00"):
            raise ValueError("Invalid selection")
        kwargs = dict(
            environ=candidate,
            write_key=lambda k, v: updates.__setitem__(k, v),
            delete_key=lambda k: updates.__setitem__(k, None),
        )
        if resolve_runtime_model_choice(raw):
            apply_runtime_model_choice(raw, **kwargs)
        else:
            apply_runtime_selection_choice(raw, **kwargs)
    except ValueError:
        return ModelSwitchReply(
            f"Unknown runtime selection. Selection unchanged: {selection_label(env)}.",
            success=False,
            code="INVALID_SELECTION",
        )
    free = resolve_runtime_selection(candidate).generic_provider == opencode_free.FREE_PROVIDER
    try:
        with _lock:
            generation = _generations.get(key, 0) + 1
            _generations[key] = generation
            before_env = _selection_snapshot(env)
            before_file = _fingerprint(path)
        if free:
            await opencode_free.probe_free_model(
                selected_runtime_model(
                    resolve_runtime_selection(candidate),
                    candidate,
                )
            )

        def commit():
            from contextlib import nullcontext

            from shared import file_lock

            with _lock, file_lock(path, timeout=5) if path is not None else nullcontext():
                if (
                    _generations.get(key) != generation
                    or _selection_snapshot(env) != before_env
                    or _fingerprint(path) != before_file
                ):
                    raise opencode_free.FreeRuntimeError(
                        "SELECTION_CHANGED", "A newer selection superseded this request"
                    )
                if path is not None:
                    _write_updates(path, updates)
                for k, v in updates.items():
                    if v is None:
                        env.pop(k, None)
                    else:
                        env[k] = v

        await asyncio.to_thread(commit)
    except Exception as exc:
        code = opencode_free.error_code(exc)
        return ModelSwitchReply(
            f"{'Free switch' if free else 'Model switch'} failed — {code}. "
            f"Selection unchanged by this request: {selection_label(env)}. Try again later.",
            success=False,
            code=code,
        )
    return ModelSwitchReply(
        f"Switched to {selection_label(env)}. "
        + ("Anonymous Free probe succeeded; paid fallback is disabled. " if free else "")
        + "Next message uses this runtime selection.",
        success=True,
    )
