"""Tests for orchestration/suggestion_catalog.py — one test per code path.

Covers the curated starter seeder: the injectable ``add_fn`` fan-out, the
``keys`` filter, idempotent re-seed via the real store's dedup latch, the
re-seed-after-dismiss latch, and the 5-field-cron / Homie-shape invariant on
every catalog ``job_spec``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import config  # noqa: E402
from orchestration import suggestion_catalog as sc  # noqa: E402
from orchestration import suggestions as sg  # noqa: E402

# The exact /api/scheduled guard regex (dashboard_api._CRON_RE), copied so this
# orchestration test stays pure.
_CRON_RE = re.compile(r"^[\d\*\-\,\/]+( +[\d\*\-\,\/]+){4}$")

_CATALOG_KEYS = {
    "catalog:daily-briefing",
    "catalog:important-mail",
    "catalog:weekly-review",
    "catalog:vault-sweep",
}


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", d)
    return d


# --------------------------------------------------------------------------- #
# Catalog shape invariants (pure — no store)
# --------------------------------------------------------------------------- #

def test_catalog_has_four_expected_entries() -> None:
    assert {e.key for e in sc.CATALOG} == _CATALOG_KEYS
    assert len(sc.CATALOG) == 4


def test_every_job_spec_is_homie_shaped_five_field_cron() -> None:
    for entry in sc.CATALOG:
        spec = entry.job_spec
        assert set(spec) == {"persona_id", "prompt", "schedule", "next_run"}, entry.key
        assert spec["next_run"] is None
        assert _CRON_RE.match(spec["schedule"]), (entry.key, spec["schedule"])
        assert spec["prompt"].strip(), entry.key


# --------------------------------------------------------------------------- #
# seed_catalog_suggestions — injected add_fn
# --------------------------------------------------------------------------- #

def test_seed_with_fake_add_fn_fans_out_all_entries() -> None:
    calls = []

    def fake_add(**kwargs):
        calls.append(kwargs)
        return {"id": kwargs["dedup_key"]}

    made = sc.seed_catalog_suggestions(add_fn=fake_add)
    assert len(made) == 4
    assert {c["dedup_key"] for c in calls} == _CATALOG_KEYS
    assert all(c["source"] == "catalog" for c in calls)
    # add_fn receives a COPY of the job_spec (mutating it must not touch CATALOG).
    for c in calls:
        c["job_spec"]["schedule"] = "MUTATED"
    assert all(e.job_spec["schedule"] != "MUTATED" for e in sc.CATALOG)


def test_seed_keys_filter_restricts_subset() -> None:
    made = sc.seed_catalog_suggestions(
        add_fn=lambda **kw: {"id": kw["dedup_key"]},
        keys=["catalog:vault-sweep"],
    )
    assert [m["id"] for m in made] == ["catalog:vault-sweep"]


def test_seed_skips_when_add_fn_returns_none() -> None:
    # Store-refused entries (dedup/cap) are dropped from the returned list.
    made = sc.seed_catalog_suggestions(add_fn=lambda **kw: None)
    assert made == []


# --------------------------------------------------------------------------- #
# seed_catalog_suggestions — real store (default add_fn wiring)
# --------------------------------------------------------------------------- #

def test_seed_default_add_fn_uses_real_store(state_dir) -> None:
    made = sc.seed_catalog_suggestions()
    assert len(made) == 4
    pending = {s["dedup_key"] for s in sg.list_pending()}
    assert pending == _CATALOG_KEYS


def test_reseed_is_idempotent(state_dir) -> None:
    assert len(sc.seed_catalog_suggestions()) == 4
    # Second seed: every dedup_key is already pending -> nothing new.
    assert sc.seed_catalog_suggestions() == []
    assert len(sg.list_pending()) == 4


def test_reseed_after_dismiss_does_not_readd(state_dir) -> None:
    sc.seed_catalog_suggestions()
    vault = next(s for s in sg.list_pending() if s["dedup_key"] == "catalog:vault-sweep")
    assert sg.dismiss_suggestion(vault["id"]) is True
    # Re-seeding must NOT resurrect the dismissed entry (latched forever).
    again = sc.seed_catalog_suggestions()
    assert again == []
    keys_pending = {s["dedup_key"] for s in sg.list_pending()}
    assert "catalog:vault-sweep" not in keys_pending
    assert len(keys_pending) == 3
