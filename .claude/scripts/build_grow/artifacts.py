"""Bounded, offline website drafts and evidence-based artifact checks.

No provider calls, shell execution, network fetches, or deployment live here.
The agent supplies owner-provided business facts; the renderer escapes them.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape

from build_grow.models import Artifact

_STYLES = """@import url('');
:root{--ink:#13221f;--paper:#f7f5ef;--muted:#52645e;--brand:BRAND_COLOR;
--line:#dce1d7;--radius:24px;font-family:Arial,Helvetica,sans-serif;color:var(--ink)}
*{box-sizing:border-box}body{margin:0;background:var(--paper);line-height:1.65}
a{color:inherit}a:focus-visible,button:focus-visible{outline:3px solid var(--brand);
outline-offset:5px}a.skip{position:absolute;top:-100px;background:white;padding:12px}
a.skip:focus{top:0;z-index:10}.wrap{max-width:1180px;margin:auto;padding:0 28px}
header{border-bottom:1px solid var(--line)}header .wrap{display:flex;align-items:center;
justify-content:space-between;gap:24px;min-height:88px}.brand{font-size:21px;
font-weight:800;letter-spacing:-.7px;text-decoration:none}nav{display:flex;gap:25px;
align-items:center}nav a{text-decoration:none;font-size:14px;font-weight:700}
nav a[aria-current=page]{text-decoration:underline;text-underline-offset:7px}
.hero{padding:88px 0 72px}.hero-grid{display:grid;grid-template-columns:1.4fr 1fr;
gap:70px;align-items:center}.eyebrow{font-size:12px;font-weight:800;text-transform:uppercase;
letter-spacing:2px;color:var(--muted);margin:0 0 20px}h1{font-size:clamp(40px,6vw,76px);
line-height:1.03;letter-spacing:-3px;margin:0 0 28px;max-width:820px}h2{font-size:34px;
letter-spacing:-1px;line-height:1.2}h3{font-size:22px;line-height:1.3}.lead{font-size:19px;
max-width:660px;color:var(--muted)}.actions{display:flex;gap:14px;flex-wrap:wrap;margin-top:32px}
.button{display:inline-block;padding:13px 23px;border-radius:99px;text-decoration:none;
font-weight:700;background:var(--ink);color:white;border:1px solid var(--ink)}
.button.secondary{background:transparent;color:var(--ink)}.hero-card{border-radius:32px;
background:var(--ink);color:var(--paper);padding:42px;min-height:350px;position:relative;
overflow:hidden}.hero-card:after{content:'';position:absolute;right:-100px;top:-130px;
width:350px;height:350px;border:65px solid var(--brand);border-radius:50%;opacity:.25}
.hero-card>*{position:relative;z-index:1}.hero-card .eyebrow{color:#d0dad3}
.hero-card h2{margin-top:85px;font-size:36px}.hero-card p{color:#d0dad3}
.section{padding:56px 0 74px;border-top:1px solid var(--line)}.section-heading{
display:flex;justify-content:space-between;gap:28px;align-items:end;margin-bottom:30px}
.section-heading h2{margin:0}.section-heading p{color:var(--muted);max-width:440px}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}
.card{border:1px solid var(--line);border-radius:var(--radius);padding:30px;background:#fff}
.card .number{font-size:12px;font-weight:800;letter-spacing:2px;color:var(--muted)}
.card p{color:var(--muted)}.card a{font-weight:700;text-underline-offset:4px}
.contact-grid{display:grid;grid-template-columns:1.3fr 1fr;gap:32px}
.contact-box{border-radius:var(--radius);background:var(--ink);color:white;padding:38px}
.contact-box a{overflow-wrap:anywhere}.contact-box .eyebrow{color:#d0dad3}
.detail{border-bottom:1px solid #ffffff30;padding:16px 0}.detail:last-child{border:0}
footer{border-top:1px solid var(--line);padding:30px 0;color:var(--muted);font-size:14px}
footer .wrap{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap}
@media(max-width:800px){.hero-grid,.contact-grid{grid-template-columns:1fr;gap:35px}
.hero{padding:54px 0}.cards{grid-template-columns:1fr 1fr}.hero-card{min-height:230px}
.hero-card h2{margin-top:30px}.section-heading{display:block}}
@media(max-width:540px){.wrap{padding:0 20px}header .wrap{flex-direction:column;
align-items:start;justify-content:center;padding-top:20px;padding-bottom:20px;gap:14px}
nav{gap:20px}.cards{grid-template-columns:1fr}h1{letter-spacing:-1.5px}
.section{padding:40px 0}.hero-card{padding:28px}}
""".replace("@import url('');\n", "")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _canonical_base(brief: dict) -> str:
    value = str(brief.get("site_url", "")).rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("site_url must be an HTTPS URL without credentials, query, or fragment")
    return value


def _schema(brief: dict, base: str) -> str:
    schema: dict = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": brief["business_name"],
        "description": brief["description"],
        "email": brief["contact_email"],
        "hasOfferCatalog": {
            "@type": "OfferCatalog",
            "name": "Services",
            "itemListElement": [
                {"@type": "Offer", "itemOffered": {"@type": "Service", **service}}
                for service in brief["services"]
            ],
        },
    }
    if base:
        schema.update({"@id": f"{base}/#business", "url": f"{base}/"})
    if brief.get("location"):
        schema["areaServed"] = brief["location"]
    if brief.get("phone"):
        schema["telephone"] = brief["phone"]
    # JSON in an HTML script raw-text element must not carry a literal closing tag.
    return _json(schema).replace("<", "\\u003c").replace(">", "\\u003e")


def _page(brief: dict, filename: str, title: str, description: str, body: str) -> str:
    base = _canonical_base(brief)
    canonical = ""
    if base:
        route = "" if filename == "index.html" else filename
        canonical = f'<link rel="canonical" href="{_e(base + "/" + route)}">'
    nav = "".join(
        f'<a href="{path}"' + (' aria-current="page"' if path == filename else "") + f">{label}</a>"
        for path, label in (
            ("index.html", "Home"),
            ("services.html", "Services"),
            ("contact.html", "Contact"),
        )
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><meta name="description" content="{_e(description)}">
<meta name="robots" content="noindex,follow">{canonical}
<link rel="stylesheet" href="styles.css">
<script type="application/ld+json">{_schema(brief, base)}</script></head>
<body><a class="skip" href="#main">Skip to content</a>
<header><div class="wrap"><a class="brand" href="index.html">{_e(brief["business_name"])}</a>
<nav aria-label="Main navigation">{nav}</nav></div></header>
<main id="main">{body}</main><footer><div class="wrap">
<span>{_e(brief["business_name"])}</span><span>{_e(brief.get("location", ""))}</span>
</div></footer></body></html>"""


def _website_files(brief: dict) -> dict[str, str]:
    name = _e(brief["business_name"])
    description = _e(brief["description"])
    location = _e(brief.get("location", ""))
    industry = _e(brief.get("industry") or "Our business")
    email = _e(brief["contact_email"])
    email_link = f"mailto:{quote(brief['contact_email'], safe='@.+')}"
    phone = brief.get("phone", "")
    phone_link = "tel:" + quote(re.sub(r"[^+0-9]", "", phone), safe="+")
    color = str(brief.get("brand_color", "#eab308"))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("brand_color must be a six-digit hex color")
    services = "".join(
        f'<article class="card"><span class="number">{i:02d}</span>'
        f"<h3>{_e(service['name'])}</h3><p>{_e(service['description'])}</p>"
        '<a href="contact.html">Discuss this service &rarr;</a></article>'
        for i, service in enumerate(brief["services"], 1)
    )
    area = f"<p>Service area: {location}</p>" if location else ""
    contact_action = (
        f'<a class="button" href="{_e(email_link)}">Get in touch &rarr;</a>'
        '<a class="button secondary" href="services.html">Explore our services</a>'
    )
    home = f"""<section class="hero"><div class="wrap hero-grid"><div>
<p class="eyebrow">{industry}{" &middot; " + location if location else ""}</p>
<h1>{name}</h1><p class="lead">{description}</p>
<div class="actions">{contact_action}</div></div><aside class="hero-card">
<p class="eyebrow">Let's talk about your project</p><h2>Start with a conversation.</h2>
<p>Tell us what you need and ask about the services below.</p>
<a href="contact.html">Contact {name} &rarr;</a></aside></div></section>
<section class="section"><div class="wrap"><div class="section-heading">
<h2>Our services</h2>{area}</div><div class="cards">{services}</div></div></section>"""
    service_body = f"""<section class="hero"><div class="wrap">
<p class="eyebrow">{name}</p><h1>Our services</h1>
<p class="lead">Explore the services offered by {name}.</p>{area}
</div></section><section class="section"><div class="wrap">
<div class="cards">{services}</div></div></section>"""
    phone_detail = (
        f'<div class="detail"><strong>Phone</strong><br><a href="{_e(phone_link)}">'
        f"{_e(phone)}</a></div>"
        if phone
        else ""
    )
    location_detail = (
        f'<div class="detail"><strong>Service area</strong><br>{location}</div>' if location else ""
    )
    contact = f"""<section class="hero"><div class="wrap contact-grid"><div>
<p class="eyebrow">Contact {name}</p><h1>Let's talk.</h1>
<p class="lead">Ask about our services or tell us about your project.</p>
<div class="actions"><a class="button" href="{_e(email_link)}">Email us &rarr;</a></div>
</div><div class="contact-box"><p class="eyebrow">Get in touch</p>
<div class="detail"><strong>Email</strong><br><a href="{_e(email_link)}">{email}</a></div>
{phone_detail}{location_detail}</div></div></section>"""
    files = {
        "index.html": _page(
            brief, "index.html", brief["business_name"], brief["description"], home
        ),
        "services.html": _page(
            brief,
            "services.html",
            f"Services | {brief['business_name']}",
            f"Services offered by {brief['business_name']}.",
            service_body,
        ),
        "contact.html": _page(
            brief,
            "contact.html",
            f"Contact | {brief['business_name']}",
            f"Contact {brief['business_name']} about its services.",
            contact,
        ),
        "styles.css": _STYLES.replace("BRAND_COLOR", color),
        "robots.txt": "User-agent: *\nDisallow: /\n",
    }
    base = _canonical_base(brief)
    if base:
        urls = [base + "/", base + "/services.html", base + "/contact.html"]
        files["sitemap.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{xml_escape(url)}</loc></url>" for url in urls)
            + "</urlset>"
        )
    files["review.txt"] = (
        "TASKCHAD WEBSITE DRAFT\n\n"
        "This site uses the business owner's supplied facts. They have not been "
        "independently verified. No testimonials, rankings, availability promises, "
        "certifications or customer results were invented.\n\n"
        "Open index.html locally to review. Email and phone links use the supplied "
        "contact details; there is no lead database or form-submission provider.\n\n"
        "The draft is intentionally noindex and robots-blocked. Before publication, "
        "confirm the domain and business facts, review all pages, configure hosting "
        "and contacts, remove preview indexing restrictions, and verify the exact "
        "live release. Approval inside Build + Grow never publishes this archive.\n"
    )
    files["manifest.json"] = _json(
        {
            "schema_version": 1,
            "facts_source": "owner_provided",
            "preview_only": True,
            "brief_sha256": hashlib.sha256(_json(brief).encode()).hexdigest(),
            "files": {
                key: hashlib.sha256(value.encode()).hexdigest()
                for key, value in sorted(files.items())
            },
        }
    )
    return files


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.language = ""
        self.h1_count = 0
        self.has_main = False
        self.meta: dict[str, str] = {}
        self.canonicals: list[str] = []
        self.links: list[str] = []
        self.structured_data: list[str] = []
        self.text: list[str] = []
        self._in_title = False
        self._hidden_depth = 0
        self._in_schema = False
        self._schema_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            self.language = values.get("lang") or ""
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self.h1_count += 1
        if tag == "main":
            self.has_main = True
        if tag == "meta" and values.get("name"):
            self.meta[str(values["name"]).lower()] = values.get("content") or ""
        if tag == "link" and "canonical" in str(values.get("rel", "")).split():
            self.canonicals.append(values.get("href") or "")
        if tag == "a" and values.get("href"):
            self.links.append(str(values["href"]))
        if tag in {"script", "style"}:
            self._hidden_depth += 1
        if tag == "script" and values.get("type") == "application/ld+json":
            self._in_schema = True
            self._schema_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_schema:
            self.structured_data.append("".join(self._schema_buffer))
            self._in_schema = False
        if tag in {"script", "style"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._in_schema:
            self._schema_buffer.append(data)
        if not self._hidden_depth:
            self.text.append(data)


def _local_target(filename: str, href: str) -> str | None:
    try:
        parsed = urlsplit(href)
    except ValueError:
        return "__invalid_link__"
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path = unquote(parsed.path)
    if path.startswith("/"):
        target = posixpath.normpath(path.lstrip("/"))
    else:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(filename), path))
    if target in {"", "."}:
        target = "index.html"
    elif target.endswith("/"):
        target += "index.html"
    return target


def _valid_canonical(values: list[str]) -> bool:
    if len(values) != 1:
        return False
    try:
        parsed = urlsplit(values[0])
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def audit_files(source_files: dict[str, str]) -> dict:
    """Inspect supplied HTML artifacts. Missing external evidence stays unknown."""
    pages = {
        path: value
        for path, value in sorted(source_files.items())
        if path.lower().endswith(".html")
    }
    if not pages:
        raise ValueError("GEO artifact audit requires at least one HTML file")
    findings: list[dict] = []
    titles: dict[str, str] = {}

    def record(page: str, check: str, status: str, evidence: str, action: str = "") -> None:
        findings.append(
            {"page": page, "check": check, "status": status, "evidence": evidence, "action": action}
        )

    for path, content in pages.items():
        doc = _Document()
        doc.feed(content)
        doc.close()
        record(
            path,
            "document_language",
            "pass" if doc.language.strip() else "fail",
            doc.language or "No html lang attribute",
            "Declare the page's actual language.",
        )
        record(
            path,
            "main_landmark",
            "pass" if doc.has_main else "warning",
            "main element present" if doc.has_main else "No main element",
            "Identify the primary content with a main element.",
        )
        record(
            path,
            "primary_heading",
            "pass" if doc.h1_count == 1 else "fail",
            f"{doc.h1_count} h1 elements",
            "Use one descriptive primary heading.",
        )
        title = doc.title.strip()
        status = "pass" if title else "fail"
        evidence = title or "No document title"
        if title and title.casefold() in titles:
            status, evidence = "warning", f"Title duplicates {titles[title.casefold()]}"
        titles[title.casefold()] = path
        record(path, "document_title", status, evidence, "Give each page a useful distinct title.")
        description = doc.meta.get("description", "").strip()
        record(
            path,
            "meta_description",
            "pass" if description else "warning",
            description or "No meta description",
            "Summarize this page's actual content.",
        )
        canonical_ok = _valid_canonical(doc.canonicals)
        record(
            path,
            "canonical",
            "pass" if canonical_ok else "warning",
            ", ".join(doc.canonicals) or "No canonical URL",
            "Confirm the final domain and one absolute HTTPS canonical for this page.",
        )
        robots = doc.meta.get("robots", "")
        record(
            path,
            "indexing_directive",
            "warning" if "noindex" in robots.lower() else "pass",
            robots or "No page-level noindex directive observed",
            "Keep drafts noindex; review indexing policy before the separate live release.",
        )
        parsed_schema = []
        schema_error = False
        for raw in doc.structured_data:
            try:
                parsed_schema.append(json.loads(raw))
            except (json.JSONDecodeError, ValueError):
                schema_error = True
        schema_status = "fail" if schema_error else "pass" if parsed_schema else "warning"
        record(
            path,
            "structured_data_json",
            schema_status,
            "Invalid JSON-LD" if schema_error else f"{len(parsed_schema)} parsed JSON-LD blocks",
            "Use valid JSON-LD with types and claims supported by the business facts.",
        )
        broken = []
        for href in doc.links:
            target = _local_target(path, href)
            if target and target not in source_files:
                # Static exports can use clean paths while keeping file extensions on disk.
                alternatives = {target + ".html", target.rstrip("/") + "/index.html"}
                if not alternatives.intersection(source_files):
                    broken.append(href)
        record(
            path,
            "internal_links",
            "fail" if broken else "pass",
            ", ".join(broken[:8]) if broken else "Local navigation targets exist in this packet",
            "Repair the missing target or include its rendered page in the evidence packet.",
        )
        words = len(" ".join(doc.text).split())
        record(
            path,
            "readable_content",
            "pass" if words >= 30 else "warning",
            f"{words} words in supplied HTML (navigation included)",
            "Include a clear explanation of this page's purpose. Word count is not a ranking goal.",
        )
    for check, detail in (
        ("live_crawl_and_indexing", "No authenticated crawl/index coverage was supplied."),
        ("search_demand", "No dated query-demand evidence was supplied."),
        ("ai_citations", "No versioned buyer-query answer panel was collected."),
        ("qualified_leads", "No analytics or lead outcome observations were supplied."),
    ):
        record(
            "site", check, "unknown", detail, "Measure this separately after a verified release."
        )
    return {
        "schema_version": 1,
        "scope": "supplied_static_artifacts",
        "pages_checked": list(pages),
        "source_sha256": {
            path: hashlib.sha256(value.encode()).hexdigest()
            for path, value in sorted(source_files.items())
        },
        "summary": {
            status: sum(row["status"] == status for row in findings)
            for status in ("pass", "warning", "fail", "unknown")
        },
        "findings": findings,
        "limits": [
            "Offline artifact checks do not establish production behavior.",
            "No search ranking, citation lift, demand, or revenue score is inferred.",
            "Business claims remain owner-provided and require review.",
        ],
    }


def _report_text(report: dict) -> str:
    lines = [
        "TASKCHAD SEO / GEO ARTIFACT CHECKS",
        "",
        f"Scope: {report['scope']}",
        f"Pages checked: {len(report['pages_checked'])}",
        "",
        "Observed findings:",
    ]
    for row in report["findings"]:
        lines.append(f"[{row['status'].upper()}] {row['page']} / {row['check']}: {row['evidence']}")
        if row["action"] and row["status"] != "pass":
            lines.append(f"  Next: {row['action']}")
    lines.extend(["", "Limits:", *report["limits"], ""])
    return "\n".join(lines)


def _archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in sorted(files.items()):
            entry = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, content.encode("utf-8"))
    return buffer.getvalue()


def build_artifacts(kind: str, brief: dict, source_files: dict[str, str]) -> list[Artifact]:
    """Produce immutable, bounded deliverables from the job's frozen input."""
    if kind == "website":
        files = _website_files(brief)
        report = audit_files(files)
        if report["summary"]["fail"]:
            raise ValueError("Generated website failed its local artifact checks")
        return [
            Artifact("website-draft.zip", "application/zip", _archive(files)),
            Artifact("website-checks.json", "application/json", _json(report).encode()),
        ]
    if kind == "geo":
        report = audit_files(source_files)
        return [
            Artifact("geo-report.json", "application/json", _json(report).encode()),
            Artifact("geo-report.txt", "text/plain", _report_text(report).encode()),
        ]
    raise ValueError("Unsupported Build + Grow job kind")
