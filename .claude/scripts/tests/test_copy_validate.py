"""Tests for .archon/scripts/copy-validate.py — one violation fixture per gate
class, plus the 3-way exit contract, hash-dedup, --ledger-settled semantics,
and copy-brief's ledger reconcile. Base fixtures are proven green first so a
failure can only mean the targeted violation was caught.
"""

from __future__ import annotations

import copy as copy_mod
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[3] / ".archon" / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cv = _load("copy_validate", "copy-validate.py")
cb = sys.modules["copy_brief"]  # loaded transitively by copy-validate
pc = sys.modules["profile_compile"]


def make_profile_dict() -> dict:
    palette = {k: "#123456" for k in (
        "primary", "primary_2", "ink", "muted", "accent", "accent_deep", "accent_soft",
        "surface", "surface_2", "white", "line", "line_strong",
    )}
    font = {"family": "T", "stack": '"T", serif', "file": "fonts/t.woff2"}
    return {
        "identity": {"slug": "acme", "display_name": "Alex Advisor", "org_name": "Acme Advisory", "vertical": "t"},
        "brand": {
            "brand_mark": "A",
            "palette": palette,
            "typography": {"display": dict(font), "body": dict(font), "mono": dict(font)},
            "banned_phrases": ["peace of mind"],
            "voice_tone": "calm",
        },
        "facts": {
            "advisor": {"name": "Alex Advisor", "title": "Specialist"},
            "org_names": ["Acme Advisory"],
            "contact": {"phone_display": "(555) 000-1111", "phone_tel": "+15550001111", "email": "a@example.com"},
            "services": [
                {"id": "s1", "name": "Widget Planning", "short_label": "Widgets", "form_label": "Widget Planning", "path": "services/widgets"},
                {"id": "s2", "name": "Gadget Planning", "short_label": "Gadgets", "form_label": "Gadget Planning", "path": "services/gadgets"},
            ],
            "number_whitelist": ["(555) 000-1111", "+15550001111"],
        },
        "page_plan": {
            "nav": [{"page": "svc", "label": "Services"}],
            "nav_cta": {"page": "contact", "label": "Book"},
            "footer_links": [{"page": "contact", "label": "Contact"}],
            "pages": [
                {"id": "svc", "path": "services", "template": "service-index",
                 "meta": {"title": "S", "description": "D", "og_image": "og.png"}, "hero": {"poster": "h.webp"}},
                {"id": "contact", "path": "contact", "template": "consultation",
                 "meta": {"title": "C", "description": "D", "og_image": "og.png"}, "hero": {"poster": "h.webp"}},
                {"id": "guide", "path": "guide", "template": "article",
                 "meta": {"title": "G", "description": "D", "og_image": "og.png"}, "hero": {"poster": "h.webp"}},
            ],
        },
        "images": {"persona_pack": "none", "assets_dir": "assets-src"},
        "compliance": {"fine_print": "Educational only."},
        "copy_gates": {"min_words": {"article": 40}, "max_overlap": 0.10},
        "deploy": {"held": True, "base_path": "/acme", "canonical_base": "https://example.com/acme"},
    }


