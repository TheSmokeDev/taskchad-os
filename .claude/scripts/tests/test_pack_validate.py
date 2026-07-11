"""Tests for the deterministic prompt-pack validator (image-node-factory).

The rule the validator enforces -- never stamp a citation the grounding did not
resolve -- previously lived only in `image-node-prompt-pack.md`, an instruction
an LLM follows. An instruction is a suggestion; these tests prove the script is
the enforcement. One test per distinct violation path.

No network, no LLM, no Archon: the validator reads three JSON artifacts.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / ".archon" / "scripts" / "pack-validate.py"

spec = importlib.util.spec_from_file_location("pack_validate", _SCRIPT)
pv = importlib.util.module_from_spec(spec)
sys.modules["pack_validate"] = pv
spec.loader.exec_module(pv)

_PIN = "a04beebfa3195ef8dfbf1c57da7df9e989c2173b"
_SHA = "3c88ef3d3c15ca319992fc82f860de6674412fe913a585a50664fc2a687261b3"


def _write(artifacts: Path, *, brief=None, grounding=None, pack=None) -> Path:
    artifacts.mkdir(parents=True, exist_ok=True)
    base_brief = {"brief": "a poster", "count": 2, "subject_mode": "generic",
                  "render_requested": "false", "persona_pack": "none"}
    base_grounding = {
        "grounded": True, "matched": 3,
        "resolved_case_ids": [17, 42, 377], "unresolved_case_ids": [],
        "prompt_engine": "gpt-image-2-style-library",
        "corpus_pin": _PIN, "corpus_sha256": _SHA, "license": "MIT",
        "exemplars": [],
    }
    base_pack = {
        "prompt_count": 2,
        "prompt_engine": "gpt-image-2-style-library",
        "corpus_pin": _PIN, "corpus_sha256": _SHA, "license": "MIT",
        "example_case_ids": [17, 377],
        "concepts": [
            {"concept_id": f"c{i}", "baked_prompt": f"Subject: hero {i}. Text handling: copy.",
             "overlay_prompt": f"Subject: hero {i}. Text handling: none.",
             "copy": {"headline": "H"}}
            for i in (1, 2)
        ],
    }
    (artifacts / "image-node-brief.json").write_text(
        json.dumps({**base_brief, **(brief or {})}), encoding="utf-8")
    (artifacts / "image-node-grounding.local.json").write_text(
        json.dumps({**base_grounding, **(grounding or {})}), encoding="utf-8")
    merged_pack = {**base_pack, **(pack or {})}
    for key, val in (pack or {}).items():
        if val is None:
            merged_pack.pop(key, None)
    (artifacts / "image-node-prompt-pack.json").write_text(
        json.dumps(merged_pack), encoding="utf-8")
    return artifacts


def _violations(tmp_path, **kw) -> list[str]:
    with pytest.raises(pv.PackInvalid) as exc:
        pv.validate_pack(_write(tmp_path, **kw))
    return exc.value.violations


# ---------------------------------------------------------------- happy paths


def test_grounded_pack_with_matching_provenance_passes(tmp_path: Path) -> None:
    out = pv.validate_pack(_write(tmp_path))
    assert out["pack_valid"] is True
    assert out["grounded"] is True
    assert out["cited_case_ids"] == [17, 377]


def test_ungrounded_self_authored_pack_passes(tmp_path: Path) -> None:
    out = pv.validate_pack(_write(
        tmp_path,
        grounding={"grounded": False, "matched": 0, "resolved_case_ids": [],
                   "prompt_engine": None, "corpus_pin": None,
                   "corpus_sha256": None, "license": None},
        pack={"prompt_engine": None, "corpus_pin": None, "corpus_sha256": None,
              "license": None, "example_case_ids": None, "self_authored": True},
    ))
    assert out["pack_valid"] is True and out["grounded"] is False


# ------------------------------------------------------- the hollow citation


def test_hollow_citation_is_rejected(tmp_path: Path) -> None:
    """grounded=false but the pack stamps prompt_engine anyway -- THE bug."""
    v = _violations(
        tmp_path,
        grounding={"grounded": False, "matched": 0, "resolved_case_ids": []},
        pack={"self_authored": True},  # provenance keys still present from base
    )
    assert any("HOLLOW CITATION" in x and "prompt_engine" in x for x in v)


def test_cited_id_outside_resolved_set_is_rejected(tmp_path: Path) -> None:
    v = _violations(tmp_path, pack={"example_case_ids": [17, 999]})
    assert any("999" in x and "never resolved" in x for x in v)


def test_grounded_pack_citing_nothing_is_rejected(tmp_path: Path) -> None:
    v = _violations(tmp_path, pack={"example_case_ids": []})
    assert any("cites no example_case_ids" in x for x in v)


def test_provenance_field_mismatch_is_rejected(tmp_path: Path) -> None:
    v = _violations(tmp_path, pack={"corpus_sha256": "f" * 64})
    assert any("provenance mismatch on corpus_sha256" in x for x in v)


def test_ungrounded_without_self_authored_is_rejected(tmp_path: Path) -> None:
    v = _violations(
        tmp_path,
        grounding={"grounded": False, "matched": 0, "resolved_case_ids": []},
        pack={"prompt_engine": None, "prompt_engine_attribution": None,
              "corpus_pin": None, "corpus_sha256": None, "license": None,
              "example_case_ids": None},
    )
    assert any("self_authored" in x for x in v)


# ----------------------------------------------------------- count and shape


def test_more_than_eight_concepts_is_rejected(tmp_path: Path) -> None:
    concepts = [{"baked_prompt": "Subject: x.", "overlay_prompt": "Subject: x.",
                 "copy": {}} for _ in range(9)]
    v = _violations(tmp_path, pack={"concepts": concepts, "prompt_count": 9})
    assert any("exceeds the cap of 8" in x for x in v)


def test_brief_count_over_cap_is_rejected_even_if_pack_is_small(tmp_path: Path) -> None:
    """The deterministic backstop for the prose-only intake cap."""
    v = _violations(tmp_path, brief={"count": 12})
    assert any("brief count=12 exceeds" in x for x in v)


def test_empty_pack_is_rejected(tmp_path: Path) -> None:
    v = _violations(tmp_path, pack={"concepts": [], "prompt_count": 0})
    assert any("no concepts" in x for x in v)


def test_prompt_count_mismatch_is_rejected(tmp_path: Path) -> None:
    v = _violations(tmp_path, pack={"prompt_count": 5})
    assert any("prompt_count says 5" in x for x in v)


def test_empty_variant_prompt_is_rejected(tmp_path: Path) -> None:
    v = _violations(tmp_path, pack={"concepts": [
        {"baked_prompt": "Subject: x.", "overlay_prompt": "  ", "copy": {}},
        {"baked_prompt": "Subject: y.", "overlay_prompt": "Subject: y.", "copy": {}},
    ]})
    assert any("concept 1: empty overlay_prompt" in x for x in v)


# ------------------------------------------------------------ placeholder mode


def test_placeholder_mode_requires_the_sentinel_everywhere(tmp_path: Path) -> None:
    v = _violations(tmp_path, brief={"subject_mode": "placeholder"})
    assert any("lacks" in x and pv.SUBJECT_SENTINEL in x for x in v)


def test_placeholder_mode_with_sentinel_passes(tmp_path: Path) -> None:
    s = pv.SUBJECT_SENTINEL
    out = pv.validate_pack(_write(
        tmp_path,
        brief={"subject_mode": "placeholder"},
        pack={"concepts": [
            {"baked_prompt": f"Subject: {s} centered.", "overlay_prompt": f"Subject: {s} left.",
             "copy": {"headline": "H"}},
            {"baked_prompt": f"Subject: {s} walking.", "overlay_prompt": f"Subject: {s} seated.",
             "copy": {"headline": "H"}},
        ]},
    ))
    assert out["subject_mode"] == "placeholder"


# --------------------------------------------------------- public-path guard


def test_absolute_local_path_in_pack_is_rejected(tmp_path: Path) -> None:
    v = _violations(tmp_path, pack={"concepts": [
        {"baked_prompt": "Subject: x. Refs at C:/work/refs/me.png",
         "overlay_prompt": "Subject: x.", "copy": {}},
        {"baked_prompt": "Subject: y.", "overlay_prompt": "Subject: y.", "copy": {}},
    ]})
    assert any("absolute local path" in x for x in v)


def test_corpus_source_url_is_not_a_false_positive(tmp_path: Path) -> None:
    """https:// must not match the drive-letter pattern (s: + //)."""
    out = pv.validate_pack(_write(
        tmp_path,
        pack={"corpus_source": "https://github.com/example/style-library"},
    ))
    assert out["pack_valid"] is True


# ------------------------------------------------------------- CLI plumbing


def test_missing_artifact_fails_with_a_named_file(tmp_path: Path) -> None:
    _write(tmp_path)
    (tmp_path / "image-node-grounding.local.json").unlink()
    with pytest.raises(pv.PackInvalid) as exc:
        pv.validate_pack(tmp_path)
    assert any("image-node-grounding.local.json" in x for x in exc.value.violations)


def test_archon_no_arg_dispatch_defaults_to_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Archon runs `uv run <path>` with no args; that must mean `validate`."""
    _write(tmp_path)
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    assert pv.main([]) == 0
    assert json.loads(capsys.readouterr().out)["pack_valid"] is True


def test_cli_exit_1_and_violations_json_on_bad_pack(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write(tmp_path, pack={"example_case_ids": [999]})
    assert pv.main(["validate", "--artifacts-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["pack_valid"] is False
    assert "never resolved" in captured.err
