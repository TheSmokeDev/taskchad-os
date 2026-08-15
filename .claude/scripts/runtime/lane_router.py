"""Lane-first runtime orchestration.

PR1 scope:
- preserve existing adapter behavior
- introduce lane-first selection
- keep registry.py as a compatibility shim

PRD-8 Phase 7a WS4 — `requireEnabled("llm")` is invoked at the head of
`run_with_runtime_lanes` so any LLM lane execution is gated by the operator
kill-switch. Module-attribute lookup (Rule 3) — `from security import
kill_switches` then `kill_switches.requireEnabled(...)`. Top-level
`from security.kill_switches import requireEnabled` would defeat
monkeypatch propagation in tests.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass, replace

from security import kill_switches

from . import base as _base
from .base import (
    RUNTIME_LANE_CLAUDE_NATIVE,
    RUNTIME_LANE_GENERIC,
    RuntimeRequest,
    RuntimeResult,
)
from .capabilities import TEXT_REASONING
from .claude_sdk import ClaudeSdkRuntime
from .errors import (
    RuntimeCallerToolTransportError,
    RuntimeConfigError,
    RuntimeExecutionError,
    RuntimeRetryableError,
    RuntimeUnsupportedCapabilityError,
)
from .gemini_cli import GeminiCliRuntime
from .health import mark_profile_retryable_failure, mark_profile_success, mark_profile_unavailable
from .openai_codex_app_server import OpenAICodexAppServerRuntime
from .openai_compatible import OpenAICompatibleRuntime
from .profiles import (
    GENERIC_PROVIDER_REGISTRY,
    RuntimeProfile,
    build_profile_for_provider,
)
from .routing import resolve_generic_runtime_profiles
from .selection import resolve_runtime_selection

_logger = logging.getLogger(__name__)

# Per-adapter deadlines resolved at CALL time (Rule 1) — never bound as
# defaults. A wedged provider CLI (a Codex/Gemini child that never exits) or a
# stalled Claude SDK stream otherwise hangs every scheduled pipeline forever
# (heartbeat, reflection, weekly, dream, cabinet, persona learning — 25+ call
# sites with no outer deadline). `asyncio.wait_for` at this one lane chokepoint
# bounds all of them; `<=0` disables the deadline (escape hatch). Issue #133.
_DEFAULT_TIMEOUT_TEXT_S = 300.0
_DEFAULT_TIMEOUT_TOOL_S = 1800.0


@dataclass(frozen=True)
class CallerToolTransportCandidate:
    """One configured runtime profile's caller-tool carriage result."""

    provider: str
    carries_caller_tools: bool
    error: str | None = None


@dataclass(frozen=True)
class CallerToolTransportProbe:
    """Read-only selected-lane transport facts for diagnostics consumers."""

    lane: str
    candidates: tuple[CallerToolTransportCandidate, ...]


def _adapter_timeout_seconds(request: RuntimeRequest) -> float | None:
    """Per-adapter deadline in seconds. ``<=0`` disables (escape hatch)."""
    if request.capability == TEXT_REASONING:
        raw = os.getenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "")
        default = _DEFAULT_TIMEOUT_TEXT_S
    else:
        raw = os.getenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TOOL_SECONDS", "")
        default = _DEFAULT_TIMEOUT_TOOL_S
    try:
        value = float(raw) if raw.strip() else default
    except ValueError:
        value = default
    if not math.isfinite(value):
        # float("nan") parses cleanly (no ValueError) but every comparison
        # against NaN is False, so `value > 0` below would silently take the
        # <=0 disable branch instead of falling back to `default`.
        value = default
    return value if value > 0 else None


def resolve_runtime_lane(request: RuntimeRequest) -> str:
    """Choose the top-level runtime lane for a request."""

    if request.runtime_lane:
        return request.runtime_lane
    selection = resolve_runtime_selection()
    if selection.lane:
        return selection.lane
    if request.resume is not None:
        return RUNTIME_LANE_CLAUDE_NATIVE
    return RUNTIME_LANE_GENERIC


