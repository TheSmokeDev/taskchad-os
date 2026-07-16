#!/usr/bin/env python3
"""Deterministic contracts and gates for the AI Citation Authority Wave."""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


SCHEMA_VERSION = 1
ARTIFACT_DIR = ".citation-authority"
ALLOWED_LOCALES = {"auto", "en", "es"}
ALLOWED_MODES = {"reddit_modifier", "direct_answer", "comparison"}
ALLOWED_EVIDENCE_TYPES = {"gsc", "openseo", "serp_autopsy"}
PROFILE_MIN_CONFIDENCE = 0.80
DEFAULT_MIN_WORDS = 1000
DEFAULT_MAX_OVERLAP = 0.30
STALE_EVIDENCE_DAYS = 90


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return copy.deepcopy(default)
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def emit(data: dict[str, Any]) -> int:
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return 0 if data.get("ok", True) is True else 1


def artifacts(root: Path) -> Path:
    return root / ARTIFACT_DIR


def resolve_input_path(root: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def is_safe_site_path(raw: str) -> bool:
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    return (
        raw.startswith("/")
        and not parsed.scheme
        and not parsed.netloc
        and ".." not in Path(parsed.path).parts
        and not parsed.query
        and not parsed.fragment
    )


def deep_get(data: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = data
        for part in path:
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current not in (None, "", [], {}):
            return current
    return None


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def without_empty(value: Any) -> Any:
    if isinstance(value, dict):
        compacted = {key: without_empty(item) for key, item in value.items()}
        return {key: item for key, item in compacted.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        return [without_empty(item) for item in value if item not in (None, "", [], {})]
    return value


def safe_relative(root: Path, candidate: Path) -> str:
    resolved = candidate.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes target repo: {candidate}") from exc


def parse_timestamp(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_controls(raw: str) -> dict[str, Any]:
    controls: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "locale": "auto",
        "max_pages": 2,
        "modes": sorted(ALLOWED_MODES),
        "evidence_packet": None,
        "fleet_intent_map": None,
        "min_profile_confidence": PROFILE_MIN_CONFIDENCE,
    }
    allowed = set(controls)
    allowed.remove("schema_version")
    token_pattern = re.compile(r"(?:^|\s+)([A-Za-z_][A-Za-z0-9_]*)=(?:\"([^\"]*)\"|'([^']*)'|(\S+))")
    position = 0
    tokens: list[tuple[str, str]] = []
    for match in token_pattern.finditer(raw or ""):
        if (raw or "")[position : match.start()].strip():
            raise ValueError(f"unsupported argument near: {(raw or '')[position:match.start()].strip()}")
        value = next(item for item in match.groups()[1:] if item is not None)
        tokens.append((match.group(1), value))
        position = match.end()
    if (raw or "")[position:].strip():
        raise ValueError(f"unsupported argument near: {(raw or '')[position:].strip()}")
    for key, value in tokens:
        if key not in allowed:
            raise ValueError(f"unsupported control: {key}")
        if key == "max_pages":
            try:
                parsed = int(value)
            except ValueError as exc:
                raise ValueError("max_pages must be an integer") from exc
            if parsed not in {1, 2, 3}:
                raise ValueError("max_pages must be 1, 2, or 3")
            controls[key] = parsed
        elif key == "locale":
            if value not in ALLOWED_LOCALES:
                raise ValueError("locale must be auto, en, or es")
            controls[key] = value
        elif key == "modes":
            modes = [item.strip() for item in value.split(",") if item.strip()]
            unknown = sorted(set(modes) - ALLOWED_MODES)
            if not modes or unknown:
                raise ValueError(f"invalid modes: {unknown or value}")
            controls[key] = list(dict.fromkeys(modes))
        elif key == "min_profile_confidence":
            parsed_float = float(value)
            if parsed_float < PROFILE_MIN_CONFIDENCE or parsed_float > 1:
                raise ValueError("min_profile_confidence must be between 0.80 and 1.00")
            controls[key] = parsed_float
        else:
            controls[key] = value or None
    controls["created_at"] = utc_now()
    return controls


def command_parse_input(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        config = parse_controls(args.arguments)
    except ValueError as exc:
        return emit({"ok": False, "status": "invalid_controls", "errors": [str(exc)]})
    write_json(artifacts(root) / "run-config.json", config)
    return emit({"ok": True, "status": "configured", **config})


def package_candidates(root: Path) -> list[Path]:
    candidates = [root / "package.json", root / "site" / "package.json"]
    for folder in (root / "apps", root / "packages"):
        if folder.is_dir():
            candidates.extend(sorted(folder.glob("*/package.json")))
    return [candidate for candidate in candidates if candidate.is_file()]


def package_manager(root: Path, package_dir: Path) -> str:
    for directory in (package_dir, root):
        if (directory / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (directory / "yarn.lock").exists():
            return "yarn"
        if (directory / "bun.lockb").exists() or (directory / "bun.lock").exists():
            return "bun"
    return "npm"


def detect_package(root: Path) -> tuple[Path | None, dict[str, Any]]:
    packages: list[tuple[Path, dict[str, Any]]] = []
    for path in package_candidates(root):
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        packages.append((path, data))
    for path, data in packages:
        if isinstance(data.get("scripts"), dict) and "build" in data["scripts"]:
            return path, data
    return packages[0] if packages else (None, {})


def detect_content_sink(root: Path) -> str | None:
    candidates = (
        "content/blog",
        "src/content/blog",
        "site/content/blog",
        "content/articles",
        "src/content/articles",
        "content",
        "src/content",
    )
    for candidate in candidates:
        if (root / candidate).is_dir():
            return candidate
    return None


def detect_content_reference(root: Path, sink: str | None) -> tuple[str | None, str | None]:
    if not sink:
        return None, None
    sink_path = root / sink
    for suffix, content_format in (("*.mdx", "mdx"), ("*.md", "markdown")):
        for candidate in sorted(sink_path.rglob(suffix)):
            if candidate.is_file() and candidate.stat().st_size <= 1_000_000:
                return safe_relative(root, candidate), content_format
    return None, None


def limited_text_files(root: Path) -> Iterable[Path]:
    names = (
        "README.md",
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
        "next-sitemap.config.js",
        "astro.config.mjs",
        "brand.config.ts",
        "site.config.ts",
        "app/layout.tsx",
        "src/app/layout.tsx",
        "site/app/layout.tsx",
        "site/src/app/layout.tsx",
    )
    for name in names:
        path = root / name
        if path.is_file() and path.stat().st_size <= 1_000_000:
            yield path


def detect_canonical_host(root: Path) -> tuple[str | None, list[str]]:
    evidence: list[str] = []
    pattern = re.compile(r"https://(?:www\.)?[a-z0-9][a-z0-9.-]+", re.I)
    rejected = {"https://example.com", "https://www.example.com"}
    for path in limited_text_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in pattern.findall(text):
            host = match.rstrip("/.,'\"")
            if host in rejected or "localhost" in host:
                continue
            evidence.append(f"{safe_relative(root, path)}:{host}")
    hosts = [item.rsplit(":https", 1)[-1] for item in evidence]
    if not hosts:
        return None, evidence
    normalized = "https" + hosts[0] if not hosts[0].startswith("https") else hosts[0]
    return normalized.rstrip("/"), evidence


def detect_locale(root: Path) -> tuple[str | None, list[str]]:
    evidence: list[str] = []
    pattern = re.compile(r"<html[^>]+lang=[\"'](en|es)(?:-[A-Za-z-]+)?[\"']", re.I)
    for path in limited_text_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = pattern.search(text)
        if match:
            evidence.append(f"{safe_relative(root, path)}:{match.group(1).lower()}")
    return (evidence[0].rsplit(":", 1)[-1] if evidence else None), evidence


def tokenmax_mapping(root: Path, token: dict[str, Any]) -> dict[str, Any]:
    content_sink = deep_get(
        token,
        ("content_sink", "path"),
        ("content", "sink"),
        ("content", "sinks", "primary", "path"),
    )
    route_pattern = deep_get(token, ("route_family", "pattern"), ("routes", "pattern"))
    canonical = deep_get(
        token,
        ("canonical_host",),
        ("site", "canonical_host"),
        ("seo", "canonical_host"),
        ("base_url",),
    )
    locale = deep_get(token, ("locale",), ("site", "locale"), ("language",))
    build_command = deep_get(token, ("build", "command"), ("commands", "build"))
    build_cwd = deep_get(token, ("build", "cwd")) or "."
    sitemap = deep_get(token, ("sitemap", "url"), ("seo", "sitemap_url"), ("sitemap_url",))
    service_hubs = deep_get(
        token,
        ("internal_links", "service_hubs"),
        ("seo", "internal_links", "service_hubs"),
    ) or []
    reference_file = deep_get(
        token,
        ("content_sink", "reference_file"),
        ("content", "reference_file"),
        ("content", "sinks", "primary", "reference_file"),
    )
    return {
        "canonical_host": canonical,
        "locale": locale if locale in {"en", "es"} else None,
        "content_sink": {
            "path": content_sink,
            "format": deep_get(token, ("content_sink", "format")) or ("markdown" if content_sink else None),
            "reference_file": reference_file,
            "frontmatter_template": deep_get(token, ("content_sink", "frontmatter_template")),
        },
        "route_family": {"pattern": route_pattern or "/blog/{slug}"},
        "build": {"command": build_command, "cwd": build_cwd, "timeout_seconds": 900},
        "sitemap": {"url": sitemap},
        "internal_links": {"service_hubs": service_hubs, "contextual": []},
        "brand": {
            "name": deep_get(token, ("brand", "name"), ("site", "name")),
            "role": deep_get(token, ("brand", "role"), ("business", "role")),
        },
        "source_profile": {
            "type": "token_max",
            "path": ".token-max/site-profile.json",
            "confidence": deep_get(token, ("confidence",), ("scan", "confidence")),
        },
    }


def score_profile(profile: dict[str, Any]) -> float:
    score = 0.0
    if profile.get("canonical_host"):
        score += 0.15
    if profile.get("locale") in {"en", "es"}:
        score += 0.15
    if deep_get(profile, ("content_sink", "path")):
        score += 0.20
    if deep_get(profile, ("build", "command")):
        score += 0.15
    render = profile.get("render") or {}
    if render.get("base_url") and (render.get("start_command") or render.get("static_dir")):
        score += 0.10
    if deep_get(profile, ("sitemap", "url")) or deep_get(profile, ("sitemap", "path")):
        score += 0.10
    if deep_get(profile, ("internal_links", "service_hubs")):
        score += 0.10
    if deep_get(profile, ("brand", "name")) and deep_get(profile, ("brand", "role")):
        score += 0.05
    token_confidence = deep_get(profile, ("source_profile", "confidence"))
    if isinstance(token_confidence, (int, float)):
        score = min(score, float(token_confidence))
    return round(min(score, 1.0), 2)


def blocking_questions(profile: dict[str, Any]) -> list[dict[str, Any]]:
    required: list[tuple[str, Any]] = [
        ("canonical_host", profile.get("canonical_host")),
        ("locale", profile.get("locale") in {"en", "es"}),
        ("content_sink.path", deep_get(profile, ("content_sink", "path"))),
        (
            "content_sink.contract",
            deep_get(profile, ("content_sink", "reference_file"))
            or deep_get(profile, ("content_sink", "frontmatter_template")),
        ),
        ("build.command", deep_get(profile, ("build", "command"))),
        ("render.base_url", deep_get(profile, ("render", "base_url"))),
        (
            "render.start",
            deep_get(profile, ("render", "start_command")) or deep_get(profile, ("render", "static_dir")),
        ),
        (
            "sitemap",
            deep_get(profile, ("sitemap", "url")) or deep_get(profile, ("sitemap", "path")),
        ),
        ("internal_links.service_hubs", deep_get(profile, ("internal_links", "service_hubs"))),
        ("brand.name", deep_get(profile, ("brand", "name"))),
        ("brand.role", deep_get(profile, ("brand", "role"))),
    ]
    if deep_get(profile, ("regulated", "is_regulated")):
        required.append(("regulated.claims_policy", deep_get(profile, ("regulated", "claims_policy"))))
        required.append(
            (
                "regulated.authoritative_source_domains",
                deep_get(profile, ("regulated", "authoritative_source_domains")),
            )
        )
    questions = []
    for field, value in required:
        if not value:
            questions.append(
                {
                    "id": field.replace(".", "_"),
                    "field": field,
                    "question": f"Confirm {field} before content writes.",
                    "blocks_writes": True,
                }
            )
    return questions


def command_scan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    config = load_json(artifact_root / "run-config.json", parse_controls(""))
    existing = load_json(artifact_root / "site-profile.json", {})
    token_path = root / ".token-max" / "site-profile.json"
    token = load_json(token_path, {})
    package_path, package = detect_package(root)
    package_dir = package_path.parent if package_path else root
    manager = package_manager(root, package_dir)
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    canonical, canonical_evidence = detect_canonical_host(root)
    detected_locale, locale_evidence = detect_locale(root)
    sink = detect_content_sink(root)
    reference_file, content_format = detect_content_reference(root, sink)
    sitemap_path = next(
        (
            candidate
            for candidate in (
                root / "public" / "sitemap.xml",
                root / "site" / "public" / "sitemap.xml",
                root / "app" / "sitemap.ts",
                root / "src" / "app" / "sitemap.ts",
            )
            if candidate.exists()
        ),
        None,
    )
    readme = (root / "README.md").read_text(encoding="utf-8", errors="ignore") if (root / "README.md").exists() else ""
    regulated_match = re.search(r"\b(insurance|legal|medical|financial|finance)\b", readme, re.I)
    detected: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "root": str(root),
        "canonical_host": canonical,
        "locale": config.get("locale") if config.get("locale") in {"en", "es"} else detected_locale,
        "content_sink": {
            "path": sink,
            "format": content_format or ("markdown" if sink else None),
            "reference_file": reference_file,
            "frontmatter_template": None,
        },
        "route_family": {"pattern": "/blog/{slug}"},
        "build": {
            "command": f"{manager} build" if "build" in scripts else None,
            "cwd": safe_relative(root, package_dir) or ".",
            "timeout_seconds": 900,
        },
        "render": {"base_url": None, "start_command": None, "static_dir": None, "timeout_seconds": 120},
        "sitemap": {
            "path": safe_relative(root, sitemap_path) if sitemap_path else None,
            "url": f"{canonical}/sitemap.xml" if canonical else None,
        },
        "internal_links": {
            "service_hubs": ["/blog"] if sink and "blog" in sink else [],
            "contextual": ["/"] if canonical else [],
        },
        "brand": {"name": package.get("displayName") or package.get("name"), "role": None},
        "regulated": {
            "is_regulated": bool(regulated_match),
            "vertical": regulated_match.group(1).lower() if regulated_match else None,
            "claims_policy": None,
            "authoritative_source_domains": [],
        },
        "quality": {
            "min_main_words": DEFAULT_MIN_WORDS,
            "min_text_html_ratio": 0.10,
            "max_pairwise_overlap": DEFAULT_MAX_OVERLAP,
        },
        "deploy": {"runbook": None},
        "evidence": {
            "canonical": canonical_evidence,
            "locale": locale_evidence,
            "package": safe_relative(root, package_path) if package_path else None,
        },
        "source_profile": {"type": "standalone_scan", "path": None, "confidence": None},
    }
    if token:
        detected = deep_merge(detected, without_empty(tokenmax_mapping(root, token)))
    profile = deep_merge(detected, existing)
    profile["schema_version"] = SCHEMA_VERSION
    profile["generated_at"] = utc_now()
    profile["root"] = str(root)
    profile["confidence"] = score_profile(profile)
    profile["open_questions"] = blocking_questions(profile)
    write_json(artifact_root / "site-profile.json", profile)
    return emit(
        {
            "ok": True,
            "status": "scanned",
            "confidence": profile["confidence"],
            "blocking_questions": len(profile["open_questions"]),
            "source_profile": profile["source_profile"]["type"],
        }
    )


def repo_path_error(root: Path, raw: str, require: str, allow_missing: bool = False) -> str | None:
    path = Path(raw)
    candidate = path if path.is_absolute() else root / path
    try:
        safe_relative(root, candidate)
    except ValueError:
        return "must stay inside the target repository"
    if not candidate.exists():
        return None if allow_missing else "does not exist"
    if require == "directory" and not candidate.is_dir():
        return "must be a directory"
    if require == "file" and not candidate.is_file():
        return "must be a file"
    return None


def profile_errors(root: Path, profile: dict[str, Any], config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    threshold = float(config.get("min_profile_confidence", PROFILE_MIN_CONFIDENCE))
    if float(profile.get("confidence") or 0) < threshold:
        errors.append(f"profile confidence {profile.get('confidence')} is below {threshold:.2f}")
    canonical = profile.get("canonical_host")
    try:
        parsed_canonical = urlparse(canonical) if isinstance(canonical, str) else None
    except ValueError:
        parsed_canonical = None
    if (
        not parsed_canonical
        or parsed_canonical.scheme != "https"
        or not parsed_canonical.hostname
        or parsed_canonical.path not in {"", "/"}
        or parsed_canonical.params
        or parsed_canonical.query
        or parsed_canonical.fragment
    ):
        errors.append("canonical_host must be an HTTPS origin without a path, query, or fragment")
    if profile.get("locale") not in {"en", "es"}:
        errors.append("locale must resolve to en or es")
    sink = deep_get(profile, ("content_sink", "path"))
    if not sink or not (root / sink).is_dir():
        errors.append("content_sink.path must identify an existing directory")
    elif safe_path_error := repo_path_error(root, str(sink), require="directory"):
        errors.append(f"content_sink.path {safe_path_error}")
    reference_file = deep_get(profile, ("content_sink", "reference_file"))
    frontmatter_template = deep_get(profile, ("content_sink", "frontmatter_template"))
    if not frontmatter_template and (not reference_file or not (root / str(reference_file)).is_file()):
        errors.append("content sink requires an existing reference_file or a frontmatter_template")
    elif reference_file and (safe_path_error := repo_path_error(root, str(reference_file), require="file")):
        errors.append(f"content_sink.reference_file {safe_path_error}")
    if not deep_get(profile, ("build", "command")):
        errors.append("build.command is required")
    if safe_path_error := repo_path_error(root, str(deep_get(profile, ("build", "cwd")) or "."), require="directory"):
        errors.append(f"build.cwd {safe_path_error}")
    base_url = deep_get(profile, ("render", "base_url"))
    start = deep_get(profile, ("render", "start_command")) or deep_get(profile, ("render", "static_dir"))
    if not base_url or not start:
        errors.append("render requires base_url and start_command or static_dir")
    else:
        try:
            parsed_render = urlparse(str(base_url))
            _ = parsed_render.port
        except ValueError:
            parsed_render = None
        if (
            not parsed_render
            or parsed_render.scheme not in {"http", "https"}
            or parsed_render.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            errors.append("render.base_url must be a valid HTTP loopback URL")
    if safe_path_error := repo_path_error(root, str(deep_get(profile, ("render", "cwd")) or "."), require="directory"):
        errors.append(f"render.cwd {safe_path_error}")
    static_dir = deep_get(profile, ("render", "static_dir"))
    if static_dir and (safe_path_error := repo_path_error(root, str(static_dir), require="directory", allow_missing=True)):
        errors.append(f"render.static_dir {safe_path_error}")
    route_pattern = str(deep_get(profile, ("route_family", "pattern")) or "")
    if (
        route_pattern.count("{slug}") != 1
        or not is_safe_site_path(route_pattern.replace("{slug}", "slug"))
    ):
        errors.append("route_family.pattern must be absolute and contain {slug} exactly once")
    if not (deep_get(profile, ("sitemap", "url")) or deep_get(profile, ("sitemap", "path"))):
        errors.append("sitemap URL or path is required")
    sitemap_path = deep_get(profile, ("sitemap", "path"))
    if sitemap_path and (safe_path_error := repo_path_error(root, str(sitemap_path), require="file", allow_missing=True)):
        errors.append(f"sitemap.path {safe_path_error}")
    service_hubs = deep_get(profile, ("internal_links", "service_hubs"))
    if not service_hubs:
        errors.append("at least one service hub internal link is required")
    elif not isinstance(service_hubs, list) or not all(isinstance(hub, str) and is_safe_site_path(hub) for hub in service_hubs):
        errors.append("service hub links must be absolute site paths without traversal, queries, or fragments")
    if not deep_get(profile, ("brand", "name")) or not deep_get(profile, ("brand", "role")):
        errors.append("brand name and truthful role are required")
    if deep_get(profile, ("regulated", "is_regulated")) and not deep_get(
        profile, ("regulated", "claims_policy")
    ):
        errors.append("regulated sites require a claims policy")
    if deep_get(profile, ("regulated", "is_regulated")) and not deep_get(
        profile, ("regulated", "authoritative_source_domains")
    ):
        errors.append("regulated sites require authoritative source domains")
    blockers = [item for item in profile.get("open_questions", []) if item.get("blocks_writes")]
    if blockers:
        errors.append(f"{len(blockers)} blocking profile questions remain")
    return errors


def command_gate_profile(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    profile = load_json(artifacts(root) / "site-profile.json")
    config = load_json(artifacts(root) / "run-config.json", parse_controls(""))
    errors = profile_errors(root, profile, config)
    result = {"ok": not errors, "status": "ready" if not errors else "blocked", "errors": errors}
    write_json(artifacts(root) / "profile-gate.json", result)
    return emit(result)


def command_prepare_evidence(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    config = load_json(artifact_root / "run-config.json", parse_controls(""))
    destination = artifact_root / "evidence-packet.json"
    source = resolve_input_path(root, config.get("evidence_packet"))
    if source:
        if not source.is_file():
            return emit({"ok": False, "status": "missing_evidence_packet", "path": str(source)})
        packet = load_json(source)
        write_json(destination, packet)
        return emit({"ok": True, "status": "loaded", "needs_research": False, "path": str(source)})
    if destination.is_file():
        return emit({"ok": True, "status": "existing", "needs_research": False})
    request = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "status": "research_required",
        "allowed_receipt_types": sorted(ALLOWED_EVIDENCE_TYPES),
        "note": "Research may use GSC, OpenSEO, or a documented live SERP autopsy.",
    }
    write_json(artifact_root / "evidence-request.json", request)
    return emit({"ok": True, "status": "research_required", "needs_research": True})


def evidence_receipt_errors(receipt: dict[str, Any], now: datetime) -> list[str]:
    errors: list[str] = []
    receipt_id = receipt.get("id") or "<missing-id>"
    if not isinstance(receipt.get("id"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", receipt["id"]):
        errors.append(f"{receipt_id}: id must be path-safe lowercase ASCII")
    kind = receipt.get("type")
    if kind not in ALLOWED_EVIDENCE_TYPES:
        errors.append(f"{receipt_id}: unsupported evidence type {kind}")
    if not isinstance(receipt.get("query"), str) or not receipt["query"].strip():
        errors.append(f"{receipt_id}: query is required")
    observed = parse_timestamp(receipt.get("observed_at", ""))
    if not observed:
        errors.append(f"{receipt_id}: observed_at must be ISO-8601")
    elif now - observed > timedelta(days=STALE_EVIDENCE_DAYS):
        errors.append(f"{receipt_id}: evidence is older than {STALE_EVIDENCE_DAYS} days")
    metrics = receipt.get("metrics") if isinstance(receipt.get("metrics"), dict) else {}
    if kind == "gsc":
        try:
            impressions = float(metrics.get("impressions") or 0)
            clicks = float(metrics.get("clicks") or 0)
        except (TypeError, ValueError):
            impressions = clicks = 0
            errors.append(f"{receipt_id}: GSC metrics must be numeric")
        if impressions <= 0 and clicks <= 0:
            errors.append(f"{receipt_id}: GSC receipt requires impressions or clicks")
        if not receipt.get("date_range"):
            errors.append(f"{receipt_id}: GSC receipt requires date_range")
    elif kind == "openseo":
        metric_keys = {"search_volume", "keyword_difficulty", "cpc", "competition", "position"}
        if not any(key in metrics and metrics[key] is not None for key in metric_keys):
            errors.append(f"{receipt_id}: OpenSEO receipt requires measured metrics")
        if not (receipt.get("run_id") or receipt.get("source_url")):
            errors.append(f"{receipt_id}: OpenSEO receipt requires run_id or source_url")
    elif kind == "serp_autopsy":
        results = receipt.get("results") if isinstance(receipt.get("results"), list) else []
        valid_urls = [
            item.get("url")
            for item in results
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and urlparse(item["url"]).scheme in {"http", "https"}
            and bool(urlparse(item["url"]).netloc)
        ]
        if len(set(valid_urls)) < 3:
            errors.append(f"{receipt_id}: SERP autopsy requires at least three result URLs")
        if not receipt.get("search_engine"):
            errors.append(f"{receipt_id}: SERP autopsy requires search_engine")
    return errors


def command_validate_evidence(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    packet_path = artifacts(root) / "evidence-packet.json"
    if not packet_path.is_file():
        result = {"ok": False, "eligible": False, "status": "missing", "errors": ["evidence packet missing"]}
        write_json(artifacts(root) / "evidence-validation.json", result)
        return emit(result)
    packet = load_json(packet_path)
    if packet.get("status") == "no_evidence":
        result = {"ok": True, "eligible": False, "status": "no_evidence", "valid_receipt_ids": [], "errors": []}
        write_json(artifacts(root) / "evidence-validation.json", result)
        return emit(result)
    receipts = packet.get("receipts") if isinstance(packet.get("receipts"), list) else []
    errors: list[str] = []
    ids: list[str] = []
    now = datetime.now(timezone.utc)
    for receipt in receipts:
        if not isinstance(receipt, dict):
            errors.append("receipt must be an object")
            continue
        receipt_errors = evidence_receipt_errors(receipt, now)
        errors.extend(receipt_errors)
        if not receipt_errors:
            ids.append(str(receipt["id"]))
    if not receipts:
        errors.append("at least one evidence receipt is required")
    if len(ids) != len(set(ids)):
        errors.append("evidence receipt ids must be unique")
    result = {
        "ok": not errors,
        "eligible": bool(ids) and not errors,
        "status": "eligible" if bool(ids) and not errors else "blocked",
        "valid_receipt_ids": ids,
        "errors": errors,
    }
    write_json(artifacts(root) / "evidence-validation.json", result)
    return emit(result)


def recursive_queries(value: Any, owner: str | None = None) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    if isinstance(value, dict):
        current_owner = value.get("site_id") or value.get("id") or owner
        for key, nested in value.items():
            if key in {"query", "primary_query", "target_query"} and isinstance(nested, str):
                found.append((normalize_text(nested), str(current_owner) if current_owner else None))
            else:
                found.extend(recursive_queries(nested, current_owner))
    elif isinstance(value, list):
        for nested in value:
            found.extend(recursive_queries(nested, owner))
    return found


def structure_overlap(left: list[str], right: list[str]) -> float:
    left_set = {normalize_text(item) for item in left if isinstance(item, str) and item.strip()}
    right_set = {normalize_text(item) for item in right if isinstance(item, str) and item.strip()}
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else 0.0


def command_validate_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    plan_path = artifact_root / "candidate-plan.json"
    if not plan_path.is_file():
        result = {"ok": False, "status": "missing", "errors": ["candidate plan missing"]}
        write_json(artifact_root / "plan-validation.json", result)
        return emit(result)
    plan = load_json(plan_path)
    config = load_json(artifact_root / "run-config.json", parse_controls(""))
    profile = load_json(artifact_root / "site-profile.json")
    evidence = load_json(artifact_root / "evidence-validation.json")
    if plan.get("status") == "no_evidence" and evidence.get("status") == "no_evidence":
        no_evidence_targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
        errors = [] if not no_evidence_targets else ["no_evidence plan must not contain targets"]
        result = {
            "ok": not errors,
            "status": "no_evidence" if not errors else "blocked",
            "target_count": len(no_evidence_targets),
            "errors": errors,
        }
        write_json(artifact_root / "plan-validation.json", result)
        return emit(result)
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    errors: list[str] = []
    if not (1 <= len(targets) <= int(config["max_pages"])):
        errors.append(f"target count must be between 1 and {config['max_pages']}")
    allowed_receipts = set(evidence.get("valid_receipt_ids") or [])
    evidence_packet = load_json(artifact_root / "evidence-packet.json")
    receipt_queries = {
        str(receipt.get("id")): normalize_text(str(receipt.get("query") or ""))
        for receipt in evidence_packet.get("receipts", [])
        if isinstance(receipt, dict) and receipt.get("id")
    }
    allowed_modes = set(config.get("modes") or [])
    sink = Path(str(deep_get(profile, ("content_sink", "path")) or ""))
    sink_root = (root / sink).resolve()
    content_format = str(deep_get(profile, ("content_sink", "format")) or "markdown")
    required_suffix = ".mdx" if content_format == "mdx" else ".md"
    route_pattern = str(deep_get(profile, ("route_family", "pattern")) or "")
    service_hubs = set(deep_get(profile, ("internal_links", "service_hubs")) or [])
    regulated = bool(deep_get(profile, ("regulated", "is_regulated")))
    authority_domains = {
        str(domain).lower().removeprefix("www.")
        for domain in (deep_get(profile, ("regulated", "authoritative_source_domains")) or [])
    }
    seen: dict[str, set[str]] = {"id": set(), "query": set(), "slug": set(), "route": set(), "output": set()}
    fleet_queries: list[tuple[str, str | None]] = []
    fleet_path = resolve_input_path(root, config.get("fleet_intent_map"))
    if fleet_path:
        if not fleet_path.is_file():
            errors.append(f"fleet intent map missing: {fleet_path}")
        else:
            fleet_queries = recursive_queries(load_json(fleet_path))
    site_id = str(profile.get("site_id") or profile.get("brand", {}).get("name") or "")
    for index, target in enumerate(targets):
        prefix = f"target[{index}]"
        if not isinstance(target, dict):
            errors.append(f"{prefix} must be an object")
            continue
        target_id = str(target.get("id") or "")
        query = str(target.get("query") or "").strip()
        title = str(target.get("title") or "").strip()
        slug = str(target.get("slug") or "").strip()
        route = str(target.get("route") or "").strip()
        output_path = str(target.get("output_path") or "").strip()
        mode = target.get("mode")
        locale = target.get("locale")
        values = {"id": target_id, "query": normalize_text(query), "slug": slug, "route": route, "output": output_path}
        for key, value in values.items():
            if not value:
                errors.append(f"{prefix}.{key} is required")
            elif value in seen[key]:
                errors.append(f"{prefix}.{key} duplicates another target")
            seen[key].add(value)
        if target_id and not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", target_id):
            errors.append(f"{prefix}.id must be path-safe lowercase ASCII")
        if mode not in ALLOWED_MODES or mode not in allowed_modes:
            errors.append(f"{prefix}.mode is not allowed")
        if locale != profile.get("locale"):
            errors.append(f"{prefix}.locale must match site profile locale")
        if normalize_text(query) not in normalize_text(title):
            errors.append(f"{prefix}.title must contain the exact query")
        if slug != slugify(query):
            errors.append(f"{prefix}.slug must be the normalized exact query")
        expected_route = route_pattern.replace("{slug}", slug) if "{slug}" in route_pattern else ""
        if not expected_route or route.rstrip("/") != expected_route.rstrip("/"):
            errors.append(f"{prefix}.route must equal the configured route pattern")
        output = Path(output_path)
        if output.is_absolute() or not output_path:
            errors.append(f"{prefix}.output_path must be relative")
        else:
            try:
                resolved_output = (root / output).resolve()
                safe_relative(root, resolved_output)
                resolved_output.relative_to(sink_root)
            except ValueError as exc:
                errors.append(f"{prefix}.output_path must stay inside content sink {sink.as_posix()}: {exc}")
            if output.suffix.lower() != required_suffix:
                errors.append(f"{prefix}.output_path must use {required_suffix}")
        refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
        if not refs or not set(refs).issubset(allowed_receipts):
            errors.append(f"{prefix}.evidence_refs must reference validated receipts")
        elif normalize_text(query) not in {receipt_queries.get(str(ref), "") for ref in refs}:
            errors.append(f"{prefix}.query must exactly match a referenced evidence query")
        sources = target.get("sources") if isinstance(target.get("sources"), list) else []
        source_urls = [
            item.get("url")
            for item in sources
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item.get("url")
        ]
        if len(source_urls) != len(sources):
            errors.append(f"{prefix}.sources entries require string URLs")
        if len(set(source_urls)) < 2 or any(
            urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc for url in source_urls
        ):
            errors.append(f"{prefix}.sources requires at least two valid URLs")
        if regulated:
            source_hosts = {urlparse(url).netloc.lower().removeprefix("www.") for url in source_urls}
            if not any(
                host == domain or host.endswith(f".{domain}")
                for host in source_hosts
                for domain in authority_domains
            ):
                errors.append(f"{prefix}.sources requires a configured authoritative domain")
        if mode == "reddit_modifier":
            if "reddit" not in normalize_text(query) or "reddit" not in normalize_text(title):
                errors.append(f"{prefix}: reddit modifier must appear in query and title")
            if not any("reddit.com" in urlparse(url).netloc for url in source_urls):
                errors.append(f"{prefix}: reddit mode requires a real Reddit discussion source")
        headings = target.get("headings") if isinstance(target.get("headings"), list) else []
        if not all(isinstance(item, str) for item in headings):
            errors.append(f"{prefix}.headings entries must be strings")
        elif len(headings) < 4 or len({normalize_text(item) for item in headings}) != len(headings):
            errors.append(f"{prefix}.headings requires at least four unique sections")
        opening = target.get("direct_answer_sentences")
        if not isinstance(opening, list) or len(opening) != 2 or not all(isinstance(item, str) and item.strip() for item in opening):
            errors.append(f"{prefix}.direct_answer_sentences must contain exactly two sentences")
        elif not all(re.search(r"[.!?]\s*$", item) for item in opening):
            errors.append(f"{prefix}.direct_answer_sentences must be complete sentences")
        links = target.get("internal_links") if isinstance(target.get("internal_links"), list) else []
        if not all(isinstance(link, str) and is_safe_site_path(link) for link in links):
            errors.append(f"{prefix}.internal_links entries must be absolute site paths")
        elif len(set(links)) < 2 or not service_hubs.intersection(links):
            errors.append(f"{prefix}.internal_links must include a service hub and contextual link")
        if not str(target.get("brand_role_passage") or "").strip():
            errors.append(f"{prefix}.brand_role_passage is required")
        for owned_query, owner in fleet_queries:
            if normalize_text(query) != owned_query:
                continue
            if not owner:
                errors.append(f"{prefix}.query has unresolved ownership in the fleet map")
            elif normalize_text(owner) != normalize_text(site_id):
                errors.append(f"{prefix}.query collides with fleet owner {owner}")
    for left_index, left in enumerate(targets):
        if not isinstance(left, dict):
            continue
        for right in targets[left_index + 1 :]:
            if isinstance(right, dict) and structure_overlap(left.get("headings", []), right.get("headings", [])) > 0.50:
                errors.append(f"targets {left.get('id')} and {right.get('id')} converge structurally")
    result = {"ok": not errors, "status": "ready" if not errors else "blocked", "target_count": len(targets), "errors": errors}
    write_json(artifact_root / "plan-validation.json", result)
    return emit(result)


def command_record_approval(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    path = artifacts(root) / "approval-record.json"
    data = load_json(path, {"schema_version": SCHEMA_VERSION, "approvals": []})
    data["approvals"].append(
        {
            "stage": args.stage,
            "decision": "approved",
            "recorded_at": utc_now(),
            "bindings": current_approval_binding(root, args.stage),
        }
    )
    write_json(path, data)
    return emit({"ok": True, "status": "recorded", "stage": args.stage})


def command_prepare_queue(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    validation = load_json(artifact_root / "plan-validation.json")
    if validation.get("ok") is not True or validation.get("status") != "ready":
        return emit({"ok": False, "status": "blocked", "errors": ["validated ready plan is required"]})
    if not has_approval(root, "targets"):
        return emit({"ok": False, "status": "blocked", "errors": ["target approval is required"]})
    plan = load_json(artifact_root / "candidate-plan.json")
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    queue = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "status": "complete" if plan.get("status") == "no_evidence" else "pending",
        "items": [
            {
                "id": target["id"],
                "status": "pending",
                "draft_path": f"{ARTIFACT_DIR}/pages/{target['id']}.draft.md",
                "packet_path": f"{ARTIFACT_DIR}/pages/{target['id']}.packet.json",
            }
            for target in targets
        ],
    }
    write_json(artifact_root / "page-queue.json", queue)
    return emit({"ok": True, "status": queue["status"], "items": len(queue["items"])})


def queue_payload(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    queue = load_json(artifacts(root) / "page-queue.json")
    plan = load_json(artifacts(root) / "candidate-plan.json")
    return queue, plan


def command_next_page(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    queue, plan = queue_payload(root)
    pending = next((item for item in queue.get("items", []) if item.get("status") == "pending"), None)
    if not pending:
        return emit({"ok": True, "status": "complete", "complete": True})
    target = next(item for item in plan["targets"] if item["id"] == pending["id"])
    return emit({"ok": True, "status": "pending", "complete": False, "queue_item": pending, "target": target})


def markdown_body(text: str) -> str:
    stripped = text.lstrip("\ufeff").replace("\r\n", "\n")
    if stripped.startswith("---\n"):
        end = stripped.find("\n---\n", 4)
        if end != -1:
            return stripped[end + 5 :]
    return stripped


def first_paragraph(text: str) -> str:
    for block in re.split(r"\n\s*\n", markdown_body(text)):
        value = block.strip()
        if value and not value.startswith("#") and not value.startswith("<"):
            return re.sub(r"\s+", " ", value)
    return ""


def markdown_headings(text: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"^##\s+(.+?)\s*$", text, re.M)]


def word_count(text: str) -> int:
    plain = re.sub(r"`[^`]+`", " ", text)
    plain = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", plain)
    return len(re.findall(r"\b[\wÀ-ÿ'-]+\b", plain, re.UNICODE))


def shingle_set(text: str, size: int = 8) -> set[tuple[str, ...]]:
    words = re.findall(r"\b[\wÀ-ÿ'-]+\b", normalize_text(text), re.UNICODE)
    return {tuple(words[index : index + size]) for index in range(max(0, len(words) - size + 1))}


def shingle_overlap(left: str, right: str) -> float:
    left_set = shingle_set(left)
    right_set = shingle_set(right)
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else 0.0


def validate_page(root: Path, target_id: str, mark_queue: bool = True) -> dict[str, Any]:
    artifact_root = artifacts(root)
    profile = load_json(artifact_root / "site-profile.json")
    plan = load_json(artifact_root / "candidate-plan.json")
    queue = load_json(artifact_root / "page-queue.json")
    target = next((item for item in plan.get("targets", []) if item.get("id") == target_id), None)
    queue_item = next((item for item in queue.get("items", []) if item.get("id") == target_id), None)
    if not target or not queue_item:
        return {"ok": False, "id": target_id, "errors": ["target is not in the approved queue"]}
    draft_path = root / queue_item["draft_path"]
    packet_path = root / queue_item["packet_path"]
    errors: list[str] = []
    if not draft_path.is_file() or not packet_path.is_file():
        return {"ok": False, "id": target_id, "errors": ["draft and packet files are required"]}
    draft = draft_path.read_text(encoding="utf-8")
    packet = load_json(packet_path)
    if packet.get("target_id") != target_id:
        errors.append("packet target_id mismatch")
    if packet.get("slug") != target.get("slug") or packet.get("route") != target.get("route"):
        errors.append("packet route contract mismatch")
    if packet.get("locale") != profile.get("locale"):
        errors.append("packet locale mismatch")
    opening = packet.get("direct_answer_sentences")
    paragraph = normalize_text(first_paragraph(draft))
    if not isinstance(opening, list) or len(opening) != 2:
        errors.append("packet requires exactly two direct-answer sentences")
    elif any(normalize_text(sentence) not in paragraph for sentence in opening):
        errors.append("the first paragraph must contain both direct-answer sentences")
    headings = markdown_headings(draft)
    planned_headings = target.get("headings") or []
    if len(headings) < 4 or [normalize_text(item) for item in headings] != [normalize_text(item) for item in planned_headings]:
        errors.append("H2 structure must match the approved distinct plan")
    minimum = int(deep_get(profile, ("quality", "min_main_words")) or DEFAULT_MIN_WORDS)
    if word_count(markdown_body(draft)) < minimum:
        errors.append(f"draft has fewer than {minimum} visible words")
    source_urls = packet.get("source_urls") if isinstance(packet.get("source_urls"), list) else []
    planned_sources = [item.get("url") for item in target.get("sources", []) if isinstance(item, dict)]
    if not set(planned_sources).issubset(set(source_urls)) or any(url not in draft for url in source_urls):
        errors.append("all approved sources must be cited in the draft")
    links = packet.get("internal_links") if isinstance(packet.get("internal_links"), list) else []
    if not set(target.get("internal_links") or []).issubset(set(links)) or any(link not in draft for link in links):
        errors.append("all approved internal links must appear in the draft")
    brand_passage = str(packet.get("brand_role_passage") or "")
    if normalize_text(brand_passage) not in normalize_text(draft):
        errors.append("transparent brand-role passage is missing")
    compliance = packet.get("compliance") if isinstance(packet.get("compliance"), dict) else {}
    required_flags = ["fabrication_scan_passed", "regulated_claims_sourced", "original_language_copy"]
    if not all(compliance.get(flag) is True for flag in required_flags):
        errors.append("fabrication, claims, and original-language checks must pass")
    prohibited = (
        r"reddit recommends (us|this brand)",
        r"reddit recomienda (esta marca|nuestro servicio)",
        r"guaranteed (approval|savings|eligibility)",
        r"(aprobación|ahorro|elegibilidad) garantizad[oa]",
    )
    for pattern in prohibited:
        if re.search(pattern, draft, re.I):
            errors.append(f"prohibited claim matched: {pattern}")
    if profile.get("locale") == "es":
        scaffold = ["what you need to know", "frequently asked questions", "get a quote", "updated:"]
        if any(phrase in normalize_text(draft) for phrase in scaffold):
            errors.append("Spanish draft contains English scaffold copy")
        spanish_markers = re.findall(r"\b(el|la|los|las|de|para|con|que|una|seguro|conductores)\b", normalize_text(draft))
        if len(spanish_markers) < 8:
            errors.append("Spanish language signal is too weak")
    numeric_claims = packet.get("numeric_claims") if isinstance(packet.get("numeric_claims"), list) else []
    verified_values = {normalize_text(str(item.get("value"))) for item in numeric_claims if isinstance(item, dict) and item.get("source_ref")}
    material_numbers = re.findall(
        r"(?:\$\s?\d[\d,.]*|\b\d+(?:/\d+){1,3}\b|\b(?:19|20)\d{2}\b|\d+(?:\.\d+)?\s?(?:%|percent|por ciento|days|years|months|días|años|meses))",
        markdown_body(draft),
        re.I,
    )
    if any(normalize_text(value) not in verified_values for value in material_numbers):
        errors.append("material numeric claims require packet source references")
    output_path = root / str(target.get("output_path") or "")
    if not output_path.is_file():
        errors.append("integrated content file is missing")
    elif normalize_text(output_path.read_text(encoding="utf-8")) != normalize_text(draft):
        errors.append("integrated content must match the validated draft")
    result = {"ok": not errors, "id": target_id, "word_count": word_count(markdown_body(draft)), "headings": headings, "errors": errors}
    write_json(artifact_root / "pages" / f"{target_id}.validation.json", result)
    if mark_queue:
        queue_item["status"] = "completed" if result["ok"] else "blocked"
        queue_item["validated_at"] = utc_now()
        queue["status"] = "complete" if queue.get("items") and all(item.get("status") == "completed" for item in queue["items"]) else "blocked" if any(item.get("status") == "blocked" for item in queue.get("items", [])) else "pending"
        write_json(artifact_root / "page-queue.json", queue)
    return result


def command_validate_page(args: argparse.Namespace) -> int:
    return emit(validate_page(Path(args.root).resolve(), args.id, mark_queue=True))


def command_queue_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    queue = load_json(artifacts(root) / "page-queue.json")
    complete = bool(queue.get("items")) and all(item.get("status") == "completed" for item in queue["items"])
    result = {"ok": complete, "status": "complete" if complete else queue.get("status", "pending"), "complete": complete}
    print(json.dumps(result, sort_keys=True))
    return 0 if complete else 1


def command_validate_pages(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    plan = load_json(artifact_root / "candidate-plan.json")
    profile = load_json(artifact_root / "site-profile.json")
    results: list[dict[str, Any]] = []
    drafts: list[tuple[str, str, list[str]]] = []
    errors: list[str] = []
    for target in plan.get("targets", []):
        result = validate_page(root, target["id"], mark_queue=False)
        results.append(result)
        if not result["ok"]:
            errors.extend(f"{target['id']}: {message}" for message in result["errors"])
        draft_path = artifact_root / "pages" / f"{target['id']}.draft.md"
        if draft_path.is_file():
            drafts.append((target["id"], draft_path.read_text(encoding="utf-8"), result.get("headings", [])))
    max_overlap = float(deep_get(profile, ("quality", "max_pairwise_overlap")) or DEFAULT_MAX_OVERLAP)
    comparisons: list[dict[str, Any]] = []
    for index, (left_id, left_text, left_headings) in enumerate(drafts):
        for right_id, right_text, right_headings in drafts[index + 1 :]:
            content_overlap = round(shingle_overlap(left_text, right_text), 4)
            heading_overlap = round(structure_overlap(left_headings, right_headings), 4)
            comparisons.append({"left": left_id, "right": right_id, "content_overlap": content_overlap, "heading_overlap": heading_overlap})
            if content_overlap > max_overlap:
                errors.append(f"{left_id}/{right_id}: content overlap {content_overlap} exceeds {max_overlap}")
            if heading_overlap > 0.50:
                errors.append(f"{left_id}/{right_id}: heading structure converges")
    result = {"ok": not errors, "status": "passed" if not errors else "blocked", "pages": results, "comparisons": comparisons, "errors": errors}
    write_json(artifact_root / "pages-validation.json", result)
    return emit(result)


def run_contract_command(root: Path, section: dict[str, Any], result_name: str) -> dict[str, Any]:
    command = section.get("command")
    cwd = root / str(section.get("cwd") or ".")
    timeout = int(section.get("timeout_seconds") or 900)
    if not command:
        result = {"ok": False, "status": "blocked", "errors": [f"{result_name} command missing"]}
        write_json(artifacts(root) / result_name, result)
        return result
    if path_error := repo_path_error(root, str(section.get("cwd") or "."), require="directory"):
        result = {"ok": False, "status": "blocked", "errors": [f"{result_name} cwd {path_error}"]}
        write_json(artifacts(root) / result_name, result)
        return result
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "CI": "1"},
        )
        result = {
            "ok": completed.returncode == 0,
            "status": "passed" if completed.returncode == 0 else "failed",
            "command": command,
            "cwd": safe_relative(root, cwd) or ".",
            "returncode": completed.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": completed.stdout[-20000:],
            "stderr_tail": completed.stderr[-20000:],
        }
    except subprocess.TimeoutExpired as exc:
        result = {
            "ok": False,
            "status": "timeout",
            "command": command,
            "duration_seconds": round(time.monotonic() - started, 3),
            "errors": [str(exc)],
        }
    write_json(artifacts(root) / result_name, result)
    return result


def command_run_build(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    profile = load_json(artifacts(root) / "site-profile.json")
    return emit(run_contract_command(root, profile.get("build") or {}, "build-result.json"))


class PageHTMLParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.lang = ""
        self.canonical = ""
        self.hrefs: list[str] = []
        self.jsonld: list[str] = []
        self._script_jsonld = False
        self._script_parts: list[str] = []
        self._tag_stack: list[tuple[str, bool]] = []
        self._hidden_depth = 0
        self._main_depth = 0
        self.text_parts: list[str] = []
        self.main_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "html":
            self.lang = values.get("lang", "")
        if tag == "a" and values.get("href"):
            self.hrefs.append(values["href"])
        if tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonical = values.get("href", "")
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._script_jsonld = True
            self._script_parts = []
        hides = tag in {"script", "style", "noscript", "template"} or "hidden" in values or values.get("aria-hidden") == "true"
        if tag not in self.VOID_TAGS:
            self._tag_stack.append((tag, hides))
            if hides:
                self._hidden_depth += 1
        if tag == "main":
            self._main_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._script_jsonld:
            self.jsonld.append("".join(self._script_parts))
            self._script_jsonld = False
            self._script_parts = []
        matching_index = next(
            (index for index in range(len(self._tag_stack) - 1, -1, -1) if self._tag_stack[index][0] == tag),
            None,
        )
        if matching_index is not None:
            closed = self._tag_stack[matching_index:]
            self._tag_stack = self._tag_stack[:matching_index]
            self._hidden_depth = max(0, self._hidden_depth - sum(1 for _, hides in closed if hides))
        if tag == "main" and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._script_jsonld:
            self._script_parts.append(data)
        if self._hidden_depth == 0:
            value = html.unescape(data).strip()
            if value:
                self.text_parts.append(value)
                if self._main_depth:
                    self.main_parts.append(value)


def jsonld_types(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        raw_type = value.get("@type")
        if isinstance(raw_type, str):
            found.add(raw_type)
        elif isinstance(raw_type, list):
            found.update(str(item) for item in raw_type)
        for nested in value.values():
            found.update(jsonld_types(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(jsonld_types(nested))
    return found


def inspect_html(html_text: str, expected: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    parser = PageHTMLParser()
    parser.feed(html_text)
    types: set[str] = set()
    json_errors: list[str] = []
    for raw in parser.jsonld:
        try:
            types.update(jsonld_types(json.loads(raw)))
        except json.JSONDecodeError as exc:
            json_errors.append(str(exc))
    visible_text = " ".join(parser.text_parts)
    main_text = " ".join(parser.main_parts)
    ratio = len(visible_text) / max(len(html_text), 1)
    minimum_words = int(deep_get(profile, ("quality", "min_main_words")) or DEFAULT_MIN_WORDS)
    minimum_ratio = float(deep_get(profile, ("quality", "min_text_html_ratio")) or 0.10)
    expected_canonical = profile["canonical_host"].rstrip("/") + expected["route"]
    hubs = deep_get(profile, ("internal_links", "service_hubs")) or []
    errors: list[str] = []
    if not parser.lang.lower().startswith(str(profile["locale"]).lower()):
        errors.append(f"html lang mismatch: {parser.lang}")
    if parser.canonical.rstrip("/") != expected_canonical.rstrip("/"):
        errors.append(f"canonical mismatch: {parser.canonical} != {expected_canonical}")
    if not ({"Article", "WebPage"} & types):
        errors.append("Article or WebPage JSON-LD is required")
    if "BreadcrumbList" not in types:
        errors.append("BreadcrumbList JSON-LD is required")
    if json_errors:
        errors.append("invalid JSON-LD")
    if word_count(main_text) < minimum_words:
        errors.append(f"rendered main has fewer than {minimum_words} words")
    if ratio < minimum_ratio:
        errors.append(f"text/html ratio {ratio:.4f} is below {minimum_ratio:.4f}")
    if not any(hub in parser.hrefs for hub in hubs):
        errors.append("rendered page does not link to a service hub")
    internal_count = sum(1 for href in set(parser.hrefs) if href.startswith("/") or href.startswith(profile["canonical_host"]))
    if internal_count < 2:
        errors.append("rendered page requires at least two unique internal links")
    return {
        "ok": not errors,
        "lang": parser.lang,
        "canonical": parser.canonical,
        "schema_types": sorted(types),
        "main_words": word_count(main_text),
        "text_html_ratio": round(ratio, 4),
        "unique_internal_links": internal_count,
        "errors": errors,
    }


def normalized_href_path(raw: str) -> str:
    path = urlparse(raw).path or "/"
    return path if path == "/" else path.rstrip("/")


def parse_sitemap_document(xml_text: str) -> tuple[str, list[str]]:
    root = ElementTree.fromstring(xml_text)
    document_type = root.tag.rsplit("}", 1)[-1].lower()
    locations = [
        (element.text or "").strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1].lower() == "loc" and (element.text or "").strip()
    ]
    return document_type, locations


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def command_validate_rendered(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    profile = load_json(artifact_root / "site-profile.json")
    plan = load_json(artifact_root / "candidate-plan.json")
    render = profile.get("render") or {}
    base_url = str(render.get("base_url") or "")
    try:
        parsed = urlparse(base_url)
        parsed_port = parsed.port
    except ValueError:
        parsed = None
        parsed_port = None
    if (
        not parsed
        or parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        result = {"ok": False, "status": "blocked", "errors": ["render base URL must be loopback-only"]}
        write_json(artifact_root / "render-validation.json", result)
        return emit(result)
    port = parsed_port or free_port()
    if port == 0:
        port = free_port()
    base_url = f"{parsed.scheme or 'http'}://{parsed.hostname or '127.0.0.1'}:{port}"
    env = {**os.environ, "PORT": str(port), "CI": "1"}
    cwd = root / str(render.get("cwd") or ".")
    if path_error := repo_path_error(root, str(render.get("cwd") or "."), require="directory"):
        result = {"ok": False, "status": "blocked", "errors": [f"render cwd {path_error}"]}
        write_json(artifact_root / "render-validation.json", result)
        return emit(result)
    process: subprocess.Popen[str] | None = None
    command = render.get("start_command")
    static_dir = render.get("static_dir")
    if command:
        command = str(command).replace("{port}", str(port))
    elif static_dir:
        if path_error := repo_path_error(root, str(static_dir), require="directory"):
            result = {"ok": False, "status": "blocked", "errors": [f"render static_dir {path_error}"]}
            write_json(artifact_root / "render-validation.json", result)
            return emit(result)
        static_path = (root / str(static_dir)).resolve()
        command = f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1 --directory "{static_path}"'
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    sitemap_result: dict[str, Any] = {"ok": False, "errors": ["not checked"]}
    hub_results: list[dict[str, Any]] = []
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        routes = [target["route"] for target in plan.get("targets", [])]
        deadline = time.monotonic() + int(render.get("timeout_seconds") or 120)
        first_url = urljoin(base_url + "/", routes[0].lstrip("/")) if routes else base_url
        while time.monotonic() < deadline:
            try:
                with urlopen(Request(first_url, headers={"User-Agent": "CitationAuthorityValidator/1.0"}), timeout=5) as response:
                    if response.status == 200:
                        break
            except Exception:
                if process.poll() is not None:
                    break
                time.sleep(1)
        for target in plan.get("targets", []):
            url = urljoin(base_url + "/", target["route"].lstrip("/"))
            try:
                with urlopen(Request(url, headers={"User-Agent": "CitationAuthorityValidator/1.0"}), timeout=15) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    page_result = inspect_html(body, target, profile)
                    page_result.update({"id": target["id"], "url": url, "http_status": response.status})
                    if response.status != 200:
                        page_result["ok"] = False
                        page_result["errors"].append(f"HTTP {response.status}")
                    results.append(page_result)
                    errors.extend(f"{target['id']}: {message}" for message in page_result["errors"])
            except Exception as exc:
                errors.append(f"{target['id']}: fetch failed: {exc}")
                results.append({"id": target["id"], "url": url, "ok": False, "errors": [str(exc)]})

        sitemap = profile.get("sitemap") or {}
        sitemap_route = str(sitemap.get("local_route") or "")
        if not sitemap_route and sitemap.get("url"):
            sitemap_route = urlparse(str(sitemap["url"])).path
        if not sitemap_route:
            sitemap_route = "/sitemap.xml"
        sitemap_url = urljoin(base_url + "/", sitemap_route.lstrip("/"))
        sitemap_errors: list[str] = []
        try:
            with urlopen(Request(sitemap_url, headers={"User-Agent": "CitationAuthorityValidator/1.0"}), timeout=15) as response:
                sitemap_body = response.read().decode("utf-8", errors="replace")
                sitemap_type, locations = parse_sitemap_document(sitemap_body)
                sitemap_statuses = [{"url": sitemap_url, "http_status": response.status, "type": sitemap_type}]
                page_locations = list(locations) if sitemap_type == "urlset" else []
                if sitemap_type == "sitemapindex":
                    if len(locations) > 100:
                        sitemap_errors.append("sitemap index exceeds the 100-child validation limit")
                    for child in locations[:100]:
                        child_url = urljoin(base_url + "/", urlparse(child).path.lstrip("/"))
                        with urlopen(Request(child_url, headers={"User-Agent": "CitationAuthorityValidator/1.0"}), timeout=15) as child_response:
                            child_body = child_response.read().decode("utf-8", errors="replace")
                            child_type, child_locations = parse_sitemap_document(child_body)
                            sitemap_statuses.append(
                                {"url": child_url, "http_status": child_response.status, "type": child_type}
                            )
                            if child_response.status != 200:
                                sitemap_errors.append(f"child HTTP {child_response.status}: {child_url}")
                            if child_type != "urlset":
                                sitemap_errors.append(f"nested sitemap is not a urlset: {child_url}")
                            page_locations.extend(child_locations)
                elif sitemap_type != "urlset":
                    sitemap_errors.append(f"unsupported sitemap document: {sitemap_type}")
                sitemap_paths = {normalized_href_path(location) for location in page_locations}
                missing_routes = [
                    target["route"]
                    for target in plan.get("targets", [])
                    if normalized_href_path(target["route"]) not in sitemap_paths
                ]
                if response.status != 200:
                    sitemap_errors.append(f"HTTP {response.status}")
                if missing_routes:
                    sitemap_errors.append(f"missing routes: {', '.join(missing_routes)}")
                sitemap_result = {
                    "ok": not sitemap_errors,
                    "url": sitemap_url,
                    "http_status": response.status,
                    "documents": sitemap_statuses,
                    "url_count": len(sitemap_paths),
                    "missing_routes": missing_routes,
                    "errors": sitemap_errors,
                }
        except Exception as exc:
            sitemap_errors.append(f"fetch failed: {exc}")
            sitemap_result = {"ok": False, "url": sitemap_url, "errors": sitemap_errors}
        errors.extend(f"sitemap: {message}" for message in sitemap_errors)

        linked_routes: set[str] = set()
        for hub in deep_get(profile, ("internal_links", "service_hubs")) or []:
            hub_url = urljoin(base_url + "/", str(hub).lstrip("/"))
            hub_errors: list[str] = []
            try:
                with urlopen(Request(hub_url, headers={"User-Agent": "CitationAuthorityValidator/1.0"}), timeout=15) as response:
                    hub_body = response.read().decode("utf-8", errors="replace")
                    parser = PageHTMLParser()
                    parser.feed(hub_body)
                    paths = {normalized_href_path(href) for href in parser.hrefs}
                    linked_routes.update(paths)
                    if response.status != 200:
                        hub_errors.append(f"HTTP {response.status}")
                    hub_results.append(
                        {
                            "route": hub,
                            "url": hub_url,
                            "http_status": response.status,
                            "unique_links": len(paths),
                            "ok": not hub_errors,
                            "errors": hub_errors,
                        }
                    )
            except Exception as exc:
                hub_errors.append(f"fetch failed: {exc}")
                hub_results.append({"route": hub, "url": hub_url, "ok": False, "errors": hub_errors})
            errors.extend(f"hub {hub}: {message}" for message in hub_errors)
        missing_inbound = [
            target["route"]
            for target in plan.get("targets", [])
            if normalized_href_path(target["route"]) not in linked_routes
        ]
        if missing_inbound:
            errors.append(f"service hubs do not link to routes: {', '.join(missing_inbound)}")
    except Exception as exc:
        errors.append(f"render server failed: {exc}")
    finally:
        if process:
            terminate_process(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
    result = {
        "ok": not errors,
        "status": "passed" if not errors else "failed",
        "base_url": base_url,
        "pages": results,
        "sitemap": sitemap_result,
        "service_hubs": hub_results,
        "errors": errors,
    }
    write_json(artifact_root / "render-validation.json", result)
    return emit(result)


def command_aggregate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    names = [
        "profile-gate.json",
        "evidence-validation.json",
        "plan-validation.json",
        "pages-validation.json",
        "build-result.json",
        "render-validation.json",
    ]
    gates: dict[str, Any] = {}
    errors: list[str] = []
    for name in names:
        path = artifact_root / name
        if not path.is_file():
            errors.append(f"missing gate artifact: {name}")
            continue
        data = load_json(path)
        gates[name] = data
        if data.get("ok") is not True:
            errors.append(f"gate failed: {name}")
    result = {"schema_version": SCHEMA_VERSION, "generated_at": utc_now(), "ok": not errors, "status": "passed" if not errors else "blocked", "gates": gates, "errors": errors}
    write_json(artifact_root / "validation-report.json", result)
    return emit({"ok": result["ok"], "status": result["status"], "errors": errors})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def current_approval_binding(root: Path, stage: str) -> dict[str, str]:
    names = {
        "targets": ["candidate-plan.json", "plan-validation.json"],
        "deploy_handoff": ["validation-report.json"],
    }[stage]
    return {
        f"{ARTIFACT_DIR}/{name}": sha256_file(artifacts(root) / name)
        for name in names
    }


def has_approval(root: Path, stage: str) -> bool:
    record = load_json(artifacts(root) / "approval-record.json", {"approvals": []})
    try:
        expected = current_approval_binding(root, stage)
    except FileNotFoundError:
        return False
    return any(
        item.get("stage") == stage
        and item.get("decision") == "approved"
        and item.get("bindings") == expected
        for item in record.get("approvals", [])
    )


def command_create_handoff(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    report = load_json(artifact_root / "validation-report.json")
    plan = load_json(artifact_root / "candidate-plan.json")
    profile = load_json(artifact_root / "site-profile.json")
    errors: list[str] = []
    if report.get("ok") is not True:
        errors.append("validation report is not passing")
    if not has_approval(root, "deploy_handoff"):
        errors.append("deploy handoff approval is missing")
    files = []
    for target in plan.get("targets", []):
        path = root / target["output_path"]
        if not path.is_file():
            errors.append(f"missing output: {target['output_path']}")
            continue
        files.append({"path": target["output_path"], "sha256": sha256_file(path), "route": target["route"]})
    if errors:
        result = {"ok": False, "status": "blocked", "errors": errors}
        write_json(artifact_root / "deploy-handoff.json", result)
        return emit(result)
    handoff = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "ok": True,
        "status": "ready_for_deploy",
        "canonical_host": profile["canonical_host"],
        "locale": profile["locale"],
        "files": files,
        "validation_report": f"{ARTIFACT_DIR}/validation-report.json",
        "deploy_runbook": deep_get(profile, ("deploy", "runbook")),
        "production_deployed": False,
        "note": "This workflow does not deploy production.",
    }
    write_json(artifact_root / "deploy-handoff.json", handoff)
    return emit({"ok": True, "status": "ready_for_deploy", "files": len(files)})


def command_create_measurement(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_root = artifacts(root)
    handoff = load_json(artifact_root / "deploy-handoff.json")
    plan = load_json(artifact_root / "candidate-plan.json")
    if handoff.get("ok") is not True:
        return emit({"ok": False, "status": "blocked", "errors": ["deploy handoff is not ready"]})
    lines = []
    checkpoints = [
        ("early_signal", 2, ["indexed_state", "impressions", "query_to_page"]),
        ("t_plus_7", 7, ["indexed_state", "impressions", "clicks", "ctr", "position", "query_to_page"]),
        ("day_28", 28, ["indexed_state", "impressions", "clicks", "ctr", "position", "query_to_page", "cannibalization"]),
    ]
    for target in plan.get("targets", []):
        for checkpoint, due_days, metrics in checkpoints:
            lines.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "pending_deploy",
                    "checkpoint": checkpoint,
                    "target_id": target["id"],
                    "query": target["query"],
                    "route": target["route"],
                    "locale": target["locale"],
                    "mode": target["mode"],
                    "evidence_refs": target["evidence_refs"],
                    "due_after_deploy_days": due_days,
                    "metrics": metrics,
                    "read_only": True,
                }
            )
    path = artifact_root / "measurement-queue.jsonl"
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines), encoding="utf-8")
    return emit({"ok": True, "status": "queued", "items": len(lines)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Target website repository root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_input = subparsers.add_parser("parse-input")
    parse_input.add_argument("--arguments", default="")
    parse_input.set_defaults(func=command_parse_input)

    for name, function in (
        ("scan", command_scan),
        ("gate-profile", command_gate_profile),
        ("prepare-evidence", command_prepare_evidence),
        ("validate-evidence", command_validate_evidence),
        ("validate-plan", command_validate_plan),
        ("prepare-queue", command_prepare_queue),
        ("next-page", command_next_page),
        ("queue-status", command_queue_status),
        ("validate-pages", command_validate_pages),
        ("run-build", command_run_build),
        ("validate-rendered", command_validate_rendered),
        ("aggregate", command_aggregate),
        ("create-handoff", command_create_handoff),
        ("create-measurement", command_create_measurement),
    ):
        subparser = subparsers.add_parser(name)
        subparser.set_defaults(func=function)

    approval = subparsers.add_parser("record-approval")
    approval.add_argument("--stage", required=True, choices=["targets", "deploy_handoff"])
    approval.set_defaults(func=command_record_approval)

    page = subparsers.add_parser("validate-page")
    page.add_argument("--id", required=True)
    page.set_defaults(func=command_validate_page)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError) as exc:
        return emit({"ok": False, "status": "error", "errors": [str(exc)]})


if __name__ == "__main__":
    raise SystemExit(main())
