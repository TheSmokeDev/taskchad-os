"""Runtime overlay capabilities aggregator (PRP-1c).

Surfaces the lane/auth profile registry as discoverable capabilities.
Lives in its own module (NOT in runtime/profiles.py) to avoid the
profiles -> capabilities -> toolsets -> routing -> profiles import cycle.

Module-bottom register_aggregator() call wires this source into the
_AGGREGATORS dispatch dict at first import.
"""
from __future__ import annotations

import os

from runtime import profiles
from runtime.auth_profiles import (
    codex_cli_exists,
    gemini_auth_available,
    resolve_codex_auth_profile,
)


def _aggregate_runtime_overlays() -> list:
    """Aggregate runtime overlay capabilities from profiles.GENERIC_PROVIDER_REGISTRY.

    Returns one Capability per generic-overlay entry plus one for the
    hardcoded claude-native lane (5 total on the canonical setup).

    enabled derivation per auth type:
    - api_key: any of overlay.api_key_env_vars is set in env.
    - codex: codex_cli_exists() -- fast shutil.which proxy, no subprocess.
      Snapshot contract: True means CLI binary is resolvable, NOT that
      subscription auth is currently valid. Use the live auth-availability
      helper from auth_profiles at runtime if a live auth check is needed
      (subprocess cost ~15s).
    - gemini: gemini_auth_available() -- shutil.which() PATH lookup +
      file-existence (~/.gemini/settings.json, oauth_creds.json,
      google_accounts.json) + env var reads (GEMINI_API_KEY, GOOGLE_API_KEY,
      GOOGLE_GENAI_USE_VERTEXAI). No subprocess.
    """
    from runtime.capabilities import Capability

    caps: list = []

    # Claude-native -- not in GENERIC_PROVIDER_REGISTRY, hardcoded branch.
    caps.append(
        Capability(
            id="runtime.overlay.claude",
            display_name="Claude",
            enabled=True,
            source="runtime_overlay",
            extension_id=None,
            description="",
        )
    )

    # Resolve codex auth profile once for the codex_cli_exists() call.
    codex_profile = resolve_codex_auth_profile()

    for canonical, overlay in profiles.GENERIC_PROVIDER_REGISTRY.items():
        if overlay.auth_type == "api_key":
            enabled = any(os.getenv(v) for v in overlay.api_key_env_vars)
        elif overlay.auth_type == "codex":
            enabled = codex_cli_exists(codex_profile.command)
        elif overlay.auth_type == "gemini":
            enabled = gemini_auth_available()
        else:
            enabled = False  # unknown auth type -- fail-closed

        caps.append(
            Capability(
                id=f"runtime.overlay.{canonical}",
                display_name=overlay.display_name,
                enabled=enabled,
                source="runtime_overlay",
                extension_id=None,
                description="",
            )
        )

    return caps


# ---------------------------------------------------------------------------
# PRP-1c: register this aggregator into the capabilities dispatch dict.
# This must remain the LAST module-level statement.
# ---------------------------------------------------------------------------
from runtime.capabilities import register_aggregator  # noqa: E402

register_aggregator("runtime_overlays", _aggregate_runtime_overlays)
