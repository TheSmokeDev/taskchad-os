"""Sync derived cognitive state files into the Obsidian vault.

Obsidian Sync handles file propagation — we just need to write copies
of staging + inference state into the vault directory. Runs post-reflection.
"""

import json
import platform
import shutil
from datetime import datetime
from pathlib import Path

from config import INFERENCE_STATE_FILE, MEMORY_DIR, STAGING_STORE_PATH

_VAULT_STATE_DIR = MEMORY_DIR / "_state"


def sync_state_to_vault() -> dict[str, bool]:
    """Copy staging + inference state files into the vault.

    Returns dict of {filename: success} for each synced file.
    """
    _VAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, bool] = {}

    # Staging store
    if STAGING_STORE_PATH.exists():
        target = _VAULT_STATE_DIR / "memory-candidates.jsonl"
        try:
            shutil.copy2(STAGING_STORE_PATH, target)
            results["memory-candidates.jsonl"] = True
        except Exception:
            results["memory-candidates.jsonl"] = False

    # Inference state
    if INFERENCE_STATE_FILE.exists():
        target = _VAULT_STATE_DIR / "self-model-inferences.json"
        try:
            shutil.copy2(INFERENCE_STATE_FILE, target)
            results["self-model-inferences.json"] = True
        except Exception:
            results["self-model-inferences.json"] = False

    # Write sync manifest
    manifest = {
        "last_sync": datetime.now().isoformat(),
        "machine": platform.node(),
        "files": results,
    }
    (_VAULT_STATE_DIR / "sync-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )

    return results


def restore_state_from_vault() -> dict[str, bool]:
    """Restore state files from vault into local state dir.

    Called on startup if local state files are missing but vault has copies.
    This enables cross-machine state portability via Obsidian Sync.
    """
    results: dict[str, bool] = {}

    # Only restore if local file is MISSING — never overwrite existing local state
    vault_staging = _VAULT_STATE_DIR / "memory-candidates.jsonl"
    if vault_staging.exists() and not STAGING_STORE_PATH.exists():
        try:
            STAGING_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vault_staging, STAGING_STORE_PATH)
            results["memory-candidates.jsonl"] = True
        except Exception:
            results["memory-candidates.jsonl"] = False

    vault_inference = _VAULT_STATE_DIR / "self-model-inferences.json"
    if vault_inference.exists() and not INFERENCE_STATE_FILE.exists():
        try:
            INFERENCE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vault_inference, INFERENCE_STATE_FILE)
            results["self-model-inferences.json"] = True
        except Exception:
            results["self-model-inferences.json"] = False

    return results
