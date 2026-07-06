"""Autonomous co-founder project orchestrator slice.

Vault-spec projects (``vault/memory/cofounder/*.md``) advanced by a
heartbeat pass that dispatches detached Archon runs, polls the run-state DB,
runs executable completion checks, and pings Telegram only on terminal flips.

Kept import-light on purpose: the heartbeat seam lazy-imports
``cofounder.run_pass``, so this package must never eagerly pull heavy
submodules at import time.
"""
