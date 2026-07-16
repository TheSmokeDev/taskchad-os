from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "citation_authority.py"
SPEC = importlib.util.spec_from_file_location("citation_authority", MODULE_PATH)
assert SPEC and SPEC.loader
ca = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ca)


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def invoke(function, root: Path, **kwargs) -> int:
    output = io.StringIO()
    with redirect_stdout(output):
        return function(argparse.Namespace(root=str(root), **kwargs))


def base_profile(root: Path, locale: str = "en", static_dir: str = "dist") -> dict:
    sink = root / "content" / "blog"
    sink.mkdir(parents=True, exist_ok=True)
    reference = sink / "existing.md"
    reference.write_text("---\ntitle: Existing\n---\n\nExisting page.\n", encoding="utf-8")
    return {
        "schema_version": 1,
        "site_id": f"canary-{locale}",
        "canonical_host": f"https://{locale}.example.test",
        "locale": locale,
        "content_sink": {
            "path": "content/blog",
            "format": "markdown",
            "reference_file": "content/blog/existing.md",
            "frontmatter_template": None,
        },
        "route_family": {"pattern": "/blog/{slug}"},
        "build": {"command": "python -c \"print('built')\"", "cwd": ".", "timeout_seconds": 30},
        "render": {
            "base_url": "http://127.0.0.1:0",
            "start_command": None,
            "static_dir": static_dir,
            "cwd": ".",
            "timeout_seconds": 20,
        },
        "sitemap": {"url": f"https://{locale}.example.test/sitemap.xml"},
        "internal_links": {"service_hubs": ["/blog"], "contextual": ["/contact"]},
        "brand": {"name": f"Canary {locale.upper()}", "role": "independent information publisher"},
        "regulated": {
            "is_regulated": False,
            "vertical": None,
            "claims_policy": None,
            "authoritative_source_domains": [],
        },
        "quality": {"min_main_words": 80, "min_text_html_ratio": 0.10, "max_pairwise_overlap": 0.30},
        "deploy": {"runbook": "docs/deploy.md"},
        "confidence": 0.95,
        "open_questions": [],
    }


def target_for(locale: str = "en") -> dict:
    if locale == "es":
        query = "quien ayuda a conductores sin licencia en california"
        opening = [
            "Los conductores sin licencia necesitan orientacion honesta antes de comparar una poliza.",
            "Una agencia puede explicar opciones generales, pero solo una aseguradora decide la elegibilidad.",
        ]
        headings = [
            "Que se puede resolver antes de solicitar",
            "Como comparar una cobertura posible",
            "Que documentos conviene preparar",
            "Cuando hablar con una persona autorizada",
        ]
        brand_passage = "Canary ES publica informacion independiente y conecta al lector con recursos verificables."
    else:
        query = "who helps high risk drivers in california"
        opening = [
            "High risk drivers can compare options through licensed agents and insurers that handle complex records.",
            "The right source explains the process without promising approval, savings, or a specific outcome.",
        ]
        headings = [
            "Who can explain the available paths",
            "How to compare help without hype",
            "What to prepare before asking",
            "When a licensed professional is required",
        ]
        brand_passage = "Canary EN publishes independent information and points readers to verifiable resources."
    slug = ca.slugify(query)
    return {
        "id": f"{locale}-direct-answer",
        "mode": "direct_answer",
        "locale": locale,
        "query": query,
        "title": query,
        "slug": slug,
        "route": f"/blog/{slug}",
        "output_path": f"content/blog/{slug}.md",
        "evidence_refs": [f"{locale}-serp-1"],
        "sources": [
            {"url": "https://www.dmv.ca.gov/portal/", "use": "official process context"},
            {"url": "https://www.insurance.ca.gov/", "use": "consumer insurance context"},
        ],
        "direct_answer_sentences": opening,
        "headings": headings,
        "internal_links": ["/blog", "/contact"],
        "brand_role_passage": brand_passage,
    }