@pytest.fixture()
def client(tmp_path: Path):
    """Client dir with profile, briefs, ledger, and PASSING artifacts."""
    profile_dict = make_profile_dict()
    profile_path = tmp_path / "client.yaml"
    profile_path.write_text(yaml.safe_dump(profile_dict, sort_keys=False), encoding="utf-8")
    cb.prepare(profile_path)
    profile = pc.load_profile(profile_path)
    gen = tmp_path / "copy-generated"
    gen.mkdir()

    pages = {p["id"]: p for p in profile["page_plan"]["pages"]}

    def fixed(pid):
        return cb.compute_fixed_slots(profile, pages[pid])

    artifacts = {
        "svc": {
            "hero_label": "Planning Services",
            "hero_headline": "Two areas of focus for careful households.",
            "hero_lede": "Widget planning and gadget planning, handled in the order that fits your family.",
            "vignettes": [
                {"href": "{{site.base}}/services/widgets", "title": "Widget Planning", "blurb": "Organize widget records before decisions get urgent for anyone involved."},
                {"href": "{{site.base}}/services/gadgets", "title": "Gadget Planning", "blurb": "Compare gadget options slowly with every document already on the table."},
            ],
            "split_eyebrow": "Where families begin",
            "split_heading": "Start where the pressure is.",
            "split_prose_html": "<p>Some households arrive with a widget question.</p>\n<p>Others begin from a gadget concern and branch out.</p>",
            "split_cta_label": "Book a review",
            "band2_eyebrow": "Before you call",
            "band2_heading": "Bring the useful records.",
            "band2_note": "A short list makes the first conversation productive.",
            "question_cards": [
                {"heading": "Family notes", "note": "Write down who depends on you today."},
                {"heading": "Records", "note": "Bring current paperwork if it is available."},
                {"heading": "Questions", "note": "List the concerns that matter most."},
            ],
        },
        "contact": {
            "hero_label": "Contact Alex",
            "hero_headline": "Reach out about a planning conversation.",
            "hero_lede": "Call directly or send a short note about the topic on your mind.",
            "left_label": "Message Alex",
            "left_heading": "Start with the topic.",
            "left_body_html": "<p>Share a high-level question first.</p>\n<ul class=\"check-list\">\n<li>Use the phone for time-sensitive matters.</li>\n<li>Use email for written questions.</li>\n<li>Select the closest service.</li>\n</ul>\n<p>Do not send sensitive documents in a first message.</p>",
            "form_submit_label": "Email Alex",
        },
        "guide": {
            **fixed("guide"),
            "hero_label": "Planning Guide",
            "hero_headline": "How to prepare for the first conversation.",
            "hero_lede": "A little preparation makes the meeting twice as useful for everyone attending.",
            "kicker": "Preparation Checklist",
            "heading": "Bring one focused question.",
            "article_lede": "The first meeting works best when priorities and records arrive together.",
            "steps": [
                {"heading": "What should your family understand?", "note": "Write down which responsibilities matter most and which questions feel hardest before anything becomes paperwork for the household."},
                {"heading": "Which records exist already?", "note": "A simple handwritten list of policies, accounts, and beneficiary forms is enough to anchor the whole review."},
                {"heading": "Are beneficiary choices current?", "note": "Designations drift quietly after marriages, births, and job changes, and stale ones override newer intentions."},
                {"heading": "Which professionals belong in the room?", "note": "Coordinated decisions may involve an attorney or a tax professional, and knowing that early saves a second meeting."},
            ],
            "resource_cta_heading": "Ready to organize the first conversation?",
            "resource_cta_note": "Share the topic and the best way to reach you.",
            "resource_cta_label": "Contact Alex",
        },
    }
    for pid, artifact in artifacts.items():
        (gen / f"{pid}.json").write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")

    return {"dir": tmp_path, "profile_path": profile_path, "profile": profile, "gen": gen, "pages": pages, "fixed": fixed}


def run(client, *args) -> int:
    return cv.main([str(client["profile_path"]), *args])


def mutate(client, pid, **changes):
    path = client["gen"] / f"{pid}.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact.update(changes)
    for key, value in list(changes.items()):
        if value is None:
            artifact.pop(key, None)
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")


def verdict_codes(client, pid) -> set[str]:
    profile = client["profile"]
    page = client["pages"][pid]
    verdicts = cv.validate_artifact(profile, page, client["gen"] / f"{pid}.json", {})
    return {v["code"] for v in verdicts}


# ------------------------------------------------------------------ the tests


def test_base_fixture_passes_all(client):
    assert run(client, "--all") == 0


def test_exit_contract_three_way(client):
    assert run(client, "--page", "guide") == 0                      # pass
    mutate(client, "guide", hero_headline=None)
    assert run(client, "--page", "guide") == 1                      # verdict
    assert run(client, "--page", "ghost-page") == 2                 # infra


def test_missing_artifact_and_invalid_json_are_verdicts(client):
    (client["gen"] / "guide.json").unlink()
    assert verdict_codes(client, "guide") == {"missing_artifact"}
    (client["gen"] / "guide.json").write_text("```json\n{}\n```", encoding="utf-8")
    assert verdict_codes(client, "guide") == {"invalid_json"}


def test_schema_missing_extra_and_wrong_kind(client):
    mutate(client, "contact", hero_lede=None, bonus_slot="x", left_body_html=["not", "a", "string"])
    codes = verdict_codes(client, "contact")
    assert {"missing_slot", "extra_slot", "wrong_kind"} <= codes


def test_fixed_slot_must_be_verbatim(client):
    tampered = client["fixed"]("guide")["detail_nav"]
    tampered = [dict(x, label=x["label"].upper()) for x in tampered]
    mutate(client, "guide", detail_nav=tampered)
    assert "fixed_slot_mismatch" in verdict_codes(client, "guide")


def test_list_shape_and_href_order(client):
    artifact = json.loads((client["gen"] / "svc.json").read_text(encoding="utf-8"))
    swapped = [artifact["vignettes"][1], artifact["vignettes"][0]]
    mutate(client, "svc", vignettes=swapped)
    assert "list_href_mismatch" in verdict_codes(client, "svc")
    mutate(client, "svc", question_cards=artifact["question_cards"][:2])
    assert "list_shape" in verdict_codes(client, "svc")


def test_fragment_safety(client):
    bad = (
        '<script>alert(1)</script>'
        '<p class="mystery-class" onclick="x()">hi</p>'
        '<a href="https://evil.example.com/">out</a>'
    )
    mutate(client, "contact", left_body_html=bad)
    codes = verdict_codes(client, "contact")
    assert {"fragment_tag", "fragment_class", "fragment_attr", "fragment_href"} <= codes


