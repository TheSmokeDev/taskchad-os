#!/usr/bin/env python3
"""Read-only scanner that emits a TokenMax site profile for a website repo."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".archon",
    ".claude",
    ".agents",
    ".worktrees",
    "node_modules",
    ".next",
    ".nuxt",
    ".vercel",
    ".turbo",
    ".tmp",
    "archive",
    "archives",
    "artifacts",
    "tmp",
    "temp",
    "dist",
    "build",
    "coverage",
    ".cache",
    "__pycache__",
}

CODE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".astro", ".vue", ".svelte"}
MARKDOWN_EXTS = {".md", ".mdx"}


@dataclass
class Evidence:
    kind: str
    path: str
    detail: str
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "detail": self.detail,
            "confidence": round(self.confidence, 3),
        }


def rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def iter_files(root: Path, exts: set[str] | None = None, max_files: int = 25000):
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_DIRS
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if exts is not None and path.suffix.lower() not in exts:
                continue
            seen += 1
            if seen > max_files:
                return
            yield path


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_text(path: Path, limit: int = 200000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    return text[:limit]


def detect_package_manager(root: Path) -> str:
    lock_order = [
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("package-lock.json", "npm"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
    ]
    for filename, manager in lock_order:
        if (root / filename).exists():
            return manager
    return "npm" if (root / "package.json").exists() else "unknown"


def find_package_roots(root: Path) -> list[Path]:
    roots: list[Path] = []
    for package_json in iter_files(root, {".json"}, max_files=4000):
        if package_json.name != "package.json":
            continue
        parent = package_json.parent
        if any(part in IGNORED_DIRS for part in parent.parts):
            continue
        roots.append(parent)
    roots.sort(key=lambda path: (0 if path == root else 1, len(path.parts), path.as_posix()))
    return roots


def detect_framework_at(path: Path) -> tuple[str, float]:
    has_next_config = any((path / name).exists() for name in ("next.config.js", "next.config.mjs", "next.config.ts"))
    has_app = (path / "app").exists() or (path / "src" / "app").exists()
    has_pages = (path / "pages").exists() or (path / "src" / "pages").exists()
    if has_next_config or has_app or has_pages:
        if has_app:
            return "next-app-router", 0.95
        if has_pages:
            return "next-pages-router", 0.86
        return "next", 0.72
    if any((path / name).exists() for name in ("astro.config.mjs", "astro.config.ts", "astro.config.js")):
        return "astro", 0.9
    if any((path / name).exists() for name in ("vite.config.ts", "vite.config.js", "vite.config.mjs")):
        return "vite", 0.65
    if (path / "src").exists() and (path / "index.html").exists():
        return "static-vite-like", 0.5
    if (path / "index.html").exists():
        return "static-html", 0.45
    return "unknown", 0.0


def discover_app_roots(root: Path, evidence: list[Evidence]) -> list[dict[str, Any]]:
    candidates = set(find_package_roots(root))
    for name in ("site", "web", "app", "frontend"):
        if (root / name).exists():
            candidates.add(root / name)
    if (root / "apps").exists():
        for child in (root / "apps").iterdir():
            if child.is_dir():
                candidates.add(child)

    app_roots: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda path: (len(path.parts), path.as_posix())):
        framework, score = detect_framework_at(candidate)
        package = load_json(candidate / "package.json") if (candidate / "package.json").exists() else {}
        has_app = (candidate / "app").exists() or (candidate / "src" / "app").exists()
        has_pages = (candidate / "pages").exists() or (candidate / "src" / "pages").exists()
        if framework == "unknown" and not package:
            continue
        if framework != "unknown" or package.get("scripts", {}).get("build"):
            app_roots.append(
                {
                    "path": rel(root, candidate),
                    "packageName": package.get("name"),
                    "framework": framework,
                    "hasAppRouter": bool(has_app),
                    "hasPagesRouter": bool(has_pages),
                    "confidence": round(max(score, 0.35 if package else 0.0), 3),
                }
            )
            if framework != "unknown":
                evidence.append(Evidence("app-root", rel(root, candidate), f"Detected {framework}", score))
    return app_roots


def route_path_from_page(root: Path, app_root: Path, page: Path) -> str | None:
    app_dirs = [app_root / "app", app_root / "src" / "app", app_root / "pages", app_root / "src" / "pages"]
    base = None
    for app_dir in app_dirs:
        try:
            page.relative_to(app_dir)
            base = app_dir
            break
        except ValueError:
            continue
    if base is None:
        return None

    route_dir = page.parent
    parts = list(route_dir.relative_to(base).parts)
    clean: list[str] = []
    for part in parts:
        if part.startswith("(") and part.endswith(")"):
            continue
        if part in ("index", "page"):
            continue
        clean.append(part)
    if not clean:
        return "/"
    return "/" + "/".join(clean)


def discover_route_files(root: Path, app_roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for app in app_roots:
        app_root = root / app["path"]
        for page in iter_files(app_root, CODE_EXTS, max_files=12000):
            if page.name not in {"page.tsx", "page.ts", "page.jsx", "page.js", "index.tsx", "index.ts", "index.jsx", "index.js"}:
                continue
            route = route_path_from_page(root, app_root, page)
            if route is None:
                continue
            routes.append({"route": route, "file": rel(root, page), "appRoot": app["path"]})
    return routes


def infer_base_path_from_content(relative_dir: str) -> str | None:
    parts = Path(relative_dir).parts
    try:
        content_idx = parts.index("content")
    except ValueError:
        return None
    tail = list(parts[content_idx + 1 :])
    if not tail:
        return None
    if tail[0] == "services" and len(tail) >= 2:
        return f"/{tail[1]}/[slug]"
    if tail[0] == "fleet-pages":
        return "/[locale]/[slug]" if len(tail) >= 2 else "/[slug]"
    if len(tail) >= 2:
        return "/" + "/".join(tail[:2]) + "/[slug]"
    return "/" + tail[0] + "/[slug]"


def scoped_routes_for_content(content_dir: str, routes: list[dict[str, Any]], app_roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content = content_dir.replace("\\", "/")
    candidates = [
        app["path"]
        for app in app_roots
        if app["path"] != "." and (content == app["path"] or content.startswith(app["path"].rstrip("/") + "/"))
    ]
    if not candidates:
        return routes
    app_root = sorted(candidates, key=len, reverse=True)[0]
    scoped = [route for route in routes if route.get("appRoot") == app_root]
    return scoped or routes


def find_matching_renderer(
    root: Path,
    content_dir: str,
    inferred_route: str | None,
    routes: list[dict[str, Any]],
    app_roots: list[dict[str, Any]],
) -> str | None:
    routes = scoped_routes_for_content(content_dir, routes, app_roots)
    content_bits = [bit for bit in Path(content_dir).parts if bit not in {"content", "services"}]
    route_hint = (inferred_route or "").replace("[slug]", "").strip("/")
    if inferred_route:
        for route in routes:
            if route["route"] == inferred_route:
                return route["file"]
    prefers_dynamic = bool(inferred_route and "[" in inferred_route)
    for route in routes:
        route_path = route["route"].replace("[slug]", "").strip("/")
        if route_hint and route_hint == route_path and (not prefers_dynamic or "[" in route["route"]):
            return route["file"]
    for route in routes:
        route_path = route["route"].replace("[slug]", "").strip("/")
        if route_hint and route_hint == route_path:
            return route["file"]
        if content_bits and all(bit in route["file"] for bit in content_bits[:2]):
            return route["file"]
    for route in routes:
        text = read_text(root / route["file"], limit=80000)
        if content_dir.replace("/", os.sep) in text or content_dir in text:
            return route["file"]
    return None


def discover_content_sinks(
    root: Path,
    routes: list[dict[str, Any]],
    app_roots: list[dict[str, Any]],
    evidence: list[Evidence],
) -> list[dict[str, Any]]:
    grouped: dict[Path, list[Path]] = defaultdict(list)
    for path in iter_files(root, MARKDOWN_EXTS, max_files=25000):
        parts = path.parts
        if "content" not in parts and "contents" not in parts:
            continue
        grouped[path.parent].append(path)

    sinks: list[dict[str, Any]] = []
    for directory, files in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0].as_posix())):
        if not files:
            continue
        relative_dir = rel(root, directory)
        inferred = infer_base_path_from_content(relative_dir)
        renderer = find_matching_renderer(root, relative_dir, inferred, routes, app_roots)
        confidence = 0.45
        if len(files) >= 5:
            confidence += 0.15
        if inferred:
            confidence += 0.15
        if renderer:
            confidence += 0.2
        sink = {
            "type": "markdown" if all(file.suffix.lower() == ".md" for file in files) else "markdown-mdx",
            "pathPattern": f"{relative_dir}/{{slug}}{files[0].suffix.lower()}",
            "sampleFiles": [rel(root, file) for file in sorted(files)[:5]],
            "count": len(files),
            "inferredBasePath": inferred,
            "matchedRenderer": renderer,
            "confidence": round(min(confidence, 0.97), 3),
        }
        sinks.append(sink)
        evidence.append(
            Evidence(
                "content-sink",
                relative_dir,
                f"{len(files)} markdown files; inferred route {inferred or 'unknown'}",
                sink["confidence"],
            )
        )

    discovered_dirs = {sink["pathPattern"].split("/{slug}", 1)[0] for sink in sinks}
    for manifest_path in iter_files(root, {".json"}, max_files=25000):
        if manifest_path.name != "manifest.json" or "content" not in manifest_path.parts:
            continue
        relative_dir = rel(root, manifest_path.parent)
        if relative_dir in discovered_dirs:
            continue
        manifest = load_json(manifest_path)
        pages = manifest.get("pages")
        if not isinstance(pages, list):
            continue
        inferred = manifest.get("route_template")
        if not isinstance(inferred, str) or not inferred.startswith("/"):
            inferred = infer_base_path_from_content(relative_dir)
        renderer = find_matching_renderer(root, relative_dir, inferred, routes, app_roots)
        confidence = 0.5
        if inferred:
            confidence += 0.15
        if renderer:
            confidence += 0.2
        sink = {
            "type": "markdown-manifest",
            "pathPattern": f"{relative_dir}/{{slug}}.md",
            "sampleFiles": [rel(root, manifest_path)],
            "count": len(pages),
            "inferredBasePath": inferred,
            "matchedRenderer": renderer,
            "confidence": round(min(confidence, 0.97), 3),
        }
        sinks.append(sink)
        evidence.append(
            Evidence(
                "content-sink",
                relative_dir,
                f"manifest-backed markdown sink with {len(pages)} pages; inferred route {inferred or 'unknown'}",
                sink["confidence"],
            )
        )
    return sinks


def discover_seo_surfaces(root: Path, evidence: list[Evidence]) -> dict[str, Any]:
    surfaces: dict[str, Any] = {
        "sitemapFiles": [],
        "robotsFiles": [],
        "llmsFiles": [],
        "aiAgentsFiles": [],
        "metadataFiles": [],
        "canonicalMentions": [],
        "jsonLdMentions": [],
        "schemaMentions": [],
    }
    for path in iter_files(root, CODE_EXTS | {".xml", ".txt"}, max_files=25000):
        name = path.name.lower()
        relative = rel(root, path)
        if name.startswith("sitemap") or "sitemap.xml" in relative:
            surfaces["sitemapFiles"].append(relative)
            evidence.append(Evidence("seo-surface", relative, "Sitemap surface", 0.9))
        if name.startswith("robots"):
            surfaces["robotsFiles"].append(relative)
            evidence.append(Evidence("seo-surface", relative, "Robots surface", 0.85))
        if "llms.txt" in relative.lower() or name == "llms.txt":
            surfaces["llmsFiles"].append(relative)
            evidence.append(Evidence("geo-surface", relative, "LLMs text surface", 0.85))
        if "ai-agents" in relative.lower():
            surfaces["aiAgentsFiles"].append(relative)
            evidence.append(Evidence("geo-surface", relative, "AI agents surface", 0.75))

        if path.suffix.lower() in CODE_EXTS:
            text = read_text(path, limit=120000)
            if "metadata" in text or "generateMetadata" in text:
                surfaces["metadataFiles"].append(relative)
            if "canonical" in text:
                surfaces["canonicalMentions"].append(relative)
            if "application/ld+json" in text or "json-ld" in text.lower():
                surfaces["jsonLdMentions"].append(relative)
            if "schema" in text.lower() or "@context" in text:
                surfaces["schemaMentions"].append(relative)

    for key in surfaces:
        if isinstance(surfaces[key], list):
            surfaces[key] = sorted(set(surfaces[key]))[:50]
    return surfaces


def discover_internal_links(root: Path, evidence: list[Evidence]) -> list[dict[str, Any]]:
    surfaces: list[dict[str, Any]] = []
    for path in iter_files(root, CODE_EXTS, max_files=25000):
        relative = rel(root, path)
        lower = relative.lower()
        if not any(token in lower for token in ("nav", "footer", "hub", "link", "sitemap", "page.tsx", "layout.tsx")):
            continue
        text = read_text(path, limit=120000)
        link_count = len(re.findall(r"<Link\b|href=[\"']/", text))
        if link_count:
            item = {"path": relative, "linkCount": link_count}
            surfaces.append(item)
            if len(surfaces) <= 20:
                evidence.append(Evidence("internal-links", relative, f"{link_count} internal link markers", 0.7))
    surfaces.sort(key=lambda item: (-item["linkCount"], item["path"]))
    return surfaces[:50]


def effective_package_manager(root: Path, app_roots: list[dict[str, Any]], fallback: str = "unknown") -> str:
    root_manager = detect_package_manager(root)
    if root_manager != "unknown":
        return root_manager
    for app in app_roots:
        app_manager = detect_package_manager(root / app["path"])
        if app_manager != "unknown":
            return app_manager
    return fallback


def build_command(root: Path, package_manager: str, app_roots: list[dict[str, Any]]) -> dict[str, Any]:
    root_pkg = load_json(root / "package.json")
    root_scripts = root_pkg.get("scripts", {}) if isinstance(root_pkg.get("scripts"), dict) else {}
    if root_scripts.get("build"):
        return {"cwd": ".", "command": f"{package_manager} build", "confidence": 0.9}

    for app in app_roots:
        app_path = root / app["path"]
        pkg = load_json(app_path / "package.json")
        scripts = pkg.get("scripts", {}) if isinstance(pkg.get("scripts"), dict) else {}
        if not scripts.get("build"):
            continue
        app_manager = package_manager if package_manager != "unknown" else detect_package_manager(app_path)
        if app_manager == "unknown":
            app_manager = "npm"
        package_name = pkg.get("name")
        if app_manager == "pnpm" and package_name and root_pkg.get("workspaces"):
            return {"cwd": ".", "command": f"pnpm --filter {package_name} build", "confidence": 0.82}
        return {"cwd": app["path"], "command": f"{app_manager} build", "confidence": 0.8}
    return {"cwd": None, "command": None, "confidence": 0.0}


def deploy_hints(root: Path) -> dict[str, Any]:
    hints: dict[str, Any] = {"provider": "unknown", "files": [], "confidence": 0.0}
    files = [
        "vercel.json",
        "netlify.toml",
        "wrangler.toml",
        ".github/workflows",
        "firebase.json",
    ]
    for name in files:
        path = root / name
        if path.exists():
            hints["files"].append(rel(root, path))
    if (root / "vercel.json").exists() or any(".vercel" in part for part in os.listdir(root) if isinstance(part, str)):
        hints["provider"] = "vercel"
        hints["confidence"] = 0.75
    elif (root / "netlify.toml").exists():
        hints["provider"] = "netlify"
        hints["confidence"] = 0.75
    elif (root / "wrangler.toml").exists():
        hints["provider"] = "cloudflare"
        hints["confidence"] = 0.7
    return hints


def make_route_families(content_sinks: list[dict[str, Any]], routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families: dict[str, dict[str, Any]] = {}
    for sink in content_sinks:
        base = sink.get("inferredBasePath")
        if not base:
            continue
        families[base] = {
            "basePath": base,
            "source": "content-sink",
            "renderer": sink.get("matchedRenderer"),
            "contentSink": sink.get("pathPattern"),
            "confidence": round(min(0.95, sink.get("confidence", 0.0)), 3),
        }
    for route in routes:
        route_path = route["route"]
        if "[" not in route_path:
            continue
        families.setdefault(
            route_path,
            {
                "basePath": route_path,
                "source": "route-file",
                "renderer": route["file"],
                "contentSink": None,
                "confidence": 0.65,
            },
        )
    return sorted(families.values(), key=lambda item: item["basePath"])


def choose_run_mode(confidence: float, framework: str, content_sinks: list[dict[str, Any]], route_families: list[dict[str, Any]]) -> str:
    strong_sink = any(sink.get("confidence", 0) >= 0.7 and sink.get("matchedRenderer") for sink in content_sinks)
    if confidence >= 0.75 and strong_sink and route_families:
        return "augment-existing"
    if framework in {"next-app-router", "next-pages-router", "astro", "monorepo-next-app-router", "monorepo-mixed"} and confidence >= 0.45:
        return "install-renderer"
    if framework != "unknown":
        return "homepage-geo"
    return "external-url-only"


def confidence_score(
    framework: str,
    app_roots: list[dict[str, Any]],
    content_sinks: list[dict[str, Any]],
    route_families: list[dict[str, Any]],
    seo_surfaces: dict[str, Any],
    internal_links: list[dict[str, Any]],
    build: dict[str, Any],
) -> float:
    score = 0.0
    if framework != "unknown":
        score += 0.2
    if app_roots:
        score += min(max(app.get("confidence", 0.0) for app in app_roots), 1.0) * 0.15
    if content_sinks:
        score += min(max(sink.get("confidence", 0.0) for sink in content_sinks), 1.0) * 0.2
    if route_families:
        score += min(max(route.get("confidence", 0.0) for route in route_families), 1.0) * 0.15
    if seo_surfaces.get("sitemapFiles"):
        score += 0.08
    if seo_surfaces.get("robotsFiles"):
        score += 0.04
    if seo_surfaces.get("jsonLdMentions") or seo_surfaces.get("schemaMentions"):
        score += 0.05
    if seo_surfaces.get("canonicalMentions"):
        score += 0.03
    if internal_links:
        score += 0.05
    if build.get("command"):
        score += build.get("confidence", 0.0) * 0.1
    return round(min(score, 0.99), 3)


def build_questions(profile: dict[str, Any]) -> list[str]:
    questions: list[str] = []
    viable_apps = [app for app in profile.get("appRoots", []) if app.get("framework") != "unknown"]
    if len(viable_apps) > 1:
        questions.append("Which app root should this TokenMax run target?")
    if profile["framework"] == "unknown":
        questions.append("Which framework or static site generator owns the public website?")
    if not profile["contentSinks"]:
        questions.append("Where should generated SEO/GEO page content be written?")
    if not profile["routeFamilies"]:
        questions.append("Which route pattern should expose generated pages?")
    if not profile["seoSurfaces"].get("sitemapFiles"):
        questions.append("Which file or service owns sitemap publication?")
    if not profile["build"].get("command"):
        questions.append("What exact build command gates production deploys?")
    return questions


def scan(root: Path) -> dict[str, Any]:
    root = root.resolve()
    evidence: list[Evidence] = []
    app_roots = discover_app_roots(root, evidence)
    package_manager = effective_package_manager(root, app_roots)
    detected_frameworks = [app["framework"] for app in app_roots if app["framework"] != "unknown"]
    if len(app_roots) > 1 and any(framework == "next-app-router" for framework in detected_frameworks):
        framework = "monorepo-next-app-router"
    elif len(set(detected_frameworks)) > 1:
        framework = "monorepo-mixed"
    elif detected_frameworks:
        framework = detected_frameworks[0]
    else:
        framework = detect_framework_at(root)[0]
    routes = discover_route_files(root, app_roots)
    content_sinks = discover_content_sinks(root, routes, app_roots, evidence)
    route_families = make_route_families(content_sinks, routes)
    seo_surfaces = discover_seo_surfaces(root, evidence)
    internal_links = discover_internal_links(root, evidence)
    build = build_command(root, package_manager, app_roots)
    deploy = deploy_hints(root)
    validation = {
        "renderedHtmlRequired": True,
        "minTextHtmlRatio": 0.10,
        "minWordsDefault": 2000,
        "requiresCanonical": True,
        "requiresJsonLd": True,
        "requiresInternalLinks": True,
        "requiresSitemapInclusion": True,
    }
    confidence = confidence_score(framework, app_roots, content_sinks, route_families, seo_surfaces, internal_links, build)
    profile: dict[str, Any] = {
        "schemaVersion": "1.0.0",
        "repoRoot": str(root),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "framework": framework,
        "packageManager": package_manager,
        "appRoots": app_roots,
        "contentSinks": content_sinks,
        "routeFamilies": route_families,
        "seoSurfaces": seo_surfaces,
        "internalLinkSurfaces": internal_links,
        "build": build,
        "deploy": deploy,
        "validation": validation,
        "confidence": confidence,
        "runMode": choose_run_mode(confidence, framework, content_sinks, route_families),
        "evidence": [item.as_dict() for item in evidence],
        "questions": [],
    }
    profile["questions"] = build_questions(profile)
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a website repo and emit a TokenMax site profile.")
    parser.add_argument("repo", nargs="?", default=".", help="Repository or app root to scan.")
    parser.add_argument("--output", help="Path to write the profile JSON. Defaults to stdout only.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON profile to stdout.")
    args = parser.parse_args()

    profile = scan(Path(args.repo))
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json or not args.output:
        print(json.dumps(profile, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "ok": True,
                    "profile": str(Path(args.output).resolve()),
                    "framework": profile["framework"],
                    "runMode": profile["runMode"],
                    "confidence": profile["confidence"],
                    "contentSinks": len(profile["contentSinks"]),
                    "routeFamilies": len(profile["routeFamilies"]),
                    "questions": len(profile["questions"]),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
