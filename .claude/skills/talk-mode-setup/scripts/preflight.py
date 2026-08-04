"""Talk Mode preflight: report what is ready and what is missing.

Reads physical state only: resolved auth source, sidecar interpreter on
disk, listening ports. Never prints a token, a key, or an OAuth secret:
the auth leg delegates to ``talk_session.talk_status()``, which reports a
source name and nothing else.

Run it from the framework scripts dir so the framework imports resolve::

    cd .claude/scripts
    uv run python ../skills/talk-mode-setup/scripts/preflight.py
    uv run python ../skills/talk-mode-setup/scripts/preflight.py --json
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

#: ``.claude/scripts`` relative to this file (skills/<name>/scripts/preflight.py).
SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
SIDECAR_DIR = SCRIPTS_DIR / "discord_voice"

#: Ports Talk Mode listens on. The sidecar control port is only up mid-session.
PORTS = {
    "orchestration API (mints the session, runs the tools)": 4322,
    "dashboard proxy (serves the /talk page)": 3141,
}

OK, WARN, BLOCK = "ok", "warn", "block"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _check_auth() -> list[dict]:
    """Report which credential wins, and whether a cheaper one was passed over.

    Two separate facts, because they drive different decisions: which source
    the resolver picks, and whether a Codex subscription exists that an API
    key is currently outranking (which silently meters the operator).
    """
    import os

    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from dotenv import load_dotenv

        load_dotenv(SCRIPTS_DIR / ".env", override=True)
    except Exception:
        pass  # .env is optional; real env vars still count.

    try:
        import talk_session
        from runtime import openai_platform_auth
    except Exception as exc:
        return [
            {
                "status": WARN,
                "label": "OpenAI Realtime auth",
                "detail": f"could not import the framework ({exc.__class__.__name__}: {exc})",
                "fix": "Run this from .claude/scripts via `uv run python`, so framework deps resolve.",
            }
        ]

    try:
        status = talk_session.talk_status()
    except Exception as exc:
        return [
            {
                "status": BLOCK,
                "label": "OpenAI Realtime auth",
                "detail": f"talk_status() failed ({exc.__class__.__name__}: {exc})",
                "fix": "Fix the error above, then re-run.",
            }
        ]

    # Codex availability independent of who wins: pass an empty env mapping so
    # the OPENAI_API_KEY leg is skipped and only the Codex leg can answer.
    try:
        codex = openai_platform_auth.openai_platform_auth_status(
            configured_api_key=None, env={}
        )
        codex_available = bool(codex.get("configured"))
    except Exception:
        codex_available = False

    talk_key_set = bool((os.environ.get("TALK_OPENAI_API_KEY") or "").strip())
    env_key_set = bool((os.environ.get("OPENAI_API_KEY") or "").strip())
    # Same truthy parsing the framework uses for the billing directive.
    prefer_codex = (os.environ.get("TALK_PREFER_CODEX_OAUTH") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    if not status.get("configured"):
        return [
            {
                "status": BLOCK,
                "label": "OpenAI Realtime auth",
                "detail": status.get("detail", "no credential resolved"),
                # Under the directive an API key is refused by design, so
                # pointing at one would send the operator somewhere that cannot work.
                "fix": (
                    "Run `codex login` to restore the subscription, or unset "
                    "TALK_PREFER_CODEX_OAUTH to allow a metered API key."
                    if prefer_codex
                    else "Run `codex login` to reuse a ChatGPT subscription at no "
                    "per-minute cost, or set TALK_OPENAI_API_KEY in .claude/scripts/.env."
                ),
                "codexAvailable": codex_available,
                "preferCodex": prefer_codex,
            }
        ]

    source = status["source"]
    winner = {
        "status": OK,
        "label": "OpenAI Realtime auth",
        "detail": f"resolved from {source}: {status['detail']}",
        "source": source,
        "codexAvailable": codex_available,
        "preferCodex": prefer_codex,
        "talkKeySet": talk_key_set,
        "envKeySet": env_key_set,
        "model": status.get("model"),
        "voice": status.get("voice"),
    }
    if status.get("killSwitchVoiceDisabled"):
        winner["status"] = BLOCK
        winner["detail"] += " (but the voice kill switch is DISABLED)"
        winner["fix"] = "Unset HOMIE_KILLSWITCH_VOICE. It is currently refusing every voice call."

    checks = [winner]

    # The metering trap: an API key outranks Codex OAuth in the resolver, so a
    # subscriber with a key set pays per minute without ever being told.
    if codex_available and source != "codex-oauth":
        which = "TALK_OPENAI_API_KEY" if talk_key_set else "OPENAI_API_KEY"
        checks.append(
            {
                "status": WARN,
                "label": "Codex subscription is being passed over",
                "detail": (
                    f"a Codex ChatGPT login is available, but {which} outranks it "
                    "in the resolver, so voice bills per minute against the API key"
                ),
                "fix": (
                    "To ride the subscription instead, set TALK_PREFER_CODEX_OAUTH=true "
                    f"in .claude/scripts/.env. That is voice-scoped, so {which} stays "
                    "available to every other consumer."
                ),
            }
        )
    return checks


def _check_sidecar() -> dict:
    """Whether the interpreter the lifecycle will actually spawn exists.

    Asks ``discord_voice_lifecycle`` for its own answer instead of assuming a
    venv layout. That keeps this correct on both the OS-aware resolver and the
    older Windows-only one, and it turns a framework/platform mismatch into a
    named failure rather than a misleading "run uv sync" when uv sync was
    already run.
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    fix = f"Run `uv sync` in {SIDECAR_DIR.as_posix()}."

    if not SIDECAR_DIR.is_dir():
        return {
            "status": WARN,
            "label": "Discord voice sidecar",
            "detail": "sidecar package not found (Discord voice unavailable; dashboard voice unaffected)",
            "fix": fix,
        }

    win = SIDECAR_DIR / ".venv" / "Scripts" / "python.exe"
    posix = SIDECAR_DIR / ".venv" / "bin" / "python"
    on_disk = next((p for p in (win, posix) if p.is_file()), None)

    try:
        import discord_voice_lifecycle

        expected = discord_voice_lifecycle._sidecar_python()
    except Exception:
        expected = None

    if expected is None:
        # Could not ask the framework; fall back to this platform's layout.
        native = win if sys.platform == "win32" else posix
        if native.is_file():
            return {
                "status": OK,
                "label": "Discord voice sidecar",
                "detail": f"venv present at {native.parent.name}/{native.name}",
            }
        return {
            "status": WARN,
            "label": "Discord voice sidecar",
            "detail": "venv not built yet (dashboard voice works without it)",
            "fix": fix,
        }

    if expected.is_file():
        return {
            "status": OK,
            "label": "Discord voice sidecar",
            "detail": f"venv present at {expected.parent.name}/{expected.name}, the path the lifecycle spawns",
        }
    if on_disk is not None:
        return {
            "status": BLOCK,
            "label": "Discord voice sidecar",
            "detail": (
                f"a venv exists at {on_disk.parent.name}/{on_disk.name} but this framework's "
                f"lifecycle spawns {expected.parent.name}/{expected.name}, so `/talk join` cannot start it"
            ),
            "fix": (
                "Update the framework to a build whose sidecar resolver is OS-aware, "
                "or use dashboard voice here."
            ),
        }
    return {
        "status": WARN,
        "label": "Discord voice sidecar",
        "detail": "venv not built yet (dashboard voice works without it)",
        "fix": fix,
    }


