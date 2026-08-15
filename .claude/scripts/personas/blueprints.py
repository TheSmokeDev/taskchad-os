"""Pure persona-blueprint compiler and capability-class contract.

This module is intentionally write-free. It turns a profile-local
``blueprint.yaml`` document into a deterministic provisioning plan; the
provisioning service owns atomic filesystem/config/channel mutations.

The compiler separates four authority classes:

``safe-core``
    Useful defaults for every interactive persona: profile-scoped recall,
    indexed search, skill reads, and private planning.
``domain-pack``
    Read-oriented capabilities required by a role, such as AI engineering or
    founder operations.
``operator-exec``
    Broad filesystem, process, shell, and write authority. It is explicit and
    never implied by profile creation or a domain pack.
``scheduled-study``
    Narrow scheduled cognition. It compiles to ``model_only`` with zero chat
    toolsets and never inherits interactive or operator-exec authority.

Existing profiles migrate in preservation mode by default: their current
effective ``toolsets`` / ``tools`` (including the deprecated
``cabinet.tools`` alias) remain byte-for-byte intent, while the plan reports
the blueprint recommendation separately. This makes migration reviewable
instead of silently widening or narrowing a live persona.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from personas.core import reject_sentinel_persona_name, validate_persona_name

SCHEMA_VERSION = 1
MANIFEST_FILENAME = "blueprint.yaml"


class BlueprintError(ValueError):
    """Raised when a persona blueprint is malformed or unsafe."""


class ProvisionMode(StrEnum):
    """How a compiled recommendation relates to current profile state."""

    CREATE = "create"
    MIGRATE = "migrate"
    RECONCILE = "reconcile"


class CapabilityClass(StrEnum):
    SAFE_CORE = "safe-core"
    DOMAIN_PACK = "domain-pack"
    OPERATOR_EXEC = "operator-exec"
    SCHEDULED_STUDY = "scheduled-study"


@dataclass(frozen=True)
class DomainPack:
    """One reusable role pack; it never carries operator-exec authority."""

    id: str
    toolsets: tuple[str, ...]
    env_groups: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    integration_requirements: tuple[str, ...] = ()
    proposal_authorities: tuple[str, ...] = ("mailbox.propose",)


DOMAIN_PACKS: dict[str, DomainPack] = {
    "ai_engineering": DomainPack(
        id="ai_engineering",
        toolsets=("ai_engineering",),
        env_groups=("runtime_core", "vault_memory"),
        integration_requirements=("sheets.read",),
    ),
    "founder_operations": DomainPack(
        id="founder_operations",
        toolsets=("founder_operations",),
        env_groups=("runtime_core", "vault_memory", "business_profile"),
        integration_requirements=("sheets.read",),
    ),
    "seo_geo": DomainPack(
        id="seo_geo",
        toolsets=("seo_geo_read",),
        env_groups=(
            "runtime_core",
            "vault_memory",
            "search_analytics",
            "business_profile",
            "seo_research",
        ),
        skills=("direct-integrations",),
        integration_requirements=("analytics.overview", "search_console.overview"),
    ),
}


@dataclass(frozen=True)
class BuiltinTemplate:
    id: str
    display_name: str
    description: str
    default_role: str
    default_model: str
    domain: str
    domain_packs: tuple[str, ...]
    scheduled_authorities: tuple[str, ...] = ()


BUILTIN_TEMPLATES: dict[str, BuiltinTemplate] = {
    "general-specialist": BuiltinTemplate(
        id="general-specialist",
        display_name="Specialist",
        description="A safe general-purpose persona with recall, planning, and skill discovery.",
        default_role="Handle scoped operator requests using safe recall, planning, and skill discovery.",
        default_model="claude-sonnet-4-7",
        domain="general",
        domain_packs=(),
    ),
    "ai-engineer": BuiltinTemplate(
        id="ai-engineer",
        display_name="AI Engineer",
        description="A read-oriented engineering persona for repository analysis and technical research.",
        default_role="Inspect repositories, research technical options, and propose implementation work.",
        default_model="claude-sonnet-4-7",
        domain="ai-engineering",
        domain_packs=("ai_engineering",),
        scheduled_authorities=("curriculum_study",),
    ),
    "founder-operator": BuiltinTemplate(
        id="founder-operator",
        display_name="Founder Operator",
        description="A founder-operations persona for business research, planning, and internal proposals.",
        default_role="Research business operations, inspect approved data, and prepare internal proposals.",
        default_model="claude-sonnet-4-7",
        domain="founder-operations",
        domain_packs=("founder_operations",),
    ),
    "seo-geo": BuiltinTemplate(
        id="seo-geo",
        display_name="SEO/GEO",
        description="A read-only SEO, GEO, and AI-visibility specialist for evidence-backed fleet operations.",
        default_role="Analyze GSC, GA4, fleet receipts, technical crawl evidence, and AI-visibility signals; produce approval-only SEO/GEO actions.",
        default_model="claude-sonnet-4-7",
        domain="seo-geo",
        domain_packs=("seo_geo",),
    ),
}


@dataclass(frozen=True)
class ChannelIntent:
    kind: str
    channel_id: str
    name: str


@dataclass(frozen=True)
class PersonaBlueprint:
    schema_version: int
    template: str
    persona_id: str
    display_name: str
    role: str | None
    model: str | None
    domain: str
    safe_core: bool
    domain_packs: tuple[str, ...]
    operator_exec: bool
    channels: tuple[ChannelIntent, ...]
    scheduled_authorities: tuple[str, ...]


@dataclass(frozen=True)
class ScheduledAuthorityPlan:
    authority: str
    capability_class: str = CapabilityClass.SCHEDULED_STUDY.value
    model_only: bool = True
    toolsets: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlueprintPlan:
    """Deterministic, serializable output of :func:`compile_blueprint`."""

    schema_version: int
    persona_id: str
    display_name: str
    role: str | None
    model: str | None
    template: str
    domain: str
    mode: str
    capability_classes: tuple[str, ...]
    recommended_toolsets: tuple[str, ...]
    applied_toolsets: tuple[str, ...]
    applied_tools: tuple[str, ...]
    preserved_existing_scope: bool
    env_groups: tuple[str, ...]
    skills: tuple[str, ...]
    integration_requirements: tuple[str, ...]
    proposal_authorities: tuple[str, ...]
    channels: tuple[ChannelIntent, ...]
    scheduled: tuple[ScheduledAuthorityPlan, ...]
    applied_declared_tools: tuple[str, ...]
    declared_tools: tuple[str, ...]
    callable_tools: tuple[str, ...] | None
    missing_tools: tuple[str, ...] | None
    config_patch: dict[str, Any]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation for CLI/API preview surfaces."""

        return asdict(self)


