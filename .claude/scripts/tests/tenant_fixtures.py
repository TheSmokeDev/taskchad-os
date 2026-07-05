"""Shared helpers for the cross-tenant leak harness (Tenant Isolation v0 — WS1).

The crux of tenant isolation v0 is process-per-tenant: each tenant runs as a
named profile under ``~/.homie/profiles/<name>/`` so ``config.py``'s import-time
singleton (``config.py:40`` — ``personas.get_persona_paths(...)``) resolves every
data root (``MEMORY_DIR``, ``CHAT_DB_PATH``, ``DATABASE_PATH``, ``STATE_DIR``) under
that tenant's own profile root. This module stands up two tmp tenant profiles,
seeds a secret into tenant A across every runtime memory surface, and probes a
surface from a FRESH SUBPROCESS pinned to a given tenant.

Why subprocesses (PRP B4 / test_default_persona_backcompat.py:56-93 pattern):
    ``config.py`` resolves the profile singleton ONCE at import. The parent test
    process has already imported ``config`` (via earlier tests) bound to the
    repo's default profile. Monkeypatching ``config.MEMORY_DIR`` after import does
    NOT reach modules that did ``from config import X``. The only honest way to
    prove "a fresh tenant-B process is blind to tenant-A's secret" is to spawn a
    cold ``python -c`` with the tenant's env pinned.

Named-profile resolution (PRP R2 NM2):
    ``get_active_profile_name()`` only recognizes a NAMED profile when
    ``HOMIE_HOME`` lives under ``Path.home()/.homie/profiles`` (``activity.py:164-175``,
    ``core.py:241``). ``Path.home()`` reads ``USERPROFILE`` on Windows and ``HOME``
    on POSIX. So to make ``tenant-a``/``tenant-b`` resolve as NAMED profiles (not
    ``"custom"``), the subprocess env pins BOTH ``HOME`` and ``USERPROFILE`` to the
    tmp root AND ``HOMIE_HOME`` to ``<tmp>/.homie/profiles/tenant-<x>``. Verified:
    a subprocess so pinned reports ``get_active_profile_name() == "tenant-a"`` and
    ``config.MEMORY_DIR`` resolves under the tmp profile root.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# tests/ -> scripts -> .claude -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "scripts"

# The canary the harness seeds into tenant A and hunts for in tenant B.
TENANT_A_SECRET = "TENANT_A_SECRET_5f3c9b2e_acme_retainer_confidential"

# Surfaces the harness probes. Each maps to a probe snippet run in a subprocess
# pinned to a tenant's profile (see ``probe_surface_subprocess``).
SURFACES = ("recall", "chat_session", "episodes", "working")


def _profile_root(tmp_root: Path, name: str) -> Path:
    """Return the named-profile root for *name* under *tmp_root*.

    Mirrors ``personas.get_persona_paths(name)`` for a named profile:
    ``<home>/.homie/profiles/<name>/``. The harness pins ``Path.home()`` to
    *tmp_root* via the subprocess env, so this is where the config singleton
    will resolve the tenant's data roots.
    """
    return tmp_root / ".homie" / "profiles" / name


def build_tenant_profile(tmp_root: Path, name: str) -> dict[str, Path]:
    """Create the on-disk skeleton for a named tenant profile under *tmp_root*.

    Returns a dict of the profile's data roots (``root``, ``memory``,
    ``chat_db``, ``episodes``, ``working``, ``data``, ``state``) — the same
    layout ``personas.get_persona_paths(name)`` produces for a named profile.
    Directories are created; the chat.db / WORKING.md / MEMORY.md files are
    created lazily by ``seed_secret`` (or left absent for the clean tenant).
    """
    root = _profile_root(tmp_root, name)
    memory = root / "memory"
    data = root / "data"
    state = root / "state"
    episodes = memory / "episodes"
    for d in (memory, data, state, episodes):
        d.mkdir(parents=True, exist_ok=True)
    return {
        "root": root,
        "memory": memory,
        "chat_db": data / "chat.db",
        "episodes": episodes,
        "working": memory / "WORKING.md",
        "data": data,
        "state": state,
    }


def seed_secret(profile_paths: dict[str, Path], secret: str) -> None:
    """Seed *secret* into a tenant across EVERY runtime memory surface.

    1. ``memory/MEMORY.md``        — the durable-memory / recall substrate.
    2. ``data/chat.db``            — a ``messages`` row (chat session history).
    3. ``memory/episodes/*.md``    — an episode narrative file.
    4. ``memory/WORKING.md``       — an Open-Threads bullet (the ``/working`` read).

    Every surface carries *secret* so the leak probe can hunt one canary across
    all four. The shapes mirror what the real pipelines write (episodes
    frontmatter from ``episodes.py:write_episode_from_flush``; WORKING.md
    sections from ``living_memory.py``), but the harness keeps them minimal —
    the test asserts on the canary string, not on parser round-trips.
    """
    memory: Path = profile_paths["memory"]
    chat_db: Path = profile_paths["chat_db"]
    episodes: Path = profile_paths["episodes"]
    working: Path = profile_paths["working"]

    # 1. MEMORY.md (durable memory / recall substrate)
    (memory / "MEMORY.md").write_text(
        f"# Memory\n\n## Active Projects\n- {secret} — do not leak across tenants\n",
        encoding="utf-8",
    )

    # 2. chat.db — a messages row. Schema kept minimal; the probe scans for the
    #    canary in the body, not a full SessionStore round-trip.
    conn = sqlite3.connect(str(chat_db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT, role TEXT, content TEXT, "
            "created_at INTEGER DEFAULT (strftime('%s','now')))"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            ("telegram:tenant:seed", "user", f"remember this: {secret}"),
        )
        conn.commit()
    finally:
        conn.close()

    # 3. episodes/*.md — an episode narrative (status: open).
    (episodes / "2026-06-22-telegram-deadbeef-120000.md").write_text(
        "---\n"
        "tags: [system, memory, living-mind]\n"
        "status: open\n"
        "---\n\n"
        "## Summary\n"
        f"- Operator shared {secret} during the retainer call.\n",
        encoding="utf-8",
    )

    # 4. WORKING.md — an Open-Threads bullet (the /working read surface).
    working.write_text(
        "---\ndate: 2026-06-22\n---\n\n"
        "# WORKING.md — Cross-Session Scratchpad\n\n"
        "## Open Threads\n"
        f"- [2026-06-22] follow up on {secret}\n\n"
        "## Active Hypotheses\n\n"
        "## Unresolved Questions\n\n"
        "## Heartbeat Observations (live)\n\n"
        "## Archived (Cold)\n",
        encoding="utf-8",
    )


# ── Probe snippets (run in a fresh subprocess pinned to a tenant) ───────────
#
# Each snippet resolves the data root via the config singleton (so it proves the
# import-time resolution, not a hand-passed path), scans the tenant's OWN
# resolved root for the canary, and prints ``FOUND`` / ``ABSENT``. The harness
# asserts ABSENT for the cross-tenant (blind) case and FOUND for the negative
# control (same-tenant). Resolving through ``config`` is the load-bearing part:
# it proves a cold tenant-B process, with config fully imported, points its
# every data root at tenant B's profile — never tenant A's.

_PROBE_RECALL = """
import config
from pathlib import Path
root = Path(config.MEMORY_DIR)
hit = False
if root.exists():
    for p in root.rglob("*.md"):
        try:
            if SECRET in p.read_text(encoding="utf-8"):
                hit = True
                break
        except OSError:
            pass
print("FOUND" if hit else "ABSENT")
"""

_PROBE_CHAT_SESSION = """
import sqlite3
import config
from pathlib import Path
db = Path(config.CHAT_DB_PATH)
hit = False
if db.exists():
    conn = sqlite3.connect(str(db))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in names:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(%s)" % t).fetchall()]
            for c in cols:
                rows = conn.execute(
                    "SELECT \\"%s\\" FROM \\"%s\\"" % (c, t)).fetchall()
                if any(SECRET in str(v[0]) for v in rows if v[0] is not None):
                    hit = True
                    break
            if hit:
                break
    finally:
        conn.close()
print("FOUND" if hit else "ABSENT")
"""

_PROBE_EPISODES = """
import config
from pathlib import Path
ep = Path(config.MEMORY_DIR) / "episodes"
hit = False
if ep.exists():
    for p in ep.rglob("*.md"):
        try:
            if SECRET in p.read_text(encoding="utf-8"):
                hit = True
                break
        except OSError:
            pass
print("FOUND" if hit else "ABSENT")
"""

_PROBE_WORKING = """
import config
from pathlib import Path
w = Path(config.MEMORY_DIR) / "WORKING.md"
hit = False
if w.exists():
    try:
        hit = SECRET in w.read_text(encoding="utf-8")
    except OSError:
        pass
print("FOUND" if hit else "ABSENT")
"""

_PROBES = {
    "recall": _PROBE_RECALL,
    "chat_session": _PROBE_CHAT_SESSION,
    "episodes": _PROBE_EPISODES,
    "working": _PROBE_WORKING,
}


def _tenant_env(tmp_root: Path, name: str) -> dict[str, str]:
    """Build a subprocess env pinned to tenant *name*'s named profile.

    Pins ``HOME`` + ``USERPROFILE`` to *tmp_root* (so ``Path.home()`` lands
    there on POSIX and Windows) and ``HOMIE_HOME`` to the profile dir (so the
    resolver classifies it as the NAMED profile *name*, not ``"custom"`` — PRP
    R2 NM2). Drops ``HOMIE_VAULT_DIR`` so a stray override can't redirect
    ``MEMORY_DIR`` away from the profile root.
    """
    env = os.environ.copy()
    env.pop("HOMIE_VAULT_DIR", None)
    env.pop("DATABASE_URL", None)  # force SQLite chat store, not a shared PG
    env["HOME"] = str(tmp_root)
    env["USERPROFILE"] = str(tmp_root)
    env["HOMIE_HOME"] = str(_profile_root(tmp_root, name))
    return env


def probe_surface_subprocess(
    tmp_root: Path,
    name: str,
    surface: str,
    *,
    secret: str = TENANT_A_SECRET,
    timeout: int = 60,
) -> str:
    """Run the *surface* probe in a fresh subprocess pinned to tenant *name*.

    Returns ``"FOUND"`` or ``"ABSENT"``. The probe resolves the data root via
    the config singleton inside the subprocess, so a ``"FOUND"`` means the
    canary is physically reachable from tenant *name*'s resolved root.
    """
    if surface not in _PROBES:
        raise ValueError(f"unknown surface {surface!r}; known: {sorted(_PROBES)}")
    # Prepend a SECRET binding so the snippet can reference it without f-string
    # interpolation hazards inside the embedded sqlite SQL.
    code = f"SECRET = {secret!r}\n" + _PROBES[surface]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SCRIPTS_DIR),
        env=_tenant_env(tmp_root, name),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = result.stdout.strip().splitlines()
    verdict = out[-1] if out else ""
    if verdict not in ("FOUND", "ABSENT"):
        raise AssertionError(
            f"probe[{surface}] for tenant {name} produced no verdict\n"
            f"--- rc={result.returncode} ---\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return verdict


def resolve_memory_dir_subprocess(tmp_root: Path, name: str, *, timeout: int = 60) -> str:
    """Return ``config.MEMORY_DIR`` as resolved in a tenant-pinned subprocess.

    Used to prove the two tenants resolve DISTINCT memory roots (a sanity gate:
    if both tenants resolved the same root the leak harness would be inert).
    """
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.MEMORY_DIR)"],
        cwd=str(_SCRIPTS_DIR),
        env=_tenant_env(tmp_root, name),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"MEMORY_DIR resolution failed for tenant {name}\n{result.stderr}"
        )
    return result.stdout.strip()


def profile_name_subprocess(tmp_root: Path, name: str, *, timeout: int = 60) -> str:
    """Return ``get_active_profile_name()`` as seen by a tenant-pinned subprocess.

    Proves the env pinning selects the NAMED profile (PRP R2 NM2 acceptance):
    the harness asserts this equals *name*, not ``"custom"``.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import personas; print(personas.get_active_profile_name())",
        ],
        cwd=str(_SCRIPTS_DIR),
        env=_tenant_env(tmp_root, name),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"profile-name resolution failed for tenant {name}\n{result.stderr}"
        )
    return result.stdout.strip()