def _check_discord_token() -> dict:
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import config

        token = (config.DISCORD_BOT_TOKEN or "").strip()
    except Exception:
        import os

        token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()

    if token:
        return {
            "status": OK,
            "label": "Discord bot token",
            "detail": "set (value not shown)",
        }
    return {
        "status": WARN,
        "label": "Discord bot token",
        "detail": "not set (Discord voice unavailable; dashboard voice unaffected)",
        "fix": "Set DISCORD_BOT_TOKEN in .claude/scripts/.env.",
    }


def _check_identity_include() -> dict:
    """Whether the voice prompt will carry the operator's identity.

    The code default is SOUL only — the behavioral contract with none of the
    personal context — and the gap is SILENT: status stays ready, audio works,
    the voice is just a stranger. This is the live default-vs-promise gap a
    fresh install actually hits, so it gets a named check instead of a
    tuning-table footnote.
    """
    import os

    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from dotenv import load_dotenv

        load_dotenv(SCRIPTS_DIR / ".env", override=True)
    except Exception:
        pass

    raw = (os.environ.get("TALK_IDENTITY_INCLUDE") or "").strip()
    fix = (
        "Set TALK_IDENTITY_INCLUDE=SOUL,USER,MEMORY,WORKING in "
        ".claude/scripts/.env (the list REPLACES the default — always keep SOUL)."
    )
    label = "Voice identity files"

    if not raw:
        return {
            "status": WARN,
            "label": label,
            "detail": (
                "TALK_IDENTITY_INCLUDE is not set, so voice carries SOUL only — "
                "it will not know who you are or what you're working on"
            ),
            "fix": fix,
        }

    names = {part.strip().upper() for part in raw.split(",") if part.strip()}
    if "SOUL" not in names:
        return {
            "status": WARN,
            "label": label,
            "detail": (
                f"TALK_IDENTITY_INCLUDE={raw} ships NO soul — the list replaces "
                "the default, so the behavioral contract is gone"
            ),
            "fix": fix,
        }
    missing = [n for n in ("USER", "MEMORY") if n not in names]
    if missing:
        return {
            "status": WARN,
            "label": label,
            "detail": (
                f"TALK_IDENTITY_INCLUDE={raw} omits {'/'.join(missing)} — voice "
                "keeps the behavioral contract but will not know who you are"
            ),
            "fix": fix,
        }
    return {
        "status": OK,
        "label": label,
        "detail": f"identity files in the voice prompt: {raw}",
    }


