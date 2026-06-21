"""Dedicated keystone tests for ``cognition.evidence_gate`` (Living Self Act 4).

Fills the gaps NOT already covered by ``test_living_self_act4.py`` (which proves
the M4 security matrix — traversal/absolute/symlink/oversized/missing rejection,
read-cap, raising-reader fail-open, the happy-path verify, empty/zero-overlap
reject). This file pins:

  - the TWO readers' contract difference: ``read_evidence_texts`` OMITS
    non-supporting paths; ``read_evidence_for_floor`` KEEPS every cited path,
    mapping non-supporting ones to "" (so the floor's no_unread_claim check can
    SEE an empty/missing cited path).
  - vault confinement + re-root helpers: a vault-relative path and a
    ``vault/memory/<tail>`` path both resolve under memory_dir; an in-repo path
    OUTSIDE the vault has no in-root candidate.
  - the deterministic-floor REUSE: a candidate that ASSERTS a doc read but cites
    an empty file is rejected on the REAL falsifiable floor check
    (``belief_regression_floor``), not the weak overlap — distinct reason strings.
  - the conservative ``evidence_check_error`` fail-open.

Born-clean, tmp_path-scoped vaults, injected readers, no live SELF.md / vault.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition import evidence_gate as eg  # noqa: E402

import config  # noqa: E402
from evolve import belief_regression as br  # noqa: E402


def _prop(*, proposed_content="Routing is lane-first then provider.",
          evidence_paths=None, summary="lane-first routing", source="reflection"):
    return SimpleNamespace(
        proposed_content=proposed_content,
        evidence_paths=list(evidence_paths or []),
        summary=summary,
        source=source,
    )


def _seed_corpus():
    """no_unread_claim + evidence_fidelity ON — the same shape Act-4 uses."""
    return [
        br.BeliefRegressionEntry(
            check_id="no-unread-claim", kind="no_unread_claim", description="", params={}
        ),
        br.BeliefRegressionEntry(
            check_id="evidence-fidelity", kind="evidence_fidelity",
            description="", params={"min_overlap": 0.10},
        ),
    ]


# ===========================================================================
# read_evidence_texts vs read_evidence_for_floor — the omit-vs-keep contract
# ===========================================================================


def test_texts_omits_nonsupporting_floor_keeps_them(tmp_path):
    """A missing cited path is OMITTED by read_evidence_texts but KEPT ("") by the floor reader."""
    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "good.md").write_text(
        "lane first routing provider selection", encoding="utf-8"
    )
    prop = _prop(evidence_paths=["daily/good.md", "daily/missing.md"])

    texts = eg.read_evidence_texts(prop, tmp_path, settings=s)
    floor = eg.read_evidence_for_floor(prop, tmp_path, settings=s)

    # the supporting path is in both; the missing one is ONLY in the floor map (as "")
    assert "daily/good.md" in texts and texts["daily/good.md"]
    assert "daily/missing.md" not in texts            # OMITTED
    assert floor["daily/missing.md"] == ""            # KEPT, empty
    assert floor["daily/good.md"]                      # present + non-empty


def test_floor_reader_keys_every_cited_path(tmp_path):
    """read_evidence_for_floor returns a key for EVERY cited path (visibility for the floor)."""
    s = config.get_belief_evolve_settings()
    prop = _prop(evidence_paths=["daily/a.md", "daily/b.md", "daily/c.md"])
    floor = eg.read_evidence_for_floor(prop, tmp_path, settings=s)
    assert set(floor.keys()) == {"daily/a.md", "daily/b.md", "daily/c.md"}
    assert all(v == "" for v in floor.values())  # none exist -> all empty, none dropped


def test_empty_file_keyed_empty_by_floor_reader(tmp_path):
    """An EXISTING but empty cited file is non-supporting -> "" in the floor map."""
    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "empty.md").write_text("   \n  ", encoding="utf-8")
    prop = _prop(evidence_paths=["daily/empty.md"])
    floor = eg.read_evidence_for_floor(prop, tmp_path, settings=s)
    assert floor["daily/empty.md"] == ""
    # and the omit-reader drops it entirely
    assert eg.read_evidence_texts(prop, tmp_path, settings=s) == {}


# ===========================================================================
# vault confinement + re-root — daily-relative and vault/memory tail both reach memory_dir
# ===========================================================================


def test_vault_relative_path_reads_under_memory_dir(tmp_path):
    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text("lane first provider", encoding="utf-8")
    prop = _prop(evidence_paths=["daily/x.md"])
    texts = eg.read_evidence_texts(prop, tmp_path, settings=s)
    assert texts["daily/x.md"] == "lane first provider"


def test_thehomie_memory_tail_reroots_under_memory_dir(tmp_path):
    """A ``vault/memory/<tail>`` cite re-roots to memory_dir/<tail> (vault-tail rule)."""
    s = config.get_belief_evolve_settings()
    (tmp_path / "MEMORY.md").write_text("lane first provider memory", encoding="utf-8")
    prop = _prop(evidence_paths=["vault/memory/MEMORY.md"])
    texts = eg.read_evidence_texts(prop, tmp_path, settings=s)
    # the re-rooted read found memory_dir/MEMORY.md
    assert any("lane first provider memory" in v for v in texts.values())


def test_in_repo_out_of_vault_path_is_omitted(tmp_path):
    """A repo-relative path OUTSIDE the vault (e.g. .env) has NO in-root candidate -> omitted."""
    s = config.get_belief_evolve_settings()
    # create the would-be secret OUTSIDE the vault root
    secret = tmp_path.parent / "scripts_env_secret.txt"
    secret.write_text("TELLER_ACCESS_TOKEN=hunter2 lane first provider", encoding="utf-8")
    prop = _prop(evidence_paths=["../scripts_env_secret.txt"])
    spy: list[str] = []

    def reader(path):
        spy.append(str(path))
        return "TELLER_ACCESS_TOKEN=hunter2"

    texts = eg.read_evidence_texts(prop, tmp_path, settings=s, read_text=reader)
    assert texts == {}                                  # never keyed -> never supporting
    assert not any("scripts_env_secret" in p for p in spy)  # never read


# ===========================================================================
# verify_evidence_support — floor REUSE distinguishes the reject reason
# ===========================================================================


def test_floor_rejects_unread_claim_distinct_from_overlap(tmp_path):
    """A 'I verified the doc' claim citing an EMPTY file fails on belief_regression_floor.

    This is the crux: the deterministic floor (no_unread_claim) catches the
    truthfulness violation on a REAL falsifiable check, NOT on the weak overlap.
    The reason string proves WHICH layer rejected it.
    """
    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "empty.md").write_text("", encoding="utf-8")
    prop = _prop(
        proposed_content="I reviewed and verified the routing doc; it confirms lane-first.",
        evidence_paths=["daily/empty.md"],
    )
    ok, reason = eg.verify_evidence_support(
        prop, tmp_path, settings=s, corpus=_seed_corpus()
    )
    assert ok is False
    assert reason == "belief_regression_floor"


def test_supported_claim_with_real_read_passes(tmp_path):
    """A claim whose cited file EXISTS, is non-empty, and shares vocabulary -> verified."""
    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text(
        "the system routes by lane first then provider", encoding="utf-8"
    )
    prop = _prop(
        proposed_content="Routing is lane-first then provider.",
        evidence_paths=["daily/x.md"],
    )
    ok, reason = eg.verify_evidence_support(
        prop, tmp_path, settings=s, corpus=_seed_corpus()
    )
    assert ok is True
    assert reason == "evidence_verified"


def test_low_overlap_rejects_as_evidence_unsupported(tmp_path):
    """The gate's OWN overlap pre-filter rejects disjoint vocabulary as evidence_unsupported.

    To isolate the gate's own ``min_overlap`` branch from the floor's optional
    ``evidence_fidelity`` check, the corpus carries ONLY ``no_unread_claim`` (which
    does NOT fire here — the content asserts no read). The floor passes; the gate's
    own overlap check then rejects with the DISTINCT ``evidence_unsupported`` reason.
    """
    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text(
        "the operator prefers concise replies in chat", encoding="utf-8"
    )
    prop = _prop(
        proposed_content="Quantum chromodynamics governs gluon confinement entirely.",
        evidence_paths=["daily/x.md"],
    )
    no_read_only = [
        br.BeliefRegressionEntry(
            check_id="no-unread-claim", kind="no_unread_claim", description="", params={}
        )
    ]
    ok, reason = eg.verify_evidence_support(
        prop, tmp_path, settings=s, corpus=no_read_only
    )
    assert ok is False
    assert reason == "evidence_unsupported"


def test_verify_fails_closed_with_error_reason(tmp_path, capsys):
    """An unexpected exception inside the gate -> (False, 'evidence_check_error'), visible.

    A belief must EARN adoption; an internal failure is conservative (False), never
    a silent pass. Forcing the floor evaluator to raise drives the outer try/except.
    """
    import evolve.belief_regression as br_mod

    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text("lane first provider", encoding="utf-8")
    prop = _prop(evidence_paths=["daily/x.md"])

    # The gate imports evaluate_belief_regression from evolve.belief_regression at
    # call time; patch it on the module so the gate body raises mid-flight.
    orig = br_mod.evaluate_belief_regression

    def _boom(*_a, **_k):
        raise RuntimeError("floor evaluator blew up")

    br_mod.evaluate_belief_regression = _boom
    try:
        ok, reason = eg.verify_evidence_support(
            prop, tmp_path, settings=s, corpus=_seed_corpus()
        )
    finally:
        br_mod.evaluate_belief_regression = orig

    assert ok is False
    assert reason == "evidence_check_error"
    assert "[evolve.gate]" in capsys.readouterr().out


def test_no_evidence_paths_rejects(tmp_path):
    """Zero cited paths -> too few supporting paths -> rejected (never a silent OK)."""
    s = config.get_belief_evolve_settings()
    prop = _prop(evidence_paths=[])
    ok, reason = eg.verify_evidence_support(
        prop, tmp_path, settings=s, corpus=_seed_corpus()
    )
    assert ok is False
    assert reason in {"evidence_unsupported", "belief_regression_floor"}