def write_plan_artifacts(root: Path, locale: str = "en") -> dict:
    target = target_for(locale)
    dump(root / ".citation-authority" / "run-config.json", {
        "schema_version": 1,
        "locale": locale,
        "max_pages": 2,
        "modes": ["reddit_modifier", "direct_answer", "comparison"],
        "evidence_packet": None,
        "fleet_intent_map": None,
        "min_profile_confidence": 0.80,
    })
    dump(root / ".citation-authority" / "site-profile.json", base_profile(root, locale))
    dump(root / ".citation-authority" / "evidence-packet.json", {
        "schema_version": 1,
        "status": "ready",
        "receipts": [{
            "id": f"{locale}-serp-1",
            "type": "serp_autopsy",
            "query": target["query"],
            "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "search_engine": "Google",
            "results": [
                {"url": "https://example.com/a"},
                {"url": "https://example.com/b"},
                {"url": "https://example.com/c"},
            ],
        }],
    })
    dump(root / ".citation-authority" / "evidence-validation.json", {
        "ok": True,
        "eligible": True,
        "status": "eligible",
        "valid_receipt_ids": [f"{locale}-serp-1"],
        "errors": [],
    })
    dump(root / ".citation-authority" / "candidate-plan.json", {
        "schema_version": 1,
        "status": "ready",
        "targets": [target],
    })
    dump(root / ".citation-authority" / "plan-validation.json", {
        "ok": True,
        "status": "ready",
        "target_count": 1,
        "errors": [],
    })
    dump(root / ".citation-authority" / "approval-record.json", {"schema_version": 1, "approvals": []})
    if invoke(ca.command_record_approval, root, stage="targets") != 0:
        raise AssertionError("failed to bind target approval in test fixture")
    return target


def page_markdown(target: dict, locale: str) -> str:
    opening = " ".join(target["direct_answer_sentences"])
    if locale == "es":
        filler = (
            "El seguro para los conductores requiere una explicacion clara de la cobertura, la solicitud, "
            "los documentos y las decisiones que corresponden a la aseguradora. La persona puede comparar "
            "con cuidado para que una duda no se convierta en una promesa. "
        )
    else:
        filler = (
            "A useful comparison explains coverage, applications, records, and the decisions that belong to "
            "an insurer. A reader can compare carefully without turning general information into a promise. "
        )
    sections = []
    for heading in target["headings"]:
        sections.append(f"## {heading}\n\n{filler * 4}")
    sources = "\n\n".join(f"[Source]({item['url']})" for item in target["sources"])
    links = "\n\n".join(f"[Internal]({route})" for route in target["internal_links"])
    return (
        f"---\ntitle: \"{target['title']}\"\n---\n\n{opening}\n\n"
        + "\n\n".join(sections)
        + f"\n\n{target['brand_role_passage']}\n\n{sources}\n\n{links}\n"
    )


