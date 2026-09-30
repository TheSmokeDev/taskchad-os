"""Small provider-independent contracts for the Build + Grow vertical slice."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_FILES = 30
SOURCE_EXTENSIONS = frozenset({".html", ".css", ".json", ".txt", ".xml"})


class NotFoundError(Exception):
    """The requested object is absent in the caller's workspace."""


class ConflictError(Exception):
    """A revision or job state changed before this operation completed."""


class ValidationError(ValueError):
    """Input violates the bounded Build + Grow contract."""


@dataclass(frozen=True)
class Artifact:
    filename: str
    media_type: str
    content: bytes


def bounded_text(value: object, name: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be text")
    text = value.strip()
    if len(text) > maximum or (required and not text):
        raise ValidationError(
            f"{name} must contain {'1' if required else '0'}–{maximum} characters"
        )
    if any(
        (ord(char) < 32 and char not in "\n\t") or 0xD800 <= ord(char) <= 0xDFFF for char in text
    ):
        raise ValidationError(f"{name} contains unsupported control characters")
    return text


def _https_url(value: str, name: str, *, github: bool = False) -> str:
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"{name} is not a valid HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
        or "\\" in value
    ):
        raise ValidationError(f"{name} must be HTTPS without credentials, query, or fragment")
    if github:
        path = parsed.path.strip("/")
        if (
            parsed.hostname.lower() != "github.com"
            or port is not None
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path)
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValidationError("repository_url must identify an HTTPS github.com repository")
    return value


def normalize_brief(brief: object) -> dict:
    if not isinstance(brief, dict):
        raise ValidationError("brief must be an object")
    limits = {
        "business_name": 160,
        "description": 4000,
        "industry": 120,
        "location": 200,
        "contact_email": 254,
        "phone": 60,
        "site_url": 1000,
        "repository_url": 1000,
        "brand_color": 7,
    }
    allowed = set(limits) | {"services"}
    if set(brief) - allowed:
        raise ValidationError("brief contains unsupported fields")
    result = {
        name: bounded_text(
            brief.get(name, "#eab308" if name == "brand_color" else ""),
            name,
            limit,
            required=name in {"business_name", "description", "contact_email"},
        )
        for name, limit in limits.items()
    }
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", result["contact_email"]):
        raise ValidationError("contact_email must be an email address")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", result["brand_color"]):
        raise ValidationError("brand_color must be a six-digit hex color")
    result["site_url"] = _https_url(result["site_url"], "site_url")
    result["repository_url"] = _https_url(result["repository_url"], "repository_url", github=True)
    services = brief.get("services")
    if not isinstance(services, list) or not 1 <= len(services) <= 20:
        raise ValidationError("services must contain 1–20 services")
    result["services"] = []
    for service in services:
        if not isinstance(service, dict) or set(service) != {"name", "description"}:
            raise ValidationError("each service must contain name and description")
        result["services"].append(
            {
                "name": bounded_text(service["name"], "service name", 160, required=True),
                "description": bounded_text(
                    service["description"], "service description", 2000, required=True
                ),
            }
        )
    return result


def validate_source_filename(filename: object) -> str:
    if not isinstance(filename, str) or not filename or len(filename) > 200:
        raise ValidationError("source filename must contain 1–200 characters")
    parts = filename.split("/")
    if (
        "\\" in filename
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 for char in filename)
        or ":" in filename
        or filename.startswith("/")
        or PurePosixPath(filename).suffix.lower() not in SOURCE_EXTENSIONS
    ):
        raise ValidationError("source files require safe relative HTML/CSS/JSON/TXT/XML paths")
    return filename


def normalize_source_files(source_files: object) -> dict[str, str]:
    if not isinstance(source_files, dict) or not 1 <= len(source_files) <= MAX_SOURCE_FILES:
        raise ValidationError(f"source_files must contain 1–{MAX_SOURCE_FILES} files")
    size = 0
    result = {}
    for filename, content in source_files.items():
        validate_source_filename(filename)
        if not isinstance(content, str):
            raise ValidationError("source file content must be text")
        if len(content) > MAX_SOURCE_BYTES:
            raise ValidationError("source files exceed the 2 MiB limit")
        try:
            size += len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValidationError("source file content must be valid UTF-8 text") from exc
        if size > MAX_SOURCE_BYTES:
            raise ValidationError("source files exceed the 2 MiB limit")
        result[filename] = content
    if not any(PurePosixPath(name).suffix.lower() == ".html" for name in result):
        raise ValidationError("GEO source files must include an HTML page")
    return result