def test_prohibited_phrases_and_em_dash(client):
    mutate(client, "guide", hero_lede="Total peace of mind — guaranteed lowest effort.")
    codes = verdict_codes(client, "guide")
    assert {"prohibited", "em_dash"} <= codes


def test_fact_whitelist(client):
    mutate(client, "contact", hero_lede="Call (999) 123-4567 or ask about License #AB12345 today.")
    codes = verdict_codes(client, "contact")
    assert codes == {"fact_whitelist"}


def test_min_words(client):
    artifact = json.loads((client["gen"] / "guide.json").read_text(encoding="utf-8"))
    artifact["steps"] = [{"heading": "Q?", "note": "Short."} for _ in range(4)]
    for slot in ("hero_lede", "article_lede", "resource_cta_note"):
        artifact[slot] = "Brief."
    (client["gen"] / "guide.json").write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    assert "min_words" in verdict_codes(client, "guide")


def test_overlap_names_page_and_shingles(client):
    long_text = " ".join(
        "the quick brown fox jumps over the lazy dog before the careful advisor "
        f"organizes every record in drawer number {i} of the tall cabinet"
        for i in ("one", "two", "three", "four", "five", "six")
    )
    mutate(client, "svc", hero_lede=long_text, split_prose_html=f"<p>{long_text}</p>")
    mutate(client, "contact", hero_lede=long_text, left_body_html=f"<p>{long_text}</p>")
    profile = client["profile"]
    others = {"svc": (json.loads((client["gen"] / "svc.json").read_text(encoding="utf-8")), cb.SCHEMAS["service-index"])}
    verdicts = cv.validate_artifact(profile, client["pages"]["contact"], client["gen"] / "contact.json", others)
    overlap = [v for v in verdicts if v["code"] == "overlap"]
    assert overlap, "shared prose must trip the overlap gate"
    detail = overlap[0]["detail"]
    assert "with svc" in detail, "verdict must name the colliding page"
    assert "shared sequences: ['" in detail, "verdict must list literal shingles for the rewrite"


def test_fixed_slots_do_not_poison_overlap(client):
    """detail_nav is identical across pages by DESIGN — it must not count."""
    profile = client["profile"]
    guide = json.loads((client["gen"] / "guide.json").read_text(encoding="utf-8"))
    text = cv.visible_words(guide, cb.SCHEMAS["article"])
    assert "Widgets" not in text  # fixed detail_nav labels excluded


def test_update_ledger_hash_dedup(client):
    mutate(client, "guide", hero_headline=None)
    assert run(client, "--page", "guide", "--update-ledger") == 1
    ledger = json.loads((client["dir"] / "copy-ledger.json").read_text(encoding="utf-8"))
    entry = next(e for e in ledger["pages"] if e["id"] == "guide")
    assert entry["attempts"] == 1 and entry["status"] == "held_back"
    # unchanged artifact revalidated -> verdict re-emitted, attempt NOT charged
    assert run(client, "--page", "guide", "--update-ledger") == 1
    ledger = json.loads((client["dir"] / "copy-ledger.json").read_text(encoding="utf-8"))
    entry = next(e for e in ledger["pages"] if e["id"] == "guide")
    assert entry["attempts"] == 1
    assert entry["failures"] and entry["failures"][0]["scope"] in ("slot", "page")


def test_ledger_settled_semantics(client):
    assert run(client, "--ledger-settled") == 1  # everything pending -> actionable
    for pid in ("svc", "contact", "guide"):
        assert run(client, "--page", pid, "--update-ledger") == 0
    assert run(client, "--ledger-settled") == 0  # all passed
    # exhausted attempts also settle
    ledger_path = client["dir"] / "copy-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    entry = next(e for e in ledger["pages"] if e["id"] == "guide")
    entry.update(status="held_back", attempts=ledger["max_attempts"])
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    assert run(client, "--ledger-settled") == 0


def test_ledger_reconcile_on_prepare(client):
    for pid in ("svc", "contact", "guide"):
        assert run(client, "--page", pid, "--update-ledger") == 0
    # tamper one passed artifact -> hash mismatch -> reset to pending
    mutate(client, "guide", hero_headline="A silently edited headline.")
    profile_dict = make_profile_dict()
    profile_dict["page_plan"]["pages"].append(
        {"id": "extra", "path": "extra", "template": "article",
         "meta": {"title": "E", "description": "D", "og_image": "og.png"}, "hero": {"poster": "h.webp"}}
    )
    client["profile_path"].write_text(yaml.safe_dump(profile_dict, sort_keys=False), encoding="utf-8")
    cb.prepare(client["profile_path"])
    ledger = json.loads((client["dir"] / "copy-ledger.json").read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in ledger["pages"]}
    assert by_id["extra"]["status"] == "pending"          # new page joined
    assert by_id["guide"]["status"] == "pending"          # tampered passed entry reset
    assert by_id["svc"]["status"] == "passed"             # untouched passed entry survives