def _adapter_for(profile: RuntimeProfile):
    if profile.provider == "claude":
        return ClaudeSdkRuntime(profile)
    overlay = GENERIC_PROVIDER_REGISTRY.get(profile.provider)
    if overlay is not None:
        if overlay.transport == "subprocess_cli":
            # subprocess_cli still dispatches by provider key because
            # OpenAICodexRuntime and GeminiCliRuntime are distinct classes.
            if profile.provider == "openai-codex":
                # Composite selection is request-sensitive inside the adapter:
                # exec for ordinary/native-tool turns, isolated app-server for
                # caller-supplied definitions. The exec-only adapter's own
                # supports_caller_tool_defs() remains truthfully False.
                return OpenAICodexAppServerRuntime(profile)
            if profile.provider == "gemini-cli":
                return GeminiCliRuntime(profile)
        if overlay.transport == "openai_responses":
            # openai_responses providers share one adapter class.
            return OpenAICompatibleRuntime(profile)
    raise RuntimeExecutionError(f"Unsupported runtime provider: {profile.provider}")


def _adapter_carries_tool_defs(adapter: object) -> bool:
    """Whether ``adapter`` will EXECUTE caller-supplied tool definitions.

    Fail-closed on two axes, both deliberate:

    * An adapter that does not declare the method at all is treated as NOT
      carrying. Adapters here are duck-typed with no shared base class, so a
      future adapter added without thinking about tools must default to safe.
      The dangerous default is the silent one.
    * A declaration that raises is treated as NOT carrying rather than
      propagating. A broken probe must not take down the whole fallback chain,
      and "skip this lane" is the conservative reading.

    This is a CAPABILITY question asked of the adapter, never a provider-name
    check. Hardcoding ``profile.provider == "openai-codex"`` here would
    reproduce the exact bug this epic already rejected in ``allowed_tools``:
    capability that is not encoded in routing degrades silently the moment
    quota pressure reshuffles the chain.
    """
    try:
        probe = getattr(adapter, "supports_caller_tool_defs", None)
    except Exception:
        # `getattr` is INSIDE the try because attribute access can execute code
        # — a property or a custom __getattr__ that raises would otherwise
        # abort the whole fallback chain instead of skipping one adapter.
        return False
    if probe is None:
        return False
    try:
        verdict = probe()
    except Exception:
        return False

    # STRICT identity, not truthiness. `bool()` coercion turned this gate from
    # fail-closed into fail-OPEN in two plausible ways (adversarial review,
    # Codex):
    #   * a probe returning the STRING "false" -> bool("false") is True
    #   * an `async def` probe -> returns a coroutine -> bool(coroutine) is
    #     True, the adapter is admitted, and the coroutine is never awaited
    # Only a literal `True` grants carriage; anything else is refused.
    if verdict is True:
        return True
    if verdict is not False:
        # An `async def` probe hands back a coroutine we are never going to
        # await. Close it explicitly: otherwise every routing decision against
        # such an adapter leaks a coroutine and emits a RuntimeWarning far from
        # the actual mistake.
        close = getattr(verdict, "close", None)
        if callable(close) and hasattr(verdict, "send"):
            try:
                close()
            except Exception:
                pass
        _logger.warning(
            "adapter %s.supports_caller_tool_defs() returned %r (%s), not a "
            "bool — refusing carriage. An `async def` probe returns a "
            "coroutine and is a likely cause.",
            type(adapter).__name__,
            verdict,
            type(verdict).__name__,
        )
    return False


def _adapter_supports_model_only(adapter: object) -> bool:
    """True only for a literal synchronous adapter zero-tool guarantee."""
    try:
        probe = getattr(adapter, "supports_model_only", None)
    except Exception:
        return False
    if probe is None:
        return False
    try:
        verdict = probe()
    except Exception:
        return False
    if verdict is True:
        return True
    if verdict is not False:
        close = getattr(verdict, "close", None)
        if callable(close) and hasattr(verdict, "send"):
            try:
                close()
            except Exception:
                pass
    return False


