"""Native design capability for The Homie.

Ports the *design power* of Open Design (nexu-io/open-design, Apache-2.0) onto
The Homie's own runtime: the taste loop (brief -> direction-lock -> artifact ->
critique) runs through ``runtime.lane_router.run_with_runtime_lanes`` — lane-first
routing, so the resolved lane's fallback applies (the generic lane's tool route
is codex -> gemini; ``claude_native`` is a separate lane selected/pinned
explicitly). The brief is lane-agnostic, so design quality survives whichever
lane runs it. Uses harvested design directions + bundled ``DESIGN.md`` brand
systems + the de-ai-slop anti-AI-slop discipline. No external design daemon, no install.

Slice ownership: this module owns *design domain logic* (direction library,
brand-system loading, brief assembly, artifact paths). The chat slice
(``.claude/chat/core_handlers.py::handle_design``) owns the ``/design`` command
and delegates generation to the runtime slice. Artifacts persist to the
sanitizer-firewalled vault substrate at ``vault/memory/design/``.

Attribution: the direction library, anti-slop method, and bundled DESIGN.md
systems are adapted from nexu-io/open-design (Apache-2.0). See
``THIRD-PARTY-NOTICES.md`` in this directory.
"""

from __future__ import annotations

from .artifacts import artifact_dir, design_root, slugify
from .brief import build_design_brief
from .directions import DESIGN_DIRECTIONS, find_direction, pick_direction, render_direction_spec
from .systems import (
    DesignSystemPackage,
    list_systems,
    load_system,
    render_system_block,
    summarize_components_manifest,
)

__all__ = [
    "DESIGN_DIRECTIONS",
    "DesignSystemPackage",
    "artifact_dir",
    "build_design_brief",
    "design_root",
    "find_direction",
    "list_systems",
    "load_system",
    "pick_direction",
    "render_direction_spec",
    "render_system_block",
    "slugify",
    "summarize_components_manifest",
]