_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "template", "persona", "capabilities", "channels", "scheduled"}
)
_PERSONA_KEYS = frozenset({"id", "display_name", "role", "model", "domain"})
_CAPABILITY_KEYS = frozenset({"safe_core", "domain_packs", "operator_exec"})
_CHANNEL_KEYS = frozenset({"kind", "channel_id", "name"})
_SCHEDULED_KEYS = frozenset({"authorities"})
_SCHEDULED_AUTHORITIES = frozenset({"curriculum_study", "persona_reflection"})
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def template_catalog() -> tuple[dict[str, Any], ...]:
    """Return the deterministic catalog shared by CLI, API, and dashboard."""

    return tuple(
        {
            "id": template.id,
            "name": template.display_name,
            "description": template.description,
            "default_role": template.default_role,
            "default_model": template.default_model,
            "domain": template.domain,
            "domain_packs": list(template.domain_packs),
            "scheduled_authorities": list(template.scheduled_authorities),
            "operator_exec_default": False,
        }
        for template in BUILTIN_TEMPLATES.values()
    )


def build_builtin_blueprint(
    template_id: str,
    *,
    persona_id: str | None = None,
    display_name: str | None = None,
    role: str | None = None,
    model: str | None = None,
    domain: str | None = None,
    channel_id: str | None = None,
    channel_name: str | None = None,
    operator_exec: bool | None = None,
) -> dict[str, Any]:
    """Build a strict blueprint document from a framework template."""

    template = BUILTIN_TEMPLATES.get(template_id)
    if template is None:
        raise BlueprintError(
            f"unknown persona template {template_id!r}; "
            f"known: {', '.join(sorted(BUILTIN_TEMPLATES))}"
        )
    resolved_persona = persona_id or template.id
    channels: list[dict[str, str]] = []
    if channel_id is not None:
        channels.append(
            {
                "kind": "discord",
                "channel_id": channel_id,
                "name": channel_name if channel_name is not None else resolved_persona,
            }
        )
    if operator_exec is not None and not isinstance(operator_exec, bool):
        raise BlueprintError("operator_exec must be a boolean")
    persona: dict[str, str] = {
        "id": resolved_persona,
        "display_name": (
            display_name if display_name is not None else template.display_name
        ),
        "domain": domain if domain is not None else template.domain,
    }
    if role is not None:
        persona["role"] = role
    if model is not None:
        persona["model"] = model
    return {
        "schema_version": SCHEMA_VERSION,
        "template": template.id,
        "persona": persona,
        "capabilities": {
            "safe_core": True,
            "domain_packs": list(template.domain_packs),
            "operator_exec": operator_exec if operator_exec is not None else False,
        },
        "channels": channels,
        "scheduled": {"authorities": list(template.scheduled_authorities)},
    }


