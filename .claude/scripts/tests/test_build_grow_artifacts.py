"""Exercise real site artifacts and distinguish observations from search outcomes."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

import pytest
from build_grow.artifacts import audit_files, build_artifacts
from build_grow.models import normalize_brief
from build_grow.service import BuildGrowService, read_site_archive
from build_grow.store import BuildGrowStore


@pytest.fixture
def brief():
    return normalize_brief(
        {
            "business_name": "Oak & River Plumbing",
            "description": "Oak & River Plumbing provides fixture installation and drain care "
            "for homes in its stated service area. Contact the business to discuss "
            "your project and ask about scheduling.",
            "industry": "Plumbing",
            "location": "Sacramento",
            "services": [
                {
                    "name": "Fixture installation",
                    "description": "Installation of owner-selected household plumbing fixtures.",
                },
                {"name": "Drain care", "description": "Drain care for residential plumbing."},
            ],
            "contact_email": "hello@example.com",
            "phone": "+1 (555) 123-4567",
            "site_url": "https://example.com",
            "brand_color": "#eab308",
        }
    )


def _files(artifacts):
    archive = next(artifact for artifact in artifacts if artifact.filename.endswith(".zip"))
    return read_site_archive(archive.content)


def test_website_bundle_has_real_connected_pages_and_owner_facts(brief):
    artifacts = build_artifacts("website", brief, {})
    files = _files(artifacts)
    assert {
        "index.html",
        "services.html",
        "contact.html",
        "styles.css",
        "robots.txt",
        "sitemap.xml",
        "manifest.json",
        "review.txt",
    } == set(files)
    assert "Oak &amp; River Plumbing" in files["index.html"]
    assert "Fixture installation" in files["services.html"]
    assert 'href="mailto:hello@example.com"' in files["contact.html"]
    assert 'href="tel:+15551234567"' in files["contact.html"]
    for page in ("index.html", "services.html", "contact.html"):
        assert all(
            f'href="{target}"' in files[page]
            for target in ("index.html", "services.html", "contact.html", "styles.css")
        )
        assert '<main id="main">' in files[page]
        assert len(re.findall(r"<h1(?:\s[^>]*)?>", files[page])) == 1
    assert "trusted by" not in files["index.html"].lower()
    assert "five-star" not in files["index.html"].lower()
    assert "guarantee" not in files["index.html"].lower()


def test_website_is_a_review_draft_and_does_not_claim_publication(brief):
    files = _files(build_artifacts("website", brief, {}))
    assert files["robots.txt"] == "User-agent: *\nDisallow: /\n"
    assert all(
        'content="noindex,follow"' in files[page]
        for page in ("index.html", "services.html", "contact.html")
    )
    manifest = json.loads(files["manifest.json"])
    assert manifest["facts_source"] == "owner_provided"
    assert manifest["preview_only"] is True
    for filename, digest in manifest["files"].items():
        assert hashlib.sha256(files[filename].encode()).hexdigest() == digest
    assert "never publishes" in files["review.txt"]


def test_untrusted_business_copy_cannot_close_schema_or_inject_markup(brief):
    payload = '</script><img src=x onerror="alert(1)">'
    brief["business_name"] = payload
    brief["description"] = payload
    files = _files(build_artifacts("website", brief, {}))
    for name in ("index.html", "services.html", "contact.html"):
        assert "<img" not in files[name]
        assert "&lt;/script&gt;" in files[name]
        raw = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', files[name], re.DOTALL
        ).group(1)
        schema = json.loads(raw)
        assert schema["name"] == payload
        assert schema["description"] == payload
        assert raw.count("</script>") == 0


def test_missing_domain_is_preserved_as_unknown_publication_configuration(brief):
    brief["site_url"] = ""
    files = _files(build_artifacts("website", brief, {}))
    assert "sitemap.xml" not in files
    assert 'rel="canonical"' not in files["index.html"]
    assert "example.invalid" not in files["index.html"]
    report = audit_files(files)
    assert any(
        row["check"] == "canonical" and row["status"] == "warning" for row in report["findings"]
    )


def test_site_zip_is_reproducible_for_same_frozen_brief(brief):
    first = build_artifacts("website", brief, {})
    second = build_artifacts("website", brief, {})
    assert [(a.filename, a.content) for a in first] == [(a.filename, a.content) for a in second]
    with zipfile.ZipFile(io.BytesIO(first[0].content)) as archive:
        assert all(not entry.is_dir() for entry in archive.infolist())


def test_audit_finds_real_defects_in_existing_html_without_running_it():
    files = {
        "index.html": "<html><head><title>Broken site</title>"
        '<script type="application/ld+json">{broken}</script></head>'
        '<body><h1>One</h1><h1>Two</h1><a href="missing.html">Missing</a>'
        '<script>fetch("https://example.com/steal")</script></body></html>'
    }
    report = audit_files(files)
    failed = {row["check"] for row in report["findings"] if row["status"] == "fail"}
    assert {
        "document_language",
        "primary_heading",
        "structured_data_json",
        "internal_links",
    } <= failed
    assert report["scope"] == "supplied_static_artifacts"
    assert (
        report["source_sha256"]["index.html"]
        == hashlib.sha256(files["index.html"].encode()).hexdigest()
    )


def test_audit_reports_search_and_revenue_evidence_as_unknown_even_for_valid_site(brief):
    report = audit_files(_files(build_artifacts("website", brief, {})))
    unknown = {row["check"] for row in report["findings"] if row["status"] == "unknown"}
    assert unknown == {
        "live_crawl_and_indexing",
        "search_demand",
        "ai_citations",
        "qualified_leads",
    }
    assert report["summary"]["fail"] == 0
    assert "score" not in report
    assert "rank" not in report


def test_malformed_urls_become_findings_instead_of_crashing_audit():
    report = audit_files(
        {
            "index.html": '<html lang="en"><head><title>Test</title>'
            '<link rel="canonical" href="http://[malformed"></head><body>'
            '<h1>Test</h1><a href="http://[malformed">Bad link</a></body></html>'
        }
    )
    assert any(
        row["check"] == "canonical" and row["status"] == "warning" for row in report["findings"]
    )
    assert any(
        row["check"] == "internal_links" and row["status"] == "fail" for row in report["findings"]
    )


def test_geo_source_packet_requires_rendered_html():
    with pytest.raises(ValueError, match="HTML"):
        build_artifacts("geo", {}, {"styles.css": "body{color:red}"})


def test_real_saved_business_to_website_to_geo_job_and_bound_review(tmp_path, brief):
    store = BuildGrowStore(tmp_path / "build-grow.db")
    service = BuildGrowService(store)
    project = service.create_project("Oak & River", brief, workspace_id=2)
    website = service.create_job(project["id"], "website", workspace_id=2)
    website = service.run_job(website["id"], workspace_id=2)
    assert website["status"] == "ready_for_review", website
    assert len(website["deliverables"]) == 2
    bundle = next(
        item for item in website["deliverables"] if item["media_type"] == "application/zip"
    )
    approved = service.review_deliverable(
        bundle["id"], "approved", bundle["sha256"], workspace_id=2
    )
    assert approved["review_status"] == "approved"
    geo = service.create_job(project["id"], "geo", workspace_id=2)
    assert geo["source_job_id"] == website["id"]
    geo = service.run_job(geo["id"], workspace_id=2)
    assert geo["status"] == "ready_for_review", geo
    assert {item["filename"] for item in geo["deliverables"]} == {
        "geo-report.json",
        "geo-report.txt",
    }
    item = next(item for item in geo["deliverables"] if item["filename"] == "geo-report.json")
    _, content = store.download_deliverable(item["id"], workspace_id=2)
    assert json.loads(content)["pages_checked"] == ["contact.html", "index.html", "services.html"]
    reopened = BuildGrowStore(tmp_path / "build-grow.db")
    assert reopened.get_job(geo["id"], workspace_id=2)["status"] == "ready_for_review"
