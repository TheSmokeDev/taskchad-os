"""Safe, reversible config overrides for replay experiments.

Patches `config` module attributes in-place for the duration of a `with` block,
then restores original values — even if an exception is raised. Also suppresses
the real `_persist_log` side effect so replays never pollute production state.

Design:
- `override_config(**kwargs)` — temporary config mutation
- `isolate_recall_side_effects()` — suppresses the RecallLogStore write
- `replay_context(overrides, isolate=True)` — both combined, the default for replay

Uses setattr on the `config` module. Recall functions read config attributes
lazily (via `from config import X` inside functions), so attribute-level patches
take effect on the next call without requiring module reload.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def override_config(**overrides: Any) -> Iterator[dict[str, Any]]:
    """Temporarily override config module attributes.

    Yields the applied set. Restores all originals on exit even if an
    exception propagates out of the block.

    Raises AttributeError if a key does not exist on config — prevents
    silent typos that would look like a successful override.
    """
    import config

    originals: dict[str, Any] = {}
    applied: dict[str, Any] = {}
    try:
        for key, value in overrides.items():
            if not hasattr(config, key):
                raise AttributeError(
                    f"config.{key} does not exist — refusing to silently add a new attr"
                )
            originals[key] = getattr(config, key)
            setattr(config, key, value)
            applied[key] = value
        yield applied
    finally:
        for key, value in originals.items():
            setattr(config, key, value)


@contextmanager
def isolate_recall_side_effects() -> Iterator[None]:
    """Suppress writes to the real RecallLogStore ring buffer.

    Replaces `recall_service._persist_log` with a no-op for the block's duration.
    Protects `.claude/data/state/recall-log.json` from replay pollution so the
    live system's observability stays clean.
    """
    import recall_service as rs

    original = rs._persist_log
    rs._persist_log = lambda _log: None
    try:
        yield
    finally:
        rs._persist_log = original


@contextmanager
def isolate_langfuse() -> Iterator[None]:
    """Force `@observe` decorators to become no-ops for the block's duration.

    `recall_service._get_observe()` and `cognition.recall._get_observe()` both
    re-check `is_langfuse_enabled()` on every invocation rather than at
    decoration time. Patching that function to return False makes every
    `@observe`-decorated call fall through to the raw function — no spans
    emitted, no OTEL exporter activity, no pollution of live traces with
    replay runs.

    Why this and not the LANGFUSE_ENABLED env var: `config.py` calls
    `load_dotenv(override=True)` at import, which clobbers shell env vars
    with the values in `.claude/scripts/.env`. Runtime monkey-patching is
    the only deterministic way to silence tracing once the process has
    already imported config.
    """
    try:
        from runtime import langfuse_setup
    except ImportError:
        # Runtime module unavailable — nothing to isolate.
        yield
        return

    original = langfuse_setup.is_langfuse_enabled
    langfuse_setup.is_langfuse_enabled = lambda: False
    try:
        yield
    finally:
        langfuse_setup.is_langfuse_enabled = original


@contextmanager
def replay_context(
    overrides: dict[str, Any] | None = None,
    *,
    isolate: bool = True,
    disable_tracing: bool = True,
    experiment_tag: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Combined context: config override + side-effect isolation + tracing.

    This is the default entry point the replay harness uses. Keeps all three
    concerns in one `with` block so no replay path can forget any of them.

    Args:
        overrides: config module attributes to temporarily patch.
        isolate: if True (default), stub `_persist_log` to protect the
            production recall ring buffer from replay pollution.
        disable_tracing: if True (default), stub `is_langfuse_enabled` to
            disable Langfuse `@observe` spans for the replay. Set False only
            when the replay itself needs to emit tagged spans (Phase 2.4).
        experiment_tag: required when ``disable_tracing=False``. Built via
            `evolve.replay_tracing.build_experiment_tag(...)`. Routes the
            traced replay under `user_id="evolve-replay"` and tags spans
            with `experiment_id` + `override_fingerprint`. Must include an
            `experiment_id` key.

    Raises:
        ValueError: when ``disable_tracing=False`` AND ``experiment_tag`` is
            None or missing ``experiment_id``. Fails loud at the boundary so
            no untagged replay spans can leak into the production Langfuse
            project (PRD Phase 2.4 AC#5).
    """
    overrides = overrides or {}

    if not disable_tracing:
        if experiment_tag is None:
            raise ValueError(
                "replay_context with disable_tracing=False requires experiment_tag "
                "(prevents untagged replay spans polluting prod). Build via "
                "evolve.replay_tracing.build_experiment_tag(experiment_id, "
                "overrides, baseline_experiment_id) — or call run_replay() "
                "which auto-builds it."
            )
        if not experiment_tag.get("experiment_id"):
            raise ValueError(
                "experiment_tag must include 'experiment_id' "
                "(use build_experiment_tag())"
            )

    from contextlib import ExitStack

    with ExitStack() as stack:
        if disable_tracing:
            stack.enter_context(isolate_langfuse())
        else:
            # Phase 2.4: route the traced replay under user_id="evolve-replay"
            # with a tagged root span; existing @observe-decorated children
            # auto-nest via OTEL context propagation.
            from evolve.replay_tracing import replay_root_span
            stack.enter_context(
                replay_root_span(experiment_tag["experiment_id"], experiment_tag)
            )
        if isolate:
            stack.enter_context(isolate_recall_side_effects())
        applied = stack.enter_context(override_config(**overrides))
        yield applied


def snapshot_config(keys: list[str]) -> dict[str, Any]:
    """Read current values of the given config keys — for report provenance.

    Unknown keys are recorded as None so the report surfaces typos rather
    than hiding them.
    """
    import config

    return {k: getattr(config, k, None) for k in keys}


# Config keys the harness considers "recall-relevant" — snapshotted into every
# ReplayReport so deltas against baselines can be reconstructed later.
RECALL_CONFIG_KEYS: list[str] = [
    "RECALL_ENABLED",
    "RECALL_MIN_SCORE",
    "RECALL_MAX_RESULTS",
    "RECALL_MIN_MSG_LEN",
    "RECALL_BACKGROUND_MAX_RESULTS",
    "RECALL_BACKGROUND_MAX_CHARS",
    "RECALL_RERANK_ENABLED",
    "RECALL_RERANK_TOP_N",
    "RECALL_RERANK_TIMEOUT_S",
    "TIER1_MAX_QUERIES",
    "TIER1_MAX_RESULTS",
    "TIER1_GRAPH_MAX_HOPS",
    "TIER1_GRAPH_MAX_NEIGHBORS",
    "SEARCH_VECTOR_WEIGHT",
    "SEARCH_KEYWORD_WEIGHT",
    "SEARCH_MIN_SCORE",
]