def _check_identity_roots() -> dict | None:
    """Whether the sidecar the lifecycle spawns will resolve the SAME profile.

    The spawn forces the child's HOMIE_HOME to ``_active_profile_root()`` and
    the child re-derives its profile from it. On pre-round-trip-fix builds the
    default profile handed the child the repo root, which reclassified it as a
    "custom" profile rooted at a nonexistent ``<repo>/memory`` — collapsing the
    voice identity prompt to the bare preamble while every status stayed green.
    Same ask-the-module pattern as ``_check_sidecar``: this asks the installed
    lifecycle what it would hand the child, then asks personas what that value
    resolves to, so an old framework build is reported by name.

    Returns ``None`` (check skipped) when the Discord lifecycle is absent —
    dashboard voice does not spawn through this seam.
    """
    import os

    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import discord_voice_lifecycle
        from personas import get_active_profile_name
        from personas.core import get_persona_paths

        parent_profile = get_active_profile_name()
        spawn_home = discord_voice_lifecycle._active_profile_root()

        saved = os.environ.get("HOMIE_HOME")
        try:
            os.environ["HOMIE_HOME"] = str(spawn_home)
            child_profile = get_active_profile_name()
            child_memory = get_persona_paths(child_profile)["memory"]
        finally:
            if saved is None:
                os.environ.pop("HOMIE_HOME", None)
            else:
                os.environ["HOMIE_HOME"] = saved
    except Exception:
        return None  # no Discord lifecycle here; dashboard voice unaffected.

    label = "Voice identity roots (sidecar round-trip)"
    if child_profile != parent_profile:
        return {
            "status": BLOCK,
            "label": label,
            "detail": (
                f"the lifecycle would spawn the sidecar as profile "
                f"'{child_profile}' but this process is '{parent_profile}' — "
                "the voice identity prompt will silently collapse to the bare preamble"
            ),
            "fix": (
                "Update the framework to a build with the HOMIE_HOME round-trip "
                "fix, then restart the orchestration API process (the spawner) — "
                "restarting the chat bot alone is not enough."
            ),
        }
    if not child_memory.is_dir():
        return {
            "status": BLOCK,
            "label": label,
            "detail": (
                f"the sidecar would resolve memory at {child_memory}, which does "
                "not exist — identity files will read empty"
            ),
            "fix": "Fix HOMIE_HOME (or the profile) so the memory dir exists on disk.",
        }
    return {
        "status": OK,
        "label": label,
        "detail": f"sidecar round-trips to profile '{child_profile}' with memory at {child_memory}",
    }


def _check_ports() -> list[dict]:
    checks = []
    for label, port in PORTS.items():
        up = _port_open(port)
        checks.append(
            {
                "status": OK if up else WARN,
                "label": f"port {port}: {label}",
                "detail": "listening" if up else "not listening",
                **({} if up else {"fix": "Start it before opening the Talk page (see SKILL.md step 2)."}),
            }
        )
    return checks


def run() -> list[dict]:
    checks = [
        *_check_auth(),
        _check_identity_include(),
        *_check_ports(),
        _check_discord_token(),
        _check_sidecar(),
    ]
    roots = _check_identity_roots()
    if roots is not None:
        checks.append(roots)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Talk Mode preflight check.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    # Framework error strings may carry non-ASCII; a cp1252 console must not
    # turn a diagnostic into a UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    checks = run()
    blocked = [c for c in checks if c["status"] == BLOCK]

    if args.json:
        print(json.dumps({"ok": not blocked, "checks": checks}, indent=2))
        return 1 if blocked else 0

    glyph = {OK: "[ OK ]", WARN: "[WARN]", BLOCK: "[FAIL]"}
    print("Talk Mode preflight\n")
    for check in checks:
        print(f"{glyph[check['status']]} {check['label']}")
        print(f"        {check['detail']}")
        if check.get("fix") and check["status"] != OK:
            print(f"        fix: {check['fix']}")
    print()
    if blocked:
        print(f"{len(blocked)} blocking issue(s). Resolve the fix lines above.")
    else:
        warns = [c for c in checks if c["status"] == WARN]
        print(
            f"Dashboard voice is ready to start. {len(warns)} optional item(s) not configured."
            if warns
            else "All checks passed."
        )
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
