"""Tests for the gpt-image-2-style-library -> Archon workflow port.

The bug this port exists to kill: the workflow stamped `prompt_engine` onto its
artifacts and cited `example_case_ids` that resolved to nothing, because the
installed skill ships pointers (URLs) and the node runs with webSearchMode
disabled. A citation with no referent.

`test_citation_resolves_or_is_absent` is the property whose absence IS that bug.
Everything else here guards a single distinct code path.

No test touches the network: `_http_get` is the monkeypatchable seam (Rule 3).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / ".archon" / "scripts" / "style-corpus.py"
_WORKFLOW = Path(__file__).resolve().parents[3] / ".archon" / "workflows" / "image-node-factory.yaml"

_TEST_PIN = "testpin0000000000000000000000000000000000"


def _load_module():
    spec = importlib.util.spec_from_file_location("style_corpus", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolves __module__ via sys.modules
    spec.loader.exec_module(mod)
    return mod


sc = _load_module()


# ---------------------------------------------------------------- fixtures


def _cases_payload() -> dict:
    return {
        "totalCases": 5,
        "cases": [
            {
                "id": 1,
                "title": "english photo",
                "prompt": "A cinematic portrait, 85mm, soft key light.",
                "category": "Photography & Realism",
                "styles": ["Photography", "Realistic"],
                "scenes": ["Commerce"],
                "featured": False,
                "sourceUrl": "https://example.invalid/1",
            },
            {
                "id": 2,
                "title": "featured english photo",
                "prompt": "A studio product shot on seamless white.",
                "category": "Photography & Realism",
                "styles": ["Photography"],
                "scenes": ["Commerce"],
                "featured": True,
                "sourceUrl": "https://example.invalid/2",
            },
            {
                "id": 3,
                "title": "cjk doc",
                # The only CJK in this repo's test data: one ideograph, to prove the filter.
                "prompt": "画一张 white paper layout",
                "category": "Documents & Publishing",
                "styles": ["Documents"],
                "scenes": ["Education"],
                "featured": False,
                "sourceUrl": "https://example.invalid/3",
            },
            {
                "id": 4,
                "title": "long prompt",
                "prompt": ("x" * 900) + "\n" + ("y" * 900),
                "category": "Illustration & Art",
                "styles": ["Illustration"],
                "scenes": ["Creative"],
                "featured": False,
                "sourceUrl": "https://example.invalid/4",
            },
            {
                "id": 5,
                "title": "empty prompt is not groundable",
                "prompt": "   ",
                "category": "Photography & Realism",
                "styles": ["Photography"],
                "scenes": ["Commerce"],
                "featured": True,
                "sourceUrl": "https://example.invalid/5",
            },
        ],
    }


def _library_payload() -> dict:
    return {
        "templates": [
            {
                "id": "realistic-photography",
                "category": "Photography & Realism",
                "templateAnchor": "tpl-photo",
                "styles": ["Photography"],
                "scenes": ["Commerce"],
                "exampleCases": [4],  # deliberately cross-category, to prove the boost
            },
            {
                "id": "document-publishing",
                "category": "Documents & Publishing",
                "templateAnchor": "tpl-doc",
                "styles": ["Documents"],
                "scenes": ["Education"],
                "exampleCases": [3],
            },
        ]
    }


def _write_corpus(root: Path, *, pin: str = _TEST_PIN, cases: dict | None = None) -> Path:
    d = root / sc.SKILL_NAME / pin
    d.mkdir(parents=True, exist_ok=True)
    (d / "cases.json").write_text(json.dumps(cases or _cases_payload()), encoding="utf-8")
    (d / "style-library.json").write_text(json.dumps(_library_payload()), encoding="utf-8")
    (d / "templates.md").write_text(
        '<a name="tpl-photo"></a>\nphoto body\n\n<a name="tpl-doc"></a>\ndoc body\n',
        encoding="utf-8",
    )
    (d / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    return d


@pytest.fixture()
def corpus(tmp_path):
    _write_corpus(tmp_path)
    return sc.require_corpus(pin=_TEST_PIN, cache_dir=tmp_path)


# ---------------------------------------------------------------- provisioning


def test_require_corpus_offline_cache_hit(tmp_path):
    _write_corpus(tmp_path)
    c = sc.require_corpus(pin=_TEST_PIN, cache_dir=tmp_path)
    assert len(c.cases) == 4  # the empty-prompt case is dropped: it cannot ground a citation
    assert 5 not in c.cases


def test_require_corpus_cold_cache_raises(tmp_path):
    with pytest.raises(sc.CorpusMissing, match="not provisioned"):
        sc.require_corpus(pin=_TEST_PIN, cache_dir=tmp_path)


def test_require_corpus_missing_one_file_raises(tmp_path):
    d = _write_corpus(tmp_path)
    (d / "templates.md").unlink()
    with pytest.raises(sc.CorpusMissing, match="missing file"):
        sc.require_corpus(pin=_TEST_PIN, cache_dir=tmp_path)


def test_require_corpus_rehashes_bytes_not_sidecar(tmp_path):
    """Rule 2: the guard reads physical bytes. A lying marker cannot bless a bad cache."""
    d = tmp_path / sc.SKILL_NAME / sc.UPSTREAM_PIN
    d.mkdir(parents=True)
    for name in sc.CORPUS_FILES:
        (d / name).write_bytes(b"corrupt")
    (d / ".ok").write_text('{"downloaded": true, "verified": true}', encoding="utf-8")

    with pytest.raises(sc.CorpusMissing, match="corrupt file"):
        sc.require_corpus(cache_dir=tmp_path)  # real pin => digests enforced


def test_prime_checksum_mismatch_never_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_http_get", lambda url: b"not the pinned bytes")
    with pytest.raises(sc.CorpusMissing, match="sha256 mismatch"):
        sc.prime(cache_dir=tmp_path)  # real pin => digests enforced
    assert not sc.corpus_dir(cache_dir=tmp_path).exists()
    assert not any(tmp_path.rglob("cases.json")), "an unverified corpus must never land"


def test_prime_installs_atomically(tmp_path, monkeypatch):
    blobs = {
        "data/cases.json": json.dumps(_cases_payload()).encode(),
        "data/style-library.json": json.dumps(_library_payload()).encode(),
        "docs/templates.md": b'<a name="tpl-photo"></a>\nbody\n',
        "LICENSE": b"MIT License\n",
    }
    monkeypatch.setattr(sc, "_http_get", lambda url: blobs[url.split(f"{_TEST_PIN}/", 1)[1]])
    root = sc.prime(pin=_TEST_PIN, cache_dir=tmp_path)
    assert root.is_dir()
    assert sc.require_corpus(pin=_TEST_PIN, cache_dir=tmp_path).pin == _TEST_PIN
    assert not list(tmp_path.glob("**/*.tmp.*")), "staging dir must not survive"


def test_http_get_is_monkeypatchable_via_module_attr(monkeypatch):
    """Rule 3: prime() must resolve _http_get through the module, not a bound import."""
    calls = []
    monkeypatch.setattr(sc, "_http_get", lambda url: calls.append(url) or b"x")
    with pytest.raises(sc.CorpusMissing):
        sc.prime(cache_dir=Path("/nonexistent-cache-root"))
    assert calls, "prime() did not route through the module-level seam"


# ---------------------------------------------------------------- retrieval


def test_select_ranks_deterministically_id_is_terminal_key(corpus):
    a = sc.select(corpus, category="Photography & Realism", k=5)
    b = sc.select(corpus, category="Photography & Realism", k=5)
    assert a.resolved_case_ids == b.resolved_case_ids
    # equal score -> featured wins -> then ascending id (unique => total order)
    assert a.resolved_case_ids == (2, 1)


def test_select_boosts_template_example_cases(corpus):
    """Case 4 is a different category, so only the exampleCases boost can lift it."""
    plain = sc.select(corpus, category="Photography & Realism", k=5)
    boosted = sc.select(corpus, template_id="realistic-photography",
                        category="Photography & Realism", k=5)
    assert 4 not in plain.resolved_case_ids
    assert boosted.resolved_case_ids[0] == 4


def test_select_english_only_excludes_cjk(corpus):
    both = sc.select(corpus, category="Documents & Publishing", k=5)
    english = sc.select(corpus, category="Documents & Publishing", lang="en", k=5)
    assert both.resolved_case_ids == (3,)
    assert english.grounded is False


def test_select_zero_match_is_ungrounded_not_error(corpus):
    """The real-corpus analogue: Documents & Publishing has 0 English cases."""
    g = sc.select(corpus, category="Documents & Publishing", lang="en")
    assert g.grounded is False
    assert g.matched == 0
    assert g.provenance == {}          # nothing resolved => nothing cited
    assert "prompt_engine" not in g.summary()


def test_select_unknown_id_lands_in_unresolved_not_crash(corpus):
    g = sc.select(corpus, case_ids=[1, 999], category="Photography & Realism", k=5)
    assert 1 in g.resolved_case_ids
    assert g.unresolved_case_ids == (999,)
    assert g.grounded is True          # cited-but-absent != matched-zero


def test_select_anchors_come_first_and_taxonomy_tops_up(corpus):
    g = sc.select(corpus, case_ids=[1], category="Photography & Realism", k=5)
    assert g.resolved_case_ids[0] == 1          # anchor keeps its cited position
    assert 2 in g.resolved_case_ids             # topped up from taxonomy
    assert len(set(g.resolved_case_ids)) == len(g.resolved_case_ids)  # no dupes


def test_select_truncates_at_newline_and_flags(corpus):
    g = sc.select(corpus, category="Illustration & Art", k=1)
    (ex,) = g.exemplars
    assert ex.truncated is True
    assert len(ex.prompt) <= sc._EXEMPLAR_CHAR_CAP
    assert not ex.prompt.endswith("y"), "should cut at the newline boundary, not mid-run"


def test_select_unknown_template_id_is_usage_error(corpus):
    with pytest.raises(sc.UsageError, match="unknown template_id"):
        sc.select(corpus, template_id="no-such-template")


def test_template_body_slices_to_next_anchor(corpus):
    body = sc.template_body(corpus, "realistic-photography")
    assert "photo body" in body
    assert "doc body" not in body


# ---------------------------------------------------------------- invariants


def test_no_default_arg_binds_module_constant():
    """Rule 1: a tunable bound at def time is cached in __defaults__ forever."""
    for fn in (sc.select, sc.prime, sc.require_corpus, sc.corpus_dir):
        for name, p in inspect.signature(fn).parameters.items():
            if p.default is inspect.Parameter.empty:
                continue
            assert p.default is None, (
                f"{fn.__name__}({name}=...) binds a value at def time; "
                "use a None sentinel and resolve it in the body"
            )


def test_citation_resolves_or_is_absent(corpus):
    """THE regression test. The property whose absence is the original bug.

    positive: every stamped id resolves to a real case with a non-empty prompt.
    negative: when nothing resolves, no engine and no ids are stamped at all.
    """
    grounded = sc.select(corpus, template_id="realistic-photography",
                         category="Photography & Realism", case_ids=[1, 999], k=5)
    assert grounded.grounded is True
    assert grounded.summary()["prompt_engine"] == sc.SKILL_NAME
    for cid in grounded.resolved_case_ids:
        assert cid in corpus.cases
        assert corpus.cases[cid].prompt.strip(), f"case {cid} cited but has no prompt"
    assert 999 not in grounded.resolved_case_ids  # absent ids are never stamped

    ungrounded = sc.select(corpus, category="Documents & Publishing", lang="en")
    summary = ungrounded.summary()
    assert ungrounded.resolved_case_ids == ()
    assert "prompt_engine" not in summary
    assert "corpus_pin" not in summary


@pytest.mark.skipif(not _WORKFLOW.is_file(), reason="workflow not present")
def test_workflow_template_enum_matches_corpus():
    """The 22 template ids live in the corpus AND in the workflow's output_format enum.
    A re-pin that renames a template must fail here, loudly, not mid-DAG."""
    try:
        real = sc.require_corpus()
    except sc.CorpusMissing:
        pytest.skip("corpus not primed on this machine")

    import yaml

    wf = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    node = next(n for n in wf["nodes"] if n["id"] == "select")
    enum = set(node["output_format"]["properties"]["template_id"]["enum"])
    assert enum == set(real.template_ids), (
        f"workflow enum drifted from corpus pin {real.pin[:8]}: "
        f"only-in-workflow={sorted(enum - set(real.template_ids))} "
        f"only-in-corpus={sorted(set(real.template_ids) - enum)}"
    )