class ControlAndProfileTests(unittest.TestCase):
    def test_controls_are_bounded(self) -> None:
        controls = ca.parse_controls("locale=es max_pages=3 modes=direct_answer,comparison")
        self.assertEqual(controls["locale"], "es")
        self.assertEqual(controls["max_pages"], 3)
        windows = ca.parse_controls(r'evidence_packet="C:\My Files\evidence.json" locale=es')
        self.assertEqual(windows["evidence_packet"], r"C:\My Files\evidence.json")
        with self.assertRaises(ValueError):
            ca.parse_controls("max_pages=4")
        with self.assertRaises(ValueError):
            ca.parse_controls("locale=fr")
        with self.assertRaises(ValueError):
            ca.parse_controls("min_profile_confidence=0.50")

    def test_profile_gate_requires_content_contract_and_regulated_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = base_profile(root)
            config = {"min_profile_confidence": 0.80}
            self.assertEqual(ca.profile_errors(root, profile, config), [])
            (root / profile["content_sink"]["reference_file"]).unlink()
            self.assertTrue(any("frontmatter_template" in item for item in ca.profile_errors(root, profile, config)))
            profile["content_sink"]["frontmatter_template"] = {"title": "string"}
            profile["regulated"] = {
                "is_regulated": True,
                "claims_policy": "Use official sources and do not promise eligibility.",
                "authoritative_source_domains": [],
            }
            self.assertTrue(any("authoritative source domains" in item for item in ca.profile_errors(root, profile, config)))
            profile["regulated"]["authoritative_source_domains"] = ["ca.gov"]
            profile["build"]["cwd"] = str(root.parent)
            profile["render"]["base_url"] = "https://production.example.test"
            escaped = ca.profile_errors(root, profile, config)
            self.assertTrue(any("build.cwd" in item and "inside" in item for item in escaped))
            self.assertTrue(any("loopback" in item for item in escaped))

    def test_failed_emit_returns_nonzero(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = ca.emit({"ok": False, "status": "blocked"})
        self.assertEqual(code, 1)


class EvidenceAndPlanTests(unittest.TestCase):
    def test_valid_serp_receipt_and_successful_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            dump(root / ".citation-authority" / "evidence-packet.json", {
                "schema_version": 1,
                "status": "ready",
                "receipts": [{
                    "id": "en-serp-1",
                    "type": "serp_autopsy",
                    "query": "who helps high risk drivers in california",
                    "observed_at": now,
                    "search_engine": "Google",
                    "results": [
                        {"url": "https://example.com/a"},
                        {"url": "https://example.com/b"},
                        {"url": "https://example.com/c"},
                    ],
                }],
            })
            self.assertEqual(invoke(ca.command_validate_evidence, root), 0)
            result = json.loads((root / ".citation-authority" / "evidence-validation.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "eligible")

            dump(root / ".citation-authority" / "evidence-packet.json", {
                "schema_version": 1,
                "status": "no_evidence",
                "reason": "No defensible receipt was available.",
                "receipts": [],
            })
            self.assertEqual(invoke(ca.command_validate_evidence, root), 0)
            result = json.loads((root / ".citation-authority" / "evidence-validation.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "no_evidence")

    def test_stale_receipt_fails_closed(self) -> None:
        stale = (datetime.now(timezone.utc) - timedelta(days=91)).replace(microsecond=0).isoformat()
        receipt = {
            "id": "stale",
            "type": "gsc",
            "query": "example query",
            "observed_at": stale,
            "date_range": "2026-01-01/2026-01-31",
            "metrics": {"impressions": 1},
        }
        errors = ca.evidence_receipt_errors(receipt, datetime.now(timezone.utc))
        self.assertTrue(any("older than" in item for item in errors))
        malformed = {
            "type": "gsc",
            "query": "example query",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "date_range": "last 7 days",
            "metrics": {"impressions": "not-a-number"},
        }
        malformed_errors = ca.evidence_receipt_errors(malformed, datetime.now(timezone.utc))
        self.assertTrue(any("id must" in item for item in malformed_errors))
        self.assertTrue(any("metrics must be numeric" in item for item in malformed_errors))

    def test_english_and_spanish_plans_validate(self) -> None:
        for locale in ("en", "es"):
            with self.subTest(locale=locale), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_plan_artifacts(root, locale)
                self.assertEqual(invoke(ca.command_validate_plan, root), 0)
                result = json.loads((root / ".citation-authority" / "plan-validation.json").read_text(encoding="utf-8"))
                self.assertTrue(result["ok"])

    def test_target_query_must_match_its_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_plan_artifacts(root, "en")
            packet_path = root / ".citation-authority" / "evidence-packet.json"
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            packet["receipts"][0]["query"] = "a different measured query"
            dump(packet_path, packet)
            self.assertEqual(invoke(ca.command_validate_plan, root), 1)
            result = json.loads((root / ".citation-authority" / "plan-validation.json").read_text(encoding="utf-8"))
            self.assertTrue(any("exactly match" in item for item in result["errors"]))

    def test_fleet_collision_and_regulated_source_gap_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = write_plan_artifacts(root, "en")
            fleet = root / "fleet.json"
            dump(fleet, {"sites": [{"site_id": "another-site", "query": target["query"]}]})
            config_path = root / ".citation-authority" / "run-config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["fleet_intent_map"] = "fleet.json"
            dump(config_path, config)
            self.assertEqual(invoke(ca.command_validate_plan, root), 1)
            result = json.loads((root / ".citation-authority" / "plan-validation.json").read_text(encoding="utf-8"))
            self.assertTrue(any("collides with fleet owner" in item for item in result["errors"]))

            config["fleet_intent_map"] = None
            dump(config_path, config)
            profile_path = root / ".citation-authority" / "site-profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["regulated"] = {
                "is_regulated": True,
                "claims_policy": "Use only regulator sources.",
                "authoritative_source_domains": ["ca.gov"],
            }
            dump(profile_path, profile)
            target["sources"] = [
                {"url": "https://example.com/a", "use": "commentary"},
                {"url": "https://example.org/b", "use": "commentary"},
            ]
            dump(root / ".citation-authority" / "candidate-plan.json", {"schema_version": 1, "status": "ready", "targets": [target]})
            self.assertEqual(invoke(ca.command_validate_plan, root), 1)
            result = json.loads((root / ".citation-authority" / "plan-validation.json").read_text(encoding="utf-8"))
            self.assertTrue(any("authoritative domain" in item for item in result["errors"]))

    def test_plan_rejects_unsafe_id_wrong_route_and_sink_prefix_trick(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = write_plan_artifacts(root, "en")
            target["id"] = "../../escape"
            target["route"] = f"/wrong/{target['slug']}"
            target["output_path"] = f"content/blog-evil/{target['slug']}.md"
            dump(root / ".citation-authority" / "candidate-plan.json", {
                "schema_version": 1,
                "status": "ready",
                "targets": [target],
            })
            self.assertEqual(invoke(ca.command_validate_plan, root), 1)
            result = json.loads((root / ".citation-authority" / "plan-validation.json").read_text(encoding="utf-8"))
            self.assertTrue(any("path-safe" in item for item in result["errors"]))
            self.assertTrue(any("route pattern" in item for item in result["errors"]))
            self.assertTrue(any("content sink" in item for item in result["errors"]))


class PageGateTests(unittest.TestCase):
    def test_english_and_native_spanish_pages_validate(self) -> None:
        for locale in ("en", "es"):
            with self.subTest(locale=locale), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = write_plan_artifacts(root, locale)
                self.assertEqual(invoke(ca.command_prepare_queue, root), 0)
                markdown = page_markdown(target, locale)
                page_dir = root / ".citation-authority" / "pages"
                page_dir.mkdir(parents=True, exist_ok=True)
                (page_dir / f"{target['id']}.draft.md").write_text(markdown, encoding="utf-8")
                output = root / target["output_path"]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(markdown, encoding="utf-8")
                dump(page_dir / f"{target['id']}.packet.json", {
                    "schema_version": 1,
                    "target_id": target["id"],
                    "slug": target["slug"],
                    "route": target["route"],
                    "locale": locale,
                    "direct_answer_sentences": target["direct_answer_sentences"],
                    "source_urls": [item["url"] for item in target["sources"]],
                    "internal_links": target["internal_links"],
                    "brand_role_passage": target["brand_role_passage"],
                    "numeric_claims": [],
                    "compliance": {
                        "fabrication_scan_passed": True,
                        "regulated_claims_sourced": True,
                        "original_language_copy": True,
                    },
                })
                result = ca.validate_page(root, target["id"])
                self.assertTrue(result["ok"], result["errors"])
                self.assertGreaterEqual(result["word_count"], 80)

    def test_thin_page_and_unapproved_number_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = write_plan_artifacts(root, "en")
            invoke(ca.command_prepare_queue, root)
            profile_path = root / ".citation-authority" / "site-profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["quality"]["min_main_words"] = 300
            dump(profile_path, profile)
            markdown = " ".join(target["direct_answer_sentences"]) + "\n\n" + "\n\n".join(
                f"## {heading}\n\nOnly 3 days." for heading in target["headings"]
            )
            markdown += f"\n\n{target['brand_role_passage']}\n\n"
            markdown += "\n".join(item["url"] for item in target["sources"])
            markdown += "\n" + "\n".join(target["internal_links"])
            page_dir = root / ".citation-authority" / "pages"
            page_dir.mkdir(parents=True, exist_ok=True)
            (page_dir / f"{target['id']}.draft.md").write_text(markdown, encoding="utf-8")
            output = root / target["output_path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(markdown, encoding="utf-8")
            dump(page_dir / f"{target['id']}.packet.json", {
                "target_id": target["id"],
                "slug": target["slug"],
                "route": target["route"],
                "locale": "en",
                "direct_answer_sentences": target["direct_answer_sentences"],
                "source_urls": [item["url"] for item in target["sources"]],
                "internal_links": target["internal_links"],
                "brand_role_passage": target["brand_role_passage"],
                "numeric_claims": [],
                "compliance": {
                    "fabrication_scan_passed": True,
                    "regulated_claims_sourced": True,
                    "original_language_copy": True,
                },
            })
            result = ca.validate_page(root, target["id"])
            self.assertFalse(result["ok"])
            self.assertTrue(any("fewer than" in item for item in result["errors"]))
            self.assertTrue(any("numeric claims" in item for item in result["errors"]))

    def test_queue_requires_validated_plan_and_target_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_plan_artifacts(root, "en")
            plan_path = root / ".citation-authority" / "candidate-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["targets"][0]["differentiation"] = "changed after approval"
            dump(plan_path, plan)
            self.assertEqual(invoke(ca.command_prepare_queue, root), 1)
            dump(root / ".citation-authority" / "approval-record.json", {"schema_version": 1, "approvals": []})
            self.assertEqual(invoke(ca.command_prepare_queue, root), 1)
            (root / ".citation-authority" / "approval-record.json").unlink()
            dump(root / ".citation-authority" / "plan-validation.json", {"ok": False, "status": "blocked"})
            self.assertEqual(invoke(ca.command_prepare_queue, root), 1)


class RenderAndHandoffTests(unittest.TestCase):
    def make_static_canary(self, root: Path, locale: str, include_sitemap: bool = True, include_inbound: bool = True) -> dict:
        profile = base_profile(root, locale)
        target = target_for(locale)
        dist = root / "dist"
        page_dir = dist / target["route"].lstrip("/")
        page_dir.mkdir(parents=True, exist_ok=True)
        words = ("seguro conductores cobertura solicitud persona comparar " if locale == "es" else "coverage drivers application source compare process ") * 30
        canonical = profile["canonical_host"] + target["route"]
        html = f"""<!doctype html><html lang="{locale}"><head>
        <link rel="canonical" href="{canonical}">
        <script type="application/ld+json">{{"@context":"https://schema.org","@graph":[{{"@type":"Article"}},{{"@type":"BreadcrumbList"}}]}}</script>
        </head><body><div hidden>hidden payload words should not count</div><main><h1>{target['title']}</h1><p>{words}</p><a href="/blog">Blog</a><a href="/contact">Contact</a></main></body></html>"""
        (page_dir / "index.html").write_text(html, encoding="utf-8")
        hub_dir = dist / "blog"
        hub_dir.mkdir(parents=True, exist_ok=True)
        link = f'<a href="{target["route"]}">Target</a>' if include_inbound else '<a href="/">Home</a>'
        (hub_dir / "index.html").write_text(f"<html lang=\"{locale}\"><body>{link}</body></html>", encoding="utf-8")
        (dist / "contact").mkdir(parents=True, exist_ok=True)
        (dist / "contact" / "index.html").write_text(f"<html lang=\"{locale}\"><body>Contact</body></html>", encoding="utf-8")
        sitemap_body = f"<urlset><url><loc>{canonical}</loc></url></urlset>" if include_sitemap else "<urlset></urlset>"
        (dist / "sitemap.xml").write_text(sitemap_body, encoding="utf-8")
        dump(root / ".citation-authority" / "site-profile.json", profile)
        dump(root / ".citation-authority" / "candidate-plan.json", {"schema_version": 1, "status": "ready", "targets": [target]})
        return target

    def test_english_and_spanish_render_canaries(self) -> None:
        for locale in ("en", "es"):
            with self.subTest(locale=locale), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_static_canary(root, locale)
                self.assertEqual(invoke(ca.command_validate_rendered, root), 0)
                result = json.loads((root / ".citation-authority" / "render-validation.json").read_text(encoding="utf-8"))
                self.assertTrue(result["ok"], result["errors"])
                self.assertTrue(result["sitemap"]["ok"])
                self.assertGreaterEqual(result["pages"][0]["text_html_ratio"], 0.10)

    def test_missing_sitemap_and_inbound_link_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_static_canary(root, "en", include_sitemap=False, include_inbound=False)
            self.assertEqual(invoke(ca.command_validate_rendered, root), 1)
            result = json.loads((root / ".citation-authority" / "render-validation.json").read_text(encoding="utf-8"))
            self.assertTrue(any("sitemap" in item for item in result["errors"]))
            self.assertTrue(any("service hubs do not link" in item for item in result["errors"]))

    def test_hidden_container_scope_ends_at_its_closing_tag(self) -> None:
        parser = ca.PageHTMLParser()
        parser.feed("<main><div hidden>secret <div>nested close</div>still hidden</div><p>visible words remain</p></main>")
        visible = " ".join(parser.main_parts)
        self.assertNotIn("secret", visible)
        self.assertNotIn("still hidden", visible)
        self.assertIn("visible words remain", parser.main_parts)

    def test_handoff_is_non_deploying_and_queues_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = write_plan_artifacts(root, "en")
            output = root / target["output_path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("validated output", encoding="utf-8")
            dump(root / ".citation-authority" / "validation-report.json", {"ok": True, "status": "passed"})
            self.assertEqual(invoke(ca.command_record_approval, root, stage="deploy_handoff"), 0)
            self.assertEqual(invoke(ca.command_create_handoff, root), 0)
            handoff = json.loads((root / ".citation-authority" / "deploy-handoff.json").read_text(encoding="utf-8"))
            self.assertFalse(handoff["production_deployed"])
            self.assertEqual(handoff["status"], "ready_for_deploy")
            self.assertEqual(invoke(ca.command_create_measurement, root), 0)
            rows = (root / ".citation-authority" / "measurement-queue.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 3)
            parsed = [json.loads(row) for row in rows]
            self.assertEqual([row["due_after_deploy_days"] for row in parsed], [2, 7, 28])
            self.assertTrue(all(row["read_only"] is True for row in parsed))


class PackageTests(unittest.TestCase):
    def test_workflow_and_command_mirrors_are_identical_when_installed(self) -> None:
        repo = Path(__file__).resolve().parents[5]
        skill = repo / ".claude" / "skills" / "ai-citation-authority-wave"
        packaged_workflow = skill / "assets" / "archon" / "ai-citation-authority-wave.yaml"
        packaged_commands = sorted((skill / "assets" / "archon" / "commands").glob("citation-authority-*.md"))
        self.assertTrue(packaged_workflow.is_file())
        self.assertGreater(len(packaged_commands), 0)

        installed_workflow = repo / ".archon" / "workflows" / "ai-citation-authority-wave.yaml"
        if not installed_workflow.exists():
            return

        self.assertEqual(installed_workflow.read_bytes(), packaged_workflow.read_bytes())
        installed_commands = sorted((repo / ".archon" / "commands").glob("citation-authority-*.md"))
        self.assertEqual([path.name for path in installed_commands], [path.name for path in packaged_commands])
        for installed, packaged in zip(installed_commands, packaged_commands, strict=True):
            self.assertEqual(installed.read_bytes(), packaged.read_bytes())

    def test_schemas_are_valid_json_and_workflow_has_safety_boundaries(self) -> None:
        repo = Path(__file__).resolve().parents[5]
        skill = repo / ".claude" / "skills" / "ai-citation-authority-wave"
        for schema in (skill / "references").glob("*.schema.json"):
            json.loads(schema.read_text(encoding="utf-8"))
        workflow = (skill / "assets" / "archon" / "ai-citation-authority-wave.yaml").read_text(encoding="utf-8")
        self.assertIn("max_iterations: 3", workflow)
        self.assertIn("fresh_context: true", workflow)
        self.assertEqual(workflow.count("approval:"), 2)
        self.assertNotIn("git push", workflow)
        self.assertNotIn("vercel deploy", workflow)


if __name__ == "__main__":
    unittest.main()