def parse_blueprint(raw: dict[str, Any]) -> PersonaBlueprint:
    """Validate and normalize a raw ``blueprint.yaml`` mapping."""

    if not isinstance(raw, dict):
        raise BlueprintError("blueprint top-level must be a mapping")
    _reject_unknown(raw, _TOP_LEVEL_KEYS, "blueprint")

    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise BlueprintError(f"schema_version must be {SCHEMA_VERSION}, got {version!r}")

    template = _required_text(raw.get("template"), "template")
    if template not in BUILTIN_TEMPLATES:
        raise BlueprintError(f"unknown template {template!r}")

    persona = _required_mapping(raw.get("persona"), "persona")
    _reject_unknown(persona, _PERSONA_KEYS, "persona")
    persona_id = _required_text(persona.get("id"), "persona.id")
    try:
        validate_persona_name(persona_id)
    except ValueError as exc:
        raise BlueprintError(str(exc)) from exc
    display_name = _bounded_text(
        persona.get("display_name"),
        "persona.display_name",
        max_length=128,
    )
    role = _optional_bounded_text(persona.get("role"), "persona.role", max_length=2000)
    model = _optional_bounded_text(persona.get("model"), "persona.model", max_length=128)
    if model is not None and not _MODEL_RE.fullmatch(model):
        raise BlueprintError(
            "persona.model may contain only letters, numbers, '.', '_', ':', '/', and '-'"
        )
    domain = _required_text(persona.get("domain"), "persona.domain")
    if not _DOMAIN_RE.fullmatch(domain):
        raise BlueprintError(
            "persona.domain must start with a lowercase letter or number and "
            "contain only lowercase letters, numbers, '-' or '_'"
        )

    capabilities = _required_mapping(raw.get("capabilities"), "capabilities")
    _reject_unknown(capabilities, _CAPABILITY_KEYS, "capabilities")
    safe_core = capabilities.get("safe_core")
    if safe_core is not True:
        raise BlueprintError(
            "capabilities.safe_core must be true; new personas may not compile "
            "to an empty tool surface"
        )
    operator_exec = capabilities.get("operator_exec", False)
    if not isinstance(operator_exec, bool):
        raise BlueprintError("capabilities.operator_exec must be a boolean")
    domain_packs = _string_tuple(
        capabilities.get("domain_packs", []),
        "capabilities.domain_packs",
    )
    unknown_packs = sorted(set(domain_packs) - set(DOMAIN_PACKS))
    if unknown_packs:
        raise BlueprintError(f"unknown domain pack(s): {', '.join(unknown_packs)}")

    channels_raw = raw.get("channels", [])
    if not isinstance(channels_raw, list):
        raise BlueprintError("channels must be a list")
    channels: list[ChannelIntent] = []
    seen_channels: set[tuple[str, str]] = set()
    for index, row in enumerate(channels_raw):
        item = _required_mapping(row, f"channels[{index}]")
        _reject_unknown(item, _CHANNEL_KEYS, f"channels[{index}]")
        kind = _required_text(item.get("kind"), f"channels[{index}].kind")
        if kind != "discord":
            raise BlueprintError(f"channels[{index}].kind must be 'discord', got {kind!r}")
        channel_id = _required_text(item.get("channel_id"), f"channels[{index}].channel_id")
        if not channel_id.isdigit():
            raise BlueprintError(f"channels[{index}].channel_id must contain digits only")
        key = (kind, channel_id)
        if key in seen_channels:
            raise BlueprintError(f"duplicate channel binding {kind}:{channel_id}")
        seen_channels.add(key)
        channels.append(
            ChannelIntent(
                kind=kind,
                channel_id=channel_id,
                name=_bounded_text(
                    item.get("name"),
                    f"channels[{index}].name",
                    max_length=128,
                ),
            )
        )

    scheduled = _required_mapping(raw.get("scheduled", {}), "scheduled")
    _reject_unknown(scheduled, _SCHEDULED_KEYS, "scheduled")
    scheduled_authorities = _string_tuple(
        scheduled.get("authorities", []),
        "scheduled.authorities",
    )
    unknown_authorities = sorted(set(scheduled_authorities) - _SCHEDULED_AUTHORITIES)
    if unknown_authorities:
        raise BlueprintError(f"unknown scheduled authority: {', '.join(unknown_authorities)}")

    return PersonaBlueprint(
        schema_version=version,
        template=template,
        persona_id=persona_id,
        display_name=display_name,
        role=role,
        model=model,
        domain=domain,
        safe_core=safe_core,
        domain_packs=domain_packs,
        operator_exec=operator_exec,
        channels=tuple(channels),
        scheduled_authorities=scheduled_authorities,
    )