def _lane_profiles(lane: str, request: RuntimeRequest) -> list[RuntimeProfile]:
    """Configured profiles for ONE lane, in that lane's own preference order."""
    if lane == RUNTIME_LANE_CLAUDE_NATIVE:
        profile = build_profile_for_provider("claude", key_prefix="primary", request=request)
        return [profile] if profile else []

    return list(resolve_generic_runtime_profiles(request))


def _resolve_lane_profiles(request: RuntimeRequest) -> list[RuntimeProfile]:
    lane = resolve_runtime_lane(request)
    profiles = _lane_profiles(lane, request)
    if not request.model_only:
        return profiles

    # Capability-constrained widening for the model_only contract.
    #
    # Lane selection is an OPERATOR PREFERENCE ("run this persona on Codex");
    # `model_only=True` is a HARD REQUIREMENT ("no provider tool surface may
    # exist for this turn"). Resolving preference first and requirement second
    # made the requirement unsatisfiable whenever the preferred lane's adapters
    # cannot prove it: the run loop below skips every incapable adapter (which
    # is correct — never weaken the contract to satisfy a preference) and the
    # request then had nowhere left to go. The shipped `crypto` profile pins
    # generic_runtime/openai-codex, and `openai_codex.supports_model_only()` is
    # False, so its scheduled zero-tool work deferred every single night.
    #
    # So for a model_only request the candidate set is the preferred lane's
    # profiles FOLLOWED BY the other configured lane's. Preference still wins:
    # if the preferred lane has a capable adapter the loop succeeds on it and
    # never reaches the appended candidates, so behavior there is unchanged.
    # Nothing is weakened either — appended candidates pass through the exact
    # same `supports_model_only` gate. When NO configured lane can prove the
    # contract the list is simply exhausted and the caller gets its deferral,
    # which is the only case that receipt should ever describe.
    #
    # Non-model_only requests return above, untouched.
    other = (
        RUNTIME_LANE_GENERIC
        if lane == RUNTIME_LANE_CLAUDE_NATIVE
        else RUNTIME_LANE_CLAUDE_NATIVE
    )
    seen = {profile.key for profile in profiles}
    for extra in _lane_profiles(other, request):
        if extra.key not in seen:
            seen.add(extra.key)
            profiles.append(extra)
    return profiles


def probe_caller_tool_transport(request: RuntimeRequest) -> CallerToolTransportProbe:
    """Inspect selected profiles without invoking a provider.

    This is the public diagnostics contract for caller-supplied tool carriage.
    It deliberately keeps adapter construction and capability probing inside
    the lane owner so readiness collectors do not depend on private router
    helpers.
    """

    lane = resolve_runtime_lane(request)
    candidates: list[CallerToolTransportCandidate] = []
    for profile in _resolve_lane_profiles(request):
        try:
            adapter = _adapter_for(profile)
        except Exception as exc:
            candidates.append(
                CallerToolTransportCandidate(
                    provider=str(profile.provider),
                    carries_caller_tools=False,
                    error=str(exc),
                )
            )
            continue
        candidates.append(
            CallerToolTransportCandidate(
                provider=str(profile.provider),
                carries_caller_tools=_adapter_carries_tool_defs(adapter),
            )
        )
    return CallerToolTransportProbe(lane=lane, candidates=tuple(candidates))


