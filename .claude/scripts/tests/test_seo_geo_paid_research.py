"""Tests for the bounded, brokered DataForSEO GEO research runner.

Every provider process is mocked.  These tests prove the ordering and local
receipt behavior without calling Google Search Console or DataForSEO.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_geo_paid_research as paid  # noqa: E402


@pytest.fixture(autouse=True)
def _fleet_registry(monkeypatch):
    monkeypatch.setattr(
        paid,
        "_fleet_domains",
        lambda: {"example.com", "your-business.example.com", "highriskautoca.com", "sacautoinsurance.com"},
    )


def _candidate(*, domain: str = "example.com", query: str = "example query") -> dict[str, str]:
    return {
        "brand_id": "example",
        "brand_name": "Example",
        "domain": domain,
        "site_url": f"sc-domain:{domain}",
        "query": query,
    }


def _slice(*pages: str, at_limit: bool = False) -> dict[str, object]:
    return {
        "data_state": "final",
        "at_limit": at_limit,
        "rows": [{"page": page, "query": "ignored"} for page in pages],
    }


def _all_eligible_slice(*, site_url: str, query: str, days: int) -> dict[str, object]:
    domain = site_url.removeprefix("sc-domain:")
    host = "www.your-business.example.com" if domain == "your-business.example.com" else domain
    return _slice(f"https://{host}/current-owner")


def _successful_process(command, **_kwargs):
    output = Path(command[command.index("--output") + 1])
    keywords = [command[index + 1] for index, value in enumerate(command) if value == "--keyword"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps([
            {
                "keyword": keyword,
                "ai_overview_present": False,
                "cited_domains": [],
                "domain_cited": False,
            }
            for keyword in keywords
        ]),
        encoding="utf-8",
    )
    return SimpleNamespace(returncode=0, stdout="Saved", stderr="")


def test_preflight_requires_one_in_domain_non_truncated_gsc_owner(monkeypatch):
    candidate = _candidate()

    monkeypatch.setattr(paid, "_query_page_slice", lambda **_kwargs: _slice("https://example.com/a"))
    accepted, rejected = paid.preflight_candidates([candidate])
    assert [item["canonical_owner_url"] for item in accepted] == ["https://example.com/a"]
    assert rejected == []

    monkeypatch.setattr(
        paid,
        "_query_page_slice",
        lambda **_kwargs: _slice("https://example.com/a", "https://example.com/b"),
    )
    accepted, rejected = paid.preflight_candidates([candidate])
    assert accepted == []
    assert rejected[0]["reason"] == "multiple_current_gsc_page_owners"

    monkeypatch.setattr(paid, "_query_page_slice", lambda **_kwargs: _slice("https://other.example/a"))
    _accepted, rejected = paid.preflight_candidates([candidate])
    assert rejected[0]["reason"] == "gsc_owner_host_mismatch"

    monkeypatch.setattr(
        paid,
        "_query_page_slice",
        lambda **_kwargs: _slice("https://example.com/a", at_limit=True),
    )
    _accepted, rejected = paid.preflight_candidates([candidate])
    assert rejected[0]["reason"] == "gsc_slice_at_limit"


def test_preflight_respects_a_known_noindex_block_without_gsc_call(monkeypatch):
    candidate = {**_candidate(), "blocked_reason": "url_inspection_noindex_2026-08-12"}
    monkeypatch.setattr(
        paid,
        "_query_page_slice",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no GSC read expected")),
    )

    accepted, rejected = paid.preflight_candidates([candidate])

    assert accepted == []
    assert rejected[0]["reason"] == "url_inspection_noindex_2026-08-12"


def test_sandbox_runs_only_geo_mentions_with_flags_after_subcommand(monkeypatch, tmp_path):
    seen: dict[str, object] = {}
    monkeypatch.setattr(paid, "_query_page_slice", _all_eligible_slice)
    def process(command, **kwargs):
        seen["command"] = command
        return _successful_process(command, **kwargs)

    def no_broker():
        raise AssertionError("sandbox must not call broker")

    monkeypatch.setattr(paid.subprocess, "run", process)
    monkeypatch.setattr(paid, "_broker_module", no_broker)

    receipt = paid.run(mode="sandbox", out_dir=tmp_path, candidates=[_candidate()])

    command = seen["command"]
    assert isinstance(command, list)
    assert command[2] == "geo-mentions"
    assert command.index("geo-mentions") < command.index("--sandbox")
    assert command.index("geo-mentions") < command.index("--max-cost")
    assert "fleet-report" not in command
    assert receipt["provider"]["status"] == "completed"
    assert receipt["budget"]["reservation_status"] == "sandbox_no_charge"
    assert receipt["site_mutations"] == []
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["mode"] == "sandbox"


def test_production_reserves_before_dispatch_then_settles(monkeypatch, tmp_path):
    calls: list[tuple[str, dict[str, object]]] = []

    def reserve(**kwargs):
        calls.append(("reserve", kwargs))
        return {"run_id": "budget-run-1", "status": "reserved", "budget": {"remaining_usd": 22.49}}

    def settle(**kwargs):
        calls.append(("settle", kwargs))
        return {"run_id": "budget-run-1", "status": "settled", "budget": {"remaining_usd": 22.49}}

    def unknown(**kwargs):
        calls.append(("unknown", kwargs))
        return {"run_id": "budget-run-1", "status": "unknown"}

    broker = SimpleNamespace(
        reserve=reserve,
        settle=settle,
        mark_unknown=unknown,
        budget_status=lambda **_kwargs: {},
    )
    monkeypatch.setattr(paid, "_broker_module", lambda: broker)
    monkeypatch.setattr(paid, "_query_page_slice", _all_eligible_slice)

    def process(command, **kwargs):
        assert calls and calls[-1][0] == "reserve"
        return _successful_process(command, **kwargs)

    monkeypatch.setattr(paid.subprocess, "run", process)
    receipt = paid.run(mode="production", out_dir=tmp_path, candidates=[_candidate()])

    assert [name for name, _kwargs in calls] == ["reserve", "settle"]
    reserved = calls[0][1]
    assert reserved["provider"] == "dataforseo"
    assert reserved["operation"] == "geo-mentions"
    assert reserved["estimated_usd"] == 0.01
    assert reserved["metadata"]["units"] == 1
    assert receipt["provider"]["status"] == "completed"
    assert receipt["budget"]["reservation_status"] == "settled"
    assert receipt["budget"]["charged_usd"] == 0.01


def test_results_measure_each_owner_and_the_registered_fleet_not_just_YourBusiness():
    results = paid._annotate_results(
        [{"keyword": "example query", "cited_domains": ["www.example.com", "outside.example"]}],
        accepted=[{**_candidate(), "canonical_owner_url": "https://example.com/a"}],
        fleet_domains={"example.com", "your-business.example.com"},
    )

    assert results[0]["owner_domain"] == "example.com"
    assert results[0]["owner_domain_cited"] is True
    assert results[0]["fleet_cited_domains"] == ["example.com"]


def test_timeout_marks_reserved_run_unknown_without_retry(monkeypatch, tmp_path):
    calls: list[tuple[str, dict[str, object]]] = []
    broker = SimpleNamespace(
        reserve=lambda **kwargs: (calls.append(("reserve", kwargs)), {"run_id": "budget-run-2"})[1],
        settle=lambda **kwargs: (calls.append(("settle", kwargs)), {})[1],
        mark_unknown=lambda **kwargs: (calls.append(("unknown", kwargs)), {"status": "unknown"})[1],
        budget_status=lambda **_kwargs: {},
    )
    monkeypatch.setattr(paid, "_broker_module", lambda: broker)
    monkeypatch.setattr(paid, "_query_page_slice", _all_eligible_slice)
    monkeypatch.setattr(
        paid.subprocess,
        "run",
        lambda command, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(command, 1)),
    )

    receipt = paid.run(mode="production", out_dir=tmp_path, candidates=[_candidate()])

    assert [name for name, _kwargs in calls] == ["reserve", "unknown"]
    assert receipt["provider"]["status"] == "unknown"
    assert receipt["provider"]["unknown_reason"] == "provider_timeout"
    assert receipt["budget"]["charged_usd"] == 0.01


def test_malformed_provider_receipt_marks_unknown(monkeypatch, tmp_path):
    calls: list[str] = []
    broker = SimpleNamespace(
        reserve=lambda **_kwargs: {"run_id": "budget-run-3"},
        settle=lambda **_kwargs: calls.append("settle"),
        mark_unknown=lambda **_kwargs: (calls.append("unknown"), {"status": "unknown"})[1],
        budget_status=lambda **_kwargs: {},
    )
    monkeypatch.setattr(paid, "_broker_module", lambda: broker)
    monkeypatch.setattr(paid, "_query_page_slice", _all_eligible_slice)

    def malformed(command, **_kwargs):
        Path(command[command.index("--output") + 1]).parent.mkdir(parents=True, exist_ok=True)
        Path(command[command.index("--output") + 1]).write_text("not-json", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paid.subprocess, "run", malformed)
    receipt = paid.run(mode="production", out_dir=tmp_path, candidates=[_candidate()])

    assert calls == ["unknown"]
    assert receipt["provider"]["status"] == "unknown"
    assert receipt["provider"]["unknown_reason"] == "provider_receipt_uncertain"


def test_broker_rejection_stops_before_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(paid, "_query_page_slice", _all_eligible_slice)
    monkeypatch.setattr(
        paid,
        "_broker_module",
        lambda: SimpleNamespace(
            reserve=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("cap"))
        ),
    )
    monkeypatch.setattr(
        paid.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not dispatch provider")
        ),
    )

    receipt = paid.run(mode="production", out_dir=tmp_path, candidates=[_candidate()])

    assert receipt["provider"]["status"] == "blocked_by_budget_broker"
    assert receipt["budget"]["reservation_status"] == "blocked"