def compile_blueprint(
    raw: dict[str, Any] | PersonaBlueprint,
    *,
    mode: ProvisionMode | str = ProvisionMode.CREATE,
    current_config: dict[str, Any] | None = None,
    callable_tools: Collection[str] | None = None,
) -> BlueprintPlan:
    """Compile a persona blueprint into a write-free provisioning plan.

    ``MIGRATE`` preserves any existing scope exactly and reports the blueprint
    recommendation separately. ``CREATE`` and ``RECONCILE`` apply the
    recommendation in the plan, but still perform no writes.
    """

    blueprint = raw if isinstance(raw, PersonaBlueprint) else parse_blueprint(raw)
    try:
        resolved_mode = mode if isinstance(mode, ProvisionMode) else ProvisionMode(mode)
    except ValueError as exc:
        raise BlueprintError(f"unknown provision mode {mode!r}") from exc

    # Issue #422 round 3 — refuse resolver sentinels on the CREATE door.
    # ``get_persona_paths("custom")`` roots that profile at ``HOMIE_HOME``
    # itself, so the atomic provisioner would stage
    # ``profiles/custom/config.yaml`` while every runtime reader keeps
    # reading ``HOMIE_HOME/config.yaml`` — a create that returns success and
    # yields a persona the learning tick can never configure. This is the
    # seam BOTH atomic doors reach (``thehomie profile create`` non-clone and
    # dashboard ``POST /api/agents``, via preview_provision/apply_provision),
    # so both refuse here, before anything is staged.
    #
    # CREATE only: RECONCILE/MIGRATE operate on a profile that already
    # exists, and rejecting them would strand it instead of repairing it.
    # The clone door refuses the same class in
    # ``personas.lifecycle.create_profile`` Step 2b (physical containment).
    if resolved_mode is ProvisionMode.CREATE:
        try:
            reject_sentinel_persona_name(blueprint.persona_id)
        except ValueError as exc:
            raise BlueprintError(str(exc)) from exc

    recommended = ["safe_core"]
    env_groups: set[str] = {"runtime_core", "vault_memory"}
    skills: set[str] = set()
    integrations: set[str] = set()
    proposals: set[str] = set()
    for pack_id in blueprint.domain_packs:
        pack = DOMAIN_PACKS[pack_id]
        recommended.extend(pack.toolsets)
        env_groups.update(pack.env_groups)
        skills.update(pack.skills)
        integrations.update(pack.integration_requirements)
        proposals.update(pack.proposal_authorities)
    if blueprint.operator_exec:
        recommended.append("operator_exec")
    recommended_toolsets = _dedupe(recommended)

    existing_toolsets, existing_tools, _has_existing_scope = _current_scope(
        current_config or {}
    )
    # A migration is preserve-first whenever a physical config was supplied,
    # including an empty mapping or a config with no scope keys. Absent scope
    # is an effective empty grant and must not be widened into the template
    # recommendation merely because there was no key to copy.
    preserve = resolved_mode is ProvisionMode.MIGRATE and current_config is not None
    applied_toolsets = existing_toolsets if preserve else recommended_toolsets
    applied_tools = existing_tools if preserve else ()

    classes = [CapabilityClass.SAFE_CORE.value]
    if blueprint.domain_packs:
        classes.append(CapabilityClass.DOMAIN_PACK.value)
    if blueprint.operator_exec:
        classes.append(CapabilityClass.OPERATOR_EXEC.value)
    if blueprint.scheduled_authorities:
        classes.append(CapabilityClass.SCHEDULED_STUDY.value)

    scheduled_plans = tuple(
        ScheduledAuthorityPlan(authority=name) for name in blueprint.scheduled_authorities
    )
    if any(plan.toolsets or plan.tools or not plan.model_only for plan in scheduled_plans):
        raise BlueprintError("scheduled study must compile model_only with no interactive tools")

    declared_tools = _resolve_declared_tools(recommended_toolsets)
    applied_declared_tools = tuple(
        sorted(set(_resolve_declared_tools(applied_toolsets)) | set(applied_tools))
    )
    if callable_tools is None:
        callable_tuple: tuple[str, ...] | None = None
        missing: tuple[str, ...] | None = None
    else:
        callable_set = {str(name).strip() for name in callable_tools if str(name).strip()}
        callable_tuple = tuple(sorted(set(applied_declared_tools) & callable_set))
        missing = tuple(sorted(set(applied_declared_tools) - callable_set))

    warnings: list[str] = []
    if preserve:
        warnings.append(
            "existing tool scope preserved; apply the recommendation only through "
            "an explicit reconcile review"
        )
    if missing:
        warnings.append("declared tools are not all callable; readiness must stay partial")

    persona_patch: dict[str, Any] = {
        "id": blueprint.persona_id,
        "display_name": blueprint.display_name,
        "domain": blueprint.domain,
    }
    if blueprint.role is not None:
        persona_patch["role"] = blueprint.role
    config_patch: dict[str, Any] = {
        "persona": persona_patch,
        "toolsets": list(applied_toolsets),
        "capability_blueprint": {
            "schema_version": blueprint.schema_version,
            "template": blueprint.template,
            "domain": blueprint.domain,
            "domain_packs": list(blueprint.domain_packs),
            "operator_exec": blueprint.operator_exec,
            "env_groups": sorted(env_groups),
            "skill_groups": [],
            "skills": sorted(skills),
            "scheduled_authorities": list(blueprint.scheduled_authorities),
        },
        # Explicit empties are load-bearing. They replace stale individual
        # grants during reconcile and record a migrated empty scope without
        # falling through to the deprecated cabinet.tools alias.
        "tools": list(applied_tools),
    }
    if blueprint.model is not None:
        config_patch["model"] = {"preferred": blueprint.model}
    if resolved_mode is ProvisionMode.CREATE:
        # Always-on at creation (issue #422 / epic #418 doctrine — "there
        # shouldn't be no off button"). This is the shared compiler seam
        # for BOTH creation doors (personas.lifecycle.create_profile's
        # clone path and the atomic blueprint provisioner used by plain
        # `thehomie profile create` + dashboard `POST /api/agents`), so a
        # persona compiled with mode=CREATE is born learning regardless of
        # which door it came through. RECONCILE/MIGRATE never touch this
        # key — an operator's later `learning.enabled: false` toggle on a
        # live profile must survive reconcile untouched.
        config_patch["learning"] = {"enabled": True}

    return BlueprintPlan(
        schema_version=blueprint.schema_version,
        persona_id=blueprint.persona_id,
        display_name=blueprint.display_name,
        role=blueprint.role,
        model=blueprint.model,
        template=blueprint.template,
        domain=blueprint.domain,
        mode=resolved_mode.value,
        capability_classes=tuple(classes),
        recommended_toolsets=recommended_toolsets,
        applied_toolsets=applied_toolsets,
        applied_tools=applied_tools,
        preserved_existing_scope=preserve,
        env_groups=tuple(sorted(env_groups)),
        skills=tuple(sorted(skills)),
        integration_requirements=tuple(sorted(integrations)),
        proposal_authorities=tuple(sorted(proposals)),
        channels=blueprint.channels,
        scheduled=scheduled_plans,
        applied_declared_tools=applied_declared_tools,
        declared_tools=declared_tools,
        callable_tools=callable_tuple,
        missing_tools=missing,
        config_patch=config_patch,
        warnings=tuple(warnings),
    )


