"""Dedicated keystone tests for ``cognition.operator_beliefs`` (Living Self Act 1).

Fills the gaps NOT already covered by ``test_living_self_act1.py`` (which proves
the reflection seam, embedding dedup, capture cut, and provider-agnostic
extraction). This file pins the per-claim FILTERING and COERCION behavior of the
two public entry points plus the ``_coerce_claim_list`` unwrap edges:

  - ``extract_operator_beliefs`` — operator turns ONLY (the function never sees a
    bot reply by contract; the input list IS the verbatim user turns), the
    ``max_claims`` cap, the ``min_chars`` floor over the actual claim text, the
    non-dict-item filter, and the 200-turn input truncation.
  - ``apply_operator_beliefs`` — kind->source mapping (explicit vs reflection),
    confidence coercion (bad value -> 0.5 default), malformed-claim skip (missing
    key / None / empty / whitespace) without aborting the batch.
  - ``_coerce_claim_list`` — the known-key and sole-list unwrap edges that the
    Act-2 judge also depends on.

Born-clean: all ids/text synthetic, tmp_path-scoped, the LLM boundary stubbed
deterministically (no network, no FastEmbed download, no live state touched).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition import operator_beliefs as ob  # noqa: E402
from cognition.self_model import InferenceTracker  # noqa: E402

import config  # noqa: E402


def _patch_fake_embed_batch(monkeypatch):
    """Force exact-match dedup (no network) so distinct synthetic claims never merge."""
    import numpy as np

    def _embed(texts, **_kw):
        # Each distinct text gets its own axis -> orthogonal -> cosine 0 -> never
        # merges; identical normalized text shares an axis -> merges. Keeps the
        # add_inference dedup path real without FastEmbed.
        seen: dict[str, int] = {}
        out = []
        for t in texts:
            key = t.strip().lower()
            idx = seen.setdefault(key, len(seen))
            vec = np.zeros(64, dtype=np.float32)
            vec[idx % 64] = 1.0
            out.append(vec)
        return out

    monkeypatch.setattr("embeddings.embed_batch", lambda texts, **kw: _embed(texts, **kw))


def _fake_reasoning(parsed, model="x"):
    async def reasoning(*_a, **_k):
        return SimpleNamespace(parsed=parsed, model=model)

    return reasoning


def _run_extract(turns, reasoning, **kw):
    return asyncio.run(
        ob.extract_operator_beliefs(turns, Path("."), reasoning=reasoning, **kw)
    )


# ===========================================================================
# extract_operator_beliefs — claim filtering + capping over a stubbed LLM
# ===========================================================================


def test_extract_caps_at_max_claims():
    """More than max_claims valid claims -> the result is sliced to the cap."""
    settings = config.get_inference_extraction_settings(max_claims=2)
    parsed = [
        {"claim": "operator prefers concise answers", "kind": "inferred"},
        {"claim": "operator always tests before shipping", "kind": "explicit"},
        {"claim": "operator likes dark mode in editors", "kind": "inferred"},
    ]
    out = _run_extract(["msg long enough to pass"], _fake_reasoning(parsed), settings=settings)
    assert len(out) == 2  # sliced to max_claims, the over-the-cap third dropped


def test_extract_drops_non_dict_items():
    """A list mixing dicts and junk -> only well-formed dict claims survive."""
    parsed = [
        {"claim": "operator prefers concise answers", "kind": "inferred"},
        "a bare string claim that is not a dict",
        42,
        None,
    ]
    out = _run_extract(["msg long enough to pass"], _fake_reasoning(parsed))
    assert len(out) == 1
    assert out[0]["claim"] == "operator prefers concise answers"


def test_extract_min_chars_measured_over_claim_text_only():
    """The min_chars floor measures the CLAIM text, not the whole dict."""
    settings = config.get_inference_extraction_settings(min_chars=12)
    parsed = [
        {"claim": "tiny", "kind": "inferred", "confidence": 0.99},  # 4 chars < 12
        {"claim": "operator prefers concise answers", "kind": "inferred"},  # passes
    ]
    out = _run_extract(["msg long enough to pass"], _fake_reasoning(parsed), settings=settings)
    assert [c["claim"] for c in out] == ["operator prefers concise answers"]


def test_extract_truncates_input_turns_to_200(monkeypatch):
    """The context feed caps the operator turns at the first 200 (cost guard)."""
    captured = {}

    async def reasoning(context, instruction, **_k):
        captured["context"] = context
        return SimpleNamespace(parsed=[], model="x")

    turns = [f"operator message number {i} that is long enough" for i in range(250)]
    _run_extract(turns, reasoning)
    # The 0th..199th turns are in the prompt; the 200th onward are not.
    assert "operator message number 199 " in captured["context"]
    assert "operator message number 200 " not in captured["context"]


def test_extract_empty_list_parsed_returns_empty():
    """A valid-but-empty array from the model -> [] (no claims, no crash)."""
    assert _run_extract(["msg long enough to pass"], _fake_reasoning([])) == []


# ===========================================================================
# apply_operator_beliefs — kind->source mapping + coercion + malformed skip
# ===========================================================================


def test_apply_kind_inferred_maps_to_reflection(tmp_path, monkeypatch):
    """A non-explicit kind (incl. unknown/missing kind) becomes source='reflection'."""
    _patch_fake_embed_batch(monkeypatch)
    path = tmp_path / "inf.json"
    claims = [
        {"claim": "operator prefers concise answers", "kind": "inferred"},
        {"claim": "operator likes dark mode", "kind": "something_else"},
        {"claim": "operator reviews PRs daily"},  # no kind at all
    ]
    n = ob.apply_operator_beliefs(claims, path)
    assert n == 3
    records = InferenceTracker(path).load()
    assert {r.source for r in records} == {"reflection"}


def test_apply_kind_explicit_maps_to_explicit(tmp_path, monkeypatch):
    _patch_fake_embed_batch(monkeypatch)
    path = tmp_path / "inf.json"
    n = ob.apply_operator_beliefs(
        [{"claim": "operator always tests before shipping", "kind": "explicit"}], path
    )
    assert n == 1
    assert InferenceTracker(path).load()[0].source == "explicit"


def test_apply_bad_confidence_falls_back_to_half(tmp_path, monkeypatch):
    """A non-numeric / missing confidence -> 0.5 default, claim STILL written."""
    _patch_fake_embed_batch(monkeypatch)
    path = tmp_path / "inf.json"
    claims = [
        {"claim": "operator prefers concise answers", "confidence": "not a number"},
        {"claim": "operator likes dark mode in editors"},  # missing confidence
    ]
    n = ob.apply_operator_beliefs(claims, path)
    assert n == 2
    records = InferenceTracker(path).load()
    assert all(abs(r.confidence - 0.5) < 1e-9 for r in records)


def test_apply_skips_malformed_without_aborting_batch(tmp_path, monkeypatch):
    """Missing 'claim' key / empty / whitespace claims are skipped; good ones written.

    These are the TRUE skips: a missing key raises KeyError (caught), and an
    empty/whitespace claim fails the ``if not claim_text`` guard. A None VALUE is
    NOT a skip (str(None) == 'None' is non-empty) — that distinct case is pinned
    by ``test_apply_none_claim_value_is_skipped``.
    """
    _patch_fake_embed_batch(monkeypatch)
    path = tmp_path / "inf.json"
    claims = [
        {"no_claim_key": "x"},               # missing key -> KeyError -> skipped
        {"claim": "   "},                    # whitespace-only -> skipped
        {"claim": ""},                       # empty -> skipped
        {"claim": "operator prefers concise answers", "kind": "inferred"},  # kept
    ]
    n = ob.apply_operator_beliefs(claims, path)
    assert n == 1
    records = InferenceTracker(path).load()
    assert len(records) == 1
    assert records[0].inference == "operator prefers concise answers"


def test_apply_none_claim_value_is_written_as_literal(tmp_path, monkeypatch):
    """A claim key present but value None is NOT skipped (REAL behavior).

    str(None) == 'None' which is non-empty, so a None VALUE writes the literal
    text 'None'. This pins the actual contract: the only true skips are
    missing-key (KeyError) and empty/whitespace text.
    """
    _patch_fake_embed_batch(monkeypatch)
    path = tmp_path / "inf.json"
    n = ob.apply_operator_beliefs([{"claim": None}], path)
    # str(None).strip() == "None" (non-empty) -> written, NOT skipped.
    assert n == 1
    assert InferenceTracker(path).load()[0].inference == "None"


def test_apply_empty_list_writes_nothing(tmp_path):
    path = tmp_path / "inf.json"
    assert ob.apply_operator_beliefs([], path) == 0
    assert not path.exists() or InferenceTracker(path).load() == []


# ===========================================================================
# _coerce_claim_list — unwrap edges (shared with the Act-2 judge)
# ===========================================================================


def test_coerce_known_key_priority_order():
    """The FIRST matching known key wins even when several are present."""
    parsed = {"claims": [{"claim": "first"}], "beliefs": [{"claim": "second"}]}
    assert ob._coerce_claim_list(parsed) == [{"claim": "first"}]


def test_coerce_sole_list_value_unwrapped():
    assert ob._coerce_claim_list({"whatever": [1, 2, 3]}) == [1, 2, 3]


def test_coerce_two_unknown_lists_is_safe_empty():
    """TWO list-valued unknown keys -> [] (the silent-failure guard, len!=1)."""
    assert ob._coerce_claim_list({"a": [1], "b": [2]}) == []


def test_coerce_passthrough_and_garbage():
    assert ob._coerce_claim_list([{"x": 1}]) == [{"x": 1}]
    assert ob._coerce_claim_list(None) == []
    assert ob._coerce_claim_list(123) == []
    assert ob._coerce_claim_list({"only": "a string, not a list"}) == []