async def run_with_runtime_lanes(request: RuntimeRequest) -> RuntimeResult:
    """Run a request through the lane-first runtime facade."""

    # PRD-8 Phase 7a WS4 — operator kill-switch. Raises KillSwitchDisabled
    # when HOMIE_KILLSWITCH_LLM=disabled. Callers (engine.py, memory_reflect,
    # memory_weekly, memory_dream) catch this explicitly and degrade cleanly.
    kill_switches.requireEnabled("llm", caller="lane_router")

    # Epic #236 — registry provenance. Every lane crosses this boundary, so it
    # is the one place a hand-assembled `tool_defs` array can be caught before
    # it reaches a provider. Without it the tool registry's "all tools must be
    # part of a toolset to be accessible" invariant is enforceable only by
    # convention at each call site (adversarial review, Codex — BLOCKER).
    # Raises ValueError, deliberately BEFORE any profile resolution or spend.
    _base.assert_tool_defs_are_registered(request)
    _base.assert_model_only_contract(request)

    lane = resolve_runtime_lane(request)
    effective_request = request
    if lane != RUNTIME_LANE_CLAUDE_NATIVE and request.resume is not None:
        # Runtime resume IDs are Claude-specific. A user-selected generic lane
        # must not be forced back to Claude by a stale Telegram/CLI session.
        effective_request = replace(request, resume=None)
    errors: list[str] = []

    for profile in _resolve_lane_profiles(effective_request):
        adapter = _adapter_for(profile)
        if effective_request.model_only and not _adapter_supports_model_only(adapter):
            errors.append(
                f"{profile.key}: cannot prove a zero-tool model-only runtime "
                "(skipped rather than weakening authority)"
            )
            continue
        # Epic #236 — tool-turn lane exclusion. Checked BEFORE supports() so the
        # error names the real reason; supports() also refuses these, but its
        # message would blame the capability tier and send the next reader
        # chasing the wrong thing.
        if _base.request_carries_tools(
            effective_request
        ) and not _adapter_carries_tool_defs(adapter):
            errors.append(
                f"{profile.key}: cannot execute caller-supplied tool definitions "
                f"(skipped for this tool turn; still eligible for text turns)"
            )
            continue
        if not adapter.supports(effective_request):
            errors.append(
                f"{profile.key}: unsupported capability {effective_request.capability}"
            )
            continue

        # `wait_for` covers `adapter.run(...)` ONLY — never the health
        # bookkeeping below. On timeout the adapter is cancelled; the CLI
        # adapters reap their child on the way out.
        timeout_s = _adapter_timeout_seconds(effective_request)
        try:
            result = await asyncio.wait_for(adapter.run(effective_request), timeout=timeout_s)
        except RuntimeUnsupportedCapabilityError as exc:
            errors.append(f"{profile.key}: {exc}")
            continue
        except RuntimeRetryableError as exc:
            mark_profile_retryable_failure(profile, str(exc))
            errors.append(f"{profile.key}: retryable error {exc}")
            continue
        except RuntimeConfigError as exc:
            mark_profile_unavailable(profile, str(exc))
            errors.append(f"{profile.key}: unavailable {exc}")
            continue
        except TimeoutError:
            # asyncio.TimeoutError IS builtins.TimeoutError on 3.11+. Must
            # precede `except Exception` (TimeoutError ⊂ OSError ⊂ Exception),
            # else the generic arm mislabels the message. `asyncio.CancelledError`
            # is BaseException, so an external operator/shutdown cancel still
            # propagates untouched past every arm here.
            mark_profile_retryable_failure(profile, f"timed out after {timeout_s}s")
            errors.append(f"{profile.key}: timed out after {timeout_s}s")
            continue
        except Exception as exc:
            mark_profile_retryable_failure(profile, str(exc))
            errors.append(f"{profile.key}: {exc}")
            continue

        # Success bookkeeping stays OUTSIDE the provider try/except: an
        # exception here must never convert a successful run into a provider
        # failure or discard the result (2026-07-16 WinError 32 incident).
        try:
            mark_profile_success(profile)
        except Exception:
            _logger.warning(
                "health bookkeeping failed after successful run for %s",
                profile.key,
                exc_info=True,
            )
        result.runtime_lane = lane
        return result

    joined = "; ".join(errors) if errors else "no runtime profiles resolved"
    message = (
        f"No runtime could satisfy task '{request.task_name}' "
        f"({request.capability}) on lane '{lane}': {joined}"
    )
    error_type = (
        RuntimeCallerToolTransportError
        if _base.request_carries_tools(request)
        else RuntimeExecutionError
    )
    raise error_type(message)