def _resolve_declared_tools(toolsets: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve structural declarations without consulting live handlers."""

    from runtime.capabilities import resolve_toolset
    from runtime.toolsets import TOOLSETS

    names: set[str] = set()
    for toolset in toolsets:
        names.update(resolve_toolset(toolset, registry=TOOLSETS))
    return tuple(sorted(names))


def _current_scope(
    config: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """Return current effective intent without importing runtime assembly."""

    if not isinstance(config, dict):
        return (), (), False
    if "toolsets" in config or "tools" in config:
        return (
            _loose_string_tuple(config.get("toolsets")),
            _loose_string_tuple(config.get("tools")),
            True,
        )
    cabinet = config.get("cabinet")
    if isinstance(cabinet, dict) and "tools" in cabinet:
        return (), _loose_string_tuple(cabinet.get("tools")), True
    return (), (), False


def _reject_unknown(
    value: dict[str, Any],
    allowed: frozenset[str],
    path: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise BlueprintError(f"{path} has unknown field(s): {', '.join(unknown)}")


def _required_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BlueprintError(f"{path} must be a mapping")
    return value


def _required_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BlueprintError(f"{path} must be a non-empty string")
    return value.strip()


def _bounded_text(value: Any, path: str, *, max_length: int) -> str:
    cleaned = _required_text(value, path)
    if len(cleaned) > max_length:
        raise BlueprintError(f"{path} must be at most {max_length} characters")
    if "\x00" in cleaned:
        raise BlueprintError(f"{path} must not contain NUL characters")
    return cleaned


def _optional_bounded_text(
    value: Any,
    path: str,
    *,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, path, max_length=max_length)


def _string_tuple(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BlueprintError(f"{path} must be a list")
    cleaned: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise BlueprintError(f"{path}[{index}] must be a non-empty string")
        cleaned.append(item.strip())
    if len(cleaned) != len(set(cleaned)):
        raise BlueprintError(f"{path} must not contain duplicates")
    return tuple(cleaned)


def _loose_string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "BUILTIN_TEMPLATES",
    "DOMAIN_PACKS",
    "MANIFEST_FILENAME",
    "SCHEMA_VERSION",
    "BlueprintError",
    "BlueprintPlan",
    "CapabilityClass",
    "ChannelIntent",
    "DomainPack",
    "PersonaBlueprint",
    "ProvisionMode",
    "ScheduledAuthorityPlan",
    "build_builtin_blueprint",
    "compile_blueprint",
    "parse_blueprint",
    "template_catalog",
]
