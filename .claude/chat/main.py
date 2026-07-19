"""
Multi-platform chat interface for The Homie.

Usage:
    cd .claude/scripts && uv run python ../chat/main.py
    cd .claude/scripts && uv run python ../chat/main.py --test       # Dry run
    cd .claude/scripts && uv run python ../chat/main.py --telegram   # Telegram only
    cd .claude/scripts && uv run python ../chat/main.py --slack      # Slack only
    cd .claude/scripts && uv run python ../chat/main.py --relay      # Relay only (no Telegram/Slack)
"""

from __future__ import annotations


import argparse
import asyncio
import atexit
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# Add both chat dir and scripts dir to path for imports
_CHAT_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _CHAT_DIR.parent / "scripts"
sys.path.insert(0, str(_CHAT_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from engine import ConversationEngine  # noqa: E402
from router import ChatRouter  # noqa: E402
from session import get_session_store  # noqa: E402

from config import (  # noqa: E402
    CHAT_ALLOWED_USERS,
    CHAT_DB_PATH,
    CHAT_MAX_BUDGET_USD,
    CHAT_MAX_TURNS,
    DISCORD_ALLOWED_GUILDS,
    DISCORD_ALLOWED_USERS,
    DISCORD_BOT_TOKEN,
    DISCORD_WATCH_ALL_GUILD_CHANNELS,
    EXTENSIONS_ALLOW,
    EXTENSIONS_BUNDLED_PATH,
    EXTENSIONS_DENY,
    EXTENSIONS_ENABLED,
    EXTENSIONS_EXTRA_PATH,
    HEALTH_CHECK_PORT,
    OPENAI_API_KEY,
    PROJECT_ROOT,
    SLACK_APP_TOKEN,
    SLACK_BOT_TOKEN,
    TELEGRAM_ALLOWED_USER_IDS,
    TELEGRAM_BOT_TOKEN,
    VOICE_STT_MODEL,
    VOICE_TTS_ENGINE,
    VOICE_TTS_VOICE_EDGE,
    VOICE_TTS_VOICE_OPENAI,
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_VERIFY_TOKEN,
    WHATSAPP_WEBHOOK_PORT,
)
from shared import (  # noqa: E402
    append_to_daily_log,
    cleanup_all_bot_processes,
    remove_pid,
    write_pid,
)

# GlitchTip/Sentry error tracking — covers runtime crashes (import-time failures not covered)
try:
    import sentry_sdk
    _dsn = os.getenv("SENTRY_DSN")
    if _dsn:
        sentry_sdk.init(
            dsn=_dsn,
            traces_sample_rate=0.3,
            environment=os.getenv("SENTRY_ENVIRONMENT", "local"),
            release="thehomie-1.0",
        )
except Exception:
    pass


def _is_bot_process_alive() -> bool:
    """Check if any bot process is actually running (not just a stale mutex).

    PRP-7c Phase 3: routes through ``personas.services.get_bot_pid_path()``
    so the pid path follows the active profile. Liveness check delegates to
    ``shared.is_pid_alive()`` (canonical cross-platform helper).
    """
    from personas import services as _services
    from shared import is_pid_alive
    pid_file = _services.get_bot_pid_path()
    if not pid_file.exists():
        return False
    try:
        old_pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return False
    if old_pid == os.getpid():
        return False
    return is_pid_alive(old_pid)


def _acquire_instance_lock() -> bool:
    """Ensure only one bot instance runs at a time.

    On Windows, uv's venv python.exe spawns a child python.exe — both
    execute the script. File locks are inherited by child processes, so
    we use a Windows named mutex instead (not inherited by default).
    On Unix, use fcntl file locking.

    If the mutex is held but no bot process is alive, force-release it
    (handles orphaned mutexes from crashes).

    PRP-7c Phase 3: mutex name and lock path are profile-scoped via
    ``personas.services.get_bot_mutex_name()`` / ``get_bot_lock_path()`` so
    two profiles can run their bots simultaneously without colliding.
    """
    from personas import services as _services
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # Profile-scoped mutex (default profile preserves the legacy
            # ``Global\\SecondBrainTelegramBot`` name; named profiles get
            # a hashed-stable variant).
            mutex_name = _services.get_bot_mutex_name()
            handle = kernel32.CreateMutexW(None, True, mutex_name)
            ERROR_ALREADY_EXISTS = 183
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                # Check if the holder is actually alive
                if _is_bot_process_alive():
                    return False  # Real instance running
                # Orphaned mutex — no bot process alive. Force acquire.
                print(f"[{datetime.now()}] Orphaned mutex detected — no bot process alive, forcing acquisition")
                # The mutex auto-releases when the dead process's handle is gone.
                # Re-create it — this time we should get it.
                handle = kernel32.CreateMutexW(None, True, mutex_name)
                if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                    # Still held (OS hasn't released the dead handle yet).
                    # Wait briefly for OS cleanup, then proceed anyway.
                    kernel32.CloseHandle(handle)
                    time.sleep(2)
                    handle = kernel32.CreateMutexW(None, True, mutex_name)
                    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                        kernel32.CloseHandle(handle)
                        print(f"[{datetime.now()}] WARNING: Could not acquire mutex after orphan detection — proceeding anyway")
                        return True  # No real bot running, safe to proceed
            globals()["_mutex_handle"] = handle
            return True
        else:
            import fcntl
            lock_path = _services.get_bot_lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            _lock_fh = open(lock_path, "w")
            try:
                fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _lock_fh.write(str(os.getpid()))
                _lock_fh.flush()
                globals()["_lock_fh"] = _lock_fh
                return True
            except OSError:
                _lock_fh.close()
                return False
    except Exception:
        return True  # If locking fails entirely, proceed anyway


def _shutdown_handler(signum: int, frame: object) -> None:
    """Handle SIGTERM/SIGINT for clean shutdown with PID cleanup."""
    print(f"\n[{datetime.now()}] Received signal {signum}, shutting down...")
    append_to_daily_log("Bot stopped (signal)", "Bot Lifecycle")
    # Clean shutdown clears the restart-loop boot log so a legitimate operator
    # restart never accumulates toward a false trip. Best-effort — a broken
    # breaker must never block shutdown.
    try:
        from orchestration import restart_loop_guard
        restart_loop_guard.clear()
    except Exception:
        pass
    try:
        from runtime.langfuse_setup import flush_langfuse
        flush_langfuse()
    except Exception:
        pass
    sys.exit(0)  # atexit handles remove_pid()


def _flush_telegram_session(token: str) -> None:
    """Force-reset Telegram's server-side getUpdates session.

    After killing previous bot processes, their long-poll session can linger
    on Telegram's servers for up to 30s. A quick getUpdates call with timeout=0
    claims the session immediately, preventing Conflict errors.
    """
    import urllib.request
    # Wait for killed processes' TCP connections to fully close
    time.sleep(3)
    # Call getUpdates to claim the session from any lingering server-side poll
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?offset=-1&timeout=0"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            print("Flushed Telegram polling session")
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"Warning: could not flush Telegram session: {e}")


def _run_test_hold(unlock_path: Path) -> None:
    """F3 — test-only "hold open" mode for the two-bot integration test.

    Enters the SAME instance-lock + write_pid + cleanup_all_bot_processes path
    as production startup, then BLOCKS on a sentinel file (or SIGTERM) instead
    of polling Telegram. This lets ``test_two_bots_run_simultaneously_on_windows``
    actually observe two bots holding their locks simultaneously — the
    previous ``--test`` mode exited before adapter polling, which would pass
    even if the second bot's startup killed the first.

    Block condition: poll for ``unlock_path`` once per 200ms. The test creates
    the file when it's done with the bot. SIGTERM/SIGINT triggers
    ``_shutdown_handler`` which calls ``sys.exit(0)`` — atexit removes the
    pid file. Keep it dead-simple — no async, no engine, no adapters.
    """
    print(f"[{datetime.now()}] --test-hold mode: holding lock + pid file. "
          f"Waiting for unlock sentinel: {unlock_path}", flush=True)
    while True:
        if unlock_path.exists():
            print(f"[{datetime.now()}] --test-hold: sentinel detected, exiting cleanly.",
                  flush=True)
            return
        time.sleep(0.2)


def _boot_breaker_decision(killed: object, *, guard: object = None) -> bool:
    """Return True iff boot-time state restore should be SKIPPED.

    Records this boot ONLY when there is a respawn signal (``killed`` is a
    non-empty list from ``cleanup_all_bot_processes``), then reports whether
    the restart-loop breaker has tripped. Fail-open at every seam: a broken
    breaker must NEVER wedge boot — on any error (including the M2 NameError
    class if ``config`` were referenced without import) it returns False so
    the normal state restore proceeds. ``guard`` is injectable for tests.
    """
    if not killed:  # no respawn signal → never record/trip
        return False
    try:
        import config  # M2: main.py imports names from config, not the module
        from orchestration import restart_loop_guard as _g

        g = guard or _g
        if g.check_and_record():  # records this boot, reports tripped
            print(
                "[WARNING] Restart-loop breaker TRIPPED — skipping boot-time "
                "state restore to break a suspected respawn loop. Delete "
                f"{config.STATE_DIR / 'restart_loop.json'} if false positive.",
                flush=True,
            )
            append_to_daily_log(
                "Restart-loop breaker TRIPPED (rapid respawn)", "Bot Lifecycle"
            )
            return True
    except Exception:  # a broken breaker must NEVER wedge boot
        return False
    return False


def _make_webhook_adapter_resolver(router: object):
    """Platform-value -> sibling adapter resolver for the webhook adapter.

    Guards ``Platform(...)`` construction — an unknown platform string returns
    ``None`` instead of raising into the delivery path. The closure reads
    ``router.adapters`` live, so adapters registered after the webhook adapter
    are still resolvable.
    """
    from models import Platform

    def _resolve(platform_value: str) -> object | None:
        try:
            platform = Platform(platform_value)
        except ValueError:
            return None
        return router.adapters.get(platform)

    return _resolve


async def _mc_heartbeat_loop(
    url: str,
    api_key: str,
    status_fn: Callable[[], str] | None = None,
    interval: int = 300,
) -> None:
    """POST to the MC heartbeat endpoint every `interval` seconds.

    The payload is rebuilt EVERY iteration from live liveness state. It used
    to be a constant ``{"status": "online"}`` encoded once before the loop —
    which is why Mission Control cheerfully reported the bot "online" for the
    entire six weeks it was wedged. A heartbeat that cannot say "I am sick"
    is not a health signal, it is a liveness signal for the loop itself.

    The blocking POST runs OFF the event loop via ``asyncio.to_thread`` and the
    per-tick version read + POST share ONE try/except, so a slow/half-open
    endpoint can no longer freeze the shared loop and a transient
    ``get_current_version`` failure can no longer permanently kill this task
    (event-loop-wedge class, #130). Module-level so it is importable in tests.
    """
    import urllib.request
    import json as _json

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    def _post_heartbeat(payload: bytes) -> None:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    while True:
        try:
            state = status_fn() if status_fn else "online"
        except Exception:  # noqa: BLE001 — never let reporting kill the loop
            state = "online"
        try:
            from update_check import get_current_version

            payload = _json.dumps(
                {"status": state, "version": get_current_version()}
            ).encode()
            await asyncio.to_thread(_post_heartbeat, payload)
        except Exception as e:
            print(f"[heartbeat] POST failed: {e}")
        await asyncio.sleep(interval)


class FatalTaskExit(RuntimeError):
    """A fatal task stopped serving WITHOUT crashing.

    Raised by _supervise_tasks when a task in fatal_tasks returns normally
    (e.g. the router after every listener gave up). Escapes asyncio.run into
    main()'s catch-all so the process exits non-zero with a flushed Langfuse
    trace — a bot that cannot serve must never exit 0 (#134).
    """


async def _supervise_tasks(
    tasks: dict[str, asyncio.Task],
    fatal_tasks: set[str],
) -> None:
    """Monitor bot tasks; non-fatal deaths are logged, fatal endings escalate.

    Promoted from a closure inside main() so tests exercise the real code
    (same move as _mc_heartbeat_loop, #130). Contract:
    - Non-fatal task crash/return: log and keep serving.
    - Fatal task crash: cancel the rest and re-raise — ANY exception, not
      just AdapterWedgedError. `return` here exits 0 and every external
      restarter reads that as a clean shutdown (#134).
    - Fatal task normal return: cancel the rest and raise FatalTaskExit —
      a router that gave up on all adapters is alive-but-deaf (#134).
    - Fatal task cancelled: benign — cancellation is an external stop
      (shutdown paths), not a failure.
    """
    while tasks:
        done, _ = await asyncio.wait(
            tasks.values(), return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            name = next((k for k, v in tasks.items() if v is task), "unknown")
            tasks.pop(name, None)
            exc = task.exception() if not task.cancelled() else None
            if exc:
                print(f"[{datetime.now()}] FATAL: Task '{name}' crashed: {type(exc).__name__}: {exc}", flush=True)
                append_to_daily_log(f"Bot task '{name}' crashed: {exc}", "Bot Lifecycle")
                if name in fatal_tasks:
                    print(f"[{datetime.now()}] '{name}' died — shutting down bot", flush=True)
                    for remaining in tasks.values():
                        remaining.cancel()
                    # ANY exception from a fatal task must exit NON-ZERO so the
                    # watchdog / service supervisor restarts a clean process.
                    # `return` here exits 0 and every external restarter reads
                    # that as a clean shutdown (#134).
                    raise exc
            elif name in fatal_tasks and not task.cancelled():
                # A fatal task that RETURNED (no exception, not cancelled) is
                # alive-but-deaf: the router gave up on every adapter yet the
                # process keeps serving /health. Escalate so it exits 1 (#134).
                print(f"[{datetime.now()}] FATAL: Task '{name}' stopped serving (returned) — shutting down bot", flush=True)
                append_to_daily_log(f"Bot task '{name}' stopped serving (returned)", "Bot Lifecycle")
                for remaining in tasks.values():
                    remaining.cancel()
                raise FatalTaskExit(f"fatal task '{name}' returned — bot cannot serve")
            else:
                print(f"[{datetime.now()}] Task '{name}' completed normally", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="The Homie Chat Interface")
    parser.add_argument("--test", action="store_true", help="Dry run — print config and exit")
    parser.add_argument(
        "--test-hold",
        metavar="UNLOCK_PATH",
        default=None,
        # Rec 1 (R2) — hide from production --help output. This flag is
        # exclusively used by ``test_two_bots_run_simultaneously_on_windows``
        # to acquire the instance lock + pid file then block on a sentinel.
        # Operators have no reason to invoke it; surfacing it in --help just
        # invites confusion ("what does --test-hold do?"). The test still
        # passes the flag explicitly, so SUPPRESS only affects help rendering.
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--telegram", action="store_true", help="Start Telegram adapter only")
    parser.add_argument("--slack", action="store_true", help="Start Slack adapter only")
    parser.add_argument("--relay", action="store_true", help="Start relay WebSocket client only")
    parser.add_argument("--discord", action="store_true", help="Start Discord adapter only")
    parser.add_argument("--whatsapp", action="store_true", help="Start WhatsApp adapter only")
    parser.add_argument("--webhook", action="store_true", help="Start webhook ingress adapter only")
    args = parser.parse_args()

    # Instance lock — prevents Windows venv double-spawn from running two polling loops
    if not _acquire_instance_lock():
        print(f"[{datetime.now()}] Another bot instance holds the lock — exiting duplicate (PID {os.getpid()})")
        sys.exit(0)

    # PID lifecycle — kill ALL bot-related processes (including service.py wrappers)
    killed = cleanup_all_bot_processes()
    if killed:
        print(f"Killed {len(killed)} stale bot process(es): {killed}")
        # NOTE: _flush_telegram_session disabled — it races with start_polling()
        # and causes "Conflict" errors. The adapter's drop_pending_updates=True
        # handles stale updates. If old sessions linger, they expire in ~30s.
    write_pid()
    atexit.register(remove_pid)

    # F3 — --test-hold short-circuits AFTER the lock + write_pid + cleanup
    # path so the integration test exercises the real lifecycle contract.
    # Register signal handlers FIRST so SIGTERM cleanup works during hold.
    if args.test_hold:
        signal.signal(signal.SIGTERM, _shutdown_handler)
        if sys.platform != "win32":
            signal.signal(signal.SIGINT, _shutdown_handler)
        unlock_path = Path(args.test_hold)
        _run_test_hold(unlock_path)
        return

    # Restart-loop breaker — record this boot on a respawn signal and decide
    # whether to skip state restore. Adapters still start regardless; the
    # breaker only skips the restore that keeps replaying a fatal path.
    _breaker_tripped = _boot_breaker_decision(killed)

    # Move 5a: Restore cognitive state from vault on startup
    try:
        if not _breaker_tripped:
            from state_sync import restore_state_from_vault
            restored = restore_state_from_vault()
            if restored:
                print(f"  State restored from vault: {restored}")
        else:
            print("  State restore skipped (restart-loop breaker tripped)")
    except Exception as e:
        print(f"  State restore skipped: {e}")

    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    # If no specific flag is set, start all configured adapters
    start_all = not (
        args.telegram or args.slack or args.relay or args.discord or args.whatsapp
        or args.webhook
    )

    has_slack = bool(SLACK_BOT_TOKEN and SLACK_APP_TOKEN)
    has_telegram = bool(TELEGRAM_BOT_TOKEN)
    has_discord = bool(DISCORD_BOT_TOKEN)
    has_whatsapp = bool(WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID)

    # Webhook ingress (hermes-v18 Phase 4) — dormant by default: the adapter
    # is only ever CONSTRUCTED when WEBHOOK_ROUTES yields at least one valid
    # route (Rule-1 call-time resolver; unset/malformed env -> routes == {}).
    from config import get_webhook_settings
    webhook_settings = get_webhook_settings()
    has_webhook = bool(webhook_settings.routes)

    # PRP-7c R1 B2 — refuse to start when another profile is configured with
    # the SAME Telegram bot token. Telegram allows ONE polling process per
    # token; the second bot would crash with HTTP 409 Conflict. Detect at
    # startup so the operator gets a clear error instead of a confusing 409.
    if has_telegram and (start_all or args.telegram):
        from personas import services as _services
        collision = _services.detect_telegram_token_collision(TELEGRAM_BOT_TOKEN)
        if collision:
            print(
                f"ERROR: Telegram bot token collision detected — profile '{collision}' "
                f"is configured with the same TELEGRAM_BOT_TOKEN as the active profile. "
                f"Telegram only allows ONE polling process per token. Refusing to start.",
                file=sys.stderr,
            )
            print(
                f"Fix: edit '{collision}'/.env or this profile's .env so each profile "
                f"uses a distinct TELEGRAM_BOT_TOKEN.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Relay config -- read from env
    relay_ws_url = os.getenv("RELAY_WS_URL", "")
    relay_auth_token = os.getenv("RELAY_AUTH_TOKEN", "")
    has_relay = bool(relay_auth_token)

    # MC heartbeat config
    mc_heartbeat_url = os.getenv("MC_HEARTBEAT_URL", "")
    mc_agent_api_key = os.getenv("MC_AGENT_API_KEY", "")
    has_heartbeat = bool(mc_heartbeat_url and mc_agent_api_key)

    # Print startup banner
    print(f"\n{'=' * 60}")
    print("The Homie Chat Interface")
    print(f"{'=' * 60}")
    print(f"  Project root:  {PROJECT_ROOT}")
    print(f"  Database:      {CHAT_DB_PATH}")
    print(f"  Max turns:     {CHAT_MAX_TURNS}")
    print(f"  Max budget:    ${CHAT_MAX_BUDGET_USD:.2f}")

    if has_slack:
        print(f"  Slack:         {SLACK_BOT_TOKEN[:12]}...")
    else:
        print("  Slack:         not configured")

    if has_telegram:
        print(f"  Telegram:      {TELEGRAM_BOT_TOKEN[:12]}...")
        if TELEGRAM_ALLOWED_USER_IDS:
            print(f"  TG users:      {TELEGRAM_ALLOWED_USER_IDS}")
        else:
            print("  TG users:      (open — set TELEGRAM_ALLOWED_USER_IDS to restrict)")
    else:
        print("  Telegram:      not configured")

    if has_relay:
        print(f"  Relay WS:      {relay_ws_url}")
    else:
        print("  Relay WS:      not configured (set RELAY_AUTH_TOKEN to enable)")

    if has_discord:
        print(f"  Discord:       {DISCORD_BOT_TOKEN[:12]}...")
    else:
        print("  Discord:       not configured")

    if has_whatsapp:
        print(f"  WhatsApp:      phone {WHATSAPP_PHONE_NUMBER_ID}")
    else:
        print("  WhatsApp:      not configured")

    if has_webhook:
        print(
            f"  Webhook:       {len(webhook_settings.routes)} route(s) on "
            f"{webhook_settings.host}:{webhook_settings.port}"
        )
    else:
        print("  Webhook:       not configured (set WEBHOOK_ROUTES to enable)")

    print(f"  Health check:  port {HEALTH_CHECK_PORT}")

    if has_heartbeat:
        print(f"  MC Heartbeat:  {mc_heartbeat_url}")
    else:
        print("  MC Heartbeat:  not configured (set MC_HEARTBEAT_URL + MC_AGENT_API_KEY to enable)")

    print(f"{'=' * 60}\n")

    # Validate at least one adapter is available
    if args.telegram and not has_telegram:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        print("Message @BotFather on Telegram to create a bot and get a token")
        sys.exit(1)

    if args.slack and not has_slack:
        print("ERROR: SLACK_BOT_TOKEN or SLACK_APP_TOKEN not set in .env")
        sys.exit(1)

    if args.relay and not has_relay:
        print("ERROR: RELAY_AUTH_TOKEN not set in .env")
        sys.exit(1)

    if args.discord and not has_discord:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env")
        sys.exit(1)

    if args.whatsapp and not has_whatsapp:
        print("ERROR: WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID not set in .env")
        sys.exit(1)

    if args.webhook and not has_webhook:
        print("ERROR: WEBHOOK_ROUTES not set in .env (or no route passed validation)")
        sys.exit(1)

    has_any = (
        has_slack or has_telegram or has_relay or has_discord or has_whatsapp
        or has_webhook
    )
    if start_all and not has_any:
        print("ERROR: No chat adapters configured.")
        print("Set TELEGRAM_BOT_TOKEN, SLACK_BOT_TOKEN + SLACK_APP_TOKEN, or RELAY_AUTH_TOKEN in .env")
        sys.exit(1)

    # Initialize Langfuse tracing (no-op if keys not configured)
    try:
        from runtime.langfuse_setup import init_langfuse
        if init_langfuse():
            print(f"[{datetime.now()}] Langfuse tracing active")
    except Exception as exc:
        print(f"[{datetime.now()}] Langfuse init skipped: {exc}")

    # Initialize shared components
    store = get_session_store(CHAT_DB_PATH)
    engine = ConversationEngine(store, PROJECT_ROOT, CHAT_MAX_TURNS, CHAT_MAX_BUDGET_USD)

    # Initialize ExtensionManager — registry for all commands, intents, extensions
    from commands import CATEGORIES, COMMANDS, CORE_INTENTS
    from core_handlers import CORE_HANDLERS, set_context
    from extension_manager import ExtensionManager, set_manager

    manager = ExtensionManager()
    manager.register_core_commands(COMMANDS, CATEGORIES, CORE_HANDLERS)
    manager.register_core_intents(CORE_INTENTS)

    # Discover extensions
    if EXTENSIONS_ENABLED:
        allow = [x.strip() for x in EXTENSIONS_ALLOW.split(",") if x.strip()] if EXTENSIONS_ALLOW else None
        deny = [x.strip() for x in EXTENSIONS_DENY.split(",") if x.strip()] if EXTENSIONS_DENY else None
        manager.configure_allow_deny(allow=allow, deny=deny)

        # 3-tier discovery: configured > bundled repo-local > user-global
        ext_paths: list[Path] = []
        if EXTENSIONS_EXTRA_PATH:
            ext_paths.append(Path(EXTENSIONS_EXTRA_PATH))
        bundled = Path(EXTENSIONS_BUNDLED_PATH)
        if bundled not in ext_paths:
            ext_paths.append(bundled)
        global_ext = Path.home() / ".claude" / "extensions"
        if global_ext.exists() and global_ext not in ext_paths:
            ext_paths.append(global_ext)

        discovered = manager.discover(ext_paths)
        if discovered:
            loaded = [e for e in discovered if e.status == "loaded"]
            errored = [e for e in discovered if e.status in ("error", "missing_env")]
            print(f"  Extensions:    {len(loaded)} loaded, {len(errored)} errored")
            for e in errored:
                print(f"    ERR {e.id}: {e.error or ', '.join(e.missing_env)}")
        else:
            print("  Extensions:    none discovered")
    else:
        print("  Extensions:    disabled (EXTENSIONS_ENABLED=false)")

    set_manager(manager)

    router = ChatRouter(engine, manager)

    # Set shared context for core_handlers (session-level state)
    set_context(
        engine=engine,
        adapters=router.adapters,
        bot_start_time=datetime.now(),
    )

    if args.test:
        print("Test mode — validating config and exiting.")
        active = store.list_active()
        print(f"  Session store OK ({len(active)} active sessions)")
        print("  Engine OK")

        if has_telegram and (start_all or args.telegram):
            from adapters.telegram import TelegramAdapter
            tg = TelegramAdapter(
                TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS,
                openai_api_key=OPENAI_API_KEY,
                voice_stt_model=VOICE_STT_MODEL,
                voice_tts_engine=VOICE_TTS_ENGINE,
                voice_tts_voice_edge=VOICE_TTS_VOICE_EDGE,
                voice_tts_voice_openai=VOICE_TTS_VOICE_OPENAI,
            )
            router.register(tg)
            print("  Telegram adapter OK")

        if has_slack and (start_all or args.slack):
            from adapters.slack import SlackAdapter
            slack = SlackAdapter(SLACK_BOT_TOKEN, SLACK_APP_TOKEN, CHAT_ALLOWED_USERS, session_store=store)
            router.register(slack)
            print("  Slack adapter OK")

        if has_discord and (start_all or args.discord):
            from adapters.discord import DiscordAdapter
            disc = DiscordAdapter(
                DISCORD_BOT_TOKEN,
                DISCORD_ALLOWED_GUILDS,
                DISCORD_ALLOWED_USERS,
                watch_all_guild_channels=DISCORD_WATCH_ALL_GUILD_CHANNELS,
            )
            router.register(disc)
            print("  Discord adapter OK")

        if has_whatsapp and (start_all or args.whatsapp):
            from adapters.whatsapp import WhatsAppAdapter
            wa = WhatsAppAdapter(
                WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID,
                WHATSAPP_VERIFY_TOKEN, WHATSAPP_WEBHOOK_PORT,
            )
            router.register(wa)
            print("  WhatsApp adapter OK")

        if has_webhook and (start_all or args.webhook):
            from adapters.webhook import WebhookAdapter
            from webhook_audit import append_webhook_audit_record
            wh = WebhookAdapter(
                webhook_settings,
                adapter_resolver=_make_webhook_adapter_resolver(router),
                audit=append_webhook_audit_record,
            )
            router.register(wh)
            print(f"  Webhook adapter OK ({len(webhook_settings.routes)} route(s))")

        if has_relay and (start_all or args.relay):
            print(f"  Relay WS OK ({relay_ws_url})")

        print("\nAll checks passed. Run without --test to start.")
        return

    # Register adapters
    if has_telegram and (start_all or args.telegram):
        from adapters.telegram import TelegramAdapter
        tg = TelegramAdapter(
            TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS,
            openai_api_key=OPENAI_API_KEY,
            voice_stt_model=VOICE_STT_MODEL,
            voice_tts_engine=VOICE_TTS_ENGINE,
            voice_tts_voice_edge=VOICE_TTS_VOICE_EDGE,
            voice_tts_voice_openai=VOICE_TTS_VOICE_OPENAI,
        )
        router.register(tg)

    if has_slack and (start_all or args.slack):
        from adapters.slack import SlackAdapter
        slack = SlackAdapter(SLACK_BOT_TOKEN, SLACK_APP_TOKEN, CHAT_ALLOWED_USERS, session_store=store)
        router.register(slack)

    if has_discord and (start_all or args.discord):
        from adapters.discord import DiscordAdapter
        disc = DiscordAdapter(
            DISCORD_BOT_TOKEN,
            DISCORD_ALLOWED_GUILDS,
            DISCORD_ALLOWED_USERS,
            watch_all_guild_channels=DISCORD_WATCH_ALL_GUILD_CHANNELS,
        )
        router.register(disc)

    if has_whatsapp and (start_all or args.whatsapp):
        from adapters.whatsapp import WhatsAppAdapter
        wa = WhatsAppAdapter(
            WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID,
            WHATSAPP_VERIFY_TOKEN, WHATSAPP_WEBHOOK_PORT,
        )
        router.register(wa)

    # Set up relay WebSocket client (runs alongside other adapters)
    relay_client = None
    if has_relay and (start_all or args.relay):
        from adapters.web import WebAdapter
        from ws_client import RelayWSClient

        web_adapter = WebAdapter(None)  # ws_client set below after creation
        relay_client = RelayWSClient(
            relay_url=relay_ws_url,
            relay_token=relay_auth_token,
            router=router,
            adapter=web_adapter,
        )
        web_adapter.ws_client = relay_client
        # Register web adapter so router can route web platform messages
        router.register(web_adapter)

    # Webhook ingress — registered LAST so the resolver closure sees every
    # sibling adapter. Constructed ONLY when routes exist (dormant default).
    if has_webhook and (start_all or args.webhook):
        from adapters.webhook import WebhookAdapter
        from webhook_audit import append_webhook_audit_record

        webhook_adapter = WebhookAdapter(
            webhook_settings,
            adapter_resolver=_make_webhook_adapter_resolver(router),
            audit=append_webhook_audit_record,
        )
        router.register(webhook_adapter)

    print(f"[{datetime.now()}] Starting chat interface...")
    append_to_daily_log(f"Bot started (PID {os.getpid()})", "Bot Lifecycle")

    async def _run_all() -> None:
        """Run router and relay client concurrently.

        Uses return_exceptions=False so the first fatal error surfaces
        immediately. Each task is individually wrapped so one crashing
        doesn't kill the others silently.
        """
        # Start health check server (runs in background via aiohttp)
        from config import get_bot_liveness_settings
        from health import HealthServer, HealthStatus
        from liveness import (
            DiagnosticsCache,
            LivenessSupervisor,
            resolve_health_status,
        )

        live = get_bot_liveness_settings()
        boot_at = time.monotonic()

        def _notify_operator(title: str, message: str) -> None:
            """Desktop toast + Slack. Fail-open — a dead toast never stops the bot."""
            try:
                from notifications import send_toast_notification

                send_toast_notification(title, message, caller="chat.liveness")
            except Exception as exc:  # noqa: BLE001
                print(f"[{datetime.now()}] [liveness] notify failed: {exc}", flush=True)

        supervisor = LivenessSupervisor(
            adapters={p.value: a for p, a in router.adapters.items()},
            interval_seconds=live.interval_seconds,
            probe_timeout_seconds=live.probe_timeout_seconds,
            failure_threshold=live.failure_threshold,
            reconnect_attempts=live.reconnect_attempts,
            fail_fast=live.fail_fast,
            startup_grace_seconds=live.startup_grace_seconds,
            notify=_notify_operator,
        )
        diag_cache = DiagnosticsCache(ttl_seconds=live.diagnostics_ttl_seconds)

        def _build_health_status() -> HealthStatus:
            """Assemble /health WITHOUT touching the network, the DB, or disk.

            Everything here is an in-memory read. The old builder called
            ``collect_diagnostics()`` inline — cold imports, a chunk-table COUNT,
            runtime resolution, CDP/adb probes — synchronously on the aiohttp
            event loop, so /health took ~3.4s warm and effectively hung at boot.
            A health endpoint that can hang is a health endpoint that cannot be
            trusted to report a hang.
            """
            adapters_status = supervisor.adapter_health_map()
            diag = diag_cache.snapshot() or {}
            status = resolve_health_status(
                adapters=adapters_status,
                any_unhealthy=supervisor.any_unhealthy(),
                has_diagnostics=bool(diag),
                uptime_seconds=time.monotonic() - boot_at,
                warmup_seconds=live.warmup_seconds,
            )

            return HealthStatus(
                status=status,
                uptime_seconds=0.0,  # filled by HealthServer
                adapters=adapters_status,
                sessions_active=int(diag.get("sessions_active", 0)),
                cognition_available=bool(diag.get("cognition_available", False)),
                runtime_providers=diag.get("runtime_providers", {}),
                memory_doc_count=diag.get("memory_doc_count", 0),
                memory_embedding_status=diag.get("memory_embedding_status", ""),
                adapter_liveness=supervisor.snapshot(),
                diagnostics_age_seconds=diag_cache.age_seconds(),
            )

        health_srv = HealthServer(HEALTH_CHECK_PORT, _build_health_status)
        await health_srv.start()

        tasks: dict[str, asyncio.Task] = {}

        # Start the relay WS client as a background task
        if relay_client:
            tasks["relay"] = asyncio.create_task(relay_client.connect_forever())

        # MC heartbeat loop — reports degraded when an adapter is proven dead.
        if has_heartbeat:
            tasks["mc_heartbeat"] = asyncio.create_task(
                _mc_heartbeat_loop(
                    mc_heartbeat_url,
                    mc_agent_api_key,
                    status_fn=lambda: "degraded" if supervisor.any_unhealthy() else "online",
                )
            )

        # Router.run() handles adapter connect + listen
        tasks["router"] = asyncio.create_task(router.run())

        # Diagnostics refresher — keeps the expensive sweep OFF the /health path.
        tasks["diagnostics"] = asyncio.create_task(diag_cache.run())

        # Liveness supervisor — the watcher that did not exist during the wedge.
        if live.enabled:
            tasks["liveness"] = asyncio.create_task(supervisor.run())
        else:
            print(f"[{datetime.now()}] [liveness] DISABLED via BOT_LIVENESS_ENABLED", flush=True)

        # Tasks whose death means the bot cannot serve messages. Everything else
        # (relay, heartbeat, diagnostics) is best-effort and may die quietly.
        await _supervise_tasks(tasks, fatal_tasks={"router", "liveness"})

    try:
        asyncio.run(_run_all())
    except KeyboardInterrupt:
        print(f"\n[{datetime.now()}] Shutting down...")
        append_to_daily_log("Bot stopped (keyboard interrupt)", "Bot Lifecycle")
        try:
            from orchestration import restart_loop_guard
            restart_loop_guard.clear()
        except Exception:
            pass
        try:
            from runtime.langfuse_setup import flush_langfuse
            flush_langfuse()
        except Exception:
            pass
        asyncio.run(router.shutdown())
        print(f"[{datetime.now()}] Goodbye!")
    except Exception as e:
        # Catch-all so no exception ever kills the process without logging
        print(f"[{datetime.now()}] FATAL UNHANDLED: {type(e).__name__}: {e}", flush=True)
        append_to_daily_log(f"Bot crashed (unhandled): {e}", "Bot Lifecycle")
        try:
            from runtime.langfuse_setup import flush_langfuse
            flush_langfuse()
        except Exception:
            pass
        # atexit handles remove_pid()
        # Exit NON-ZERO. This branch used to fall through and return 0, so a
        # crashed bot was indistinguishable from a clean shutdown to service.py
        # (which checks the return code) and to any supervisor with a
        # restart-on-failure policy. A crash must look like a crash.
        sys.exit(1)


if __name__ == "__main__":
    main()
