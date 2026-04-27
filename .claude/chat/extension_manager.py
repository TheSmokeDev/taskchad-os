"""Extension manager — registry-driven command and intent architecture.

Replaces the static COMMANDS list and hardcoded elif dispatch chain with a
dynamic registry. Supports core commands, bundled extensions, and user-global
extensions via manifest-first discovery.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DESC_STOPWORDS = {
    "a", "an", "and", "by", "for", "from", "in", "of", "on", "or",
    "the", "to", "via", "with",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CommandSpec:
    """A registered command (core or extension)."""

    name: str
    description: str
    type: str  # "router" or "engine"
    min_role: str  # "viewer", "operator", "admin"
    handler: Callable | None = None  # None until lazy-loaded
    handler_ref: str = ""  # "module:function" — for lazy loading
    extension_id: str | None = None  # None for core commands
    category: str = ""  # For help grouping


@dataclass
class CommandCollision:
    """Operator-facing details about a rejected command collision."""

    command_name: str
    existing_owner: str
    kind: str  # "possible_duplicate" | "name_conflict"
    summary: str
    guidance: str
    suggested_name: str | None = None


class CommandCollisionError(ValueError):
    """Raised when a command name collides with an existing command."""

    def __init__(self, collision: CommandCollision) -> None:
        super().__init__(f"Duplicate command /{collision.command_name}: {collision.summary}")
        self.collision = collision


@dataclass
class IntentSpec:
    """A data-intent mapping keywords to a router command."""

    command: str  # Target command name
    keywords: list[str] = field(default_factory=list)
    included_in_brief: bool = False
    extension_id: str | None = None


@dataclass
class ExtensionMeta:
    """Metadata about a discovered extension."""

    id: str
    name: str
    version: str
    description: str
    path: Path
    source: str  # "bundled", "local", "global", "configured"
    enabled: bool = True
    status: str = "loaded"  # "loaded", "disabled", "error", "missing_env", "partial"
    error: str | None = None
    commands: list[CommandSpec] = field(default_factory=list)
    intents: list[IntentSpec] = field(default_factory=list)
    env_requirements: list[dict] = field(default_factory=list)
    missing_env: list[str] = field(default_factory=list)
    command_collisions: list[CommandCollision] = field(default_factory=list)
    load_issues: list[str] = field(default_factory=list)
    blocked_commands: set[str] = field(default_factory=set)


# Role hierarchy — same as router.py
ROLE_LEVEL: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}


# ---------------------------------------------------------------------------
# Extension Manager
# ---------------------------------------------------------------------------

class ExtensionManager:
    """Central registry for commands, intents, and extensions.

    Replaces the static COMMANDS list and frozen regex/set globals.
    Supports dynamic registration from core code and external extensions.
    """

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}  # name -> spec
        self._intents: list[IntentSpec] = []
        self._extensions: dict[str, ExtensionMeta] = {}
        self._command_regex: re.Pattern | None = None  # None = needs recompile
        self._handler_cache: dict[str, Callable] = {}  # handler_ref -> loaded fn

        # Analysis signals — same as the original router
        self._analysis_signals: list[str] = [
            "how are", "how's", "how're", "how we", "how is",
            "summary", "summarize", "brief me", "briefing", "overview",
            "prioritize", "recommend", "analyze", "analysis",
            "good morning", "morning", "gm,",
            "tell me", "what should", "what do you think",
            "across all", "across the board",
            "catch me up", "fill me in", "update me", "debrief",
            "status report", "give me a rundown",
            "i got paid", "got paid", "allocate",
            "remember", "what do you know about", "recall",
            "what have we", "what did we", "last time", "history of",
            "tell me about", "what happened with",
        ]

        # Broad query signals — trigger returning all brief intents
        self._broad_query_signals: list[str] = [
            "across all", "all boards", "across the board",
            "how are we looking", "how's everything", "how is everything",
            "status update", "full status", "everything looking",
            "give me a rundown", "catch me up", "fill me in",
        ]

        # Allow/deny lists (set via config)
        self._allow_list: list[str] = []  # empty = allow all
        self._deny_list: list[str] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_command(self, spec: CommandSpec) -> None:
        """Register a command. Raises ValueError on duplicates."""
        if spec.name in self._commands:
            existing = self._commands[spec.name]
            raise CommandCollisionError(
                self._classify_command_collision(existing, spec),
            )
        self._commands[spec.name] = spec
        self._command_regex = None  # invalidate

    def register_intent(self, spec: IntentSpec) -> None:
        """Register a data intent."""
        self._intents.append(spec)

    def register_core_commands(
        self,
        commands: list[tuple[str, str, str, str]],
        categories: list[tuple[str, list[str]]],
        handlers: dict[str, Callable],
    ) -> None:
        """Bulk-register core commands from the COMMANDS list.

        Args:
            commands: List of (name, description, type, min_role) tuples.
            categories: List of (category_name, [command_names]) for help grouping.
            handlers: Dict of command_name -> handler function for router commands.
        """
        # Build category lookup
        cat_lookup: dict[str, str] = {}
        for cat_name, cmd_names in categories:
            for name in cmd_names:
                cat_lookup[name] = cat_name

        for name, desc, cmd_type, min_role in commands:
            handler = handlers.get(name)
            self.register_command(CommandSpec(
                name=name,
                description=desc,
                type=cmd_type,
                min_role=min_role,
                handler=handler,
                handler_ref="",
                extension_id=None,
                category=cat_lookup.get(name, ""),
            ))

    def register_core_intents(
        self, intents: list[tuple[list[str], str, bool]],
    ) -> None:
        """Register core data intents.

        Args:
            intents: List of (keywords, command_name, included_in_brief).
        """
        for keywords, command, in_brief in intents:
            self.register_intent(IntentSpec(
                command=command,
                keywords=keywords,
                included_in_brief=in_brief,
                extension_id=None,
            ))

    def _tokenize_description(self, description: str) -> set[str]:
        words = re.findall(r"[a-z0-9]+", description.lower())
        return {
            word for word in words
            if len(word) > 2 and word not in _DESC_STOPWORDS
        }

    def _looks_like_same_behavior(
        self, existing: CommandSpec, incoming: CommandSpec,
    ) -> bool:
        if existing.type != incoming.type:
            return False

        existing_desc = existing.description.strip().lower()
        incoming_desc = incoming.description.strip().lower()
        if existing_desc and incoming_desc and existing_desc == incoming_desc:
            return True

        existing_tokens = self._tokenize_description(existing.description)
        incoming_tokens = self._tokenize_description(incoming.description)
        if not existing_tokens or not incoming_tokens:
            return False

        overlap = len(existing_tokens & incoming_tokens)
        similarity = overlap / max(len(existing_tokens), len(incoming_tokens))
        return similarity >= 0.7

    def _suggest_command_name(self, spec: CommandSpec) -> str:
        base = re.sub(r"[^a-z0-9_]+", "_", (spec.extension_id or "ext").lower())
        base = base.strip("_") or "ext"
        candidate = f"{base}_{spec.name}"
        suffix = 2
        while candidate in self._commands:
            candidate = f"{base}_{spec.name}_{suffix}"
            suffix += 1
        return candidate

    def _classify_command_collision(
        self, existing: CommandSpec, incoming: CommandSpec,
    ) -> CommandCollision:
        existing_owner = existing.extension_id or "core"
        if self._looks_like_same_behavior(existing, incoming):
            return CommandCollision(
                command_name=incoming.name,
                existing_owner=existing_owner,
                kind="possible_duplicate",
                summary=(
                    f"/{incoming.name} collides with existing {existing_owner} "
                    f"command and appears to duplicate the same capability."
                ),
                guidance=(
                    "You likely already have this behavior. Keep the existing "
                    "command unless you intentionally need a separate workflow."
                ),
            )

        suggested_name = self._suggest_command_name(incoming)
        return CommandCollision(
            command_name=incoming.name,
            existing_owner=existing_owner,
            kind="name_conflict",
            summary=(
                f"/{incoming.name} collides with existing {existing_owner} "
                f"command but appears to be a different capability."
            ),
            guidance=(
                f"Rename the incoming command to something like /{suggested_name}. "
                "Overrides are not supported in v1."
            ),
            suggested_name=suggested_name,
        )

    def _record_extension_issue(self, ext: ExtensionMeta, message: str) -> None:
        if message not in ext.load_issues:
            ext.load_issues.append(message)
        if not ext.error:
            ext.error = message
        if ext.status == "loaded":
            ext.status = "partial"

    def _record_command_collision(
        self, ext: ExtensionMeta, collision: CommandCollision,
    ) -> None:
        if not any(
            existing.command_name == collision.command_name
            and existing.existing_owner == collision.existing_owner
            for existing in ext.command_collisions
        ):
            ext.command_collisions.append(collision)
        ext.blocked_commands.add(collision.command_name)
        self._record_extension_issue(
            ext,
            (
                f"Command collision /{collision.command_name}: {collision.kind}. "
                f"{collision.guidance}"
            ),
        )

    def _register_extension_command(
        self, ext: ExtensionMeta, spec: CommandSpec,
    ) -> bool:
        try:
            self.register_command(spec)
            return True
        except CommandCollisionError as e:
            logger.warning("Extension %s: %s", ext.id, e.collision.summary)
            self._record_command_collision(ext, e.collision)
        except ValueError as e:
            logger.warning("Extension %s: %s", ext.id, e)
            self._record_extension_issue(ext, str(e))
        return False

    def _register_extension_intent(
        self, ext: ExtensionMeta, intent: IntentSpec,
    ) -> bool:
        if intent.command in ext.blocked_commands:
            collision = next(
                (c for c in ext.command_collisions if c.command_name == intent.command),
                None,
            )
            guidance = (
                collision.guidance if collision
                else "Resolve the command collision before enabling this intent."
            )
            self._record_extension_issue(
                ext,
                f"Skipped intent for /{intent.command}. {guidance}",
            )
            logger.warning(
                "Extension %s: skipping intent for blocked command /%s",
                ext.id, intent.command,
            )
            return False

        if intent.command not in self._commands:
            self._record_extension_issue(
                ext,
                f"Skipped intent for /{intent.command}. Target command is not registered.",
            )
            logger.warning(
                "Extension %s: skipping intent for unknown command /%s",
                ext.id, intent.command,
            )
            return False

        self.register_intent(intent)
        return True

    # ------------------------------------------------------------------
    # Command regex (lazy-compiled)
    # ------------------------------------------------------------------

    @property
    def command_regex(self) -> re.Pattern:
        """Regex matching all registered command names. Recompiles when stale."""
        if self._command_regex is None:
            names = "|".join(re.escape(n) for n in self._commands.keys())
            if names:
                self._command_regex = re.compile(
                    rf"^/({names})\b(.*)", re.IGNORECASE,
                )
            else:
                # No commands — regex that matches nothing
                self._command_regex = re.compile(r"(?!)")
        return self._command_regex

    # ------------------------------------------------------------------
    # Query helpers (replace commands.py functions)
    # ------------------------------------------------------------------

    def get_all_command_names(self) -> list[str]:
        """Return all registered command names."""
        return list(self._commands.keys())

    def get_router_commands(self) -> set[str]:
        """Return set of router-handled command names."""
        return {n for n, s in self._commands.items() if s.type == "router"}

    def get_all_extensions(self) -> list[ExtensionMeta]:
        """Return all registered extensions. Public API for capability aggregator."""
        return list(self._extensions.values())

    def get_engine_command_description(self, name: str) -> str | None:
        """Return description for an engine command, or None."""
        spec = self._commands.get(name)
        if spec and spec.type == "engine":
            return spec.description
        return None

    def get_command_min_role(self, name: str) -> str:
        """Return minimum role for a command. Defaults to 'viewer'."""
        spec = self._commands.get(name)
        return spec.min_role if spec else "viewer"

    def get_help_text(self, user_role: str = "admin") -> str:
        """Return formatted help string grouped by category.

        Filters commands based on user_role.
        """
        user_level = ROLE_LEVEL.get(user_role, 0)

        # Group commands by category, preserving registration order
        seen_categories: list[str] = []
        cat_commands: dict[str, list[CommandSpec]] = {}
        for spec in self._commands.values():
            cat = spec.category or (
                f"Extension: {spec.extension_id}" if spec.extension_id else "Other"
            )
            if cat not in cat_commands:
                cat_commands[cat] = []
                seen_categories.append(cat)
            cat_commands[cat].append(spec)

        lines = ["*Available Commands*\n"]
        for cat in seen_categories:
            specs = cat_commands[cat]
            visible = [
                s for s in specs
                if user_level >= ROLE_LEVEL.get(s.min_role, 0)
            ]
            if not visible:
                continue
            lines.append(f"*{cat}*")
            for s in visible:
                lines.append(f"  /{s.name} — {s.description}")
            lines.append("")

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # Intent detection (replaces router._detect_data_intents)
    # ------------------------------------------------------------------

    def detect_intents(self, text: str) -> list[str]:
        """Detect data-intent commands matching a natural language message.

        Returns list of command names. For broad queries, returns all brief intents.
        """
        text_lower = text.lower()

        # Broad status query → return all brief intents
        if any(sig in text_lower for sig in self._broad_query_signals):
            return self.get_brief_intents()

        detected: list[str] = []
        for intent in self._intents:
            if any(kw in text_lower for kw in intent.keywords):
                if intent.command not in detected:
                    detected.append(intent.command)
        return detected

    def wants_analysis(self, text: str) -> bool:
        """Check if the message wants AI analysis beyond raw data."""
        text_lower = text.lower()
        return any(sig in text_lower for sig in self._analysis_signals)

    def get_brief_intents(self) -> list[str]:
        """Return command names included in the default /brief set."""
        return [i.command for i in self._intents if i.included_in_brief]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        command: str,
        adapter: Any,
        incoming: Any,
        args: str,
        *,
        collect_only: bool = False,
    ) -> str | None:
        """Dispatch a router command to its handler.

        Returns the reply text. If collect_only=True, does NOT send via adapter.
        Returns None if command not found or not a router command.
        """
        spec = self._commands.get(command)
        if not spec or spec.type != "router":
            return None

        # Role check
        user_role = getattr(incoming, "user_role", "admin")
        if ROLE_LEVEL.get(user_role, 0) < ROLE_LEVEL.get(spec.min_role, 0):
            return f"Permission denied: /{command} requires {spec.min_role} role."

        # Lazy-load handler if needed
        if spec.handler is None and spec.handler_ref:
            try:
                spec.handler = self._load_handler(spec.handler_ref, spec.extension_id)
            except Exception as e:
                logger.error("Failed to load handler for /%s: %s", command, e)
                return f"Extension error: failed to load handler for /{command}: {e}"

        if spec.handler is None:
            return f"No handler registered for /{command}."

        try:
            return await spec.handler(adapter, incoming, args, collect_only=collect_only)
        except Exception as e:
            logger.error("Handler error for /%s: %s", command, e)
            return f"Error executing /{command}: {e}"

    def _load_handler(self, handler_ref: str, extension_id: str | None) -> Callable:
        """Lazy-load a handler from a 'module:function' reference.

        For core commands: importlib.import_module(module_name)
        For extensions: importlib.util.spec_from_file_location()
        """
        cache_key = f"{extension_id or 'core'}:{handler_ref}"
        if cache_key in self._handler_cache:
            return self._handler_cache[cache_key]

        if ":" not in handler_ref:
            raise ValueError(
                f"Invalid handler_ref: {handler_ref!r} (expected 'module:function')"
            )

        module_name, func_name = handler_ref.rsplit(":", 1)

        if extension_id and extension_id in self._extensions:
            # Extension handler — load from extension path
            ext = self._extensions[extension_id]
            module_path = ext.path / f"{module_name}.py"
            if not module_path.exists():
                raise FileNotFoundError(f"Handler module not found: {module_path}")

            spec = importlib.util.spec_from_file_location(
                f"ext_{extension_id}_{module_name}", str(module_path),
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot create module spec for {module_path}")

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        else:
            # Core handler — normal import
            module = importlib.import_module(module_name)

        handler = getattr(module, func_name, None)
        if handler is None:
            raise AttributeError(f"Function {func_name!r} not found in module {module_name!r}")

        self._handler_cache[cache_key] = handler
        return handler

    # ------------------------------------------------------------------
    # Extension discovery
    # ------------------------------------------------------------------

    def configure_allow_deny(
        self, allow: list[str] | None = None, deny: list[str] | None = None,
    ) -> None:
        """Set allow/deny lists for extension loading."""
        self._allow_list = allow or []
        self._deny_list = deny or []

    def discover(self, paths: list[Path]) -> list[ExtensionMeta]:
        """Scan paths for extensions and register their commands/intents.

        Discovery order matters — first path wins on duplicate extension IDs.
        Returns list of all discovered extensions (including errored ones).
        """
        discovered: list[ExtensionMeta] = []

        for search_path in paths:
            if not search_path.exists():
                continue
            for ext_dir in sorted(search_path.iterdir()):
                if not ext_dir.is_dir():
                    continue
                manifest_path = ext_dir / "extension.json"
                if not manifest_path.exists():
                    continue

                ext = self._load_extension(ext_dir, manifest_path)
                discovered.append(ext)

        return discovered

    def _load_extension(self, ext_dir: Path, manifest_path: Path) -> ExtensionMeta:
        """Load and validate a single extension from its manifest."""
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return ExtensionMeta(
                id=ext_dir.name,
                name=ext_dir.name,
                version="0.0.0",
                description="",
                path=ext_dir,
                source="unknown",
                enabled=False,
                status="error",
                error=f"Manifest parse error: {e}",
            )

        # Validate required fields
        required = ["id", "name", "version"]
        missing = [f for f in required if f not in raw]
        if missing:
            return ExtensionMeta(
                id=raw.get("id", ext_dir.name),
                name=raw.get("name", ext_dir.name),
                version=raw.get("version", "0.0.0"),
                description=raw.get("description", ""),
                path=ext_dir,
                source="unknown",
                enabled=False,
                status="error",
                error=f"Missing required fields: {missing}",
            )

        ext_id = raw["id"]

        # Check allow/deny
        if self._deny_list and ext_id in self._deny_list:
            return ExtensionMeta(
                id=ext_id,
                name=raw["name"],
                version=raw["version"],
                description=raw.get("description", ""),
                path=ext_dir,
                source="unknown",
                enabled=False,
                status="disabled",
                error="Blocked by deny list",
            )

        if self._allow_list and ext_id not in self._allow_list:
            return ExtensionMeta(
                id=ext_id,
                name=raw["name"],
                version=raw["version"],
                description=raw.get("description", ""),
                path=ext_dir,
                source="unknown",
                enabled=False,
                status="disabled",
                error="Not in allow list",
            )

        # Check if already loaded (first path wins)
        if ext_id in self._extensions:
            return self._extensions[ext_id]

        # Check env requirements
        env_reqs = raw.get("envRequirements", [])
        missing_env = []
        for req in env_reqs:
            if req.get("required") and not os.getenv(req["name"], "").strip():
                missing_env.append(req["name"])

        # Determine enabled status
        enabled_default = raw.get("enabledByDefault", True)
        is_enabled = enabled_default and not missing_env

        ext = ExtensionMeta(
            id=ext_id,
            name=raw["name"],
            version=raw["version"],
            description=raw.get("description", ""),
            path=ext_dir,
            source="unknown",  # caller can override
            enabled=is_enabled,
            status="missing_env" if missing_env else ("loaded" if is_enabled else "disabled"),
            env_requirements=env_reqs,
            missing_env=missing_env,
        )

        # Register commands (v1: router-only)
        for cmd_raw in raw.get("commands", []):
            try:
                cmd_name = cmd_raw.get("name")
                if not cmd_name:
                    logger.warning(
                        "Extension %s: command entry missing 'name', skipping: %s",
                        ext_id, cmd_raw,
                    )
                    continue

                cmd_type = cmd_raw.get("type", "router")
                if cmd_type != "router":
                    logger.warning(
                        "Extension %s: command /%s has type '%s' — v1 only supports 'router', skipping",
                        ext_id, cmd_name, cmd_type,
                    )
                    continue

                spec = CommandSpec(
                    name=cmd_name,
                    description=cmd_raw.get("description", ""),
                    type="router",
                    min_role=cmd_raw.get("minRole", "viewer"),
                    handler=None,  # lazy-loaded
                    handler_ref=cmd_raw.get("handler", ""),
                    extension_id=ext_id,
                    category=f"Extension: {raw['name']}",
                )
                ext.commands.append(spec)

                if is_enabled:
                    self._register_extension_command(ext, spec)
            except Exception as e:
                logger.warning(
                    "Extension %s: malformed command entry, skipping: %s",
                    ext_id, e,
                )

        # Register intents
        for intent_raw in raw.get("dataIntents", []):
            try:
                intent_cmd = intent_raw.get("command")
                if not intent_cmd:
                    logger.warning(
                        "Extension %s: intent entry missing 'command', skipping: %s",
                        ext_id, intent_raw,
                    )
                    continue

                intent = IntentSpec(
                    command=intent_cmd,
                    keywords=intent_raw.get("keywords", []),
                    included_in_brief=intent_raw.get("includedInBrief", False),
                    extension_id=ext_id,
                )
                ext.intents.append(intent)
                if is_enabled:
                    self._register_extension_intent(ext, intent)
            except Exception as e:
                logger.warning(
                    "Extension %s: malformed intent entry, skipping: %s",
                    ext_id, e,
                )

        self._extensions[ext_id] = ext
        logger.info(
            "Extension loaded: %s v%s (%d commands, %d intents) [%s]",
            ext.name, ext.version, len(ext.commands), len(ext.intents), ext.status,
        )
        return ext

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    def enable_extension(self, ext_id: str) -> str:
        """Enable a disabled extension. Returns status message."""
        ext = self._extensions.get(ext_id)
        if not ext:
            return f"Extension '{ext_id}' not found."
        if ext.status == "loaded" and ext.enabled:
            return f"Extension '{ext_id}' is already enabled."
        if ext.missing_env:
            return f"Cannot enable '{ext_id}' - missing env vars: {', '.join(ext.missing_env)}"

        ext.error = None
        ext.command_collisions.clear()
        ext.load_issues.clear()
        ext.blocked_commands.clear()
        ext.status = "loaded"

        # Register commands and intents
        for spec in ext.commands:
            self._register_extension_command(ext, spec)
        for intent in ext.intents:
            self._register_extension_intent(ext, intent)

        ext.enabled = True
        if ext.command_collisions:
            first = ext.command_collisions[0]
            return (
                f"Extension '{ext_id}' enabled with attention needed. "
                f"{first.summary} {first.guidance}"
            )
        return f"Extension '{ext_id}' enabled ({len(ext.commands)} commands)."

    def disable_extension(self, ext_id: str) -> str:
        """Disable an enabled extension. Returns status message."""
        ext = self._extensions.get(ext_id)
        if not ext:
            return f"Extension '{ext_id}' not found."
        if not ext.enabled:
            return f"Extension '{ext_id}' is already disabled."

        # Unregister commands
        for spec in ext.commands:
            registered = self._commands.get(spec.name)
            if registered and registered.extension_id == ext_id:
                self._commands.pop(spec.name, None)
        self._command_regex = None  # invalidate

        # Remove intents
        self._intents = [
            i for i in self._intents if i.extension_id != ext_id
        ]

        ext.enabled = False
        ext.status = "disabled"
        return f"Extension '{ext_id}' disabled."

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_diagnostics(self) -> str:
        """Return a formatted diagnostics report for all extensions."""
        if not self._extensions:
            lines = ["*Extensions*", "", "  No extensions discovered.", ""]
            lines.append(f"Core commands: {len(self._commands)}")
            return "\n".join(lines)

        lines = ["*Extensions*", ""]
        for ext in self._extensions.values():
            status_icon = {
                "loaded": "ON",
                "disabled": "OFF",
                "error": "ERR",
                "missing_env": "ENV",
                "partial": "WARN",
            }.get(ext.status, "?")
            lines.append(
                f"  {status_icon} *{ext.name}* v{ext.version} - "
                f"{len(ext.commands)} cmds"
            )
            if ext.command_collisions:
                for collision in ext.command_collisions:
                    lines.append(
                        f"      Collision /{collision.command_name}: "
                        f"{collision.kind.replace('_', ' ')} vs {collision.existing_owner}"
                    )
                    lines.append(f"      Action: {collision.guidance}")
            elif ext.error:
                lines.append(f"      Error: {ext.error}")
            for issue in ext.load_issues:
                if ext.command_collisions and issue.startswith("Command collision "):
                    continue
                lines.append(f"      Note: {issue}")
            if ext.missing_env:
                lines.append(f"      Missing: {', '.join(ext.missing_env)}")
        lines.append("")
        lines.append(f"Total commands: {len(self._commands)} (core + extensions)")
        return "\n".join(lines)

    def doctor(self) -> str:
        """Run extension health checks. Returns diagnostic report."""
        issues: list[str] = []

        # Check for duplicate command names — core + extension cross-check
        seen_names: dict[str, str] = {}
        # Pre-seed with core commands so extension-vs-core collisions surface
        for name, spec in self._commands.items():
            if spec.extension_id is None:
                seen_names[name] = "core"

        for ext in self._extensions.values():
            if ext.command_collisions:
                for collision in ext.command_collisions:
                    issues.append(
                        f"Command collision /{collision.command_name}: "
                        f"{collision.existing_owner} vs {ext.id} - "
                        f"{collision.guidance}"
                    )
            else:
                for cmd in ext.commands:
                    if cmd.name in seen_names:
                        owner = seen_names[cmd.name]
                        issues.append(
                            f"Command collision /{cmd.name}: "
                            f"{owner} vs {ext.id}"
                        )
                    else:
                        seen_names[cmd.name] = ext.id

        # Check load issues that require operator action
        for ext in self._extensions.values():
            for issue in ext.load_issues:
                if issue.startswith("Command collision "):
                    continue
                issues.append(f"Extension '{ext.id}': {issue}")

        # Check handler files exist
        for ext in self._extensions.values():
            if ext.status in ("error", "partial"):
                if ext.status == "error":
                    issues.append(f"Extension '{ext.id}': {ext.error}")
                continue
            for cmd in ext.commands:
                if cmd.handler_ref and ":" in cmd.handler_ref:
                    module_name = cmd.handler_ref.rsplit(":", 1)[0]
                    handler_path = ext.path / f"{module_name}.py"
                    if not handler_path.exists():
                        issues.append(
                            f"Extension '{ext.id}': handler module missing: {handler_path.name}"
                        )

        # Check env requirements
        for ext in self._extensions.values():
            if ext.missing_env:
                issues.append(
                    f"Extension '{ext.id}': missing env vars: {', '.join(ext.missing_env)}"
                )

        if not issues:
            return (
                f"*Extension Doctor*\n\n"
                f"All {len(self._extensions)} extension(s) healthy.\n"
                f"Total commands: {len(self._commands)}"
            )

        lines = ["*Extension Doctor*", "", f"Found {len(issues)} issue(s):", ""]
        for issue in issues:
            lines.append(f"  - {issue}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Singleton — initialized by main.py, accessed by commands.py
# ---------------------------------------------------------------------------

_manager: ExtensionManager | None = None


def get_manager() -> ExtensionManager:
    """Return the global ExtensionManager singleton. Creates one if needed."""
    global _manager
    if _manager is None:
        _manager = ExtensionManager()
    return _manager


def set_manager(mgr: ExtensionManager) -> None:
    """Set the global ExtensionManager singleton (called by main.py)."""
    global _manager
    _manager = mgr
