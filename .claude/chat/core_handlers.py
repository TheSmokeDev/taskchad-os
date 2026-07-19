"""Core command handlers — extracted from router.py's elif chain.

Each handler has the signature:
    async def handle_X(adapter, incoming, args, *, collect_only=False) -> str

Handlers are stateless functions. Access to router-level state (engine, session
store, adapters) is via the _ctx module-level dict, set by the router at startup.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from session_keys import build_session_key, resolve_thread_id

from recap import build_recap

from runtime import routing as runtime_routing
from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE
from runtime.model_control import (
    apply_runtime_model_choice,
    format_model_choice,
    model_observability_warning,
    resolve_runtime_model_choice,
    runtime_model_warnings,
    selected_runtime_model,
)
from runtime.selection import (
    apply_runtime_selection_choice,
    describe_runtime_selection,
    provider_display_name,
    resolve_runtime_selection,
)

# ---------------------------------------------------------------------------
# Shared context — set once by router at startup via set_context()
# ---------------------------------------------------------------------------

_ctx: dict[str, Any] = {}


def set_context(
    *,
    engine: Any = None,
    adapters: dict | None = None,
    bot_start_time: datetime | None = None,
) -> None:
    """Set shared state for handlers. Called once by ChatRouter."""
    if engine is not None:
        _ctx["engine"] = engine
    if adapters is not None:
        _ctx["adapters"] = adapters
    if bot_start_time is not None:
        _ctx["bot_start_time"] = bot_start_time


# ---------------------------------------------------------------------------
# Cache for cleanup dry-run reports
# ---------------------------------------------------------------------------
_cleanup_cache: dict[str, tuple[Any, Any, float]] = {}


# ---------------------------------------------------------------------------
# Session helpers (imported lazily to avoid circular imports)
# ---------------------------------------------------------------------------

def _get_session(incoming: Any) -> tuple[Any, Any, str, str, str]:
    """Return (store, existing_session, platform_str, channel_id, thread_id).

    Uses the engine's session store from the shared context.
    """
    engine = _ctx.get("engine")
    if engine is None:
        raise RuntimeError("core_handlers: engine not set — call set_context() first")
    store = engine.session_store
    platform_str = incoming.platform.value
    channel_id = incoming.channel.platform_id
    thread_id = resolve_thread_id(
        channel_id,
        incoming.thread.thread_id if incoming.thread else None,
    )
    existing = store.get(platform_str, channel_id, thread_id)
    return store, existing, platform_str, channel_id, thread_id


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_help(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show available commands."""
    from extension_manager import get_manager

    user_role = getattr(incoming, "user_role", "admin")
    return get_manager().get_help_text(user_role=user_role)


async def handle_commands(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show the curated Telegram menu or the full command registry."""
    from commands import get_telegram_command_menu
    from extension_manager import get_manager

    view = (args or "native").strip().lower()
    user_role = getattr(incoming, "user_role", "admin")

    if view in {"", "native", "menu", "telegram"}:
        menu, hidden_count = get_telegram_command_menu()
        lines = [
            "*Native Telegram Commands*",
            "These are the commands shown in Telegram's slash menu. Hidden commands still work when typed.",
            "",
        ]
        for name, desc in menu:
            lines.append(f"  /{name} - {desc}")
        lines.extend(
            [
                "",
                f"Hidden registered commands: {hidden_count}",
                "Use `/commands all` for the full registry.",
            ]
        )
        return "\n".join(lines)

    if view in {"all", "full", "registry"}:
        return get_manager().get_help_text(user_role=user_role)

    return "Unknown commands view. Use: /commands native or /commands all"


async def handle_status(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show session and bot status."""
    store, existing, *_ = _get_session(incoming)
    bot_start = _ctx.get("bot_start_time", datetime.now())
    uptime = datetime.now() - bot_start
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60

    if existing:
        mode = existing.mode
        msgs = existing.message_count
        cost = f"${existing.total_cost_usd:.4f}"
        created = existing.created_at.strftime("%Y-%m-%d %H:%M")
    else:
        mode = "execute"
        msgs = 0
        cost = "$0.0000"
        created = "—"

    return (
        f"*Session Status*\n"
        f"  Mode: {mode}\n"
        f"  Messages: {msgs}\n"
        f"  Cost: {cost}\n"
        f"  Session started: {created}\n"
        f"  Bot uptime: {hours}h {minutes}m\n"
        f"  PID: {os.getpid()}"
    )


async def handle_diagnostics(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show system diagnostics."""
    from diagnostics import collect_diagnostics

    # collect_diagnostics() reaches a synchronous browser_readiness() CDP probe
    # (diagnostics._check_browser) plus other blocking file/subprocess reads, so
    # it must run OFF the event loop — a dead CDP socket here would otherwise
    # freeze Telegram/Discord/liveness for the whole ~9s chain (#130).
    report = await asyncio.to_thread(collect_diagnostics)
    adapters = _ctx.get("adapters", {})
    report.adapters_connected = {p.value: True for p in adapters.keys()}

    lines = ["*System Diagnostics*", ""]
    lines.append(f"*Cognition*: {'Active' if report.cognition_available else 'Unavailable'}")
    for move, active in report.cognition_moves.items():
        lines.append(f"  {move}: {'ON' if active else 'OFF'}")
    lines.append("")
    lines.append("*Recall*:")
    lines.append(f"  Last query: {report.recall_last_query or 'none'}")
    lines.append(f"  Tier: {report.recall_last_tier or 'n/a'}")
    lines.append(f"  Results: {report.recall_last_count}")
    lines.append("")
    lines.append("*Memory DB*:")
    lines.append(f"  Documents: {report.memory_doc_count}")
    lines.append(f"  Embeddings: {report.memory_embedding_status}")
    _append_cognitive_loop_lines(lines, report.cognitive_loop)
    lines.append("")
    lines.append("*Runtime*:")
    lines.append(f"  selected lane: {report.runtime_selected_lane}")
    if report.runtime_selected_generic_provider:
        lines.append(
            "  generic preferred provider: "
            f"{provider_display_name(report.runtime_selected_generic_provider)}"
        )
    else:
        lines.append("  generic preferred provider: auto")
    lines.append(
        "  configured model: "
        f"{report.runtime_selected_model or 'auto (route-dependent)'}"
    )
    for warning in report.runtime_model_warnings:
        lines.append(f"  warning: {warning}")
    if report.runtime_generic_text_route:
        lines.append(
            "  generic text route: "
            + " -> ".join(
                provider_display_name(provider)
                for provider in report.runtime_generic_text_route
            )
        )
    if report.runtime_generic_tool_route:
        lines.append(
            "  generic tool route: "
            + " -> ".join(
                provider_display_name(provider)
                for provider in report.runtime_generic_tool_route
            )
        )
    for lane_name, lane_status in report.runtime_lanes.items():
        lines.append(f"  lane:{lane_name}: {lane_status}")
    for name, status in report.runtime_providers.items():
        lines.append(f"  {name}: {status}")
    if report.runtime_auth_issues:
        lines.append("  Auth attention:")
        for provider, issue in report.runtime_auth_issues.items():
            lines.append(f"    {provider_display_name(provider)}: {issue}")
    lines.append("")
    lines.append("*Lifecycle*:")
    lines.append(
        f"  Clear lifecycle warnings/errors (recent): "
        f"{report.clear_lifecycle_recent_failures}"
    )
    if report.clear_lifecycle_last_failure:
        lines.append(
            "  Last clear lifecycle warning: "
            f"{report.clear_lifecycle_last_failure}"
        )
    lines.append("")
    lines.append("*Ghost* (the Homie's own background Android):")
    ghost = report.ghost or {}
    if not ghost.get("enabled"):
        lines.append(f"  disabled ({ghost.get('detail') or 'HOMIE_GHOST_ENABLED not set'})")
    else:
        lines.append(
            f"  running={bool(ghost.get('running'))} booted={bool(ghost.get('booted'))} "
            f"serial={ghost.get('serial') or 'n/a'} avd={ghost.get('avd') or 'n/a'}"
        )
        lines.append(
            f"  cdp={ghost.get('cdp_port') or 'n/a'} "
            f"reachable={bool(ghost.get('cdp_reachable'))} (boot with /ghost up)"
        )
    lines.append("")
    lines.append("*Sessions*:")
    lines.append(f"  Active: {report.sessions_active}")
    lines.append(f"  Messages: {report.sessions_total_messages}")
    lines.append(f"  Cost: ${report.sessions_total_cost_usd:.4f}")
    lines.append("")
    lines.append("*Adapters*:")
    for name, connected in report.adapters_connected.items():
        lines.append(f"  {name}: {'connected' if connected else 'off'}")
    return "\n".join(lines)


def _append_cognitive_loop_lines(lines: list[str], cognitive_loop: dict[str, Any]) -> None:
    """Append compact cognitive-loop status to router diagnostics output."""

    if not cognitive_loop:
        return

    lines.append("")
    lines.append("*Cognitive Loop*:")
    lines.append(f"  Overall: {str(cognitive_loop.get('overall', 'unknown')).upper()}")

    subsystems = cognitive_loop.get("subsystems", {})
    if isinstance(subsystems, dict):
        for name in sorted(subsystems):
            item = subsystems.get(name) or {}
            if not isinstance(item, dict):
                continue
            state = str(item.get("state", "unknown")).upper()
            evidence = str(item.get("evidence", "")).strip()
            line = f"  {name}: {state}"
            if evidence:
                line += f" - {evidence}"
            lines.append(line)

    next_actions = cognitive_loop.get("next_actions", [])
    if next_actions:
        lines.append("  Next actions:")
        for action in next_actions:
            lines.append(f"    - {action}")


async def handle_cost(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show session cost."""
    _, existing, *_ = _get_session(incoming)
    if existing:
        return f"Session cost: *${existing.total_cost_usd:.4f}*"
    return "No active session — cost: *$0.00*"


async def handle_clear(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Clear the current session."""
    store, existing, platform_str, channel_id, thread_id = _get_session(incoming)
    if existing:
        from session_lifecycle_hooks import clear_session_with_lifecycle

        engine = _ctx.get("engine")
        # Living Mind Act 4 (R1 B4): /clear skips _persist_router_turn but its
        # interactive clear event counts as presence and closes the away gap —
        # capture the brief-owed marker FIRST. Defensive getattr keeps fake
        # engines green; the engine method is whole-body fail-open.
        note_router_activity = getattr(engine, "note_router_activity", None)
        if callable(note_router_activity):
            note_router_activity(incoming)

        result = clear_session_with_lifecycle(
            store=store,
            session=existing,
            platform=platform_str,
            channel_id=channel_id,
            thread_id=thread_id,
            engine=engine,
            source="clear",
            # The EVENT label above stays "clear"; trigger identity is a
            # different fact (R1 B1).
            trigger_source=getattr(incoming, "source", "interactive"),
        )
        warning = result.warning_summary()
        if warning:
            return (
                "Session cleared. Next message starts fresh.\n"
                f"Lifecycle warning: {warning}"
            )
        return "Session cleared. Next message starts fresh."
    return "No active session to clear."


async def handle_plan(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Enable plan mode."""
    from session import Session

    store, existing, platform_str, channel_id, thread_id = _get_session(incoming)
    if existing:
        existing.mode = "plan"
        store.update(existing)
    else:
        now = datetime.now()
        # PRP-7d R1 B2: read source from incoming; set-once on create
        # (the `if existing:` UPDATE branch above MUST NOT touch source).
        message_source = getattr(incoming, "source", "interactive")
        store.create(
            Session(
                session_id=build_session_key(platform_str, channel_id, thread_id),
                agent_session_id="",
                platform=platform_str,
                channel_id=channel_id,
                thread_id=thread_id,
                user_id=incoming.user.platform_id,
                created_at=now,
                updated_at=now,
                mode="plan",
                source=message_source,
            )
        )
    return "Plan mode enabled. I'll research and propose — no file changes until you say /go."


async def handle_go(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Enable execute mode."""
    store, existing, *_ = _get_session(incoming)
    if existing:
        existing.mode = "execute"
        store.update(existing)
        return "Execute mode enabled. Ready to implement."
    return "Already in execute mode (default for new conversations)."


async def handle_mode(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show current mode."""
    _, existing, *_ = _get_session(incoming)
    mode = existing.mode if existing else "execute"
    return f"Current mode: *{mode}*"


async def handle_voice(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show or persist the shared Telegram/Discord voice reply mode."""
    from voice_preferences import get_voice_reply_mode, set_voice_reply_mode

    requested = (args or "").strip().lower()
    if requested in {"", "status"}:
        mode = get_voice_reply_mode()
    elif requested in {"always", "auto", "off", "on"}:
        mode = set_voice_reply_mode(requested)
    else:
        return "Usage: /voice [always | auto | off]"

    descriptions = {
        "always": "every Telegram and Discord reply includes voice + text",
        "auto": "voice replies follow incoming voice messages; normal replies stay text",
        "off": "all replies are text only",
    }
    return f"Voice mode: *{mode}* — {descriptions[mode]}. This survives restarts."


async def handle_reload(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Reload config, soul context, and clear session."""
    from config import reload_config

    store, existing, platform_str, channel_id, thread_id = _get_session(incoming)
    engine = _ctx.get("engine")
    adapters = _ctx.get("adapters", {})
    parts: list[str] = []

    changes = reload_config()
    if changes:
        from config import CHAT_MAX_BUDGET_USD, CHAT_MAX_TURNS

        if engine:
            engine.max_turns = CHAT_MAX_TURNS
            engine.max_budget_usd = CHAT_MAX_BUDGET_USD

        from models import Platform

        tg_adapter = adapters.get(Platform.TELEGRAM)
        if tg_adapter:
            from config import (
                OPENAI_API_KEY,
                VOICE_STT_MODEL,
                VOICE_TTS_ENGINE,
                VOICE_TTS_VOICE_EDGE,
                VOICE_TTS_VOICE_OPENAI,
            )

            tg_adapter.configure_voice(
                openai_api_key=OPENAI_API_KEY,
                voice_stt_model=VOICE_STT_MODEL,
                voice_tts_engine=VOICE_TTS_ENGINE,
                voice_tts_voice_edge=VOICE_TTS_VOICE_EDGE,
                voice_tts_voice_openai=VOICE_TTS_VOICE_OPENAI,
            )

        lines = [f"  {k}: {old} → {new}" for k, (old, new) in changes.items()]
        parts.append("Config changes:\n" + "\n".join(lines))

    if engine:
        engine.reload_soul_context()
    parts.append("Soul context reloaded (SOUL.md, USER.md, MEMORY.md)")

    if existing:
        store.delete(platform_str, channel_id, thread_id)
        parts.append("Session cleared — next message starts a fresh conversation")

    return "Full reload complete:\n" + "\n".join(f"- {p}" for p in parts)


async def handle_provider(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show runtime provider status."""
    return await asyncio.to_thread(_get_provider_status)


async def handle_model(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Switch runtime provider."""
    return _switch_provider(args.strip() if args else "")


async def handle_restart(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Self-restart the bot."""
    if collect_only:
        return "Cannot chain /restart — use it alone."

    import sys

    from models import OutgoingMessage
    from shared import spawn_detached

    reply = "Restarting myself... back in a few seconds."
    await adapter.send(
        OutgoingMessage(
            text=reply,
            channel=incoming.channel,
            thread=incoming.thread,
        )
    )
    # A process can't reliably restart itself — spawn the DETACHED relauncher
    # (chat/relaunch.py). It survives our exit, waits for us to die, then spawns
    # a fresh bot with the Claude-Code nesting markers scrubbed and the profile
    # preserved. Pure Python (no bash dependency); see chat/relaunch.py.
    chat_dir = Path(__file__).resolve().parent
    relaunch_script = chat_dir / "relaunch.py"
    spawn_detached([sys.executable, str(relaunch_script)], cwd=str(chat_dir))
    await asyncio.sleep(1)
    print(f"[{datetime.now()}] Self-restart initiated — exiting (PID {os.getpid()})")
    os._exit(0)


async def handle_autostart(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Toggle the bot-at-logon scheduled task — status | on | off."""
    action = (args or "").strip().lower() or "status"
    if action in ("enable",):
        action = "on"
    if action in ("disable",):
        action = "off"
    if action not in ("status", "on", "off"):
        return "Usage: /autostart [status | on | off]"

    # The schtasks/PowerShell calls take 1-60s — off-loop so a slow Task
    # Scheduler stalls only this command, not every chat user on this loop.
    return await asyncio.to_thread(_autostart_sync, action)


def _autostart_sync(action: str) -> str:
    """Blocking tail of /autostart — runs in a worker thread."""
    import autostart
    from security import kill_switches

    try:
        if action == "on":
            result = autostart.enable(caller="chat:/autostart")
        elif action == "off":
            result = autostart.disable(caller="chat:/autostart")
        else:
            result = autostart.status()
    except kill_switches.KillSwitchDisabled:
        return "Autostart is disabled by operator (HOMIE_KILLSWITCH_AUTOSTART). No changes made."
    except Exception as exc:  # noqa: BLE001 — a broken toggle must not crash the router
        return f"Autostart error: {type(exc).__name__}: {exc}"

    if not result["supported"]:
        return "Autostart is only supported on Windows right now."
    state = "ON" if result["enabled"] else "OFF"
    lines = [f"Autostart: {state} — task '{result['task_name']}' (at logon)"]
    if action != "status" and not result.get("ok", True):
        lines.append(f"FAILED: {result['detail']}")
    elif result["detail"]:
        lines.append(result["detail"])
    return "\n".join(lines)


async def handle_update(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Safe stable-release update status, execution, schedule, and history."""
    tokens = (args or "status").strip().lower().split()
    action = tokens[0] if tokens else "status"
    if action not in {"status", "now", "auto", "history"}:
        return "Usage: /update [status | now | auto on|off|status | history]"
    if collect_only and action in {"now", "auto"}:
        return f"Cannot chain /update {action} — use it alone."
    return await asyncio.to_thread(_update_sync, action, tokens[1:], incoming)


def _update_sync(action: str, extra: list[str], incoming: Any) -> str:
    """Blocking tail of /update; network, Git, and scheduler calls stay off-loop."""
    import config
    import update_scheduler
    from framework_update import FrameworkUpdater

    updater = FrameworkUpdater(config.PROJECT_ROOT)
    if action == "status":
        result = updater.status().to_dict()
        schedule = result.get("schedule") or {}
        schedule_state = "ON" if schedule.get("enabled") else "OFF"
        lines = [
            "*The Homie Update*",
            f"  Current: v{result['current_version']} ({result['current_revision'][:8]})",
            f"  Latest stable: {('v' + result['latest_version']) if result.get('latest_version') else 'unavailable'}",
            f"  Deployment: {result['deployment_mode']}",
            f"  Auto-update: {schedule_state} — {schedule.get('time', '04:00')} {schedule.get('timezone', 'America/Los_Angeles')}",
        ]
        if schedule.get("next_run"):
            lines.append(f"  Next run: {schedule['next_run']}")
        if result.get("blocker"):
            lines.append(f"  Blocked: {result['blocker']}")
        elif result.get("update_available"):
            lines.append("  Update available — use `/update now`.")
        else:
            lines.append("  Already on the latest stable release.")
        return "\n".join(lines)

    if action == "history":
        history = updater.history(limit=5)
        if not history:
            return "No framework update receipts yet."
        lines = ["*Recent Homie Updates*"]
        for item in history:
            lines.append(
                f"  {item.get('finished_at') or item.get('started_at')} — "
                f"{item.get('status')} {item.get('target_tag') or ''} "
                f"({item.get('receipt_id')})"
            )
        return "\n".join(lines)

    if action == "auto":
        sub = extra[0] if extra else "status"
        if sub in {"enable"}:
            sub = "on"
        if sub in {"disable"}:
            sub = "off"
        if sub not in {"on", "off", "status"}:
            return "Usage: /update auto [on | off | status]"
        if sub == "on":
            result = update_scheduler.enable(config.PROJECT_ROOT)
        elif sub == "off":
            result = update_scheduler.disable(config.PROJECT_ROOT)
        else:
            result = update_scheduler.status(config.PROJECT_ROOT)
        state = "ON" if result.get("enabled") else "OFF"
        lines = [
            f"Auto-update: {state} — {result.get('time')} {result.get('timezone')}",
            f"Scheduler: {result.get('platform')}",
        ]
        if result.get("next_run"):
            lines.append(f"Next run: {result['next_run']}")
        if result.get("detail") and (not result.get("enabled") or result.get("ok") is False):
            lines.append(str(result["detail"]))
        return "\n".join(lines)

    platform_value = getattr(incoming.platform, "value", str(incoming.platform))
    requester = {
        "platform": str(platform_value),
        "channel": str(incoming.channel.platform_id),
        "thread": str(getattr(getattr(incoming, "thread", None), "thread_id", "") or ""),
    }
    launched = update_scheduler.launch_now(config.PROJECT_ROOT, requester=requester)
    if not launched.get("ok"):
        return f"Update not started: {launched.get('detail', 'unknown launcher error')}"
    return (
        "Update started safely in the background. I will stage and test the release, "
        "preserve operator files, restart only after validation, and post the receipt here. "
        f"Worker: {launched.get('worker_id', 'started')}"
    )


async def handle_gsc(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Fetch Google Search Console data."""
    try:
        from integrations.search_console_api import (
            format_queries_for_context,
            get_overall_stats,
            get_top_queries,
        )
        from integrations.search_console_api import (
            format_stats_for_context as format_gsc_stats,
        )

        parts_gsc: list[str] = []
        stats = get_overall_stats(days=28)
        parts_gsc.append(format_gsc_stats(stats))
        queries = get_top_queries(days=28, max_results=10)
        parts_gsc.append(format_queries_for_context(queries))
        return "\n\n".join(parts_gsc)
    except Exception as e:
        return f"Error fetching Search Console data: {e}"


async def handle_email(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Fetch email from Gmail and Outlook."""
    try:
        parts_email: list[str] = []
        sub = args.strip().lower() if args else ""

        source = "both"
        if sub.startswith("gmail"):
            source = "gmail"
            sub = sub[5:].strip()
        elif sub.startswith("outlook"):
            source = "outlook"
            sub = sub[7:].strip()

        if source in ("both", "gmail"):
            try:
                from integrations.gmail import (
                    check_for_urgent_emails as gmail_urgent,
                )
                from integrations.gmail import (
                    format_emails_for_context as gmail_fmt,
                )
                from integrations.gmail import (
                    get_unread_count as gmail_unread,
                )
                from integrations.gmail import (
                    list_emails as gmail_list,
                )

                parts_email.append(f"*Gmail* ({gmail_unread()} unread)")
                if sub == "urgent":
                    emails_g = gmail_urgent(hours_ago=4)
                elif sub == "unread":
                    emails_g = gmail_list(max_results=10, unread_only=True)
                elif sub:
                    emails_g = gmail_list(max_results=10, query=sub)
                else:
                    emails_g = gmail_list(max_results=10)
                parts_email.append(gmail_fmt(emails_g, max_chars=2000))
            except Exception as e:
                parts_email.append(f"*Gmail* — error: {e}")

        if source in ("both", "outlook"):
            try:
                from integrations.outlook import (
                    format_emails_for_context as outlook_fmt,
                )
                from integrations.outlook import (
                    get_unread_count as outlook_unread,
                )
                from integrations.outlook import (
                    is_configured as outlook_ok,
                )
                from integrations.outlook import (
                    list_emails as outlook_list,
                )

                if not outlook_ok():
                    parts_email.append("*Outlook* — not configured (missing Graph API creds)")
                else:
                    parts_email.append(f"*Outlook* ({outlook_unread()} unread)")
                    if sub == "urgent":
                        emails_o = outlook_list(max_results=10, hours_ago=4)
                    elif sub == "unread":
                        emails_o = outlook_list(max_results=10, unread_only=True)
                    elif sub:
                        emails_o = outlook_list(max_results=10, query=sub)
                    else:
                        emails_o = outlook_list(max_results=10)
                    parts_email.append(outlook_fmt(emails_o, max_chars=2000))
            except Exception as e:
                parts_email.append(f"*Outlook* — error: {e}")

        if sub and sub not in ("urgent", "unread"):
            parts_email.insert(0, f'_Search: "{sub}"_')
        elif sub:
            parts_email.insert(0, f"_Filter: {sub}_")

        return "\n\n".join(parts_email)
    except Exception as e:
        return f"Error fetching email: {e}"


async def handle_personal_email(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False
) -> str:
    """Fetch personal Gmail (your-calendar@gmail.com) — read-only."""
    try:
        from integrations.personal_gmail import (
            format_personal_emails_for_context,
            get_personal_email,
            get_personal_unread_count,
            is_personal_gmail_configured,
            list_personal_emails,
        )

        if not is_personal_gmail_configured():
            return (
                "*Personal Gmail* — not set up yet.\n"
                "Run `uv run python setup_auth.py --personal` to authenticate."
            )

        sub = args.strip().lower() if args else ""

        # /personal-email read <id>
        if sub.startswith("read "):
            msg_id = sub[5:].strip()
            email = get_personal_email(msg_id)
            if not email:
                return f"Email `{msg_id}` not found."
            from integrations.email_sanitizer import sanitize_external_text
            safe_subject = sanitize_external_text(email.subject)
            safe_sender = sanitize_external_text(email.sender)
            safe_body = sanitize_external_text((email.body or "(no body)")[:3000])
            return (
                f"**{safe_subject}**\n"
                f"From: {safe_sender} <{email.sender_email}>\n"
                f"Date: {email.date.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"{safe_body}"
            )

        unread = get_personal_unread_count()
        header = f"*Personal Gmail* ({unread} unread)"

        if sub == "unread":
            emails = list_personal_emails(max_results=10, unread_only=True)
        elif sub == "list":
            emails = list_personal_emails(max_results=20)
        elif sub:
            emails = list_personal_emails(max_results=10, query=sub)
        else:
            # Default: unread count + last 5 subjects
            emails = list_personal_emails(max_results=5)

        return header + "\n\n" + format_personal_emails_for_context(emails, max_chars=2000)
    except Exception as e:
        return f"Error fetching personal email: {e}"


async def handle_accounts(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show social media account status."""
    try:
        from integrations.social_media import get_accounts_status
        return get_accounts_status()
    except Exception as e:
        return f"Error loading accounts: {e}"


def _browser_actor_surface(adapter: Any, incoming: Any) -> str:
    from browser_audit import normalize_surface

    platform = getattr(incoming, "platform", None) or getattr(adapter, "platform", None)
    value = getattr(platform, "value", None) or str(platform or "")
    return normalize_surface(value)


def _browser_session_id(incoming: Any) -> str | None:
    if incoming is None:
        return None
    try:
        platform = getattr(incoming.platform, "value", str(incoming.platform))
        channel_id = incoming.channel.platform_id
        thread_id = resolve_thread_id(
            channel_id,
            incoming.thread.thread_id if incoming.thread else None,
        )
        return build_session_key(platform, channel_id, thread_id)
    except Exception:
        return None


def _audit_browser_action(
    *,
    adapter: Any,
    incoming: Any,
    command: str,
    workflow_id: str | None,
    outcome: str,
    reason: str = "",
    readiness: dict[str, Any] | None = None,
    cdp_port: int | None = None,
    target_url: str | None = None,
    subtask_id: int | None = None,
    executor_name: str | None = None,
    target: str | None = None,
) -> None:
    from browser_audit import append_browser_audit_record
    from browser_workflows import get_browser_workflow

    workflow = get_browser_workflow(workflow_id) if workflow_id else None
    append_browser_audit_record(
        command=command,
        workflow_id=workflow_id,
        action=workflow.audit_action if workflow else None,
        outcome=outcome,
        reason=reason,
        cdp_port=cdp_port if cdp_port is not None else (readiness or {}).get("cdp_port"),
        cdp_reachable=(readiness or {}).get("cdp_reachable"),
        surface=_browser_actor_surface(adapter, incoming),
        session_id=_browser_session_id(incoming),
        target_url=target_url,
        subtask_id=subtask_id,
        executor_name=executor_name,
        target=target,
    )


def _format_browser_blocked(decision: Any) -> str:
    return (
        "Browser workflow blocked.\n"
        f"  workflow: {decision.workflow_id}\n"
        f"  reason: {decision.reason}\n"
        f"  next: {decision.next_action}"
    )


def _reject_browser_target(value: str, targets: tuple[str, ...]) -> ValueError:
    exc = ValueError(
        f"unknown browser target {value!r} — valid targets: " + ", ".join(targets)
    )
    exc.rejected_target = value  # type: ignore[attr-defined]
    return exc


def _extract_browser_target(parts: list[str], targets: tuple[str, ...]) -> tuple[str, list[str]]:
    """Pull an optional browser target out of a parsed ``/browser`` arg list.

    Accepts ``--target X`` / ``-t X`` / ``--target=X`` anywhere, or a bare
    ``phone`` / ``ghost`` keyword in LEADING or TRAILING position only —
    whole-list bare-keyword scanning ate real arguments (PhoneOps review F3,
    issue #91). A bare ``desktop`` is NOT stripped, so a real argument is never
    mistaken for the target. An invalid value on the explicit flag forms raises
    ValueError (mirrors the HTTP path's 400) instead of silently falling back
    to the desktop — a typo must never reroute a command to the operator's
    visible Chrome.
    """

    target = "desktop"
    remaining: list[str] = []
    i = 0
    n = len(parts)
    while i < n:
        tok = parts[i]
        low = tok.lower()
        if low in ("--target", "-t") and i + 1 < n:
            value = parts[i + 1].lower()
            if value not in targets:
                raise _reject_browser_target(parts[i + 1], targets)
            target = value
            i += 2
            continue
        if low.startswith("--target="):
            value = low.split("=", 1)[1]
            if value not in targets:
                raise _reject_browser_target(value, targets)
            target = value
            i += 1
            continue
        if low in targets and low != "desktop" and i in (0, n - 1):
            target = low
            i += 1
            continue
        remaining.append(tok)
        i += 1
    return target, remaining


def _format_ghost_status(st: dict[str, Any]) -> str:
    running, booted = bool(st.get("running")), bool(st.get("booted"))
    icon = "🟢" if (running and booted) else ("🟡" if running else "⚪")
    lines = [f"*Ghost Phone* {icon}"]
    lines.append(f"  running: {running}")
    lines.append(f"  booted: {booted}")
    lines.append(f"  serial: {st.get('serial') or '(unset — set HOMIE_GHOST_ADB_SERIAL)'}")
    if st.get("avd"):
        lines.append(f"  avd: {st.get('avd')}")
    detail = str(st.get("detail") or "").strip()
    if detail:
        lines.append(f"  detail: {detail}")
    return "\n".join(lines)


async def handle_ghost(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Ghost Phone lifecycle — status | up | down (the Homie's own background Android)."""

    import config
    import ghost_control
    from security import kill_switches

    raw = (args or "").strip()
    sub = raw.split()[0].lower() if raw else "status"

    if sub in {"help", "-h", "--help"}:
        return (
            "*Ghost Phone*\n"
            "  /ghost status — is the ghost up?\n"
            "  /ghost up — boot the ghost (headless AVD, or connect a spare)\n"
            "  /ghost down — shut it down, reclaim RAM\n\n"
            "The Homie's own background Android. Drive its browser with "
            "`/browser status ghost`. Needs HOMIE_GHOST_ENABLED=true."
        )

    if not config.get_ghost_settings().enabled:
        return "Ghost is disabled — set HOMIE_GHOST_ENABLED=true to use the ghost phone."

    if sub in {"up", "start", "boot"}:
        try:
            kill_switches.requireEnabled("ghost", caller="/ghost up")
        except kill_switches.KillSwitchDisabled:
            return "Ghost boot is disabled by kill-switch (HOMIE_KILLSWITCH_GHOST=disabled)."
        result = ghost_control.ensure_ghost_running()
        head = "🟢 Ghost is up" if result.get("ok") else "⚠️ Ghost boot did not complete"
        return f"{head}\n  status: {result.get('status')}\n  {result.get('detail', '')}".rstrip()

    if sub in {"down", "stop", "kill", "shutdown"}:
        result = ghost_control.ghost_shutdown()
        head = "⚪ Ghost shut down" if result.get("ok") else "⚠️ Ghost shutdown issue"
        return f"{head}\n  status: {result.get('status')}\n  {result.get('detail', '')}".rstrip()

    return _format_ghost_status(ghost_control.ghost_status())


async def handle_browser(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Framework-owned browser automation checks over visible Chrome CDP."""

    import config
    from browser_control import BROWSER_TARGETS

    raw = (args or "").strip()
    if not raw or raw.lower() in {"help", "-h", "--help"}:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser help",
            workflow_id=None,
            outcome="succeeded",
            reason="help displayed",
        )
        return (
            "*Browser Commands*\n"
            "  /browser status\n"
            "  /browser tabs\n"
            "  /browser open <url>\n"
            "  /browser snapshot\n"
            "  /browser capabilities\n"
            "  /browser guide\n\n"
            "Add a target to drive the phone or the ghost, e.g. "
            "`/browser status ghost` or `/browser open <url> phone` "
            "(default: the desktop's visible Chrome). Phone needs "
            "HOMIE_PHONEOPS_ENABLED; ghost needs HOMIE_GHOST_ENABLED.\n"
            "Uses the persistent visible Chrome/Chromium CDP session. "
            "No headless/test browser fallback."
        )

    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser",
            workflow_id=None,
            outcome="failed",
            reason=f"command parse error: {exc}",
        )
        return f"Browser command parse error: {exc}"
    try:
        target, parts = _extract_browser_target(parts, BROWSER_TARGETS)
    except ValueError as exc:
        # PhoneOps review F3 (issue #91): an invalid --target value refuses
        # loudly — it must never silently fall back to the desktop Chrome.
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser",
            workflow_id=None,
            outcome="blocked",
            reason=str(exc),
            target=getattr(exc, "rejected_target", None),
        )
        return f"Browser target error: {exc}"
    if not parts:
        return "Unknown browser command. Use: /browser status, tabs, open <url>, snapshot"
    subcommand = parts[0].lower()
    rest = parts[1:]

    # Per-target gate (default-deny): phone and ghost are separate capabilities,
    # each OFF until its own switch is set. Desktop stays ungated (M12).
    # PhoneOps review F2 (issue #90): the gate runs BEFORE the browserops
    # delegation — `/browser capabilities ghost` with the ghost disabled must
    # refuse, not silently answer for the desktop.
    if target == "phone" and not config.get_phoneops_settings().enabled:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/browser {subcommand}",
            workflow_id=None,
            outcome="blocked",
            reason="PhoneOps is disabled (HOMIE_PHONEOPS_ENABLED off)",
            target=target,
        )
        return "PhoneOps is disabled — set HOMIE_PHONEOPS_ENABLED=true to drive the phone browser."
    if target == "ghost" and not config.get_ghost_settings().enabled:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/browser {subcommand}",
            workflow_id=None,
            outcome="blocked",
            reason="Ghost is disabled (HOMIE_GHOST_ENABLED off)",
            target=target,
        )
        return "Ghost is disabled — set HOMIE_GHOST_ENABLED=true to drive the ghost browser."

    if subcommand in {"capabilities", "guide", "context", "specialist", "ops", "browserops"}:
        delegated = "capabilities" if subcommand in {"ops", "browserops", "specialist"} else subcommand
        return await handle_browserops(adapter, incoming, delegated, collect_only=collect_only)

    # PhoneOps review F3 (issue #91): a leftover token past a subcommand's
    # arity is almost always a mistyped target (`/browser open <url> ghsot`) —
    # refuse loudly instead of running the command against the default desktop.
    max_args = {"status": 0, "tabs": 0, "snapshot": 0, "open": 1}.get(subcommand)
    if max_args is not None and len(rest) > max_args:
        extra = " ".join(rest[max_args:])
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/browser {subcommand}",
            workflow_id=None,
            outcome="blocked",
            reason=f"unexpected argument(s): {extra}",
            target=target,
        )
        return (
            f"Unexpected browser argument(s): {extra}\n"
            "If that was a target, valid targets are desktop | phone | ghost "
            "(trailing keyword or --target <t>).\n"
            "Usage: /browser status | tabs | open <url> | snapshot [target]"
        )

    # PhoneOps F6 (issue #94 class): everything below runs synchronous CDP
    # probes (browser_readiness alone is up to ~4s of socket timeouts) and
    # agent-browser subprocess spawns (20s timeout each). Off-loop, so a
    # stalled browser stalls only this command — not every other chat user,
    # the heartbeat, and the MC relay sharing this event loop.
    return await asyncio.to_thread(
        _handle_browser_subcommand_sync, adapter, incoming, raw, target, subcommand, rest
    )


def _handle_browser_subcommand_sync(
    adapter: Any,
    incoming: Any,
    raw: str,
    target: str,
    subcommand: str,
    rest: list[str],
) -> str:
    """Blocking tail of /browser — runs in a worker thread (issue #94 class)."""

    from browser_control import (
        _ensure_phone_transport,
        _resolve_adb_serial_or_raise,
        browser_readiness,
        browser_status,
        ensure_phone_chrome_ready,
        format_browser_readiness,
        format_browser_status,
        format_tabs,
        is_adb_target,
        list_cdp_tabs,
        redact_text_urls,
        redact_url,
        resolve_target_port,
        run_agent_browser,
        session_for_target,
    )
    from browser_workflows import require_browser_workflow_permission

    try:
        port = resolve_target_port(target)
    except ValueError as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/browser {subcommand}",
            workflow_id=None,
            outcome="failed",
            reason=str(exc),
            target=target,
        )
        return f"Browser config error: {exc}"

    readiness = browser_readiness(port=port, target=target)

    if subcommand == "status":
        workflow_id = "browser.status"
        decision = require_browser_workflow_permission(workflow_id, raw)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser status",
            workflow_id=workflow_id,
            target=target,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        if is_adb_target(target):
            # adb targets have no desktop-window visibility guard; the readiness
            # envelope (guard status + CDP + tabs) is the meaningful status.
            output = format_browser_readiness(readiness, label=f"{target.capitalize()} Browser")
        else:
            output = format_browser_status(browser_status(port=port))
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser status",
            workflow_id=workflow_id,
            target=target,
            outcome="succeeded",
            reason="status rendered",
            readiness=readiness,
        )
        return output

    if subcommand == "tabs":
        workflow_id = "browser.tabs"
        decision = require_browser_workflow_permission(workflow_id, raw)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser tabs",
            workflow_id=workflow_id,
            target=target,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        tabs = list_cdp_tabs(port)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser tabs",
            workflow_id=workflow_id,
            target=target,
            outcome="succeeded" if tabs.get("reachable") else "failed",
            reason=str(tabs.get("error") or "tabs rendered"),
            readiness=readiness,
        )
        return format_tabs(tabs)

    if subcommand == "open":
        workflow_id = "browser.open"
        if not rest:
            decision = require_browser_workflow_permission(workflow_id, raw)
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/browser open",
                workflow_id=workflow_id,
                target=target,
                outcome=decision.outcome,
                reason=decision.reason,
                readiness=readiness,
            )
            return _format_browser_blocked(decision)
        url = rest[0]
        decision = require_browser_workflow_permission(workflow_id, raw, target_url=url)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser open",
            workflow_id=workflow_id,
            target=target,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
            target_url=url,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        try:
            if is_adb_target(target):
                # Ghost threads its OWN serial (raises if unset) — never the phone's.
                ensure_phone_chrome_ready(
                    local_port=port, serial=_resolve_adb_serial_or_raise(target)
                )
            result = run_agent_browser(
                ["open", url], port=port, session=session_for_target(target)
            )
        except Exception as exc:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/browser open",
                workflow_id=workflow_id,
                target=target,
                outcome="failed",
                reason=str(exc),
                readiness=readiness,
                target_url=url,
            )
            return f"Browser open failed: {redact_text_urls(str(exc))}"
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser open",
            workflow_id=workflow_id,
            target=target,
            outcome="succeeded" if result.ok else "failed",
            reason=result.output[:1200] if not result.ok else "opened",
            readiness=readiness,
            target_url=url,
        )
        if result.ok:
            return f"Opened in visible browser: {redact_url(url)}"
        return (
            "Browser open failed.\n"
            f"  command: {redact_text_urls(result.command_label)}\n"
            f"  exit: {result.returncode}\n"
            f"  output: {redact_text_urls(result.output[:1200]) or '(no output)'}"
        )

    if subcommand == "snapshot":
        workflow_id = "browser.snapshot"
        decision = require_browser_workflow_permission(workflow_id, raw)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser snapshot",
            workflow_id=workflow_id,
            target=target,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        try:
            if is_adb_target(target):
                # Read prehook (heal forward, wake, dismiss) — no foregrounding.
                _ensure_phone_transport(port, serial=_resolve_adb_serial_or_raise(target))
            result = run_agent_browser(
                ["snapshot", "-i", "-c"], port=port, session=session_for_target(target)
            )
        except Exception as exc:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/browser snapshot",
                workflow_id=workflow_id,
                target=target,
                outcome="failed",
                reason=str(exc),
                readiness=readiness,
            )
            return f"Browser snapshot failed: {redact_text_urls(str(exc))}"
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browser snapshot",
            workflow_id=workflow_id,
            target=target,
            outcome="succeeded" if result.ok else "failed",
            reason=result.output[:1200] if not result.ok else "snapshot completed",
            readiness=readiness,
        )
        if result.ok:
            return redact_text_urls(result.output[:4000]) or "Snapshot completed with no output."
        return (
            "Browser snapshot failed.\n"
            f"  command: {redact_text_urls(result.command_label)}\n"
            f"  exit: {result.returncode}\n"
            f"  output: {redact_text_urls(result.output[:1200]) or '(no output)'}"
        )

    _audit_browser_action(
        adapter=adapter,
        incoming=incoming,
        command=f"/browser {subcommand}",
        workflow_id=None,
        outcome="failed",
        reason="unknown browser command",
        readiness=readiness,
        target=target,
    )
    return "Unknown browser command. Use: /browser status, tabs, open <url>, snapshot"


async def handle_browserops(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str:
    """Load Browser Homie context and agent-browser best practices on demand."""

    # PhoneOps F6 (issue #94 class): capability/guide/context all run the
    # synchronous browser_readiness CDP probes (and the guide loader reads
    # the installed agent-browser package) — same off-loop treatment as
    # /browser subcommands so a dead CDP socket can't stall the whole bot.
    return await asyncio.to_thread(
        _handle_browserops_sync, adapter, incoming, args, collect_only
    )


def _handle_browserops_sync(
    adapter: Any,
    incoming: Any,
    args: str,
    collect_only: bool,
) -> str:
    """Blocking body of /browserops — runs in a worker thread (issue #94 class)."""

    from browser_control import browser_readiness
    from browser_ops import (
        build_browserops_capability_pack,
        build_browserops_prefetch_context,
        format_browserops_capabilities,
        format_browserops_guide,
    )
    from browser_workflows import require_browser_workflow_permission

    raw = (args or "").strip()
    subcommand = raw.lower().split()[0] if raw else ("context" if collect_only else "capabilities")
    if subcommand in {"status", "capability", "capabilities", "specialist"}:
        workflow_id = "browserops.capabilities"
        decision = require_browser_workflow_permission(workflow_id, raw or "capabilities")
        readiness = browser_readiness()
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browserops capabilities",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        pack = build_browserops_capability_pack(raw, include_core_guide=False)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browserops capabilities",
            workflow_id=workflow_id,
            outcome="succeeded",
            reason="capabilities rendered",
            readiness=pack.get("readiness"),
        )
        return format_browserops_capabilities(pack)

    if subcommand == "guide":
        workflow_id = "browserops.guide"
        decision = require_browser_workflow_permission(workflow_id, raw)
        readiness = browser_readiness()
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browserops guide",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        pack = build_browserops_capability_pack(raw, include_core_guide=True)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browserops guide",
            workflow_id=workflow_id,
            outcome="succeeded",
            reason=str(pack.get("guide", {}).get("reason") or "guide rendered"),
            readiness=pack.get("readiness"),
        )
        return format_browserops_guide(pack)

    if subcommand in {"context", "prefetch"}:
        workflow_id = "browserops.context"
        decision = require_browser_workflow_permission(workflow_id, raw)
        readiness = browser_readiness()
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browserops context",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        context = build_browserops_prefetch_context(raw)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/browserops context",
            workflow_id=workflow_id,
            outcome="succeeded",
            reason="context rendered",
            readiness=readiness,
        )
        return context

    _audit_browser_action(
        adapter=adapter,
        incoming=incoming,
        command=f"/browserops {subcommand}",
        workflow_id=None,
        outcome="failed",
        reason="unknown browserops command",
    )
    return "Unknown BrowserOps command. Use: /browserops capabilities, guide, or context"


async def handle_linkedin_profile(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str:
    """LinkedIn-specific wrapper over the shared browser helper."""

    from browser_control import (
        browser_readiness,
        browser_status,
        format_browser_status,
        redact_text_urls,
        redact_url,
        resolve_cdp_port,
        resolve_linkedin_profile_url,
        run_agent_browser,
    )
    from browser_workflows import require_browser_workflow_permission

    raw = (args or "").strip()
    if not raw:
        # Issue #36 — natural-language intent dispatch passes args="" (router.py);
        # infer the subcommand from the message text. Explicit slash commands always
        # carry the subcommand, so this branch only affects the NL path. Default is
        # the read-only "status"; "open" fires only on an explicit navigation signal
        # and still passes through the linkedin.profile.open workflow gate below.
        nl_text = (getattr(incoming, "text", "") or "").lower()
        open_signals = ("open", "navigate", "go to", "pull up", "launch", "show me")
        raw = "open" if any(sig in nl_text for sig in open_signals) else "status"
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/linkedin_profile",
            workflow_id=None,
            outcome="failed",
            reason=f"command parse error: {exc}",
        )
        return f"LinkedIn profile command parse error: {exc}"
    subcommand = parts[0].lower() if parts else "status"
    try:
        port = resolve_cdp_port(
            env_names=(
                "HOMIE_LINKEDIN_CDP_PORT",
                "LINKEDIN_BROWSER_CDP_PORT",
                "HOMIE_BROWSER_CDP_PORT",
                "AGENT_BROWSER_CDP_PORT",
            )
        )
    except ValueError as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/linkedin_profile {subcommand}",
            workflow_id=None,
            outcome="failed",
            reason=str(exc),
        )
        return f"LinkedIn browser config error: {exc}"

    readiness = await asyncio.to_thread(browser_readiness, port=port)

    if subcommand in {"", "status"}:
        workflow_id = "browser.status"
        decision = require_browser_workflow_permission(workflow_id, raw)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/linkedin_profile status",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        output = format_browser_status(
            await asyncio.to_thread(browser_status, port=port), label="LinkedIn Browser"
        )
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/linkedin_profile status",
            workflow_id=workflow_id,
            outcome="succeeded",
            reason="status rendered",
            readiness=readiness,
        )
        return output

    if subcommand == "open":
        workflow_id = "linkedin.profile.open"
        url = resolve_linkedin_profile_url()
        if not url:
            reason = "LinkedIn profile URL is not configured."
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/linkedin_profile open",
                workflow_id=workflow_id,
                outcome="blocked",
                reason=reason,
                readiness=readiness,
            )
            return (
                f"{reason} Set HOMIE_LINKEDIN_PROFILE_URL or LINKEDIN_PROFILE_URL, "
                "then retry."
            )
        decision = require_browser_workflow_permission(workflow_id, raw, target_url=url)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/linkedin_profile open",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
            target_url=url,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        try:
            result = await asyncio.to_thread(run_agent_browser, ["open", url], port=port)
        except Exception as exc:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/linkedin_profile open",
                workflow_id=workflow_id,
                outcome="failed",
                reason=str(exc),
                readiness=readiness,
                target_url=url,
            )
            return f"LinkedIn profile open failed: {redact_text_urls(str(exc))}"
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/linkedin_profile open",
            workflow_id=workflow_id,
            outcome="succeeded" if result.ok else "failed",
            reason=result.output[:1200] if not result.ok else "opened",
            readiness=readiness,
            target_url=url,
        )
        if result.ok:
            return f"Opened LinkedIn profile in visible browser: {redact_url(url)}"
        return (
            "LinkedIn profile open failed.\n"
            f"  command: {redact_text_urls(result.command_label)}\n"
            f"  exit: {result.returncode}\n"
            f"  output: {redact_text_urls(result.output[:1200]) or '(no output)'}"
        )

    if subcommand == "edit":
        workflow_id = "linkedin.profile.edit"
        decision = require_browser_workflow_permission(workflow_id, raw)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/linkedin_profile edit",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        url = resolve_linkedin_profile_url()
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/linkedin_profile edit",
            workflow_id=workflow_id,
            outcome="failed",
            reason="write workflow registered but not implemented in Phase 2",
            readiness=readiness,
            target_url=url,
        )
        return (
            "LinkedIn profile edit is permissioned but not implemented in Phase 2. "
            "No browser write action was performed."
        )

    _audit_browser_action(
        adapter=adapter,
        incoming=incoming,
        command=f"/linkedin_profile {subcommand}",
        workflow_id=None,
        outcome="failed",
        reason="unknown LinkedIn profile command",
        readiness=readiness,
    )
    return "Unknown LinkedIn profile command. Use: /linkedin_profile status or /linkedin_profile open"


def _reddit_search_url(query: str) -> str:
    """Build a Reddit URL for a research query. A leading 'r/<sub>' browses that
    subreddit; anything else runs a site search sorted by relevance over the year."""
    from urllib.parse import quote_plus

    q = query.strip()
    if q.lower().startswith("r/"):
        sub = q.split()[0].strip("/")
        return f"https://www.reddit.com/{sub}/"
    return f"https://www.reddit.com/search/?q={quote_plus(q)}&sort=relevance&t=year"


_REDDIT_SUBREDDIT_RE = None  # lazily compiled in _validate_reddit_subreddit


def _validate_reddit_thread_url(thread_url: str) -> str | None:
    """Return an error string if ``thread_url`` is not a safe reddit https URL.

    Closes the comment-target injection surface: the thread URL is driven straight
    into ``open`` with no scheme/host check. Require an absolute https URL on a
    reddit host. Returns None when valid.
    """
    from urllib.parse import urlsplit

    from browser_control import validate_web_url

    try:
        validate_web_url(thread_url)
    except ValueError as exc:
        return str(exc)
    parsed = urlsplit(thread_url)
    if parsed.scheme != "https":
        return "thread URL must use https"
    host = (parsed.hostname or "").lower()
    if host != "reddit.com" and not host.endswith(".reddit.com"):
        return "thread URL must be on reddit.com"
    return None


def _validate_reddit_subreddit(sub: str) -> str | None:
    """Return an error string if ``sub`` is not a safe subreddit name.

    Closes the r/{sub}/submit interpolation surface: a subreddit with a slash,
    '?', '#', or '..' would smuggle a path/query into the constructed submit URL.
    Reddit subreddit names are ``^[A-Za-z0-9_]{2,21}$``. Returns None when valid.
    """
    import re as _re

    global _REDDIT_SUBREDDIT_RE
    if _REDDIT_SUBREDDIT_RE is None:
        _REDDIT_SUBREDDIT_RE = _re.compile(r"^[A-Za-z0-9_]{2,21}$")
    if not _REDDIT_SUBREDDIT_RE.match(sub):
        return "subreddit must match ^[A-Za-z0-9_]{2,21}$ (no slashes, query, or path)"
    # Belt-and-suspenders: the constructed submit URL must also be a valid https URL.
    from browser_control import validate_web_url

    submit_url = f"https://www.reddit.com/r/{sub}/submit?type=TEXT"
    try:
        validate_web_url(submit_url)
    except ValueError as exc:
        return str(exc)
    return None


def _strip_phrase(text: str, phrase: str) -> str:
    """Remove a trailing approval phrase (case-insensitive) from drafted content."""
    lower = text.lower()
    idx = lower.rfind(phrase.lower())
    if idx == -1:
        return text.strip()
    return (text[:idx] + text[idx + len(phrase):]).strip()


def _split_social_args(rest: str, approval: str, *, body_segments: int) -> tuple[list[str], bool]:
    """Split pipe-delimited social-write args into content segments + an isolated
    confirmation flag.

    Ban-safety invariant (the FIX): the operator's approval MUST be a DISTINCT
    trailing pipe-delimited segment that EXACTLY matches the approval phrase. The
    body can NEVER satisfy approval, even if the body itself ends with the phrase,
    because approval is decided ONLY on the last segment by exact equality — never
    by scanning the whole message.

    Args:
        rest: the operator's args after the subcommand (e.g. "<url> | <body> | <phrase>").
        approval: the EXACT approval phrase for this workflow.
        body_segments: how many leading content segments the command carries
            (LinkedIn post/connect = 2: url|body; reddit comment = 2: url|body;
            reddit post = 3: sub|title|body).

    Returns:
        (segments, approved) where:
          - segments is a list of exactly ``body_segments`` content strings
            (stripped, padded with "" if the operator supplied fewer). The FINAL
            content field keeps any literal pipes the body contained. The
            confirmation segment, when present, is NEVER included here.
          - approved is True ONLY when a trailing segment beyond the content
            segments exists AND exactly equals the approval phrase (normalized).

    Pipe-in-body safety: a body may itself contain '|'. Approval is decided by
    peeling ONLY the final '|'-delimited segment and exact-matching it to the
    phrase. The body keeps its pipes; only a true trailing confirmation is peeled.
    """

    raw = rest or ""
    approved = False
    content = raw
    # Peel ONLY the final segment and test it for an exact confirmation match. A
    # body that merely ENDS with the phrase (no preceding '|') never matches,
    # because there is no trailing segment to peel.
    if "|" in raw:
        head, _, tail = raw.rpartition("|")
        if _normalize_confirmation(tail) == _normalize_confirmation(approval):
            approved = True
            content = head  # everything before the confirmation segment
    # Split the content into the first (body_segments - 1) fields; the FINAL
    # field keeps any remaining pipes so a body with '|' is preserved intact.
    if body_segments <= 1:
        parts = [content.strip()]
    else:
        parts = [p.strip() for p in content.split("|", body_segments - 1)]
    segments = [parts[i] if i < len(parts) else "" for i in range(body_segments)]
    return segments, approved


def _normalize_confirmation(text: str) -> str:
    """Whitespace/case-normalize a confirmation segment for EXACT comparison."""
    import re as _re

    return _re.sub(r"\s+", " ", (text or "").strip().lower())


def _reddit_drive_comment(port: int, thread_url: str, body: str) -> tuple[bool, str]:
    """Drive the visible browser to post a comment reply on a Reddit thread.

    SELECTORS: verify against the live Reddit (shreddit) UI before the first real
    post. The composer is a contenteditable and the submit control is the "Comment"
    button; finalize these refs interactively during the supervised first run.
    """
    from browser_control import redact_text_urls, run_agent_browser

    for step in (["open", thread_url], ["wait", "--load", "networkidle"]):
        result = run_agent_browser(step, port=port)
        if not result.ok:
            return False, f"{step[0]} failed: {redact_text_urls(result.output[:600]) or '(no output)'}"
    fill = run_agent_browser(["find", "role", "textbox", "fill", body], port=port)
    if not fill.ok:
        return False, f"comment box fill failed: {redact_text_urls(fill.output[:600]) or '(no output)'}"
    submit = run_agent_browser(["find", "role", "button", "click", "--name", "Comment"], port=port)
    if not submit.ok:
        return False, f"comment submit failed: {redact_text_urls(submit.output[:600]) or '(no output)'}"
    return True, "comment submitted"


def _reddit_drive_post(port: int, subreddit: str, title: str, body: str) -> tuple[bool, str]:
    """Drive the visible browser to create a Reddit self-post.

    SELECTORS: verify against the live Reddit (shreddit) submit UI before the first
    real post; finalize the title/body/Post refs during the supervised first run.
    """
    from browser_control import redact_text_urls, run_agent_browser

    sub = subreddit.strip().strip("/")
    if sub.lower().startswith("r/"):
        sub = sub[2:]
    submit_url = f"https://www.reddit.com/r/{sub}/submit?type=TEXT"
    for step in (["open", submit_url], ["wait", "--load", "networkidle"]):
        result = run_agent_browser(step, port=port)
        if not result.ok:
            return False, f"{step[0]} failed: {redact_text_urls(result.output[:600]) or '(no output)'}"
    title_step = run_agent_browser(["find", "placeholder", "Title", "fill", title], port=port)
    if not title_step.ok:
        return False, f"title fill failed: {redact_text_urls(title_step.output[:600]) or '(no output)'}"
    body_step = run_agent_browser(["find", "role", "textbox", "fill", body], port=port)
    if not body_step.ok:
        return False, f"body fill failed: {redact_text_urls(body_step.output[:600]) or '(no output)'}"
    submit = run_agent_browser(["find", "role", "button", "click", "--name", "Post"], port=port)
    if not submit.ok:
        return False, f"post submit failed: {redact_text_urls(submit.output[:600]) or '(no output)'}"
    return True, "post submitted"


def _reddit_drive_comment_locked(port: int, thread_url: str, body: str) -> tuple[bool, str]:
    """Comment drive under the cross-process browser-write lock (to_thread tail)."""
    from shared import browser_write_lock

    with browser_write_lock():
        return _reddit_drive_comment(port, thread_url, body)


def _reddit_drive_post_locked(port: int, subreddit: str, title: str, body: str) -> tuple[bool, str]:
    """Post drive under the cross-process browser-write lock (to_thread tail)."""
    from shared import browser_write_lock

    with browser_write_lock():
        return _reddit_drive_post(port, subreddit, title, body)


async def handle_reddit(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str:
    """Reddit operator over the visible Chrome CDP session.

    research = read-only thread search. comment / post = explicit-approval browser
    writes (the first real write-execution path). Drives whatever Reddit account is
    logged into the visible session. Generic framework capability - the business
    playbook (target subs, voice) lives in the consuming repo, not here.
    """

    from browser_control import (
        browser_readiness,
        browser_status,
        format_browser_status,
        redact_text_urls,
        redact_url,
        resolve_cdp_port,
        run_agent_browser,
    )
    from browser_workflows import require_browser_workflow_permission

    raw = (args or "").strip()
    if not raw or raw.lower() in {"help", "-h", "--help"}:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit help",
            workflow_id=None,
            outcome="succeeded",
            reason="help displayed",
        )
        return (
            "*Reddit Commands*\n"
            "  /reddit status\n"
            "  /reddit research <query or r/subreddit>\n"
            "  /reddit comment <thread_url> | <body> | <approval phrase>\n"
            "  /reddit post <subreddit> | <title> | <body> | <approval phrase>\n\n"
            "Research is read-only. Comment and post require explicit approval as the\n"
            "FINAL pipe-delimited segment, matching EXACTLY:\n"
            "  comment -> \"post this comment to reddit now\"\n"
            "  post    -> \"post this to reddit now\"\n"
            "The body can never approve itself - the confirmation is a separate segment.\n"
            "Drives the visible Chrome CDP session - no API, no headless fallback."
        )

    split_once = raw.split(None, 1)
    subcommand = split_once[0].lower()
    rest = split_once[1].strip() if len(split_once) > 1 else ""

    try:
        port = resolve_cdp_port(
            env_names=(
                "HOMIE_REDDIT_CDP_PORT",
                "REDDIT_BROWSER_CDP_PORT",
                "HOMIE_BROWSER_CDP_PORT",
                "AGENT_BROWSER_CDP_PORT",
            )
        )
    except ValueError as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/reddit {subcommand}",
            workflow_id=None,
            outcome="failed",
            reason=str(exc),
        )
        return f"Reddit browser config error: {exc}"

    readiness = await asyncio.to_thread(browser_readiness, port=port)

    if subcommand == "status":
        workflow_id = "browser.status"
        decision = require_browser_workflow_permission(workflow_id, raw)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit status",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        output = format_browser_status(
            await asyncio.to_thread(browser_status, port=port), label="Reddit Browser"
        )
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit status",
            workflow_id=workflow_id,
            outcome="succeeded",
            reason="status rendered",
            readiness=readiness,
        )
        return output

    if subcommand == "research":
        if not rest:
            return "Usage: /reddit research <query or r/subreddit>"
        workflow_id = "reddit.research"
        url = _reddit_search_url(rest)
        decision = require_browser_workflow_permission(workflow_id, raw)
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit research",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
            target_url=url,
        )
        if not decision.allowed:
            return _format_browser_blocked(decision)
        def _research_drive() -> tuple[Any, Any]:
            open_res = run_agent_browser(["open", url], port=port)
            if open_res.ok:
                run_agent_browser(["wait", "--load", "networkidle"], port=port)
            snap = run_agent_browser(["snapshot", "-i", "-c"], port=port)
            return open_res, snap

        try:
            open_res, snap = await asyncio.to_thread(_research_drive)
        except Exception as exc:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/reddit research",
                workflow_id=workflow_id,
                outcome="failed",
                reason=str(exc),
                readiness=readiness,
                target_url=url,
            )
            return f"Reddit research failed: {redact_text_urls(str(exc))}"
        ok = open_res.ok and snap.ok
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit research",
            workflow_id=workflow_id,
            outcome="succeeded" if ok else "failed",
            reason=(redact_text_urls(snap.output[:1200]) or "(no output)") if not ok else "research snapshot rendered",
            readiness=readiness,
            target_url=url,
        )
        if ok:
            return redact_text_urls(snap.output[:4000]) or "Research returned no readable threads."
        return (
            "Reddit research failed.\n"
            f"  command: {redact_text_urls(snap.command_label)}\n"
            f"  exit: {snap.returncode}\n"
            f"  output: {redact_text_urls(snap.output[:1200]) or '(no output)'}"
        )

    if subcommand == "comment":
        workflow_id = "reddit.comment.create"
        approval = "post this comment to reddit now"
        # FIX (ban-safety): approval is a DISTINCT trailing "| <phrase>" segment.
        # The body can never satisfy approval — even if it ends with the phrase.
        (thread_url, body), approved = _split_social_args(
            rest, approval, body_segments=2
        )
        # Validate the thread URL before driving (subreddit/URL injection hardening).
        url_error = _validate_reddit_thread_url(thread_url) if thread_url else None
        # GATE on an EMPTY user_text + the structurally-isolated approved flag —
        # the gate's own .endswith scan can NEVER see the body.
        decision = require_browser_workflow_permission(
            workflow_id, "", approved=approved, target_url=thread_url or None
        )
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit comment",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
            target_url=thread_url or None,
        )
        if not decision.allowed:
            preview = ""
            if thread_url and body:
                preview = (
                    f"\n\nReady to comment on {redact_url(thread_url)}:\n{body}\n\n"
                    f"To post it, resend with \"| {approval}\" as the final segment."
                )
            return _format_browser_blocked(decision) + preview
        if not thread_url or not body:
            return (
                "Usage: /reddit comment <thread_url> | <body> | <approval phrase>  "
                "(final segment must be exactly \"post this comment to reddit now\")"
            )
        if url_error:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/reddit comment",
                workflow_id=workflow_id,
                outcome="failed",
                reason=url_error,
                readiness=readiness,
                target_url=thread_url or None,
            )
            return f"Reddit comment rejected: {url_error}"
        if not readiness.get("enabled"):
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/reddit comment",
                workflow_id=workflow_id,
                outcome="failed",
                reason="visible-chrome not ready",
                readiness=readiness,
                target_url=thread_url or None,
            )
            return "Reddit comment failed: visible-chrome not ready."
        try:
            ok, detail = await asyncio.wait_for(
                asyncio.to_thread(_reddit_drive_comment_locked, port, thread_url, body),
                timeout=_BROWSER_WRITE_REPLY_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return (
                f"Reddit comment is still running after "
                f"{int(_BROWSER_WRITE_REPLY_TIMEOUT_S)}s — verify on Reddit before re-firing."
            )
        except Exception as exc:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/reddit comment",
                workflow_id=workflow_id,
                outcome="failed",
                reason=str(exc),
                readiness=readiness,
                target_url=thread_url,
            )
            return f"Reddit comment failed: {redact_text_urls(str(exc))}"
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit comment",
            workflow_id=workflow_id,
            outcome="succeeded" if ok else "failed",
            reason=detail,
            readiness=readiness,
            target_url=thread_url,
        )
        if ok:
            return f"Comment posted to {redact_url(thread_url)}."
        return f"Reddit comment failed: {detail}"

    if subcommand == "post":
        workflow_id = "reddit.post.create"
        approval = "post this to reddit now"
        # FIX (ban-safety): approval is a DISTINCT trailing "| <phrase>" segment.
        (subreddit, title, body), approved = _split_social_args(
            rest, approval, body_segments=3
        )
        sub_clean = subreddit.strip("/")
        if sub_clean.lower().startswith("r/"):
            sub_clean = sub_clean[2:]
        # Validate subreddit + constructed submit URL before driving (injection).
        url_error = _validate_reddit_subreddit(sub_clean) if sub_clean else None
        decision = require_browser_workflow_permission(
            workflow_id, "", approved=approved
        )
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit post",
            workflow_id=workflow_id,
            outcome=decision.outcome,
            reason=decision.reason,
            readiness=readiness,
        )
        if not decision.allowed:
            preview = ""
            if subreddit and title:
                preview = (
                    f"\n\nReady to post to r/{sub_clean}:\n{title}\n{body}\n\n"
                    f"To post it, resend with \"| {approval}\" as the final segment."
                )
            return _format_browser_blocked(decision) + preview
        if not subreddit or not title:
            return (
                "Usage: /reddit post <subreddit> | <title> | <body> | <approval phrase>  "
                "(final segment must be exactly \"post this to reddit now\")"
            )
        if url_error:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/reddit post",
                workflow_id=workflow_id,
                outcome="failed",
                reason=url_error,
                readiness=readiness,
            )
            return f"Reddit post rejected: {url_error}"
        if not readiness.get("enabled"):
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/reddit post",
                workflow_id=workflow_id,
                outcome="failed",
                reason="visible-chrome not ready",
                readiness=readiness,
            )
            return "Reddit post failed: visible-chrome not ready."
        try:
            ok, detail = await asyncio.wait_for(
                asyncio.to_thread(_reddit_drive_post_locked, port, subreddit, title, body),
                timeout=_BROWSER_WRITE_REPLY_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return (
                f"Reddit post is still running after "
                f"{int(_BROWSER_WRITE_REPLY_TIMEOUT_S)}s — verify on Reddit before re-firing."
            )
        except Exception as exc:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/reddit post",
                workflow_id=workflow_id,
                outcome="failed",
                reason=str(exc),
                readiness=readiness,
            )
            return f"Reddit post failed: {redact_text_urls(str(exc))}"
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/reddit post",
            workflow_id=workflow_id,
            outcome="succeeded" if ok else "failed",
            reason=detail,
            readiness=readiness,
        )
        if ok:
            return f"Post created in r/{subreddit.strip('/')}."
        return f"Reddit post failed: {detail}"

    _audit_browser_action(
        adapter=adapter,
        incoming=incoming,
        command=f"/reddit {subcommand}",
        workflow_id=None,
        outcome="failed",
        reason="unknown reddit command",
        readiness=readiness,
    )
    return "Unknown Reddit command. Use: /reddit status, research, comment, post"


# ---------------------------------------------------------------------------
# LinkedIn social-write handlers (Phase 1) — the HANDLER is the approval
# authority. It gates on the operator's VERBATIM message text via
# require_browser_workflow_permission and dispatches a SocialWriteTask to a
# locally-constructed BrowserExecutor ONLY when decision.allowed. There is no
# approval_token; the post body NEVER reaches the gate's user_text.
# ---------------------------------------------------------------------------


def _build_social_write_subtask(task: Any) -> Any:
    """Serialize a SocialWriteTask into a Subtask.metadata JSON envelope."""

    import json as _json
    from dataclasses import asdict

    from orchestration.models import Subtask

    return Subtask(
        title=f"social-write:{task.workflow_id}",
        metadata=_json.dumps(asdict(task)),
    )


# Bound on how long a per-action browser write may hold up its CHAT REPLY.
# The drive itself is not cancelled on timeout — threads can't be killed —
# it finishes (or dies) in the background; the operator is told to verify.
_BROWSER_WRITE_REPLY_TIMEOUT_S = 300.0


def _dispatch_social_write_locked(executor: Any, subtask: Any) -> Any:
    """Sync tail for per-action browser writes — run OFF the loop via to_thread.

    Holds the cross-process browser_write_lock for the whole drive: the CDP
    browser is one logged-in session, so the Browser Homie runner, the cadence
    cron, and per-action writes must never drive it concurrently.
    """
    from shared import browser_write_lock

    with browser_write_lock():
        return executor.dispatch(subtask)


async def _handle_social_write(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    workflow_id: str,
    command: str,
    approval: str,
    action: str,
    usage: str,
    tracker_lane: str,
    success_label: str,
) -> str:
    """Shared gate->dispatch->audit->tracker path for /linkedin_post and /linkedin_connect."""

    from browser_control import browser_readiness, redact_text_urls, redact_url, resolve_cdp_port
    from browser_workflows import require_browser_workflow_permission
    from social_write_driver import AgentBrowserSocialWriteDriver, append_tracker_row

    from orchestration.browser_executor import BrowserExecutor
    from orchestration.models import SocialWriteTask

    raw = (args or "").strip()  # operator's verbatim args (NM2 — guard None)
    if not raw or raw.lower() in {"help", "-h", "--help"}:
        return usage

    # FIX (ban-safety): the operator's approval MUST be a DISTINCT trailing
    # "| <approval phrase>" segment that EXACTLY matches the phrase. The body can
    # never satisfy approval, even if the body itself ends with the phrase — the
    # confirmation is decided ONLY on the isolated final segment by exact match.
    #   /linkedin_post <feed_url> | <body> | <approval phrase>
    (target_url, body), approved = _split_social_args(raw, approval, body_segments=2)

    try:
        port = resolve_cdp_port(
            env_names=(
                "HOMIE_LINKEDIN_CDP_PORT",
                "LINKEDIN_BROWSER_CDP_PORT",
                "HOMIE_BROWSER_CDP_PORT",
                "AGENT_BROWSER_CDP_PORT",
            )
        )
    except ValueError as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=command,
            workflow_id=workflow_id,
            outcome="failed",
            reason=str(exc),
            executor_name="browser",
        )
        return f"LinkedIn browser config error: {exc}"

    readiness = await asyncio.to_thread(browser_readiness, port=port)

    # GATE on an EMPTY user_text + the structurally-isolated approved flag — the
    # gate's own .endswith scan can NEVER see the body (R1-B3 / NM1 close-out).
    driver = AgentBrowserSocialWriteDriver()
    decision = require_browser_workflow_permission(
        workflow_id, "", approved=approved, target_url=target_url or None
    )
    _audit_browser_action(
        adapter=adapter,
        incoming=incoming,
        command=command,
        workflow_id=workflow_id,
        outcome=decision.outcome,
        reason=decision.reason,
        readiness=readiness,
        target_url=target_url or None,
        executor_name="browser",
    )
    if not decision.allowed:
        preview = ""
        if target_url and body:
            preview = (
                f"\n\nReady ({action}) to {redact_url(target_url)}:\n{body}\n\n"
                f"To run it, resend with \"| {approval}\" as the final segment."
            )
        return _format_browser_blocked(decision) + preview

    if not target_url or not body:
        return usage

    # ALLOW -> build the task (carries NO approval claim) and dispatch through a
    # LOCAL BrowserExecutor (never the shared registry — R1-M1).
    task = SocialWriteTask(
        workflow_id=workflow_id,
        target_url=target_url,
        payload_text=body,
        action=action,
    )
    subtask = _build_social_write_subtask(task)
    executor = BrowserExecutor(driver)
    # Off-loop: the drive is 20-120s of blocking subprocess work and a hung
    # agent-browser child must never wedge the event loop (2026-07-13 class).
    try:
        receipt = await asyncio.wait_for(
            asyncio.to_thread(_dispatch_social_write_locked, executor, subtask),
            timeout=_BROWSER_WRITE_REPLY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return (
            f"LinkedIn {action} is still running after "
            f"{int(_BROWSER_WRITE_REPLY_TIMEOUT_S)}s — verify on LinkedIn before re-firing."
        )

    if receipt.status == "completed":
        try:
            append_tracker_row(
                name=redact_url(target_url),
                lane=tracker_lane,
                action=action,
                status="invite-sent" if action == "connect" else "posted",
                notes=command,
            )
        except Exception:  # noqa: BLE001 - tracker write must never fail a landed write
            pass
        return f"{success_label} {redact_url(target_url)}"
    return f"LinkedIn {action} failed: {redact_text_urls(receipt.error or 'unknown error')}"


async def handle_linkedin_post(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str:
    """Create a LinkedIn post on the visible Chrome session — operator-approved per action.

    Default-deny: no write fires unless the operator's verbatim message ends with
    the approval phrase. Slash-command-only (NL phrasing stays read-only).
    """

    return await _handle_social_write(
        adapter,
        incoming,
        args,
        workflow_id="linkedin.post.create",
        command="/linkedin_post",
        approval="post this to linkedin now",
        action="post",
        usage=(
            "Usage: /linkedin_post <feed_url> | <body> | <approval phrase>  "
            "(final segment must be exactly \"post this to linkedin now\")"
        ),
        tracker_lane="LinkedIn post",
        success_label="Posted to LinkedIn:",
    )


async def handle_linkedin_connect(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str:
    """Send a LinkedIn connection request — operator-approved per action.

    Default-deny: no invite fires unless the operator's verbatim message ends
    with the approval phrase. One approval, one invite — no bulk fan-out, no
    auto-invite tooling.
    """

    return await _handle_social_write(
        adapter,
        incoming,
        args,
        workflow_id="linkedin.connection.request",
        command="/linkedin_connect",
        approval="send this linkedin connection request now",
        action="connect",
        usage=(
            "Usage: /linkedin_connect <profile_url> | <note> | <approval phrase>  "
            "(final segment must be exactly \"send this linkedin connection request now\")"
        ),
        tracker_lane="LinkedIn connect",
        success_label="Connection request sent:",
    )


def _import_x_scout() -> Any:
    """Import the scripts-dir x_scout module from the chat slice.

    x_scout.py lives in ``.claude/scripts/``; core_handlers.py lives in
    ``.claude/chat/``. Add the scripts dir to sys.path once so the read-only
    scout brain (planner + browser collector + rate guard) is importable from
    the router process. x_scout itself imports ``browser_control`` (already on
    the chat path) for the cookie-free visible-browser collector.
    """
    import sys

    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import x_scout  # type: ignore[import-not-found]

    return x_scout


async def handle_x(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """X scout - read-only timeline/search/scout via the visible Chrome CDP session.

    Cookie-free: drives the EXISTING logged-in browser on CDP 9222 (no cookie
    read/seed/export). Every subcommand is gated through the browser workflow
    registry (x.scout / x.timeline / x.search are read-classification) and
    audited via append_browser_audit_record. The scout NEVER posts.

    Subcommands:
      /x scout [angle] [intent...]   - scouted signal digest (default angle: latest)
      /x timeline [angle]            - alias for scout on the bare-feed angles
      /x search <subject>            - scout the LATEST angle for a subject
    """

    from browser_control import browser_readiness, redact_text_urls, resolve_cdp_port
    from browser_workflows import require_browser_workflow_permission

    raw = (args or "").strip()
    if not raw or raw.lower() in {"help", "-h", "--help"}:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/x help",
            workflow_id=None,
            outcome="succeeded",
            reason="help displayed",
        )
        return (
            "*X Scout (read-only)*\n"
            "  /x scout [angle] [intent]   - signal digest (angles: latest, trusted, breaking, threads, who, projects)\n"
            "  /x timeline [angle]         - read the timeline for a bare-feed angle\n"
            "  /x search <subject>         - scout the latest angle for a subject\n\n"
            "Drives the existing logged-in visible Chrome (CDP). No cookies handled, "
            "no posting. @your-x-handle is never automated."
        )

    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command="/x",
            workflow_id=None,
            outcome="failed",
            reason=f"command parse error: {exc}",
        )
        return f"X command parse error: {exc}"

    subcommand = parts[0].lower()
    rest = parts[1:]

    workflow_by_sub = {
        "scout": "x.scout",
        "timeline": "x.timeline",
        "search": "x.search",
    }
    if subcommand not in workflow_by_sub:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/x {subcommand}",
            workflow_id=None,
            outcome="failed",
            reason="unknown x command",
        )
        return "Unknown X command. Use: /x scout, /x timeline, /x search <subject>"

    workflow_id = workflow_by_sub[subcommand]

    try:
        port = resolve_cdp_port()
    except ValueError as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/x {subcommand}",
            workflow_id=workflow_id,
            outcome="failed",
            reason=str(exc),
        )
        return f"X browser config error: {exc}"

    readiness = await asyncio.to_thread(browser_readiness, port=port)

    # Default-deny gate (read-classification workflows pass without approval).
    decision = require_browser_workflow_permission(workflow_id, raw)
    _audit_browser_action(
        adapter=adapter,
        incoming=incoming,
        command=f"/x {subcommand}",
        workflow_id=workflow_id,
        outcome=decision.outcome,
        reason=decision.reason,
        readiness=readiness,
    )
    if not decision.allowed:
        return _format_browser_blocked(decision)

    # CDP readiness gate - refuse cleanly instead of spawning a headless fallback.
    if not readiness.get("cdp_reachable"):
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/x {subcommand}",
            workflow_id=workflow_id,
            outcome="failed",
            reason=str(readiness.get("reason") or "CDP unreachable"),
            readiness=readiness,
        )
        return (
            "X scout needs the visible Chrome CDP session.\n"
            f"  {readiness.get('reason') or 'CDP unreachable'}\n"
            "Start real visible Chrome with the debug port and retry. No headless fallback."
        )

    # Resolve angle + subject/intent per subcommand.
    valid_angles = {"latest", "trusted", "breaking", "threads", "who", "projects"}
    angle = "latest"
    subject = ""
    intent = ""

    if subcommand == "search":
        if not rest:
            _audit_browser_action(
                adapter=adapter,
                incoming=incoming,
                command="/x search",
                workflow_id=workflow_id,
                outcome="failed",
                reason="search needs a subject",
                readiness=readiness,
            )
            return "Usage: /x search <subject>"
        subject = " ".join(rest)
    else:
        # scout / timeline: optional leading angle, remainder is a free intent.
        if rest and rest[0].lower() in valid_angles:
            angle = rest[0].lower()
            intent = " ".join(rest[1:])
        else:
            intent = " ".join(rest)

    try:
        x_scout = _import_x_scout()
    except Exception as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/x {subcommand}",
            workflow_id=workflow_id,
            outcome="failed",
            reason=f"x_scout import failed: {exc}",
            readiness=readiness,
        )
        return f"X scout unavailable: {redact_text_urls(str(exc))}"

    # Planner fan-out: when an intent is given, translate it to 1-3 signal-dense
    # subjects via the Pass-0 planner (subscription lane).
    #
    # TODO(ask-user-preview): the QM server gated planned-intent digests behind
    # an auq_bridge.ask preview (Run all / Run #1 / Broaden / Narrow / Cancel)
    # before searching X. No local ask-user bridge exists in the chat slice yet
    # (grep for auq_bridge / AskUserQuestion: none). Until one lands, run the
    # planned subjects directly. When a bridge exists, preview the planned
    # subjects here and only run the approved set.
    subjects = None
    if intent.strip():
        try:
            planned = await x_scout.plan(intent.strip())
            subjects = [s["search_query"] for s in planned] or None
        except Exception:
            subjects = None  # planner never raises, but stay defensive

    # run_scout is sync + does blocking subprocess/sleep against the browser;
    # run it off the event loop.
    try:
        output = await asyncio.to_thread(
            x_scout.run_scout,
            angle,
            subject,
            None,
            False,
            kind="adhoc",
            subjects=subjects,
            port=port,
        )
    except Exception as exc:
        _audit_browser_action(
            adapter=adapter,
            incoming=incoming,
            command=f"/x {subcommand}",
            workflow_id=workflow_id,
            outcome="failed",
            reason=f"scout error: {exc}",
            readiness=readiness,
        )
        return f"X scout failed: {redact_text_urls(str(exc))}"

    _audit_browser_action(
        adapter=adapter,
        incoming=incoming,
        command=f"/x {subcommand}",
        workflow_id=workflow_id,
        outcome="succeeded",
        reason="scout rendered",
        readiness=readiness,
    )
    return output


async def handle_inbox(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Scan inbox for triage briefing."""
    try:
        from integrations.email_triage import format_briefing, scan_inbox

        sub = args.strip().lower() if args else ""
        unread_only = sub != "all"
        max_per = 30 if sub == "all" else 20
        briefing = scan_inbox(max_per_source=max_per, unread_only=unread_only)
        return format_briefing(briefing)
    except Exception as e:
        return f"Error scanning inbox: {e}"


async def handle_cleanup(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Email cleanup — dry run or execute."""
    try:
        from integrations.email_cleanup import (
            execute_cleanup,
            format_dry_run,
            scan_gmail,
            scan_outlook,
        )

        sub = args.strip().lower() if args else ""
        platform_str = incoming.platform.value
        channel_id = incoming.channel.platform_id
        cache_key = f"{platform_str}:{channel_id}"

        if sub == "go":
            cached = _cleanup_cache.get(cache_key)
            if cached and (time.time() - cached[2]) < 300:
                gmail_r, outlook_r = cached[0], cached[1]
            else:
                gmail_r = scan_gmail(max_results=100)
                outlook_r = scan_outlook(max_results=50)
            reply = execute_cleanup(gmail_r, outlook_r)
            _cleanup_cache.pop(cache_key, None)
        else:
            gmail_r = scan_gmail(max_results=100)
            outlook_r = scan_outlook(max_results=50)
            _cleanup_cache[cache_key] = (gmail_r, outlook_r, time.time())
            reply = format_dry_run(gmail_r, outlook_r)
        return reply
    except Exception as e:
        return f"Error during cleanup: {e}"


async def handle_analytics(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Fetch Google Analytics data."""
    try:
        from integrations.analytics_api import (
            format_overview_for_context,
            format_sources_for_context,
            get_overview,
            get_traffic_sources,
        )

        parts_ga: list[str] = []
        overview = get_overview(days=28)
        parts_ga.append(format_overview_for_context(overview))
        sources = get_traffic_sources(days=28, max_results=10)
        parts_ga.append(format_sources_for_context(sources))
        return "\n\n".join(parts_ga)
    except Exception as e:
        return f"Error fetching Analytics data: {e}"


async def handle_signal(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Business signal digest — status or refresh."""
    subcmd = args.strip().lower() if args.strip() else ""

    if subcmd == "refresh":
        try:
            import sys
            from pathlib import Path

            _scripts = Path(__file__).resolve().parent.parent / "scripts"
            if str(_scripts) not in sys.path:
                sys.path.insert(0, str(_scripts))

            from business_signal.signal_engine import run_signal_engine

            result = await run_signal_engine(test_mode=False)
            return f"Signal engine run complete: {result}"
        except Exception as e:
            return f"Signal engine error: {e}"

    try:
        import sys
        from pathlib import Path

        _scripts = Path(__file__).resolve().parent.parent / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))

        from business_signal.signal_engine import get_latest_status

        return get_latest_status()
    except Exception as e:
        return f"Signal status error: {e}"


def _stars_unknown_repo(name: str) -> str:
    """Error message for an unresolvable repo name, listing current picks."""
    try:
        import sys
        from pathlib import Path

        _scripts = Path(__file__).resolve().parent.parent / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))
        from github_signal import state as gh_state

        picks = gh_state.load().get("last_picks", [])
        if picks:
            names = ", ".join(str(p.get("full_name", "?")) for p in picks)
            return f"Couldn't match {name!r}. Current picks: {names}"
    except Exception:
        pass
    return f"Couldn't match {name!r} — use the full owner/repo name."


async def handle_stars(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """GitHub star backlog — status, refresh (detached), used/snooze, trending."""
    import sys
    from pathlib import Path

    _scripts = Path(__file__).resolve().parent.parent / "scripts"
    if str(_scripts) not in sys.path:
        sys.path.insert(0, str(_scripts))

    parts = args.strip().split()
    subcmd = parts[0].lower() if parts else "status"

    try:
        if subcmd == "status":
            from github_signal.engine import get_latest_status

            return get_latest_status()

        if subcmd == "refresh":
            # The starred fetch is ~8s of sync urllib + an LLM call — never
            # run that on the bot event loop (2026-07-13 wedge rule). Spawn
            # detached; the engine delivers its own Telegram card when done.
            from config import STATE_DIR
            from shared import spawn_detached

            pid = spawn_detached(
                ["uv", "run", "python", "-m", "github_signal.engine"],
                cwd=_scripts,
                log_path=STATE_DIR / "github-signal-refresh.log",
            )
            return (
                f"⭐ GitHub signal run started (pid {pid}) — the digest and "
                f"Telegram card will land when it finishes (~1 min)."
            )

        if subcmd == "eval":
            if len(parts) < 2:
                return "Usage: /stars eval <owner/repo>"
            from config import STATE_DIR
            from github_signal import state as gh_state
            from shared import spawn_detached

            resolved = gh_state.resolve_name(parts[1])
            if resolved is None:
                return _stars_unknown_repo(parts[1])
            log_name = f"github-signal-eval-{resolved.replace('/', '__')}.log"
            pid = spawn_detached(
                ["uv", "run", "python", "-m", "github_signal.eval_runner", resolved],
                cwd=_scripts,
                log_path=STATE_DIR / log_name,
            )
            return (
                f"🔬 Eval started for {resolved} (pid {pid}) — read-only "
                f"analysis; the verdict card lands in a few minutes."
            )

        if subcmd == "used":
            if len(parts) < 2:
                return "Usage: /stars used <repo>"
            from github_signal import state as gh_state

            resolved = gh_state.mark_used(parts[1])
            if resolved is None:
                return _stars_unknown_repo(parts[1])
            return f"✅ Marked used: {resolved} — it won't be resurfaced again."

        if subcmd == "snooze":
            if len(parts) < 2:
                return "Usage: /stars snooze <repo> [weeks]"
            from github_signal import state as gh_state
            from github_signal.config import get_github_signal_settings

            weeks = get_github_signal_settings().snooze_weeks
            if len(parts) >= 3:
                try:
                    weeks = max(1, int(parts[2]))
                except ValueError:
                    return f"weeks must be a number (got {parts[2]!r})"
            resolved = gh_state.mark_snoozed(parts[1], weeks=weeks)
            if resolved is None:
                return _stars_unknown_repo(parts[1])
            return f"💤 Snoozed {resolved} for {weeks} weeks."

        if subcmd == "trending":
            from github_signal import state as gh_state

            trending = gh_state.load().get("last_trending", [])
            if not trending:
                return "No trending hits stored yet — /stars refresh to fetch."
            lines = ["🔥 Trending (last run):"]
            for t in trending[:10]:
                desc = (t.get("description") or "").strip()[:80]
                lines.append(
                    f"- {t.get('full_name')} ★{t.get('stars', '?')} — {desc}"
                )
            return "\n".join(lines)

        return (
            "Unknown subcommand. Usage: /stars [status|refresh|eval <owner/repo>|"
            "used <repo>|snooze <repo> [weeks]|trending]"
        )
    except TimeoutError:
        return "A signal run is writing state right now — try again in a moment."
    except Exception as e:
        return f"Stars error: {e}"


async def handle_budget(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Personal finance / budget commands — requires finance integration modules."""
    return "Finance module not configured. See docs for Teller/Plaid setup."

async def handle_cabinet(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False
) -> str:
    """Multi-persona text meeting — `/cabinet [create | list | send <id> <text> | end <id>]`.

    Roster comes from whichever cabinet-eligible personas Phase 5a's
    `_roster_from_personas()` snapshots at meeting-create time
    (`cabinet/text_orchestrator.py:81-130`). Operators manage active personas
    via `/persona` BEFORE running `/cabinet create`. R1 B4 fix — handlers do
    NOT validate persona ids; the API layer auto-snapshots.
    """
    from integrations import cabinet_api  # lazy: avoid HTTP/httpx cost on every import
    from security import kill_switches  # Phase 7b WS3 — Rule 3 module-attribute lookup
    from cabinet_relay import ensure_relay  # lazy: relay persona turns to chat

    args = (args or "").strip()
    if not args or args.lower() in {"help", "?"}:
        return _cabinet_usage_text()

    chat_id = getattr(incoming, "chat_id", None)
    chat_id_str = str(chat_id) if chat_id else None

    try:
        # Phase 7b WS3 kill-switch — chat-process side of symmetric cabinet gate.
        # Phase 5a's API-process orchestrator also gates; this adds chat-side refusal counting.
        kill_switches.requireEnabled("cabinet", caller="handle_cabinet")

        if args.lower() == "list":
            meetings = await cabinet_api.list_meetings(limit=20, chat_id=chat_id_str)
            return _format_meeting_list(meetings)

        if args.lower() == "create":
            ref = await cabinet_api.create_meeting(chat_id=chat_id_str)
            if ensure_relay(ref.id, adapter, incoming):
                return (
                    f"Cabinet meeting #{ref.id} started — the homies will answer "
                    f"right here.\n"
                    f"Add a turn: /cabinet send {ref.id} <message>"
                )
            return (
                f"Cabinet meeting #{ref.id} started.\n"
                f"Watch live: http://localhost:3141/cabinet?id={ref.id}\n"
                f"Add a turn: /cabinet send {ref.id} <message>"
            )

        if args.lower().startswith("send"):
            # /cabinet send <id> <text>
            rest = args[len("send"):].strip()
            meeting_id_str, _, text = rest.partition(" ")
            try:
                meeting_id = int(meeting_id_str)
            except ValueError:
                return "Usage: /cabinet send <meeting_id> <text>"
            if not text.strip():
                return "Usage: /cabinet send <meeting_id> <text>"
            # /api/cabinet/send is fire-and-forget — returns 200 {ok, queued}.
            # Kill-switch refusal surfaces via SSE error event in Cabinet.tsx,
            # NOT a 503 here (R1 B6 + verified dashboard_api.py:2362-XXXX).
            await cabinet_api.send_message(
                meeting_id, text.strip(), chat_id=chat_id_str,
            )
            if ensure_relay(meeting_id, adapter, incoming):
                return f"Sent to meeting #{meeting_id} — answers will come through here."
            return (
                f"Sent to meeting #{meeting_id}. "
                f"Watch http://localhost:3141/cabinet?id={meeting_id} for persona turns "
                f"(or any system notes if cabinet is disabled)."
            )

        if args.lower().startswith("voice"):
            # PRD-8 Phase 6 — /cabinet voice [meeting_id]
            # With explicit meeting_id: verify exists + return browser URL.
            # Without: create new meeting + return browser URL.
            rest = args[len("voice"):].strip()

            if rest:
                try:
                    target_meeting_id = int(rest)
                except ValueError:
                    return "Usage: /cabinet voice [meeting_id]"
                # Verify the meeting exists in this chat scope by listing.
                # (We could add a dedicated cabinet_api.get_meeting_details
                # helper, but list_meetings is sufficient for a single
                # one-shot lookup and avoids a new API surface.)
                meetings = await cabinet_api.list_meetings(
                    limit=50, chat_id=chat_id_str
                )
                match = next(
                    (m for m in meetings if int(m.get("id", -1)) == target_meeting_id),
                    None,
                )
                if match is None:
                    return (
                        f"Meeting #{target_meeting_id} not found in this chat. "
                        f"Use /cabinet list to see meetings here."
                    )
                if match.get("ended_at"):
                    return f"Meeting #{target_meeting_id} has ended. Start a new one with /cabinet voice."
                meeting_id_for_url = target_meeting_id
            else:
                ref = await cabinet_api.create_meeting(chat_id=chat_id_str)
                meeting_id_for_url = ref.id

            # Build the browser URL operators tap to open the voice page.
            # Prefer ORCHESTRATION_API_BASE_URL when set; fall back to the
            # canonical loopback host. Token defaults to empty string in
            # loopback no-token mode.
            #
            # PRD-8 Phase 6 v2 fix-pass 2026-05-10 (B1) — use urlencode so a
            # token containing `&` / `=` / `?` / spaces does not corrupt the
            # query string and so chat_id values are URL-safe.
            import os as _os
            from urllib.parse import urlencode as _urlencode
            base = _os.environ.get(
                "ORCHESTRATION_API_BASE_URL", "http://127.0.0.1:4322"
            ).rstrip("/")
            token = _os.environ.get("ORCHESTRATION_API_TOKEN", "")
            chat_for_url = chat_id_str or ""
            qs = _urlencode(
                {
                    "token": token,
                    "meetingId": meeting_id_for_url,
                    "chatId": chat_for_url,
                }
            )
            url = f"{base}/api/cabinet/voice/ui?{qs}"
            return (
                f"Cabinet voice meeting #{meeting_id_for_url} ready.\n"
                f"Open in browser (Chrome/Edge for mic permission):\n{url}"
            )

        if args.lower().startswith("end"):
            rest = args[len("end"):].strip()
            try:
                meeting_id = int(rest)
            except ValueError:
                return "Usage: /cabinet end <meeting_id>"
            result = await cabinet_api.end_meeting(meeting_id, chat_id=chat_id_str)
            if result.get("alreadyEnded"):
                return f"Meeting #{meeting_id} was already ended."
            return f"Meeting #{meeting_id} ended."

        return _cabinet_usage_text()

    except kill_switches.KillSwitchDisabled:
        return "Cabinet is disabled by operator. Reach out for an override."
    except cabinet_api.CabinetAPIError as e:
        return e.friendly_message


async def handle_standup(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False
) -> str:
    """Standup-style seed question — `/standup [optional seed question]`.

    UX shorthand for "create cabinet meeting + send standup question as
    operator turn". Roster comes from whatever Phase 5a's
    `_roster_from_personas()` snapshots at meeting-create time
    (`cabinet/text_orchestrator.py:81-130`). NO rotating-speaker semantics —
    the Haiku classifier in `text_orchestrator` routes the question normally;
    if no @mention, it goes to the most appropriate persona.

    R2 NM2 fallback (DOCUMENTED, ALLOWED): when no cabinet-eligible personas
    are registered (or `list_profiles()` fails), `_roster_from_personas()`
    returns `[_MAIN_AGENT]` only — `/standup` then runs as a Main-only reply.
    The chat reply explicitly tells the operator a Main-only fallback may
    occur. To get a multi-persona standup, register cabinet-eligible
    personas via `/persona` BEFORE running `/standup`.

    R1/R2 fix: send the standup question as PLAIN user text. Do NOT prefix
    with `/standup` — `text_orchestrator.parse_slash_command` (`:266-276`)
    would short-circuit with a "requires Phase 5b" system_note.
    """
    from integrations import cabinet_api  # lazy
    from security import kill_switches  # Phase 7b WS3 — Rule 3 module-attribute lookup

    chat_id = getattr(incoming, "chat_id", None)
    chat_id_str = str(chat_id) if chat_id else None

    standup_q = (args or "").strip() or os.getenv(
        "CABINET_STANDUP_QUESTION",
        "What are you working on, what's blocking you, "
        "what's your highest-priority next step?",
    )

    try:
        # Phase 7b WS3 kill-switch — chat-process side of symmetric cabinet gate.
        kill_switches.requireEnabled("cabinet", caller="handle_standup")

        ref = await cabinet_api.create_meeting(chat_id=chat_id_str)
        await cabinet_api.send_message(
            ref.id, standup_q, chat_id=chat_id_str,
        )
        from cabinet_relay import ensure_relay  # lazy: relay persona turns to chat
        if ensure_relay(ref.id, adapter, incoming):
            return (
                f"Standup #{ref.id} started — the homies will answer right here "
                f"(or a Main-only reply if no cabinet-eligible personas are registered)."
            )
        return (
            f"Standup #{ref.id} started.\n"
            f"Watch http://localhost:3141/cabinet?id={ref.id} for persona answers "
            f"(or a Main-only reply if no cabinet-eligible personas are registered)."
        )
    except kill_switches.KillSwitchDisabled:
        return "Cabinet is disabled by operator. Reach out for an override."
    except cabinet_api.CabinetAPIError as e:
        return e.friendly_message


async def handle_discuss(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False
) -> str:
    """Forward operator slash to active cabinet meeting via cabinet_api.send_message.

    Phase 5a's text_orchestrator (`text_orchestrator.py:795-810`) recognizes
    `/discuss` as a slash-prefixed input and emits a system_note; Phase 5b's
    job is to ensure the slash reaches a cabinet meeting context (creates
    one if needed, then forwards the operator turn). No "multi-agent debate"
    framing — actual fan-out behavior is determined by Phase 5a's roster
    snapshot at meeting create-time. The operator's text is the SEED TURN
    (sent as plain user text via cabinet_api.send_message), not as a
    slash-prefixed command (which would short-circuit at parse_slash_command).
    """
    from integrations import cabinet_api  # lazy
    from security import kill_switches  # Phase 7b WS3 — Rule 3 module-attribute lookup

    args = (args or "").strip()
    if not args:
        return "Usage: /discuss <topic>\nExample: /discuss should we deprecate mc?"

    chat_id = getattr(incoming, "chat_id", None)
    chat_id_str = str(chat_id) if chat_id else None

    try:
        # Phase 7b WS3 kill-switch — chat-process side of symmetric cabinet gate.
        kill_switches.requireEnabled("cabinet", caller="handle_discuss")

        ref = await cabinet_api.create_meeting(chat_id=chat_id_str)
        await cabinet_api.send_message(ref.id, args, chat_id=chat_id_str)
        from cabinet_relay import ensure_relay  # lazy: relay persona turns to chat
        if ensure_relay(ref.id, adapter, incoming):
            return (
                f"Discussion #{ref.id} started — topic: {args}\n"
                f"The homies will answer right here."
            )
        return (
            f"Discussion #{ref.id} started — topic: {args}\n"
            f"Watch: http://localhost:3141/cabinet?id={ref.id}"
        )
    except kill_switches.KillSwitchDisabled:
        return "Cabinet is disabled by operator. Reach out for an override."
    except cabinet_api.CabinetAPIError as e:
        return e.friendly_message


def _parse_teamtick_args(args: str) -> tuple[int, dict[str, Any]] | str:
    usage = (
        "Usage: /teamtick <team_id> [--agent <id>] [--runtime] [--lane <lane>] "
        "[--complete] [--execute-running] [--command <preset>] [--cwd <path>] "
        "[--complete-on-success] [--allow-live-agent-run]"
    )
    try:
        tokens = shlex.split(args or "")
    except ValueError as exc:
        return f"{usage}\nParse error: {exc}"
    if not tokens:
        return usage
    try:
        team_id = int(tokens[0])
    except ValueError:
        return usage

    opts: dict[str, Any] = {
        "agent_id": None,
        "use_runtime": False,
        "runtime_lane": None,
        "complete_running": False,
        "execute_running": False,
        "executor_command": "git_status",
        "executor_cwd": None,
        "complete_on_executor_success": False,
        "allow_live_agent_run": False,
    }
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "--runtime":
            opts["use_runtime"] = True
            i += 1
            continue
        if token in ("--complete", "--complete-running"):
            opts["complete_running"] = True
            i += 1
            continue
        if token in ("--execute", "--execute-running", "--executor"):
            opts["execute_running"] = True
            i += 1
            continue
        if token in ("--complete-on-success", "--complete-on-executor-success"):
            opts["complete_on_executor_success"] = True
            i += 1
            continue
        if token == "--allow-live-agent-run":
            opts["allow_live_agent_run"] = True
            i += 1
            continue
        if token in (
            "--agent",
            "--lane",
            "--runtime-lane",
            "--command",
            "--executor-command",
            "--cwd",
            "--executor-cwd",
        ):
            if i + 1 >= len(tokens):
                return f"Missing value for {token}"
            value = tokens[i + 1]
            if token == "--agent":
                opts["agent_id"] = value
            elif token in ("--lane", "--runtime-lane"):
                opts["runtime_lane"] = value
                opts["use_runtime"] = True
            elif token in ("--command", "--executor-command"):
                opts["executor_command"] = value
                opts["execute_running"] = True
            elif token == "--cwd":
                opts["executor_cwd"] = value
                opts["execute_running"] = True
            elif token == "--executor-cwd":
                opts["executor_cwd"] = value
                opts["execute_running"] = True
            i += 2
            continue
        return f"Unknown option: {token}"
    return team_id, opts


def _teamtick_code(value: Any) -> str:
    text = str(value).replace("`", "'")
    return f"`{text}`"


def _format_team_tick_reply(result: Any) -> str:
    lines = [f"*Team Tick #{result.team_id}*", f"Action: {_teamtick_code(result.selected_action)}"]
    if result.agent_id:
        lines.append(f"Agent: {_teamtick_code(result.agent_id)}")
    if result.subtask_id:
        lines.append(f"Subtask: {_teamtick_code(f'#{result.subtask_id}')}")
    lines.append(f"Reason: {result.reason}")
    if result.error:
        lines.append(f"Error: {result.error}")
        return "\n".join(lines)
    if result.waited:
        lines.append("Result: waited")
        return "\n".join(lines)
    if result.step:
        after = result.step.subtask_after.status if result.step.subtask_after else "unknown"
        lines.append(
            f"Step: {_teamtick_code(result.step.action)}; claimed {len(result.step.claimed)}; "
            f"status {_teamtick_code(after)}"
        )
        if result.step.runtime:
            lines.append(
                "Runtime: "
                f"{_teamtick_code(result.step.runtime.runtime_lane)} / "
                f"{_teamtick_code(result.step.runtime.provider)}"
            )
    if getattr(result, "executor", None):
        lines.append(
            "Executor: "
            f"{_teamtick_code(result.executor.command_key)}; "
            f"exit {_teamtick_code(result.executor.exit_code if result.executor.exit_code is not None else 'timeout')}; "
            f"success {_teamtick_code(result.executor.success)}"
        )
    return "\n".join(lines)


async def handle_teamtick(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False
) -> str:
    """Run one autonomous team scheduler tick from a chat channel."""
    parsed = _parse_teamtick_args(args)
    if isinstance(parsed, str):
        return parsed
    team_id, opts = parsed

    from config import ORCHESTRATION_DB_PATH, ensure_directories
    from orchestration.db import OrchestrationDB
    from orchestration.live_safety import LiveExecutionRefused, require_live_agent_run
    from orchestration.observability import init_orchestration_observability
    from orchestration.team_loop import TeamTickService

    allow_live_agent_run = bool(opts.pop("allow_live_agent_run", False))
    try:
        require_live_agent_run(
            "chat /teamtick",
            explicit_opt_in=allow_live_agent_run,
        )
    except LiveExecutionRefused as exc:
        return str(exc)

    ensure_directories()
    init_orchestration_observability()
    db = OrchestrationDB(ORCHESTRATION_DB_PATH)
    try:
        result = TeamTickService(db).run_team_tick(team_id, **opts)
    finally:
        db.close()
    return _format_team_tick_reply(result)


def _parse_teamroom_args(args: str) -> dict[str, Any] | str:
    usage = (
        "Usage: /teamroom [--v2] [--runtime] [--lane <lane>] "
        "[--workflow growth_boardroom] [--context <text>] [--max-rounds <n>] "
        "[--allow-live-agent-run] <goal>"
    )
    try:
        tokens = shlex.split(args or "")
    except ValueError as exc:
        return f"{usage}\nParse error: {exc}"

    opts: dict[str, Any] = {
        "goal": None,
        "workflow_id": "growth_boardroom",
        "context": None,
        "use_runtime": False,
        "runtime_lane": None,
        "max_rounds": None,
        "meeting_mode": None,
        "allow_live_agent_run": False,
    }
    goal_parts: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--runtime":
            opts["use_runtime"] = True
            i += 1
            continue
        if token == "--v2":
            opts["meeting_mode"] = "facilitated_boardroom"
            i += 1
            continue
        if token == "--allow-live-agent-run":
            opts["allow_live_agent_run"] = True
            i += 1
            continue
        if token in (
            "--lane",
            "--runtime-lane",
            "--workflow",
            "--workflow-id",
            "--context",
            "--goal",
            "--max-rounds",
            "--meeting-mode",
            "--mode",
        ):
            if i + 1 >= len(tokens):
                return f"Missing value for {token}"
            value = tokens[i + 1].strip()
            if token in ("--lane", "--runtime-lane"):
                opts["runtime_lane"] = value
                opts["use_runtime"] = True
            elif token in ("--workflow", "--workflow-id"):
                opts["workflow_id"] = value
            elif token == "--context":
                opts["context"] = value
            elif token == "--goal":
                opts["goal"] = value
            elif token == "--max-rounds":
                try:
                    opts["max_rounds"] = int(value)
                except ValueError:
                    return "--max-rounds must be an integer"
            elif token in ("--meeting-mode", "--mode"):
                opts["meeting_mode"] = value
            i += 2
            continue
        if token.startswith("--"):
            return f"Unknown option: {token}\n{usage}"
        goal_parts.append(token)
        i += 1
    if goal_parts:
        extra_goal = " ".join(goal_parts).strip()
        opts["goal"] = f"{opts['goal']} {extra_goal}".strip() if opts["goal"] else extra_goal
    if not opts["goal"]:
        return usage
    _apply_teamroom_natural_language_shortcuts(opts)
    return opts


def _apply_teamroom_natural_language_shortcuts(opts: dict[str, Any]) -> None:
    """Infer common Team Room options from conversational slash-command text."""

    goal = str(opts.get("goal") or "").strip()
    if not goal:
        return

    lowered = goal.lower()
    words = {word.strip(".,:;!?()[]{}") for word in lowered.split()}

    live_words = {
        "agent",
        "agents",
        "call",
        "calling",
        "live",
        "run",
        "runtime",
    }
    if not opts.get("allow_live_agent_run") and words & live_words:
        opts["allow_live_agent_run"] = True

    runtime_words = {"call", "calling", "live", "runtime"}
    if not opts.get("use_runtime") and words & runtime_words:
        opts["use_runtime"] = True

    facilitated_words = {
        "boardroom",
        "facilitated",
        "meeting",
        "v2",
        "v3",
    }
    if not opts.get("meeting_mode") and words & facilitated_words:
        opts["meeting_mode"] = "facilitated_boardroom"

    removable_prefixes = (
        "call the team about ",
        "call the team on ",
        "call the team for ",
        "call team about ",
        "call team on ",
        "call team for ",
        "calling the team about ",
        "calling the team on ",
        "calling the team for ",
        "run a team room about ",
        "run a team room on ",
        "run a team room for ",
        "run a facilitated boardroom about ",
        "run a facilitated boardroom on ",
        "run a facilitated boardroom for ",
        "run facilitated boardroom about ",
        "run facilitated boardroom on ",
        "run facilitated boardroom for ",
        "run team room about ",
        "run team room on ",
        "run team room for ",
        "run the team room about ",
        "run the team room on ",
        "run the team room for ",
    )
    for prefix in removable_prefixes:
        if lowered.startswith(prefix):
            opts["goal"] = goal[len(prefix):].strip() or goal
            return


async def handle_team(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False
) -> str:
    """Slash alias for conversational `/team room ...` usage."""

    tokens = (args or "").strip().split(maxsplit=1)
    if tokens and tokens[0].lower() == "room":
        room_args = tokens[1] if len(tokens) > 1 else ""
        return await handle_teamroom(adapter, incoming, room_args, collect_only=collect_only)
    return "Usage: /team room <goal>"


def _clip_team_room_text(text: str, *, max_chars: int = 1800) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 14].rstrip() + "\n...[truncated]"


def _format_runtime_cost(cost: Any) -> str:
    if not isinstance(cost, (int, float)):
        return "n/a"
    return f"${cost:.6f}"


def _format_team_room_reply(result: Any, *, use_runtime: bool, runtime_lane: str | None) -> str:
    from orchestration.team_room import team_room_runtime_summary, team_room_turn_summary

    convoy = result.convoy.convoy
    final_brief = _clip_team_room_text(result.final_brief)
    runtime_summary = team_room_runtime_summary(result)
    lines = [
        "*Team Room Workflow*",
        f"Workflow: {_teamtick_code(result.workflow_id)}",
        f"Mode: {_teamtick_code(result.meeting_mode)}",
        f"Rounds: {_teamtick_code(str(result.max_rounds))}",
        f"Goal: {_teamtick_code(_clip_team_room_text(result.goal, max_chars=180))}",
        f"Team: {_teamtick_code(f'#{result.team.session.id}')}",
        f"Convoy: {_teamtick_code(f'#{convoy.id}')}",
        f"Progress: {_teamtick_code(f'{convoy.completed_subtasks}/{convoy.total_subtasks}')} subtasks",
        f"Turns: {team_room_turn_summary(result)}",
        f"Confidence: {_teamtick_code(f'{result.synthesis.confidence:.2f}')}",
        f"Votes: {_teamtick_code(str(len(result.vote_board)))} roles; "
        f"interrupts {_teamtick_code(str(len(result.interrupts)))}",
        f"Runtime turns: {_teamtick_code('on' if use_runtime else 'off')}",
    ]
    if result.synthesis.agreements:
        lines.append(f"Agreement: {result.synthesis.agreements[0]}")
    if result.synthesis.disagreements:
        lines.append(f"Disagreement: {result.synthesis.disagreements[0]}")
    if runtime_lane:
        lines.append(f"Runtime lane: {_teamtick_code(runtime_lane)}")
    if use_runtime:
        lines.append(
            "Runtime metadata: "
            f"{_teamtick_code(str(runtime_summary['turn_count']))} turns; "
            f"lanes {_teamtick_code(', '.join(runtime_summary['lanes']) or 'unknown')}; "
            f"providers {_teamtick_code(', '.join(runtime_summary['providers']) or 'unknown')}; "
            f"models {_teamtick_code(', '.join(runtime_summary['models']) or 'unknown')}; "
            f"tools {_teamtick_code(str(runtime_summary['tool_call_count']))}; "
            f"cost {_teamtick_code(_format_runtime_cost(runtime_summary['cost_usd']))}; "
            f"elapsed {_teamtick_code(str(runtime_summary['execution_time_ms'] or 0) + 'ms')}"
        )
        if runtime_summary["errors"]:
            clipped_errors = "; ".join(runtime_summary["errors"])[:280]
            lines.append(f"Runtime errors: {_teamtick_code(clipped_errors)}")
    lines.extend(["", "*Final Brief*", final_brief])
    return "\n".join(lines)


async def handle_teamroom(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False
) -> str:
    """Run the bounded Growth Boardroom team room workflow."""
    parsed = _parse_teamroom_args(args)
    if isinstance(parsed, str):
        return parsed

    from config import ORCHESTRATION_DB_PATH, ensure_directories
    from orchestration.db import OrchestrationDB
    from orchestration.live_safety import LiveExecutionRefused, require_live_agent_run
    from orchestration.observability import init_orchestration_observability
    from orchestration.team_room import TeamRoomWorkflowService

    allow_live_agent_run = bool(parsed.pop("allow_live_agent_run", False))
    try:
        require_live_agent_run(
            "chat /teamroom",
            explicit_opt_in=allow_live_agent_run,
        )
    except LiveExecutionRefused as exc:
        return str(exc)

    def _run_team_room() -> Any:
        ensure_directories()
        init_orchestration_observability()
        db = OrchestrationDB(ORCHESTRATION_DB_PATH)
        try:
            return TeamRoomWorkflowService(db).run_team_room(**parsed)
        finally:
            db.close()

    if parsed["use_runtime"]:
        result = await asyncio.to_thread(_run_team_room)
    else:
        result = _run_team_room()
    return _format_team_room_reply(
        result,
        use_runtime=bool(parsed["use_runtime"]),
        runtime_lane=parsed["runtime_lane"],
    )


async def handle_send(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Send an email draft from the drafts folder."""
    try:
        import shutil

        from integrations.capabilities import require_integration_action
        from integrations.outlook import send_email

        draft_name = args.strip()
        if not draft_name:
            return "Usage: `/send <draft_filename_or_slug>`"

        from config import DRAFTS_ACTIVE_DIR, DRAFTS_SENT_DIR

        draft_path = DRAFTS_ACTIVE_DIR / draft_name
        if not draft_path.exists():
            draft_path = DRAFTS_ACTIVE_DIR / f"{draft_name}.md"

        if not draft_path.exists():
            matches = list(DRAFTS_ACTIVE_DIR.glob(f"*{draft_name}*"))
            if not matches:
                return f"Draft '{draft_name}' not found in `drafts/active/`."
            elif len(matches) > 1:
                return f"Multiple drafts match '{draft_name}':\n" + "\n".join(
                    f"- `{m.name}`" for m in matches
                )
            else:
                draft_path = matches[0]

        if draft_path.exists():
            content = draft_path.read_text(encoding="utf-8")
            meta: dict[str, str] = {}
            body = ""
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    for line in content[3:end].strip().split("\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip().lower()] = v.strip()
                    body_raw = content[end + 3:].strip()
                    marker = "**Pitch Response (AI-drafted — review before sending):**"
                    if marker in body_raw:
                        body = body_raw.split(marker, 1)[1].strip()
                    else:
                        body = body_raw
                else:
                    body = content
            else:
                body = content

            to_email = meta.get("to", meta.get("reply-to", ""))
            subject = meta.get("subject", f"Re: {meta.get('original-subject', 'Your email')}")

            if not to_email:
                return f"Draft '{draft_path.name}' has no 'to' address in frontmatter."

            require_integration_action(
                "outlook",
                "send_email",
                surface="operator_confirmed",
                caller="chat.core_handlers.handle_send",
            )
            ok = send_email(to_email=to_email, subject=subject, body=body)
            if ok:
                DRAFTS_SENT_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(draft_path), str(DRAFTS_SENT_DIR / draft_path.name))
                return f"Sent `{draft_path.name}` to {to_email} and moved to `drafts/sent/`."
            else:
                return f"Failed to send draft `{draft_path.name}` via Outlook."
        return f"Draft '{draft_name}' not found."
    except Exception as e:
        return f"Error sending draft: {e}"


async def handle_brief(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Fetch a brief across multiple data sources."""
    from extension_manager import get_manager

    manager = get_manager()
    sub = args.strip().lower() if args else ""
    if sub == "all":
        # All commands that have data intents (not ALL router commands)
        intent_cmds = list({i.command for i in manager._intents})
        # Also include non-intent data commands: gsc, analytics
        for extra in ["email", "gsc", "analytics", "budget"]:
            if extra not in intent_cmds and extra in manager._commands:
                intent_cmds.append(extra)
        sub_cmds = intent_cmds
    else:
        sub_cmds = manager.get_brief_intents() or ["email", "budget"]

    parts_brief: list[str] = []
    for cmd in sub_cmds:
        try:
            r = await manager.dispatch(cmd, adapter, incoming, "", collect_only=True)
            if r:
                parts_brief.append(f"*/{cmd}*\n{r}")
        except Exception as e:
            parts_brief.append(f"*/{cmd}*\nError: {e}")
    return "\n\n━━━━━━━━━━━━━━━\n\n".join(parts_brief)


async def handle_working(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show/edit WORKING.md — cross-session scratchpad (Living Mind Phase 1).

    Subcommands:
      /working                       — show all active sections
      /working add "<text>"          — append to Open Threads with today's date
      /working resolve <N>           — move item N (1-based) from Open Threads to Archived
    """
    from config import MEMORY_DIR
    from living_memory import (
        append_open_thread,
        read_working_memory,
        resolve_open_thread,
    )

    sub = args.strip() if args else ""

    # --- add subcommand ---
    if sub.lower().startswith("add "):
        payload = sub[4:].strip().strip('"').strip("'")
        if not payload:
            return "Usage: `/working add \"<what you're working on>\"`"
        written = append_open_thread(MEMORY_DIR, subject=payload, status="open")
        if written:
            return f"Added to Open Threads: {payload}"
        return f"Already in Open Threads (deduped within {payload[:40]}...)"

    # --- resolve subcommand ---
    if sub.lower().startswith("resolve "):
        tail = sub[8:].strip()
        try:
            index = int(tail)
        except ValueError:
            return "Usage: `/working resolve <N>` (N is the 1-based item number)"
        ok, detail = resolve_open_thread(MEMORY_DIR, index)
        return f"Resolved: {detail}" if ok else detail

    # --- default: show active sections ---
    data = read_working_memory(MEMORY_DIR)
    if not data.exists:
        return (
            "*Working Memory*\nWORKING.md does not exist yet. "
            "Add your first thread: `/working add \"<subject>\"`"
        )

    lines = ["*Working Memory*"]

    def _render(label: str, bullets: list[str]) -> None:
        if not bullets:
            return
        lines.append(f"\n*{label}*")
        for i, bullet in enumerate(bullets, start=1):
            clean = bullet.lstrip("- ").strip()
            prefix = f"  {i}. " if label == "Open Threads" else "  • "
            lines.append(f"{prefix}{clean}")

    _render("Open Threads", data.open_threads)
    _render("Active Hypotheses", data.active_hypotheses)
    _render("Unresolved Questions", data.unresolved_questions)
    _render("Heartbeat Observations", data.heartbeat_observations)

    if not (
        data.open_threads
        or data.active_hypotheses
        or data.unresolved_questions
        or data.heartbeat_observations
    ):
        lines.append("\n(all sections empty — nothing tracked yet)")

    arch_count = len(data.archived)
    if arch_count:
        lines.append(f"\n_Archive: {arch_count} cold item(s)_")

    return "\n".join(lines)


async def handle_vault(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Registry guard for /vault.

    ChatRouter intercepts the production /vault route because ingest needs
    router-owned attachment and persistence behavior. This keeps the central
    command registry internally consistent for audits and fallback dispatch.
    """

    return (
        "*Vault Commands*\n"
        "`/vault status [vault]`\n"
        "`/vault db [vault]`\n"
        "`/vault search <query> [--vault name]`\n"
        "`/vault context <topic> [--vault name]`\n"
        "`/vault contacts [query] [--vault name]`\n"
        "`/vault ingest <url> [--vault name]`\n"
        "`/vault ops <routine> [args] [--vault name]`"
    )


# Skill-from-experience loop (WS4): operator surface for the promotion gate.
# Default-deny — a self-authored skill draft can only be promoted into the
# prompt through THIS explicit operator command (operator_approved=True), after
# it has recurred enough to be eligible AND passed the security scan. Reject is
# its own verb (B6), never `promote(operator_approved=True)`.
_SKILLS_USAGE = (
    "*Skills* — self-authored skill drafts\n"
    "  `/skills review` — list promotion-eligible drafts (with scan preview)\n"
    "  `/skills promote <name>` — promote an eligible, scan-passed draft\n"
    "  `/skills promote <name> --override-caution` — promote despite a `caution` scan\n"
    "  `/skills reject <name> [| reason]` — archive a draft so it stops being surfaced"
)

_SKILL_PROMOTE_STATUS_TEXT = {
    "promoted": "promoted — it is now live in the prompt",
    "already_promoted": "already promoted (no change)",
    "killswitch_disabled": "refused — the skill_promotion kill-switch is disabled",
    "not_eligible": "refused — not eligible yet (needs more recurrences, or already handled)",
    "not_found": "refused — could not locate the generated draft on disk",
    "promote_target_invalid": "refused — a promoted/<name> dir already exists but is empty or invalid (partial prior run); remove it and retry",
    "scan_dangerous": "refused — the security scan flagged it DANGEROUS",
    "scan_caution": "refused — the scan returned CAUTION (re-run with --override-caution to force)",
    "not_approved": "refused — operator approval missing",
    "move_failed": "refused — the file move out of generated/ failed",
}


async def handle_skills(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Review / promote / reject self-authored skill drafts (WS4 operator gate).

    Subcommands:
      /skills review                       — list eligible drafts + scan preview
      /skills promote <name> [--override-caution]   (name may contain spaces)
      /skills reject <name> [| reason]              (name may contain spaces)
    """
    try:
        from cognition import skill_promotion
    except Exception as exc:  # noqa: BLE001 - module optional outside scripts env
        return f"Skills promotion is unavailable: {exc}"

    sub = (args or "").strip()
    if not sub:
        return _SKILLS_USAGE

    # F1: a generated skill's DISPLAY name can contain spaces (write_skill keeps
    # the display name in frontmatter; recurrence + the usage sidecar are keyed
    # on that exact name). So the NAME is the full remainder of the line after
    # the verb — NOT just the first whitespace token. Split ONCE on whitespace.
    verb_split = sub.split(None, 1)
    verb = verb_split[0].lower()
    remainder = verb_split[1].strip() if len(verb_split) > 1 else ""

    # --- review: list promotion-eligible drafts with a fresh scan preview ---
    if verb == "review":
        try:
            promotable = skill_promotion.list_promotable()
        except Exception as exc:  # noqa: BLE001 - never break the turn
            return f"Could not list promotable skills: {exc}"
        if not promotable:
            return "*Skills* — no promotion-eligible drafts right now."
        lines = ["*Promotion-eligible skill drafts*"]
        for item in promotable:
            name = item.get("name", "?")
            verdict = item.get("verdict", "unknown")
            count = item.get("recurrence_count", 0)
            lines.append(f"  • *{name}* — scan: {verdict}, recurrences: {count}")
        lines.append(
            "\nPromote with `/skills promote <name>` "
            "or reject with `/skills reject <name>`."
        )
        return "\n".join(lines)

    # --- promote: explicit operator approval (default-deny gate) ---
    if verb == "promote":
        # F1: handle the flag FIRST, then the rest of the remainder (joined,
        # stripped) is the full multi-word name. `/skills promote Daily Spend`
        # → name "Daily Spend"; `--override-caution` may appear anywhere.
        tokens = remainder.split()
        override = "--override-caution" in tokens
        name = " ".join(t for t in tokens if not t.startswith("--")).strip()
        if not name:
            return "Usage: `/skills promote <name> [--override-caution]`"
        try:
            result = skill_promotion.promote(
                name, operator_approved=True, override_caution=override,
            )
        except Exception as exc:  # noqa: BLE001 - never break the turn
            return f"Promotion failed for `{name}`: {exc}"
        status = result.get("status", "unknown")
        detail = _SKILL_PROMOTE_STATUS_TEXT.get(status, status)
        line = f"*/skills promote {name}* — {detail}"
        if status in ("promoted", "already_promoted") and result.get("path"):
            line += f"\n  path: `{result['path']}`"
        return line

    # --- reject: distinct verb (B6) — archive the draft + audit ---
    if verb == "reject":
        # F1: the name is multi-word, so an optional reason is delimited by a
        # single `|`: `/skills reject Daily Spend | too risky`. With no `|`, the
        # whole remainder is the name and the reason defaults.
        name_part, sep, reason_part = remainder.partition("|")
        name = name_part.strip()
        if not name:
            return "Usage: `/skills reject <name> [| reason]`"
        reason = reason_part.strip() if sep else ""
        reason = reason or "operator_rejected"
        try:
            result = skill_promotion.reject_skill(name, reason)
        except Exception as exc:  # noqa: BLE001 - never break the turn
            return f"Reject failed for `{name}`: {exc}"
        return f"*/skills reject {name}* — {result.get('status', 'rejected')} (reason: {reason})"

    return _SKILLS_USAGE


async def handle_learn(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Author a reusable skill draft from a source (Hermes /learn port).

    Sources: a URL, a local dir/file, this conversation, or pasted notes.
    Optionally end with ``--focus <hint>`` / ``focus on <hint>``.

      /learn https://docs.example.com/api/quickstart
      /learn ~/projects/acme-sdk focus on auth + pagination
      /learn what we just did
      /learn filing an expense: open portal, New > Expense, attach receipt, submit

    The draft is written inert under ``skills/generated/`` and security-scanned;
    graduation stays manual via ``/skills review`` -> ``promote``.
    """
    if not (args or "").strip():
        return (
            "*Learn* — turn a source into a reusable skill draft.\n"
            "Usage: `/learn <url | path | \"this conversation\" | notes>` "
            "`[--focus <hint>]`\n"
            "Drafts stage in `skills/generated/`; promote with `/skills`."
        )

    try:
        from cognition import skill_learn
    except Exception as exc:  # noqa: BLE001 - optional outside the scripts env
        return f"Learn is unavailable: {exc}"

    # Best-effort: pull recent conversation history for the "this conversation"
    # source. Fail-soft — an unavailable store just means an empty transcript.
    transcript = ""
    session_id = ""
    try:
        from session import build_session_key, get_session_store

        store = get_session_store()
        session_id = build_session_key(
            getattr(incoming, "platform", ""),
            getattr(incoming, "channel", ""),
            getattr(incoming, "thread", ""),
        )
        msgs = store.list_messages(session_id, limit=40)
        transcript = "\n".join(f"{m.role}: {m.content}" for m in msgs)
    except Exception:  # noqa: BLE001 - transcript is optional
        transcript = ""

    try:
        from commands import get_all_command_names

        known_commands = list(get_all_command_names())
    except Exception:  # noqa: BLE001
        known_commands = None

    try:
        result = await skill_learn.learn_skill(
            args,
            transcript=transcript,
            cwd=Path.cwd(),
            known_commands=known_commands,
            source_session=session_id if transcript else "",
        )
    except Exception as exc:  # noqa: BLE001 - never break the turn
        return f"Learn failed: {exc}"

    return result.message


_WATCH_USAGE = (
    "*Watch* — learn strategy from one video and compare it with our current work.\n"
    "Usage: `/watch <video-url> [question] [--detail smart|transcript|deep] [--no-save]`\n"
    "       `/watch status [job_id]` · `/watch retry <job_id>` · `/watch cancel <job_id>`\n"
    "       `/watch apply <job_id>` proposes local changes; the exact proposal needs a second approval.\n"
    "`smart` reads captions first and inspects frames only when visual cues matter."
)


def _watch_service() -> Any:
    from video_learning import get_video_learning_service

    return get_video_learning_service()


def _watch_conversation_context(incoming: Any, *, limit: int = 50) -> str:
    """Best-effort recent context, isolated to the current canonical session."""
    try:
        store, existing, platform, channel_id, thread_id = _get_session(incoming)
        session_id = getattr(existing, "session_id", "") or build_session_key(platform, channel_id, thread_id)
        messages = store.list_messages(session_id, limit=limit)
        return "\n".join(f"{row.role}: {row.content}" for row in messages)[-30_000:]
    except Exception:
        return ""


def _parse_watch_request(args: str) -> tuple[str, str, str, bool]:
    try:
        tokens = shlex.split(args, posix=False)
    except ValueError as exc:
        raise ValueError(f"Could not parse /watch arguments: {exc}") from exc
    if not tokens:
        raise ValueError(_WATCH_USAGE)
    source = tokens.pop(0).strip('"\'')
    detail = "smart"
    save_note = True
    question_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--no-save":
            save_note = False
        elif token == "--detail":
            index += 1
            if index >= len(tokens):
                raise ValueError("`--detail` needs smart, transcript, or deep.")
            detail = tokens[index].lower()
        elif token.startswith("--detail="):
            detail = token.split("=", 1)[1].lower()
        else:
            question_parts.append(token.strip('"\''))
        index += 1
    if detail not in {"smart", "transcript", "deep"}:
        raise ValueError("Detail must be `smart`, `transcript`, or `deep`.")
    return source, " ".join(question_parts).strip(), detail, save_note


def _format_watch_status(row: dict[str, Any] | None) -> str:
    if not row:
        return "No video-learning jobs yet. Start one with `/watch <video-url>`."
    result = row.get("result") or {}
    lines = [
        f"*Watch job `{row.get('job_id', '?')}`* — {row.get('status', 'unknown')}",
        f"{row.get('stage_detail', '')}",
        f"Source: {(row.get('request') or {}).get('source', '')}",
    ]
    if result.get("title"):
        lines.append(f"Video: {result['title']}")
    if result.get("note_path"):
        lines.append(f"Note: `{result['note_path']}`")
    if row.get("error"):
        lines.append(f"Error: {row['error']}")
    return "\n".join(line for line in lines if line)


async def _deliver_watch_result(adapter: Any, incoming: Any, task: Any) -> None:
    from models import MessageComponent, OutgoingMessage

    try:
        result = await task
        if result.success:
            summary = result.summary.strip()
            if len(summary) > 3500:
                summary = summary[:3500].rstrip() + "\n…(full dossier is in the sourced note)"
            meta = [f"job `{result.job_id}`", f"via {result.provider}/{result.model}"]
            if result.note_path:
                meta.append(f"saved `{result.note_path}`")
            text = summary + "\n\n" + " · ".join(meta)
            components = [
                MessageComponent(
                    label="Apply to Current Work",
                    custom_id=f"watch:apply:{result.job_id}",
                    style="primary",
                )
            ]
        else:
            text = f"Watch job `{result.job_id}` {result.status}: {result.error}"
            components = [
                MessageComponent(
                    label="Retry",
                    custom_id=f"watch:retry:{result.job_id}",
                    style="secondary",
                )
            ]
        await adapter.send(
            OutgoingMessage(
                text=text,
                channel=incoming.channel,
                thread=incoming.thread,
                is_error=not result.success,
                components=components,
            )
        )
    except Exception as exc:
        await adapter.send(
            OutgoingMessage(
                text=f"Video-learning delivery failed: {exc}",
                channel=incoming.channel,
                thread=incoming.thread,
                is_error=True,
            )
        )


async def handle_watch(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str | None:
    """Analyze one video through Homie's shared, model-agnostic learning lane."""
    from video_learning import VideoLearningRequest

    raw = (args or "").strip()
    if not raw:
        return _WATCH_USAGE
    parts = raw.split()
    sub = parts[0].lower()
    service = _watch_service()

    if sub == "status":
        return _format_watch_status(service.status(parts[1] if len(parts) > 1 else ""))
    if sub == "cancel":
        if len(parts) != 2:
            return "Usage: `/watch cancel <job_id>`"
        return "Cancellation requested." if service.cancel(parts[1]) else "That job is not running."
    if sub == "retry":
        if len(parts) != 2:
            return "Usage: `/watch retry <job_id>`"
        row = service.retry(parts[1])
        if not row:
            return "Only failed, cancelled, or interrupted jobs can be retried."
        task = service.start(row["job_id"])
        if collect_only or getattr(incoming.platform, "value", "") == "cli":
            return _format_watch_result(await task)
        delivery = asyncio.create_task(_deliver_watch_result(adapter, incoming, task))
        _BACKGROUND_TASKS.add(delivery)
        delivery.add_done_callback(_BACKGROUND_TASKS.discard)
        return f"Retry queued as watch job `{row['job_id']}`."
    if sub == "apply":
        if len(parts) != 2:
            return "Usage: `/watch apply <job_id>`"
        try:
            proposal = await service.propose(parts[1])
        except Exception as exc:
            return f"Application proposal failed: {exc}"
        return (
            proposal["proposal"]
            + f"\n\nApproval token: `{proposal['approval_token']}`\n"
            + f"Approve this exact proposal with `/watch approve {parts[1]} {proposal['approval_token']}`. "
              "No files have been changed."
        )
    if sub == "approve":
        if len(parts) != 3:
            return "Usage: `/watch approve <job_id> <exact-token>`"
        try:
            application = await service.apply(parts[1], parts[2])
        except Exception as exc:
            return f"Approved application failed: {exc}"
        return application.get("report") or "The approved proposal was applied locally."

    try:
        source, question, detail, save_note = _parse_watch_request(raw)
    except ValueError as exc:
        return str(exc)
    platform = getattr(incoming.platform, "value", str(incoming.platform)).lower()
    if platform != "cli" and not source.lower().startswith(("http://", "https://")):
        return "Remote chat channels accept public http(s) video URLs only. Local files are CLI-only."
    missing = service.dependency_report()
    if missing:
        return "Video learning needs these local tools first: " + ", ".join(missing)

    request = VideoLearningRequest(
        source=source,
        question=question,
        detail=detail,
        save_note=save_note,
        conversation_context=_watch_conversation_context(incoming),
        workspace=Path.cwd(),
        origin={
            "platform": platform,
            "channel_id": getattr(incoming.channel, "platform_id", ""),
            "thread_id": getattr(getattr(incoming, "thread", None), "thread_id", ""),
        },
    )
    row = service.create_job(request)
    task = service.start(row["job_id"])
    if collect_only or platform == "cli":
        return _format_watch_result(await task)
    delivery = asyncio.create_task(_deliver_watch_result(adapter, incoming, task))
    _BACKGROUND_TASKS.add(delivery)
    delivery.add_done_callback(_BACKGROUND_TASKS.discard)
    return (
        f"Watch job `{row['job_id']}` queued. I’ll read the transcript, inspect visuals only if useful, "
        "compare it with our current work, and send the sourced dossier back here."
    )


def _format_watch_result(result: Any) -> str:
    if not result.success:
        return f"Watch job `{result.job_id}` {result.status}: {result.error}"
    suffix = f"\n\nJob: `{result.job_id}`"
    if result.note_path:
        suffix += f"\nSaved: `{result.note_path}`"
    return result.summary + suffix


async def handle_watch_button(adapter: Any, incoming: Any, custom_id: str) -> None:
    """Interactive proposal/retry/approval flow; exact approval is default-deny."""
    from models import MessageComponent, OutgoingMessage

    pieces = custom_id.split(":")
    if len(pieces) < 3:
        return
    _, action, job_id, *rest = pieces
    service = _watch_service()
    try:
        if action == "retry":
            row = service.retry(job_id)
            if not row:
                text, components = "That watch job cannot be retried.", []
            else:
                task = service.start(row["job_id"])
                delivery = asyncio.create_task(_deliver_watch_result(adapter, incoming, task))
                _BACKGROUND_TASKS.add(delivery)
                delivery.add_done_callback(_BACKGROUND_TASKS.discard)
                text, components = f"Retry queued as `{row['job_id']}`.", []
        elif action == "apply":
            proposal = await service.propose(job_id)
            text = proposal["proposal"] + "\n\nNo files have been changed. Approve only this exact proposal:"
            components = [MessageComponent(
                label="Approve Exact Local Changes",
                custom_id=f"watch:approve:{job_id}:{proposal['approval_token']}",
                style="success",
            )]
        elif action == "approve":
            raw_event = getattr(incoming, "raw_event", None) or {}
            if raw_event.get("interaction_type") != "button":
                raise ValueError("Application approval only runs from the authenticated approval button.")
            token = rest[0] if rest else ""
            application = await service.apply(job_id, token)
            text = application.get("report") or "The approved proposal was applied locally."
            components = []
        else:
            return
        await adapter.send(OutgoingMessage(
            text=text,
            channel=incoming.channel,
            thread=incoming.thread,
            components=components,
        ))
    except Exception as exc:
        await adapter.send(OutgoingMessage(
            text=f"Watch action failed: {exc}",
            channel=incoming.channel,
            thread=incoming.thread,
            is_error=True,
        ))


async def handle_extensions(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Show extension diagnostics or manage extensions."""
    from extension_manager import get_manager

    manager = get_manager()
    sub = args.strip().lower() if args else ""

    if sub == "doctor":
        return manager.doctor()
    elif sub.startswith("enable "):
        ext_id = sub[7:].strip()
        return manager.enable_extension(ext_id)
    elif sub.startswith("disable "):
        ext_id = sub[8:].strip()
        return manager.disable_extension(ext_id)
    elif sub.startswith("migrate "):
        parts = sub[8:].strip().split()
        system = parts[0] if parts else ""
        if system in ("openclaw", "hermes"):
            return (
                f"Migration from {system} is planned but not yet implemented. "
                f"Extension system ready for future import."
            )
        return f"Unknown migration source: {system}. Supported: openclaw, hermes"
    else:
        return manager.get_diagnostics()


# ---------------------------------------------------------------------------
# Provider helpers (moved from router.py module level)
# ---------------------------------------------------------------------------

_LANE_SELECTION_ALIASES = {
    "anthropic": "claude",
}


def _get_provider_status() -> str:
    """Build a lane-first status report of runtime selection and provider health."""
    try:
        from runtime.auth_profiles import (
            CodexAuthProfile,
            GeminiAuthProfile,
            codex_auth_status,
            gemini_auth_status,
        )
        from runtime.health import is_profile_available
        from runtime.profiles import build_profile_for_provider
        from runtime.routing import DEFAULT_PROVIDER_CHAIN

        selection = resolve_runtime_selection()
        lines = ["*Runtime Provider Status*\n"]
        lines.append("Selection:")
        lines.append(f"  lane: {selection.lane or 'auto'}")
        lines.append(f"  mode: {describe_runtime_selection(selection)}")
        if selection.lane != RUNTIME_LANE_CLAUDE_NATIVE:
            preferred = (
                provider_display_name(selection.generic_provider)
                if selection.generic_provider
                else "auto"
            )
            lines.append(f"  generic preferred provider: {preferred}")
        model = selected_runtime_model(selection)
        lines.append(f"  configured model: {model or 'auto (route-dependent)'}")
        for warning in runtime_model_warnings(selection):
            lines.append(f"  warning: {warning}")
        lines.append("")
        lines.append(
            "Generic text route: "
            + " -> ".join(
                provider_display_name(provider) for provider in runtime_routing.GENERIC_TEXT_ROUTE
            )
        )
        lines.append(
            "Generic tool route: "
            + " -> ".join(
                provider_display_name(provider) for provider in runtime_routing.GENERIC_TOOL_ROUTE
            )
        )
        lines.append("")
        lines.append("Provider health:")

        provider_checks = {
            "claude": lambda: ("Claude Agent SDK", "subscription", True),
            "openai_codex": lambda: (
                "Codex CLI",
                "ChatGPT sub",
                codex_auth_status(CodexAuthProfile(key="default", command="codex")).available,
            ),
            "gemini": lambda: (
                "Gemini CLI",
                "Google sub",
                gemini_auth_status(GeminiAuthProfile(key="default", command="gemini", auth_type="oauth-personal")).available,
            ),
            "openrouter": lambda: (
                "OpenRouter API",
                "API key",
                bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
            ),
            "openai": lambda: (
                "OpenAI API",
                "API key",
                bool(os.getenv("OPENAI_API_KEY", "").strip()),
            ),
            "kimi": lambda: (
                "Kimi API",
                "API key",
                bool(os.getenv("KIMI_API_KEY", "").strip()),
            ),
        }

        for provider in DEFAULT_PROVIDER_CHAIN:
            try:
                name, auth_type, available = provider_checks.get(provider, lambda: (provider, "unknown", False))()
                profile = build_profile_for_provider(provider, key_prefix="status-check")
                healthy = is_profile_available(profile) if profile else False
                status_icon = "ON" if (available and healthy) else ("AUTH" if available else "OFF")
                lines.append(f"  {status_icon} *{name}* ({auth_type})")
            except Exception as e:
                lines.append(f"  ERR *{provider}*: {e}")

        return "\n".join(lines)
    except Exception as e:
        return f"Provider status check failed: {e}"


def _write_env_var(env_path: Path, key: str, value: str) -> None:
    """Update or append a key=value pair in the .env file."""
    import re

    env_path.parent.mkdir(parents=True, exist_ok=True)
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
    else:
        content += f"\n{key}={value}\n"
    env_path.write_text(content, encoding="utf-8")


def _delete_env_var(env_path: Path, key: str) -> None:
    """Delete a key=value pair from the .env file if present."""
    import re

    if not env_path.exists():
        return
    content = env_path.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(key)}=.*(?:\r?\n)?"
    updated = re.sub(pattern, "", content, flags=re.MULTILINE)
    env_path.write_text(updated.rstrip() + ("\n" if updated.strip() else ""), encoding="utf-8")


def _switch_provider(choice: str) -> str:
    """Switch the runtime lane/provider or Claude model by updating .env."""
    choice = (choice or "").strip()
    if not choice:
        selection = resolve_runtime_selection()
        model = selected_runtime_model(selection)
        preferred = (
            provider_display_name(selection.generic_provider)
            if selection.generic_provider
            else "auto"
        )
        model_info = f" | configured model: {model or 'auto (route-dependent)'}"
        if selection.lane != RUNTIME_LANE_CLAUDE_NATIVE:
            model_info += f" | preferred generic provider: {preferred}"
        warnings = "\n".join(f"Warning: {warning}" for warning in runtime_model_warnings(selection))
        warning_block = f"\n{warnings}\n" if warnings else ""
        return (
            f"Current selection: {describe_runtime_selection(selection)}{model_info}\n"
            f"{warning_block}\n"
            "Usage: /model <lane|provider|provider:model|model>\n"
            "  /model claude - Claude native lane\n"
            "  /model sonnet - Claude Sonnet 4.6\n"
            "  /model opus - Claude Opus 4.6\n"
            "  /model codex - generic runtime lane via Codex\n"
            "  /model codex:default - Codex plan default (no --model passed)\n"
            "  /model gpt5.5 - Codex pinned model shortcut\n"
            "  /model codex 5.5 - Codex pinned model shortcut\n"
            "  /model gemini - generic runtime lane via Gemini\n"
            "  /model openrouter - generic runtime lane via OpenRouter\n"
            "  /model openai - generic runtime lane via OpenAI-compatible\n"
            "  /model kimi - generic runtime lane via Kimi\n"
            "  /model kimi:k3 - Kimi pinned model (default k3)\n"
            "  /model auto - automatic lane/provider routing"
        )

    if resolve_runtime_model_choice(choice):
        try:
            from config import ENV_FILE as env_path
            from config import reload_config

            model_choice = apply_runtime_model_choice(
                choice,
                environ=os.environ,
                write_key=lambda key, value: _write_env_var(env_path, key, value),
                delete_key=lambda key: _delete_env_var(env_path, key),
            )
            reload_config()
            warning = model_observability_warning(model_choice.provider, model_choice.model)
            warning_text = f"\nWarning: {warning}" if warning else ""
            return (
                f"Switched to {format_model_choice(model_choice)}. "
                "Next message uses this runtime selection."
                f"{warning_text}"
            )
        except Exception as e:
            return f"Failed to switch model: {e}"

    normalized = _LANE_SELECTION_ALIASES.get(choice.lower(), choice.lower())

    try:
        from config import ENV_FILE as env_path
        from config import reload_config

        selection = apply_runtime_selection_choice(
            normalized,
            environ=os.environ,
            write_key=lambda key, value: _write_env_var(env_path, key, value),
            delete_key=lambda key: _delete_env_var(env_path, key),
        )
        reload_config()
        return f"Switched to {describe_runtime_selection(selection)}. Next message uses this runtime selection."
    except ValueError:
        return (
            "Unknown runtime selection: "
            f"{choice}. Use: claude, sonnet, opus, codex, codex:default, "
            "codex:<model>, gpt5.5, gpt 5.5, codex 5.5, gemini, openrouter, openai, kimi, or auto"
        )
    except Exception as e:
        return f"Failed to switch provider: {e}"


# ---------------------------------------------------------------------------
# /design — native brand-grade design capability (Open Design power, no daemon)
# ---------------------------------------------------------------------------


def _parse_design_flags(text: str) -> tuple[str, dict[str, str]]:
    """Pull --system/--direction/--tone/--accent flags out of a design brief.

    Windows-safe (Codex MEDIUM): tokenizes on whitespace honouring quotes but
    WITHOUT shlex's POSIX backslash-escaping, which would mangle a Windows path
    like ``C:\\Users\\x`` in a brief into ``C:Usersx``. A flag whose value is
    missing or is itself another flag is dropped (Codex MEDIUM), not silently
    consumed.
    """
    import re

    flag_keys = {"--system": "system", "--direction": "direction",
                 "--tone": "tone", "--accent": "accent"}
    tokens = re.findall(r"\"[^\"]*\"|'[^']*'|\S+", text or "")

    def _unquote(tok: str) -> str:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            return tok[1:-1]
        return tok

    opts: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        key = flag_keys.get(tokens[i])
        if key:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is not None and not nxt.startswith("--"):
                opts[key] = _unquote(nxt)
                i += 2
                continue
            i += 1  # flag with no value or a flag-as-value → drop it
            continue
        rest.append(_unquote(tokens[i]))
        i += 1
    return " ".join(rest).strip(), opts


async def handle_design(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Generate brand-grade design artifacts natively through the runtime.

    Ports Open Design's taste loop (brief -> direction-lock -> artifact ->
    critique) onto The Homie's own runtime. No external daemon, no install:
    generation routes through ``run_with_runtime_lanes`` (auto-fallback
    claude -> codex -> gemini); artifacts persist to the firewalled vault
    substrate at ``vault/memory/design/``.

    Subcommands:
        /design html <brief>          generate a single-file HTML artifact
        /design system <name> <brief> generate using a bundled brand system
        /design systems               list bundled brand systems
        /design directions            list the 5 built-in visual directions
    """
    from datetime import datetime

    import config
    from design import (
        artifact_dir,
        build_design_brief,
        list_systems,
        load_system,
        pick_direction,
    )
    from design.directions import DESIGN_DIRECTIONS, find_direction

    raw = args.strip()
    subcmd = (raw.split()[0].lower() if raw else "")
    rest = raw[len(subcmd):].strip() if subcmd else ""

    usage = (
        "Native design. Usage:\n"
        "`/design html <brief>` — build a single-file HTML artifact\n"
        "`/design system <name> <brief>` — build using a bundled brand system\n"
        "`/design systems` — list brand systems\n"
        "`/design directions` — list visual directions\n"
        "Flags: `--system <slug>` `--direction <id>` `--tone <tone>` `--accent \"<override>\"`"
    )

    if not subcmd or subcmd in {"help", "?"}:
        return usage

    if subcmd in {"systems", "system-list"}:
        systems = list_systems()
        if not systems:
            return "No brand systems bundled yet under `vault/memory/design/_systems/`."
        return "Bundled brand systems:\n" + "\n".join(f"- {s}" for s in systems)

    if subcmd in {"directions", "dirs"}:
        return "Visual directions:\n" + "\n".join(
            f"- `{d.id}` — {d.label}" for d in DESIGN_DIRECTIONS
        )

    # Generation subcommands: html | system
    system = None  # DesignSystemPackage | None
    system_name: str | None = None
    brief_text = rest

    if subcmd == "system":
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return "Usage: `/design system <name> <brief>`. List names with `/design systems`."
        system_name = parts[0].strip().lower()
        brief_text = parts[1].strip()
        system = load_system(system_name)
        if system is None:
            avail = ", ".join(list_systems()) or "(none bundled)"
            return f"No brand system named `{system_name}`. Available: {avail}"
    elif subcmd not in {"html", "page", "build"}:
        # Treat unknown subcommand as part of the brief for the html path.
        brief_text = raw

    brief_text, opts = _parse_design_flags(brief_text)
    if not brief_text:
        return usage

    # `--system <slug>` selects a system only when the `system <name>` subcommand
    # path did not already set one (the subcommand wins if both are given).
    if opts.get("system") and system is None:
        system_name = opts["system"].strip().lower()
        system = load_system(system_name)
        if system is None:
            avail = ", ".join(list_systems()) or "(none bundled)"
            return f"No brand system named `{system_name}`. Available: {avail}"

    # Resolve the visual direction when no brand system is chosen.
    direction = None
    brand_locked = system is not None
    if system is None:
        if opts.get("direction"):
            direction = find_direction(opts["direction"])
            brand_locked = direction is not None
        if direction is None:
            direction = pick_direction(opts.get("tone") or brief_text)

    if collect_only:
        # Design generation is an explicit action, never part of a passive
        # data-sweep. Surface usage instead of spending a generation.
        return usage

    # Resolve artifact paths under the firewalled vault substrate.
    date_str = datetime.now().strftime("%Y%m%d")
    slug = _design_slug(brief_text)
    if system is not None:
        # Keep multi-system runs of the same brief in distinct dirs (fleet: one
        # brief, many brand systems) instead of overwriting one finalized.html.
        slug = f"{system.slug}-{slug}"
    out_dir = artifact_dir(slug, "html", date_str=date_str)
    finalized = out_dir / "finalized.html"
    out_dir.mkdir(parents=True, exist_ok=True)

    prompt = build_design_brief(
        kind="html",
        brief_text=brief_text,
        finalized_path=str(finalized),
        out_dir=str(out_dir),
        direction=direction,
        system=system,
        accent_override=opts.get("accent"),
        brand_locked=brand_locked,
    )

    try:
        from runtime.base import RuntimeRequest
        from runtime.capabilities import TOOL_REASONING
        from runtime.lane_router import run_with_runtime_lanes

        request = RuntimeRequest(
            prompt=prompt,
            # Containment (Codex HIGH): cwd is the artifact dir, NOT repo root, so
            # relative writes land in the firewalled vault dir; and no Bash tool,
            # so a single standalone-HTML task has no shell escape hatch. A full
            # write-sandbox is future hardening — this bounds the obvious vectors.
            cwd=out_dir,
            task_name="design_generate",
            capability=TOOL_REASONING,
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
            permission_mode="acceptEdits",
            max_turns=20,
            max_budget_usd=1.0,
            # Brief lives in `prompt` (lane-agnostic). system_prompt left None so
            # the generic CLI lanes (codex/gemini) receive identical instructions
            # — prompt_builder only forwards string system_prompts.
        )
        result = await run_with_runtime_lanes(request)
    except Exception as exc:
        # Kill-switch (HOMIE_KILLSWITCH_LLM=disabled) → friendly message. Use
        # isinstance via a late MODULE import (Rule 3 — direct-symbol import
        # breaks monkeypatch propagation and the killswitch import-style test).
        try:
            from security import kill_switches
            if isinstance(exc, kill_switches.KillSwitchDisabled):
                return "Design is unavailable: the LLM kill-switch is disabled (`HOMIE_KILLSWITCH_LLM`)."
        except ImportError:
            pass
        return f"Design generation failed: {exc}"

    try:
        wrote = finalized.is_file() and finalized.stat().st_size > 0
    except OSError:
        wrote = False

    rel = finalized
    try:
        rel = finalized.relative_to(config.PROJECT_ROOT)
    except ValueError:
        pass

    report = (result.text or "").strip()
    if len(report) > 1800:
        report = report[:1800].rstrip() + "\n…(truncated)"

    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "n/a"
    meta = f"via {result.provider}/{result.model} · {cost}"
    used = f"system `{system.slug}`" if system else f"direction `{direction.id}`"

    if wrote:
        header = f"Design ready ({used}, {meta})\n`{rel}`"
    else:
        header = (
            f"Design ran ({used}, {meta}) but no finalized.html landed at `{rel}`. "
            "Agent report below."
        )
    return f"{header}\n\n{report}" if report else header


def _design_slug(brief_text: str) -> str:
    """Short slug from the first words of a design brief."""
    from design import slugify

    return slugify(" ".join(brief_text.split()[:6]))


# ---------------------------------------------------------------------------
# /video — native, model-agnostic video generation (all adapters)
# ---------------------------------------------------------------------------
#
# Router-typed on purpose: the pipeline is deterministic Python and its only
# LLM moments run through run_with_runtime_lanes inside video_pipeline, so the
# command behaves identically on claude/codex/gemini lanes. No Skill-tool
# dependency (that path is claude_native-only). Renders run as a background
# asyncio task; the finished MP4 is delivered back through the SAME adapter
# via OutgoingMessage.attachments. Mirrors the /x graceful-degrade pattern:
# the backing module/system deps may be absent on a given install.

_VIDEO_RENDER_STATE: dict[str, Any] = {"running": False, "started": "", "brief": ""}

# Strong refs for fire-and-forget tasks (CPython only weak-refs running tasks;
# an unreferenced task can be GC'd mid-await and silently never complete).
_BACKGROUND_TASKS: set["asyncio.Task[Any]"] = set()


def _import_video_pipeline() -> Any:
    """Import the scripts-dir video_pipeline module from the chat slice.

    video_pipeline.py lives in ``.claude/scripts/``; core_handlers.py lives in
    ``.claude/chat/`` with the chat dir itself on sys.path (flat-import
    convention), so the scripts dir must be appended explicitly. Mirrors
    _import_x_scout.
    """
    import sys
    from pathlib import Path as _Path

    scripts_dir = str(_Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import video_pipeline  # type: ignore[import-not-found]

    return video_pipeline


def _parse_video_flags(tokens: list[str]) -> tuple[str, dict[str, Any]]:
    """Split ``/video`` args into (brief, options). Unknown flags stay in the brief.

    Every option is a None sentinel unless its flag is given: an explicit
    value here overrides intent the pipeline extracts from the brief itself
    ("two minute vertical video" must win over a handler default).

    V3 wizard flags: ``--kind`` (validated against _VIDEO_KINDS),
    ``--url`` (research source), ``--research on|off``, ``--voice``
    (a curated voice key resolves via _VIDEO_VOICES; anything else is
    treated as a raw 'ShortName|+N%' spec), ``--imagery stylized|photos|css``.
    """
    opts: dict[str, Any] = {
        "style": None,
        "design_file": None,
        "aspect": None,
        "duration": None,
        "kind": None,
        "url": None,
        "research": None,
        "voice": None,
        "voice_key": None,
        "imagery": None,
    }
    brief_parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low == "--style" and i + 1 < len(tokens):
            opts["style"] = tokens[i + 1]
            i += 2
        elif low == "--design" and i + 1 < len(tokens):
            opts["design_file"] = tokens[i + 1]
            i += 2
        elif low == "--aspect" and i + 1 < len(tokens):
            opts["aspect"] = tokens[i + 1]
            i += 2
        elif low == "--duration" and i + 1 < len(tokens):
            try:
                opts["duration"] = max(8, min(120, int(tokens[i + 1])))
            except ValueError:
                pass
            i += 2
        elif low == "--kind" and i + 1 < len(tokens):
            value = tokens[i + 1].lower()
            if value in {k for k, _, _ in _VIDEO_KINDS}:
                opts["kind"] = value
            i += 2
        elif low == "--url" and i + 1 < len(tokens):
            opts["url"] = tokens[i + 1]
            i += 2
        elif low == "--research" and i + 1 < len(tokens):
            value = tokens[i + 1].lower()
            if value in ("on", "off"):
                opts["research"] = value == "on"
            i += 2
        elif low == "--voice" and i + 1 < len(tokens):
            raw = tokens[i + 1]
            spec = _video_voice_for_key(raw.lower())
            if spec:
                opts["voice"], opts["voice_key"] = spec, raw.lower()
            else:
                opts["voice"], opts["voice_key"] = raw, raw.split("|", 1)[0]
            i += 2
        elif low == "--imagery" and i + 1 < len(tokens):
            value = tokens[i + 1].lower()
            if value in ("stylized", "photos", "css"):
                opts["imagery"] = value
            i += 2
        else:
            brief_parts.append(tok)
            i += 1
    return " ".join(brief_parts).strip(), opts


def _video_render_kwargs(opts: dict[str, Any]) -> dict[str, Any]:
    """Optional render_brief kwargs from parsed/bound opts (None-sentinel
    passthrough; absent keys keep the pipeline's own resolution order)."""
    kwargs: dict[str, Any] = {}
    for opt_key, kw in (
        ("voice", "voice"),
        ("research_dossier", "research_dossier"),
        ("vision", "vision"),
        ("imagery", "imagery"),
        ("research_query", "research"),
        ("art_max", "art_max"),
    ):
        if opts.get(opt_key) is not None:
            kwargs[kw] = opts[opt_key]
    return kwargs


async def _run_video_render(
    adapter: Any,
    incoming: Any,
    pipeline: Any,
    brief: str,
    opts: dict[str, Any],
) -> None:
    """Background render + same-adapter delivery. Never raises."""
    from models import Attachment, OutgoingMessage

    try:
        kwargs = _video_render_kwargs(opts)
        result = await asyncio.to_thread(
            lambda: pipeline.render_brief(
                brief,
                style=opts.get("style"),
                design_file=opts.get("design_file"),
                aspect=opts.get("aspect"),
                duration_target_s=opts.get("duration"),
                **kwargs,
            )
        )
    except Exception as exc:  # defensive: render_brief should return ok=False instead
        result = {"ok": False, "error": str(exc), "mp4_path": "", "output_dir": ""}
    finally:
        _VIDEO_RENDER_STATE.update({"running": False, "started": "", "brief": ""})

    try:
        if result.get("ok"):
            mp4 = result.get("mp4_path", "")
            size = 0
            try:
                size = Path(mp4).stat().st_size
            except OSError:
                pass
            score = result.get("score") or {}
            text = (
                "Video ready.\n"
                f"  style: {result.get('style', 'neutral')}  |  "
                f"length: {result.get('duration_s', 0):.1f}s  |  "
                f"score: {score.get('final', 'n/a')}\n"
                f"  copy via: {result.get('provider', 'runtime lane')}\n"
                f"  file: {mp4}"
            )
            await adapter.send(
                OutgoingMessage(
                    text=text,
                    channel=incoming.channel,
                    attachments=[
                        Attachment(
                            filename=Path(mp4).name or "video.mp4",
                            mimetype="video/mp4",
                            url=mp4,
                            size_bytes=size or None,
                        )
                    ],
                )
            )
        else:
            await adapter.send(
                OutgoingMessage(
                    text=(
                        "Video render failed.\n"
                        f"  {result.get('error', 'unknown error')}\n"
                        + (f"  artifacts: {result.get('output_dir')}" if result.get("output_dir") else "")
                    ),
                    channel=incoming.channel,
                    is_error=True,
                )
            )
    except Exception as exc:
        print(f"[video] delivery failed: {exc}")


_VIDEO_USAGE = (
    "Usage: /video <brief> [--style name] [--aspect 16:9|9:16|1:1] [--duration s]\n"
    "       /video --kind promo --url https://yoursite.example   (flagged wizard, text vision)\n"
    "       /video styles | /video status | bare /video for the guided wizard\n"
    "       /video approve | /video redo [notes] | /video cancel"
)


async def handle_video(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str | None:
    """/video — generate a branded video: guided wizard with a vision approval
    gate, or a one-shot power path. Any adapter, any model lane.

    Subcommands:
      /video                             guided wizard (kind > input > style > voice > vision)
      /video styles                      list the style library
      /video status                      render state + wizard stage
      /video approve|redo [notes]|cancel drive a pending vision
      /video <brief> [--style name] [--aspect 16:9|9:16|1:1] [--design file]
             [--duration s] [--voice key] [--url u] [--research on|off]
             [--imagery stylized|photos|css]   (power path: no wizard, no gate)
      /video --kind k [--url u] ...      flagged wizard: text vision + /video approve
    """
    tokens = args.split() if args else []
    sub = tokens[0].lower() if tokens else ""

    try:
        pipeline = _import_video_pipeline()
    except Exception as exc:
        return f"Video pipeline unavailable: {exc}"

    key = _video_channel_key(incoming)

    if not tokens:
        if collect_only:
            # Prefetch surfaces can't host a wizard; describe the command instead.
            return _VIDEO_USAGE
        restarted = _video_wizard_get(key) is not None
        await _send_video_kind_keyboard(adapter, incoming, restarted=restarted)
        return None

    if sub == "styles":
        try:
            import video_styles  # type: ignore[import-not-found]

            rows = video_styles.list_styles()
        except Exception as exc:
            return f"Style registry unavailable: {exc}"
        lines = ["Video styles (use --style <name>):"]
        lines += [f"  {row['name']} - {row['tagline']}" for row in rows]
        lines.append("Or derive from your brand: /video <brief> --design <your design.md>")
        return "\n".join(lines)

    if sub == "status":
        lines = []
        if _VIDEO_RENDER_STATE["running"]:
            lines.append(
                f"Render running since {_VIDEO_RENDER_STATE['started']}\n"
                f"  brief: {_VIDEO_RENDER_STATE['brief']}"
            )
        else:
            lines.append("No render running. /video <brief> to start one.")
        pending = _video_wizard_get(key)
        if pending:
            stage = pending.get("stage") or "?"
            kind = pending.get("kind")
            lines.append(f"Wizard: stage {stage}" + (f", kind {kind}" if kind else ""))
        return "\n".join(lines)

    if sub == "cancel" and len(tokens) == 1:
        if _VIDEO_PENDING.pop(key, None) is not None:
            return _VIDEO_CANCEL_TEXT
        return "Nothing to cancel. /video to start a video."

    if sub == "approve" and len(tokens) == 1:
        pending = _video_wizard_get(key)
        if not pending or not pending.get("vision"):
            return "Nothing awaiting approval. /video to start a video."
        return await _video_approve(adapter, incoming, pending, collect_only=collect_only)

    if sub == "redo":
        pending = _video_wizard_get(key)
        if pending and pending.get("vision"):
            feedback = " ".join(tokens[1:]).strip()
            return await _advance_to_vision(
                adapter,
                incoming,
                feedback=feedback,
                prior=pending.get("vision"),
                as_text=pending.get("text_mode", False),
            )
        if len(tokens) == 1:
            return "Nothing to redo. /video to start a video."
        # "/video redo ..." with no pending vision falls through as a brief.

    brief, opts = _parse_video_flags(tokens)

    if not brief:
        if opts.get("kind") or opts.get("url"):
            if collect_only:
                return _VIDEO_USAGE
            return await _run_flagged_wizard(adapter, incoming, pipeline, opts)
        return _VIDEO_USAGE

    # POWER PATH: an explicit brief bypasses the wizard and the vision gate.
    _VIDEO_PENDING.pop(key, None)
    if opts.get("url") and opts.get("research") is not False:
        opts["research_query"] = opts["url"]
    return await _kickoff_video_render(adapter, incoming, pipeline, brief, opts, collect_only=collect_only)


async def _kickoff_video_render(
    adapter: Any,
    incoming: Any,
    pipeline: Any,
    brief: str,
    opts: dict[str, Any],
    *,
    collect_only: bool = False,
) -> str:
    """Shared render kickoff: dep preflight, concurrency guard, inline-vs-background."""
    # "auto" style (the guided flow's surprise-me) resolves against the BRIEF
    # (and the research dossier when one rode along), so the pick matches the
    # idea instead of being random.
    if (opts.get("style") or "").lower() == "auto":
        try:
            import video_styles  # type: ignore[import-not-found]

            if hasattr(video_styles, "suggest_style"):
                opts["style"] = video_styles.suggest_style(brief, opts.get("research_dossier"))
            else:
                opts["style"] = None
        except Exception:
            opts["style"] = None

    missing = pipeline.check_dependencies()
    if missing:
        return (
            "Video rendering needs these tools installed first: " + ", ".join(missing) + "\n"
            "  node + npx (nodejs.org), ffmpeg/ffprobe (ffmpeg.org), edge-tts (pip install edge-tts).\n"
            "  Then retry /video."
        )

    if _VIDEO_RENDER_STATE["running"]:
        return (
            "A render is already running (one at a time).\n"
            f"  started: {_VIDEO_RENDER_STATE['started']}  brief: {_VIDEO_RENDER_STATE['brief']}\n"
            "  /video status to check on it."
        )

    platform_name = str(getattr(getattr(incoming, "platform", ""), "value", getattr(incoming, "platform", ""))).lower()
    _VIDEO_RENDER_STATE.update(
        {"running": True, "started": datetime.now().strftime("%H:%M:%S"), "brief": brief[:120]}
    )

    if collect_only or platform_name == "cli":
        # One-shot surfaces (CLI quiet mode, brief prefetch) cannot outlive the
        # turn, so render inline instead of backgrounding.
        try:
            kwargs = _video_render_kwargs(opts)
            result = await asyncio.to_thread(
                lambda: pipeline.render_brief(
                    brief,
                    style=opts.get("style"),
                    design_file=opts.get("design_file"),
                    aspect=opts.get("aspect", "16:9"),
                    duration_target_s=opts.get("duration", 30),
                    **kwargs,
                )
            )
        finally:
            _VIDEO_RENDER_STATE.update({"running": False, "started": "", "brief": ""})
        if result.get("ok"):
            score = result.get("score") or {}
            return (
                "Video ready.\n"
                f"  style: {result.get('style', 'neutral')}  score: {score.get('final', 'n/a')}\n"
                f"  file: {result.get('mp4_path')}"
            )
        return f"Video render failed: {result.get('error', 'unknown error')}"

    _render_task = asyncio.create_task(
        _run_video_render(adapter, incoming, pipeline, brief, opts)
    )
    _BACKGROUND_TASKS.add(_render_task)
    _render_task.add_done_callback(_BACKGROUND_TASKS.discard)
    style_note = opts.get("style") or "auto (use /video styles to pick)"
    aspect_note = opts.get("aspect") or "from brief"
    duration_note = f"~{opts.get('duration')}s" if opts.get("duration") else "from brief"
    return (
        "Rendering your video now. This usually takes a few minutes; "
        "I'll send the MP4 here when it's done.\n"
        f"  brief: {brief[:160]}\n"
        f"  style: {style_note}  aspect: {aspect_note}  target: {duration_note}"
    )


# Guided /video wizard (V3): bare /video -> kind -> raw material (URL/theme/
# brief, with optional site research) -> ranked style -> voice -> a VISION
# card the operator approves BEFORE anything renders. State is per-channel
# with a TTL refreshed on every transition; every step carries a numbered
# typed fallback so buttonless adapters degrade to plain text. Pickers are
# MATCH-ONLY: a non-matching reply falls through to normal chat.

_VIDEO_PENDING: dict[str, dict[str, Any]] = {}
_VIDEO_PENDING_TTL_S = 600

_VIDEO_STAGES = (
    "await_kind",
    "await_input",
    "researching",
    "await_style",
    "await_voice",
    "await_vision",
)

# (key, label, default duration seconds) - the kind picker, in display order.
_VIDEO_KINDS: list[tuple[str, str, int]] = [
    ("event", "event recap", 30),
    ("promo", "brand promo", 30),
    ("launch", "product launch", 30),
    ("explainer", "explainer", 45),
    ("hype", "hype reel", 20),
    ("surprise", "surprise me", 30),
]

_VIDEO_EXPIRED_TEXT = "That video setup expired (10 min). /video to start fresh."
_VIDEO_CANCEL_TEXT = "Scrapped. /video when you want back in."

_VIDEO_IMAGERY_LABELS = {
    "stylized": "stylized identity-locked art",
    "photos": "real photos from the site",
    "css": "pure CSS scenes",
}


def _video_channel_key(incoming: Any) -> str:
    ch = getattr(incoming, "channel", None)
    platform = str(getattr(ch, "platform", "") or getattr(incoming, "platform", ""))
    pid = str(getattr(ch, "platform_id", "") or "")
    return f"{platform}:{pid}"


def _video_wizard_get(key: str) -> dict[str, Any] | None:
    """Live pending wizard state, or None (expired/legacy entries are popped)."""
    pending = _VIDEO_PENDING.get(key)
    if not pending:
        return None
    if pending.get("v") != 2 or time.time() > pending.get("expires", 0):
        _VIDEO_PENDING.pop(key, None)
        return None
    return pending


def _video_wizard_set(key: str, **updates: Any) -> dict[str, Any]:
    """Create-or-update the pending wizard state; every call refreshes the TTL."""
    pending = _VIDEO_PENDING.get(key)
    if not pending or pending.get("v") != 2:
        pending = {
            "v": 2,
            "stage": "await_kind",
            "kind": None,
            "input": "",
            "url": None,
            "dossier_path": None,
            "dossier": None,
            "style": None,
            "voice": None,
            "voice_key": None,
            "aspect": None,
            "duration": None,
            "imagery": None,
            "vision": None,
            "text_mode": False,
            "expires": 0.0,
        }
        _VIDEO_PENDING[key] = pending
    pending.update(updates)
    pending["expires"] = time.time() + _VIDEO_PENDING_TTL_S
    return pending


def _extract_url(text: str) -> str | None:
    """First http(s) URL in the text, trailing punctuation stripped."""
    import re

    match = re.search(r"https?://\S+", text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)>]}\"'")


def _url_host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or url
    except Exception:
        return url


def _video_kind_label(kind: str | None) -> str:
    return next((label for k, label, _ in _VIDEO_KINDS if k == kind), "video")


def _video_effective_brief(pending: dict[str, Any]) -> str:
    """The render/vision brief: typed input > dossier title > URL > kind."""
    text = (pending.get("input") or "").strip()
    if text:
        return text
    dossier = pending.get("dossier")
    if isinstance(dossier, dict):
        title = str(dossier.get("title") or "").strip()
        if title:
            return f"a video about {title}"
    if pending.get("url"):
        return f"a video about {pending['url']}"
    kind_label = _video_kind_label(pending.get("kind"))
    return f"a short {kind_label if kind_label != 'video' else 'brand'} video"


# Curated voice menu for the guided flow (label, key, "ShortName|rate").
# All free edge-tts neural voices; andrew-fast is the default and the
# operator's bake-off winner.
_VIDEO_VOICES: list[tuple[str, str, str]] = [
    ("andrew (fast, natural)", "andrew", "en-US-AndrewMultilingualNeural|+14%"),
    ("brian (deep narrator)", "brian", "en-US-BrianMultilingualNeural|-4%"),
    ("ryan (british announcer)", "ryan", "en-GB-RyanNeural|-8%"),
    ("roger (upbeat)", "roger", "en-US-RogerNeural|+12%"),
    ("ava (female, natural)", "ava", "en-US-AvaMultilingualNeural|+6%"),
    ("ana (cartoon)", "ana", "en-US-AnaNeural|+18%"),
]


def _video_voice_for_key(key: str) -> str | None:
    for _, k, spec in _VIDEO_VOICES:
        if k == key:
            return spec
    return None


def _import_video_research() -> Any:
    """Import the scripts-dir video_research module (mirrors _import_x_scout).

    The research stage is optional: a checkout without ``video_research``
    keeps the wizard alive in theme-only mode.
    """
    import sys
    from pathlib import Path as _Path

    scripts_dir = str(_Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import video_research  # type: ignore[import-not-found]

    return video_research


async def _video_send(adapter: Any, incoming: Any, text: str, components: list | None = None) -> None:
    """One wizard message on the incoming channel. Never raises."""
    from models import OutgoingMessage

    try:
        await adapter.send(
            OutgoingMessage(
                text=text,
                channel=incoming.channel,
                thread=incoming.thread,
                components=components or [],
            )
        )
    except Exception as exc:
        print(f"[video] wizard send failed: {exc}")


async def _send_video_kind_keyboard(adapter: Any, incoming: Any, *, restarted: bool = False) -> None:
    """STEP 1: what kind of video? Buttons + numbered typed fallback."""
    from models import MessageComponent

    key = _video_channel_key(incoming)
    _VIDEO_PENDING.pop(key, None)
    _video_wizard_set(key, stage="await_kind")
    components = [
        MessageComponent(
            label=label,
            custom_id=f"video_kind:{kind}",
            style="secondary" if kind == "surprise" else "primary",
        )
        for kind, label, _ in _VIDEO_KINDS
    ]
    text = (
        "Let's make a video. What kind?\n"
        "  1 event recap  2 brand promo  3 product launch\n"
        "  4 explainer  5 hype reel  6 surprise me\n"
        "(reply with a number, or skip everything: /video <brief> --style <name>)"
    )
    if restarted:
        text = "Restarting the video setup.\n" + text
    await _video_send(adapter, incoming, text, components)


async def _send_video_input_prompt(adapter: Any, incoming: Any, pending: dict[str, Any]) -> None:
    """STEP 2: the raw material. Plain-text reply; flags ride along."""
    kind_label = _video_kind_label(pending.get("kind"))
    await _video_send(
        adapter,
        incoming,
        (
            f"{kind_label} it is. Now the raw material - drop a URL to build from, "
            "a theme, or a full brief. Flags ride along: --aspect 9:16 --duration 20."
        ),
    )


def _video_style_options(pending: dict[str, Any]) -> list[tuple[str, str]]:
    """Ranked (label, value) style options for STEP 3. Never raises.

    Order: "your brand (from the site)" first (only when the dossier derived
    a usable design), then the ranked registry styles with the top pick
    tagged recommended, then "surprise me" (auto) last.
    """
    options: list[tuple[str, str]] = []
    dossier = pending.get("dossier")
    if (
        isinstance(dossier, dict)
        and dossier.get("ok")
        and isinstance(dossier.get("derived_design"), dict)
    ):
        options.append(("your brand (from the site)", "derived"))
    ranked: list[str] = []
    try:
        _import_video_pipeline()  # puts the scripts dir on sys.path
        import video_styles  # type: ignore[import-not-found]

        basis = _video_effective_brief(pending)
        ranked = list(
            video_styles.suggest_styles_ranked(
                basis, dossier if isinstance(dossier, dict) else None,
                kind=pending.get("kind") or "",
            )
        )
    except Exception:
        ranked = []
    for i, name in enumerate(ranked):
        label = f"* {name} (recommended)" if i == 0 else name
        options.append((label, name))
    options.append(("surprise me", "auto"))
    return options


async def _send_video_style_keyboard(adapter: Any, incoming: Any, *, intro: str = "") -> None:
    """STEP 3: ranked style picker (derived-first), numbered typed fallback."""
    from models import MessageComponent

    key = _video_channel_key(incoming)
    pending = _video_wizard_set(key, stage="await_style")
    options = _video_style_options(pending)
    components = []
    for label, value in options:
        if value == "derived":
            bstyle = "success"
        elif value == "auto":
            bstyle = "secondary"
        else:
            bstyle = "primary"
        components.append(
            MessageComponent(label=label[:80], custom_id=f"video_style:{value}", style=bstyle)
        )
    numbered = "\n".join(f"  {i}. {label}" for i, (label, _v) in enumerate(options, 1))
    text = "Here's how I'd style it:\n" + numbered + "\n(reply with a number or a name)"
    if intro:
        text = intro + "\n" + text
    await _video_send(adapter, incoming, text, components)


async def _send_video_voice_keyboard(adapter: Any, incoming: Any, pending: dict[str, Any]) -> None:
    """STEP 4: narration voice picker, numbered typed fallback."""
    from models import MessageComponent

    key = _video_channel_key(incoming)
    _video_wizard_set(key, stage="await_voice")
    style = pending.get("style")
    if style == "derived":
        style_note = "your brand (from the site)"
    elif style == "auto" or not style:
        style_note = "matched to your idea"
    else:
        style_note = str(style)
    components = [
        MessageComponent(label=label, custom_id=f"video_voice:{voice_key}")
        for label, voice_key, _ in _VIDEO_VOICES
    ]
    numbered = "\n".join(
        f"  {i}. {label}" for i, (label, _k, _s) in enumerate(_VIDEO_VOICES, 1)
    )
    await _video_send(
        adapter,
        incoming,
        (
            f"Style locked: {style_note}. Pick the narration voice:\n"
            f"{numbered}\n(reply with a number or a name)"
        ),
        components,
    )


def _format_vision_card(pending: dict[str, Any]) -> str:
    """The STEP 5 vision card (exact card format; text-first by design)."""
    vision = pending.get("vision") or {}
    lines = ["THE VISION", str(vision.get("angle") or "")]
    lines.append("")
    lines.append("beats:")
    for i, beat in enumerate(vision.get("beats") or [], 1):
        kind = str((beat or {}).get("kind") or "caption")
        summary = str((beat or {}).get("summary") or "")
        lines.append(f"  {i}. [{kind}] {summary}")
    imagery = vision.get("imagery") or {}
    treatment = str(imagery.get("treatment") or "stylized")
    imagery_label = _VIDEO_IMAGERY_LABELS.get(treatment, treatment)
    note = str(imagery.get("note") or "").strip()
    lines.append(f"imagery: {imagery_label}" + (f" - {note}" if note else ""))
    style = pending.get("style")
    look = "your brand (from the site)" if style == "derived" else (style or "auto")
    voice_key = pending.get("voice_key") or "andrew"
    duration = int(vision.get("duration_s") or 0)
    aspect = str(vision.get("aspect") or "")
    lines.append(f"look: {look}   voice: {voice_key}   ~{duration}s   {aspect}")
    lines.append("")
    lines.append("Reply with notes to redo it your way.")
    return "\n".join(lines)


async def _send_video_vision_card(adapter: Any, incoming: Any, pending: dict[str, Any]) -> None:
    """STEP 5: the vision card + the approval gate buttons."""
    from models import MessageComponent

    components = [
        MessageComponent(label="Approve & render", custom_id="video_vision:approve", style="success"),
        MessageComponent(label="Change style", custom_id="video_vision:style", style="secondary"),
        MessageComponent(label="Redo vision", custom_id="video_vision:redo", style="secondary"),
        MessageComponent(label="Cancel", custom_id="video_vision:cancel", style="danger"),
    ]
    await _video_send(adapter, incoming, _format_vision_card(pending), components)


async def _run_video_research(adapter: Any, incoming: Any, url: str) -> None:
    """Wizard research step: build the dossier off-loop, then the style step.

    Failure never stalls the wizard: an unreadable site (or a checkout
    without video_research at all) drops to theme-only with a notice.
    """
    key = _video_channel_key(incoming)
    _video_wizard_set(key, stage="researching", url=url)
    try:
        await adapter.send_typing(incoming.channel)
    except Exception:
        pass
    await _video_send(adapter, incoming, "Reading the site... a few seconds.")

    dossier: dict[str, Any] | None = None
    failure = ""
    research_mod = None
    try:
        research_mod = _import_video_research()
    except Exception:
        failure = "site research isn't wired yet - going theme-only."
    if research_mod is not None:
        try:
            dossier = await asyncio.to_thread(research_mod.build_dossier, url)
        except Exception as exc:
            failure = (
                f"Couldn't read {_url_host(url)} ({type(exc).__name__}). "
                "Going theme-only - your words carry it."
            )
            dossier = None
        if dossier is not None and not dossier.get("ok"):
            notes = [str(n) for n in (dossier.get("notes") or []) if str(n).strip()]
            reason = (notes[0][:80] if notes else "no readable content")
            failure = (
                f"Couldn't read {_url_host(url)} ({reason}). "
                "Going theme-only - your words carry it."
            )
            dossier = None

    _video_wizard_set(key, dossier=dossier)
    if failure:
        await _video_send(adapter, incoming, failure)
    await _send_video_style_keyboard(adapter, incoming)


def _apply_imagery_override(vision: dict[str, Any], pending: dict[str, Any]) -> None:
    """An explicit --imagery flag overrides the vision's proposed treatment.

    "photos" is only honored when the dossier carries images or a cached
    fetched page (mirrors the pipeline's own coercion rule).
    """
    forced = str(pending.get("imagery") or "").strip().lower()
    if forced not in ("stylized", "photos", "css"):
        return
    if forced == "photos":
        dossier = pending.get("dossier")
        has_visuals = isinstance(dossier, dict) and bool(
            dossier.get("images") or (dossier.get("html_text") and dossier.get("url"))
        )
        if not has_visuals:
            return
    if not isinstance(vision.get("imagery"), dict):
        vision["imagery"] = {}
    vision["imagery"]["treatment"] = forced
    vision["imagery"]["note"] = "operator override"


async def _advance_to_vision(
    adapter: Any,
    incoming: Any,
    *,
    feedback: str = "",
    prior: dict[str, Any] | None = None,
    as_text: bool = False,
) -> str | None:
    """Draft (or redraft) the vision off-loop, then present the gate.

    Returns the card as text when ``as_text`` (the flagged-wizard/CLI path);
    otherwise sends the card with buttons and returns None.
    """
    key = _video_channel_key(incoming)
    pending = _video_wizard_get(key)
    if pending is None:
        await _video_send(adapter, incoming, _VIDEO_EXPIRED_TEXT)
        return None
    try:
        pipeline = _import_video_pipeline()
    except Exception as exc:
        message = f"Video pipeline unavailable: {exc}"
        if as_text:
            return message
        await _video_send(adapter, incoming, message)
        return None

    try:
        await adapter.send_typing(incoming.channel)
    except Exception:
        pass
    if not as_text:
        await _video_send(adapter, incoming, "Drafting the vision...")

    # Late "surprise me" resolution: with brief + dossier + kind in hand the
    # pick can actually match the idea, and the card shows a real name.
    if (pending.get("style") or "").lower() == "auto":
        try:
            import video_styles  # type: ignore[import-not-found]

            ranked = video_styles.suggest_styles_ranked(
                _video_effective_brief(pending),
                pending.get("dossier"),
                kind=pending.get("kind") or "",
            )
            if ranked:
                pending = _video_wizard_set(key, style=ranked[0])
        except Exception:
            pass

    brief = _video_effective_brief(pending)
    vision = await asyncio.to_thread(
        pipeline.generate_vision,
        brief,
        kind=pending.get("kind"),
        dossier=pending.get("dossier"),
        style=pending.get("style"),
        voice_label=pending.get("voice_key"),
        duration_s=pending.get("duration"),
        aspect=pending.get("aspect"),
        feedback=feedback,
        prior_vision=prior,
    )
    _apply_imagery_override(vision, pending)
    pending = _video_wizard_set(key, vision=vision, stage="await_vision")
    if as_text:
        return (
            _format_vision_card(pending)
            + "\n\n-> /video approve | /video redo [notes] | /video cancel"
        )
    await _send_video_vision_card(adapter, incoming, pending)
    return None


async def _video_approve(
    adapter: Any,
    incoming: Any,
    pending: dict[str, Any],
    *,
    collect_only: bool = False,
) -> str:
    """Bind the approved vision into render opts and kick off.

    The pending wizard is popped ONLY after the guards pass (render already
    running / missing dependencies keep it, so approve can be retried).
    """
    key = _video_channel_key(incoming)
    vision = pending.get("vision")
    if not isinstance(vision, dict):
        return "Nothing awaiting approval. /video to start a video."
    try:
        pipeline = _import_video_pipeline()
    except Exception as exc:
        return f"Video pipeline unavailable: {exc}"
    if _VIDEO_RENDER_STATE["running"]:
        return (
            "A render is already running (one at a time).\n"
            f"  started: {_VIDEO_RENDER_STATE['started']}  brief: {_VIDEO_RENDER_STATE['brief']}\n"
            "  Your vision is saved - /video approve to retry once it finishes."
        )
    missing = pipeline.check_dependencies()
    if missing:
        return (
            "Video rendering needs these tools installed first: " + ", ".join(missing) + "\n"
            "  node + npx (nodejs.org), ffmpeg/ffprobe (ffmpeg.org), edge-tts (pip install edge-tts).\n"
            "  Your vision is saved - /video approve once they're in."
        )

    style = pending.get("style")
    imagery = str((vision.get("imagery") or {}).get("treatment") or "").strip().lower()
    opts: dict[str, Any] = {
        "style": None if style == "derived" else (style or None),
        "design_file": None,
        "aspect": vision.get("aspect"),
        "duration": vision.get("duration_s"),
        "voice": pending.get("voice"),
        "research_dossier": pending.get("dossier"),
        "vision": vision,
        "imagery": imagery or None,
    }
    brief = _video_effective_brief(pending)
    _VIDEO_PENDING.pop(key, None)  # the guards passed; kickoff cannot refuse now
    return await _kickoff_video_render(
        adapter, incoming, pipeline, brief, opts, collect_only=collect_only
    )


async def _run_flagged_wizard(
    adapter: Any, incoming: Any, pipeline: Any, opts: dict[str, Any]
) -> str:
    """No-brief flagged /video (--kind/--url): inline research + ranked style +
    a TEXT vision with the approve/redo/cancel footer. Works on every adapter
    (no buttons needed) - this is the CLI path."""
    key = _video_channel_key(incoming)
    _VIDEO_PENDING.pop(key, None)

    kind = opts.get("kind")
    url = opts.get("url")
    lines: list[str] = []
    dossier: dict[str, Any] | None = None
    if url and opts.get("research") is not False:
        try:
            research_mod = _import_video_research()
            dossier = await asyncio.to_thread(research_mod.build_dossier, url)
        except Exception:
            dossier = None
        if dossier is not None and not dossier.get("ok"):
            dossier = None
        if dossier is None:
            lines.append(f"Couldn't read {_url_host(url)}. Going theme-only.")

    pending = _video_wizard_set(
        key,
        stage="await_vision",
        kind=kind,
        input="",
        url=url,
        dossier=dossier,
        aspect=opts.get("aspect"),
        duration=opts.get("duration"),
        imagery=opts.get("imagery"),
        text_mode=True,
    )

    style = opts.get("style")
    if not style:
        try:
            import video_styles  # type: ignore[import-not-found]

            ranked = video_styles.suggest_styles_ranked(
                _video_effective_brief(pending), dossier, kind=kind or ""
            )
            style = ranked[0] if ranked else None
        except Exception:
            style = None
    voice_key = opts.get("voice_key") or "andrew"
    voice_spec = opts.get("voice") or _video_voice_for_key(voice_key)
    _video_wizard_set(key, style=style, voice=voice_spec, voice_key=voice_key)

    card = await _advance_to_vision(adapter, incoming, as_text=True)
    if card:
        lines.append(card)
    return "\n".join(lines)


async def handle_video_button(adapter: Any, incoming: Any, custom_id: str) -> None:
    """Button steps of the guided /video wizard.

    video_kind:<key>       store the kind, prompt for the raw material
    video_style:<value>    store the style; voice step (or straight back to
                           the vision on a change-style loopback). A stale
                           tap with no pending starts fresh at the voice step.
    video_voice:<key>      store the voice, draft the vision (legacy 3-part
                           video_voice:<style>:<key> tolerated)
    video_vision:<action>  approve | style | redo | cancel

    Buttons act on the CURRENT pending state, never on message identity, so
    re-sent or stale cards stay safe.
    """
    key = _video_channel_key(incoming)
    pending = _video_wizard_get(key)

    if custom_id.startswith("video_kind:"):
        if pending is None:
            await _video_send(adapter, incoming, _VIDEO_EXPIRED_TEXT)
            return
        kind = custom_id.split(":", 1)[1]
        if kind not in {k for k, _, _ in _VIDEO_KINDS}:
            kind = "surprise"
        pending = _video_wizard_set(key, kind=kind, stage="await_input")
        await _send_video_input_prompt(adapter, incoming, pending)
        return

    if custom_id.startswith("video_style:"):
        choice = custom_id.split(":", 1)[1] or "auto"
        if choice == "surprise":  # legacy v1 custom_id value
            choice = "auto"
        if pending is None:
            # Stale pre-wizard style button: start fresh at the voice step
            # with that style (kind unknown is fine - it only tunes ranking).
            pending = _video_wizard_set(key, style=choice, stage="await_voice")
            await _send_video_voice_keyboard(adapter, incoming, pending)
            return
        pending = _video_wizard_set(key, style=choice)
        if pending.get("voice"):
            # Change-style loopback: the voice is already chosen, so skip
            # straight to a fresh vision in the new look.
            await _advance_to_vision(adapter, incoming)
        else:
            await _send_video_voice_keyboard(adapter, incoming, pending)
        return

    if custom_id.startswith("video_voice:"):
        parts = custom_id.split(":")
        if len(parts) > 2:
            legacy_style, voice_key = parts[1], parts[2]
        else:
            legacy_style, voice_key = None, (parts[1] if len(parts) > 1 else "andrew")
        spec = _video_voice_for_key(voice_key) or _video_voice_for_key("andrew")
        if pending is None:
            if legacy_style is None:
                await _video_send(adapter, incoming, _VIDEO_EXPIRED_TEXT)
                return
            # Legacy 3-part tap on a dead wizard: restore style + voice and
            # ask for the raw material (mirrors the old v1 flow).
            pending = _video_wizard_set(
                key, style=legacy_style, voice=spec, voice_key=voice_key, stage="await_input"
            )
            await _send_video_input_prompt(adapter, incoming, pending)
            return
        updates: dict[str, Any] = {"voice": spec, "voice_key": voice_key}
        if legacy_style and not pending.get("style"):
            updates["style"] = legacy_style
        _video_wizard_set(key, **updates)
        await _advance_to_vision(adapter, incoming)
        return

    if custom_id.startswith("video_vision:"):
        action = custom_id.split(":", 1)[1]
        if pending is None or not isinstance(pending.get("vision"), dict):
            await _video_send(adapter, incoming, _VIDEO_EXPIRED_TEXT)
            return
        if action == "approve":
            reply = await _video_approve(adapter, incoming, pending)
            if reply:
                await _video_send(adapter, incoming, reply)
            return
        if action == "style":
            await _send_video_style_keyboard(
                adapter, incoming, intro="New look, same raw material."
            )
            return
        if action == "redo":
            await _advance_to_vision(adapter, incoming, prior=pending.get("vision"))
            return
        if action == "cancel":
            _VIDEO_PENDING.pop(key, None)
            await _video_send(adapter, incoming, _VIDEO_CANCEL_TEXT)
            return


def _match_wizard_option(text: str, options: list[tuple[str, str]]) -> str | None:
    """EXACT-token option matching for picker stages.

    The whole message must BE the option: its 1-based number, its value, its
    label, or an unambiguous prefix (>= 2 chars) of exactly one option.
    Returns the option value, or None so the message falls through to chat
    ("2" matches; "2pm works for me" does not).
    """
    import re

    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower()).rstrip(".!")
    if not normalized:
        return None
    for i, (value, label) in enumerate(options, 1):
        if normalized in (str(i), value.lower(), label.lower()):
            return value
    if len(normalized) < 2:
        return None
    hits = [
        value
        for value, label in options
        if label.lower().startswith(normalized) or value.lower().startswith(normalized)
    ]
    unique = list(dict.fromkeys(hits))
    return unique[0] if len(unique) == 1 else None


async def try_consume_video_message(adapter: Any, incoming: Any) -> bool:
    """Router hook: stage-gated typed input for the /video wizard.

    Returns True when the message was consumed. Rules:
      - commands ("/...") and button events ("__...") always pass through
      - "cancel"/"stop" cancels at any stage
      - pickers (kind/style/voice) are MATCH-ONLY: non-matching messages fall
        through to normal chat with the wizard kept pending
      - await_input consumes any plain text (flags parsed, URL extracted)
      - researching answers option-ish replies with a hold-on note
      - await_vision consumes any plain text as redo feedback
    """
    import re

    key = _video_channel_key(incoming)
    pending = _video_wizard_get(key)
    if not pending:
        return False

    text = (getattr(incoming, "text", "") or "").strip()
    if not text or text.startswith("/") or text.startswith("__"):
        return False

    lowered = text.lower()
    stage = pending.get("stage")

    if lowered in ("cancel", "stop"):
        _VIDEO_PENDING.pop(key, None)
        await _video_send(adapter, incoming, _VIDEO_CANCEL_TEXT)
        return True

    if stage == "await_kind":
        options = [(kind, label) for kind, label, _ in _VIDEO_KINDS]
        choice = _match_wizard_option(lowered, options)
        if choice is None:
            return False
        pending = _video_wizard_set(key, kind=choice, stage="await_input")
        await _send_video_input_prompt(adapter, incoming, pending)
        return True

    if stage == "await_input":
        brief, opts = _parse_video_flags(text.split())
        url = opts.get("url") or _extract_url(brief)
        if url and not opts.get("url"):
            brief = re.sub(r"\s+", " ", brief.replace(url, " ")).strip()
        updates: dict[str, Any] = {"input": brief, "url": url}
        for field in ("aspect", "duration", "imagery", "style"):
            if opts.get(field) is not None:
                updates[field] = opts[field]
        if opts.get("voice"):
            updates["voice"] = opts["voice"]
            updates["voice_key"] = opts.get("voice_key")
        _video_wizard_set(key, **updates)
        if url and opts.get("research") is not False:
            await _run_video_research(adapter, incoming, url)
        else:
            await _send_video_style_keyboard(adapter, incoming)
        return True

    if stage == "researching":
        # Option-ish replies get a hold-on note; real messages fall through.
        if re.fullmatch(r"[\w*.()-]{1,16}", lowered):
            await _video_send(adapter, incoming, "Still reading the site - hold on.")
            return True
        return False

    if stage == "await_style":
        options = [(value, label) for label, value in _video_style_options(pending)]
        choice = _match_wizard_option(lowered, options)
        if choice is None:
            return False
        pending = _video_wizard_set(key, style=choice)
        if pending.get("voice"):
            await _advance_to_vision(adapter, incoming)
        else:
            await _send_video_voice_keyboard(adapter, incoming, pending)
        return True

    if stage == "await_voice":
        options = [(voice_key, label) for label, voice_key, _ in _VIDEO_VOICES]
        choice = _match_wizard_option(lowered, options)
        if choice is None:
            return False
        _video_wizard_set(key, voice=_video_voice_for_key(choice), voice_key=choice)
        await _advance_to_vision(adapter, incoming)
        return True

    if stage == "await_vision":
        reply = await _advance_to_vision(
            adapter,
            incoming,
            feedback=text,
            prior=pending.get("vision"),
            as_text=pending.get("text_mode", False),
        )
        if reply:
            await _video_send(adapter, incoming, reply)
        return True

    return False


# ---------------------------------------------------------------------------
# LinkedIn on-the-fly workshop
# ---------------------------------------------------------------------------

_LINKEDIN_PENDING: dict[str, dict[str, Any]] = {}
_LINKEDIN_PENDING_TTL_S = 900
_LINKEDIN_EXPIRED_TEXT = "That LinkedIn workshop expired (15 min). /linkedin to start fresh."


def _linkedin_channel_key(incoming: Any) -> str:
    channel = getattr(incoming, "channel", None)
    user = getattr(incoming, "user", None)
    platform = str(
        getattr(channel, "platform", "") or getattr(incoming, "platform", "")
    )
    channel_id = str(getattr(channel, "platform_id", "") or "")
    user_id = str(getattr(user, "platform_id", "") or "")
    return f"{platform}:{channel_id}:{user_id}"


def _linkedin_workshop_get(key: str) -> dict[str, Any] | None:
    pending = _LINKEDIN_PENDING.get(key)
    if not pending:
        return None
    if pending.get("v") != 1 or time.time() > pending.get("expires", 0):
        _LINKEDIN_PENDING.pop(key, None)
        return None
    return pending


def _linkedin_workshop_set(key: str, **updates: Any) -> dict[str, Any]:
    pending = _LINKEDIN_PENDING.get(key)
    if not pending or pending.get("v") != 1:
        pending = {
            "v": 1,
            "stage": "await_mode",
            "mode": None,
            "post_id": None,
            "expires": 0.0,
        }
        _LINKEDIN_PENDING[key] = pending
    pending.update(updates)
    pending["expires"] = time.time() + _LINKEDIN_PENDING_TTL_S
    return pending


async def _linkedin_send(
    adapter: Any,
    incoming: Any,
    text: str,
    *,
    components: list | None = None,
    attachments: list | None = None,
    is_error: bool = False,
) -> None:
    from models import OutgoingMessage

    await adapter.send(
        OutgoingMessage(
            text=text,
            channel=incoming.channel,
            thread=incoming.thread,
            components=components or [],
            attachments=attachments or [],
            is_error=is_error,
        )
    )


async def _send_linkedin_mode_picker(adapter: Any, incoming: Any) -> None:
    from models import MessageComponent

    key = _linkedin_channel_key(incoming)
    _LINKEDIN_PENDING.pop(key, None)
    globals().get("_PRIMO_PENDING", {}).pop(key, None)
    _linkedin_workshop_set(key, stage="await_mode")
    await _linkedin_send(
        adapter,
        incoming,
        (
            "Let's make a LinkedIn post. How do you want to do it?\n\n"
            "1. Cook Together: bring me the rough idea and we'll shape the copy and image.\n"
            "2. Run It for Me: I'll choose the angle and generate the full post plus image.\n\n"
            "You can tap a button or reply with 1 or 2. Nothing posts until Approve & Post."
        ),
        components=[
            MessageComponent(
                label="Cook Together",
                custom_id="linkedin_flow:mode:cook",
                style="primary",
            ),
            MessageComponent(
                label="Run It for Me",
                custom_id="linkedin_flow:mode:run",
                style="success",
            ),
            MessageComponent(
                label="Cancel",
                custom_id="linkedin_flow:cancel",
                style="danger",
            ),
        ],
    )


async def _send_linkedin_topic_prompt(adapter: Any, incoming: Any) -> None:
    await _linkedin_send(
        adapter,
        incoming,
        (
            "What do you want to post about? Send the messy version: an idea, lesson, "
            "story, link, transcript, or a few bullets. I'll turn it into the first copy "
            "and image pass."
        ),
    )


async def _send_linkedin_preview(adapter: Any, incoming: Any, post: Any) -> None:
    from models import Attachment, MessageComponent

    media_path = str(getattr(post, "media_path", "") or "")
    attachments = []
    if media_path and Path(media_path).is_file():
        suffix = Path(media_path).suffix.lower()
        mimetype = "image/png"
        if suffix in {".jpg", ".jpeg"}:
            mimetype = "image/jpeg"
        elif suffix == ".webp":
            mimetype = "image/webp"
        attachments.append(
            Attachment(
                filename=Path(media_path).name,
                mimetype=mimetype,
                url=media_path,
                size_bytes=Path(media_path).stat().st_size,
            )
        )
    media_note = "image ready" if attachments else "image unavailable (copy is still editable)"
    await _linkedin_send(
        adapter,
        incoming,
        (
            f"LINKEDIN DRAFT #{post.id} ({media_note})\n\n{post.body}\n\n"
            "Reply with edits to keep cooking, start with `image:` to direct the visual, "
            "or use the buttons below."
        ),
        attachments=attachments,
        components=[
            MessageComponent(
                label="Approve & Post",
                custom_id=f"social:approve:{post.id}",
                style="success",
            ),
            MessageComponent(
                label="Cook the Copy",
                custom_id=f"linkedin_flow:revise:{post.id}",
                style="primary",
            ),
            MessageComponent(
                label="Redo Image",
                custom_id=f"linkedin_flow:image:{post.id}",
                style="secondary",
            ),
            MessageComponent(
                label="Reject",
                custom_id=f"social:reject:{post.id}",
                style="danger",
            ),
            MessageComponent(
                label="Start Over",
                custom_id="linkedin_flow:restart",
                style="secondary",
            ),
        ],
    )


async def _generate_linkedin_workshop_draft(
    adapter: Any,
    incoming: Any,
    *,
    topic: str | None,
    mode: str,
) -> None:
    key = _linkedin_channel_key(incoming)
    _linkedin_workshop_set(key, stage="generating", mode=mode)
    await _linkedin_send(
        adapter,
        incoming,
        "Cooking the post and image now...",
    )
    try:
        from social.linkedin_workshop import create_linkedin_draft

        post = await asyncio.to_thread(
            create_linkedin_draft,
            topic=topic,
            mode=mode,
        )
    except Exception as exc:
        _linkedin_workshop_set(key, stage="await_topic" if mode == "cook" else "await_mode")
        await _linkedin_send(
            adapter,
            incoming,
            f"LinkedIn draft generation failed: {type(exc).__name__}: {exc}",
            is_error=True,
        )
        return
    _linkedin_workshop_set(
        key,
        stage="await_review",
        mode=mode,
        post_id=post.id,
    )
    await _send_linkedin_preview(adapter, incoming, post)


async def _revise_linkedin_workshop_draft(
    adapter: Any,
    incoming: Any,
    *,
    post_id: int,
    feedback: str,
) -> None:
    key = _linkedin_channel_key(incoming)
    _linkedin_workshop_set(key, stage="generating", post_id=post_id)
    await _linkedin_send(adapter, incoming, "Reworking the copy...")
    try:
        from social.linkedin_workshop import revise_linkedin_copy

        post = await asyncio.to_thread(revise_linkedin_copy, post_id, feedback)
    except Exception as exc:
        _linkedin_workshop_set(key, stage="await_review", post_id=post_id)
        await _linkedin_send(
            adapter,
            incoming,
            f"Copy revision failed: {type(exc).__name__}: {exc}",
            is_error=True,
        )
        return
    _linkedin_workshop_set(key, stage="await_review", post_id=post.id)
    await _send_linkedin_preview(adapter, incoming, post)


async def _regenerate_linkedin_workshop_image(
    adapter: Any,
    incoming: Any,
    *,
    post_id: int,
    direction: str,
) -> None:
    key = _linkedin_channel_key(incoming)
    _linkedin_workshop_set(key, stage="generating", post_id=post_id)
    await _linkedin_send(adapter, incoming, "Reworking the image...")
    try:
        from social.linkedin_workshop import regenerate_linkedin_image

        post = await asyncio.to_thread(
            regenerate_linkedin_image,
            post_id,
            direction,
        )
    except Exception as exc:
        _linkedin_workshop_set(key, stage="await_review", post_id=post_id)
        await _linkedin_send(
            adapter,
            incoming,
            f"Image revision failed: {type(exc).__name__}: {exc}",
            is_error=True,
        )
        return
    _linkedin_workshop_set(key, stage="await_review", post_id=post.id)
    await _send_linkedin_preview(adapter, incoming, post)


async def handle_linkedin(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str | None:
    """Guided LinkedIn post workshop backed by the real approval queue."""

    text = (args or "").strip()
    if not text:
        await _send_linkedin_mode_picker(adapter, incoming)
        return None
    lowered = text.lower()
    if lowered in {"cancel", "stop"}:
        _LINKEDIN_PENDING.pop(_linkedin_channel_key(incoming), None)
        return "LinkedIn workshop cancelled."
    if lowered in {"run", "run it", "auto", "surprise me"}:
        await _generate_linkedin_workshop_draft(
            adapter,
            incoming,
            topic=None,
            mode="run",
        )
        return None
    if lowered == "cook":
        _linkedin_workshop_set(
            _linkedin_channel_key(incoming),
            stage="await_topic",
            mode="cook",
        )
        await _send_linkedin_topic_prompt(adapter, incoming)
        return None
    if lowered.startswith("cook "):
        text = text[5:].strip()
    await _generate_linkedin_workshop_draft(
        adapter,
        incoming,
        topic=text,
        mode="cook",
    )
    return None


async def handle_linkedin_button(
    adapter: Any,
    incoming: Any,
    custom_id: str,
) -> None:
    """Handle authenticated local-workshop buttons; publishing stays social:* owned."""

    raw_event = getattr(incoming, "raw_event", None) or {}
    if raw_event.get("interaction_type") != "button":
        await _linkedin_send(
            adapter,
            incoming,
            "LinkedIn workshop actions only run from the displayed buttons.",
            is_error=True,
        )
        return
    key = _linkedin_channel_key(incoming)

    if custom_id == "linkedin_flow:mode:cook":
        _linkedin_workshop_set(key, stage="await_topic", mode="cook")
        await _send_linkedin_topic_prompt(adapter, incoming)
        return
    if custom_id == "linkedin_flow:mode:run":
        await _generate_linkedin_workshop_draft(
            adapter,
            incoming,
            topic=None,
            mode="run",
        )
        return
    if custom_id in {"linkedin_flow:restart", "linkedin_flow:cancel"}:
        if custom_id.endswith("cancel"):
            _LINKEDIN_PENDING.pop(key, None)
            await _linkedin_send(adapter, incoming, "LinkedIn workshop cancelled.")
        else:
            await _send_linkedin_mode_picker(adapter, incoming)
        return

    parts = custom_id.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await _linkedin_send(
            adapter,
            incoming,
            f"Malformed LinkedIn workshop action: {custom_id}",
            is_error=True,
        )
        return
    action, post_id = parts[1], int(parts[2])
    if action == "revise":
        _linkedin_workshop_set(key, stage="await_revision", post_id=post_id)
        await _linkedin_send(
            adapter,
            incoming,
            "What should I change in the copy? Send it naturally, like: make the hook more direct and cut the last paragraph.",
        )
        return
    if action == "image":
        _linkedin_workshop_set(key, stage="await_image", post_id=post_id)
        await _linkedin_send(
            adapter,
            incoming,
            "What should change about the image? Describe the direction, or reply `surprise me`.",
        )
        return
    await _linkedin_send(
        adapter,
        incoming,
        f"Unknown LinkedIn workshop action: {action}",
        is_error=True,
    )


async def try_consume_linkedin_message(adapter: Any, incoming: Any) -> bool:
    """Consume typed input only while an explicit /linkedin workshop is active."""

    key = _linkedin_channel_key(incoming)
    pending = _linkedin_workshop_get(key)
    if not pending:
        return False
    text = (getattr(incoming, "text", "") or "").strip()
    if not text or text.startswith("/") or text.startswith("__"):
        return False
    lowered = text.lower()
    if lowered in {"cancel", "stop"}:
        _LINKEDIN_PENDING.pop(key, None)
        await _linkedin_send(adapter, incoming, "LinkedIn workshop cancelled.")
        return True

    stage = pending.get("stage")
    if stage == "await_mode":
        if lowered in {"1", "cook", "cook together"}:
            _linkedin_workshop_set(key, stage="await_topic", mode="cook")
            await _send_linkedin_topic_prompt(adapter, incoming)
            return True
        if lowered in {"2", "run", "run it", "run it for me", "auto"}:
            await _generate_linkedin_workshop_draft(
                adapter,
                incoming,
                topic=None,
                mode="run",
            )
            return True
        return False

    if stage == "await_topic":
        await _generate_linkedin_workshop_draft(
            adapter,
            incoming,
            topic=text,
            mode="cook",
        )
        return True
    if stage in {"await_revision", "await_review"}:
        post_id = int(pending.get("post_id") or 0)
        if post_id <= 0:
            _LINKEDIN_PENDING.pop(key, None)
            await _linkedin_send(adapter, incoming, _LINKEDIN_EXPIRED_TEXT)
            return True
        if lowered.startswith("image:"):
            await _regenerate_linkedin_workshop_image(
                adapter,
                incoming,
                post_id=post_id,
                direction=text.split(":", 1)[1].strip() or "surprise me",
            )
        else:
            await _revise_linkedin_workshop_draft(
                adapter,
                incoming,
                post_id=post_id,
                feedback=text,
            )
        return True
    if stage == "await_image":
        post_id = int(pending.get("post_id") or 0)
        if post_id <= 0:
            _LINKEDIN_PENDING.pop(key, None)
            await _linkedin_send(adapter, incoming, _LINKEDIN_EXPIRED_TEXT)
            return True
        await _regenerate_linkedin_workshop_image(
            adapter,
            incoming,
            post_id=post_id,
            direction=text,
        )
        return True
    if stage == "generating":
        await _linkedin_send(adapter, incoming, "Still cooking it. Give me a moment.")
        return True
    return False


# ---------------------------------------------------------------------------
# Primo X on-the-fly workshop
# ---------------------------------------------------------------------------

_PRIMO_PENDING: dict[str, dict[str, Any]] = {}
_PRIMO_PENDING_TTL_S = 900
_PRIMO_EXPIRED_TEXT = "That Primo workshop expired (15 min). /primo to start fresh."


def _primo_channel_key(incoming: Any) -> str:
    return _linkedin_channel_key(incoming)


def _primo_workshop_get(key: str) -> dict[str, Any] | None:
    pending = _PRIMO_PENDING.get(key)
    if not pending:
        return None
    if pending.get("v") != 1 or time.time() > pending.get("expires", 0):
        _PRIMO_PENDING.pop(key, None)
        return None
    return pending


def _primo_workshop_set(key: str, **updates: Any) -> dict[str, Any]:
    pending = _PRIMO_PENDING.get(key)
    if not pending or pending.get("v") != 1:
        pending = {
            "v": 1,
            "stage": "await_mode",
            "mode": None,
            "media_mode": None,
            "topic": None,
            "post_id": None,
            "expires": 0.0,
        }
        _PRIMO_PENDING[key] = pending
    pending.update(updates)
    pending["expires"] = time.time() + _PRIMO_PENDING_TTL_S
    return pending


async def _primo_send(
    adapter: Any,
    incoming: Any,
    text: str,
    *,
    components: list | None = None,
    attachments: list | None = None,
    is_error: bool = False,
) -> None:
    await _linkedin_send(
        adapter,
        incoming,
        text,
        components=components,
        attachments=attachments,
        is_error=is_error,
    )


async def _send_primo_mode_picker(adapter: Any, incoming: Any) -> None:
    from models import MessageComponent

    key = _primo_channel_key(incoming)
    _PRIMO_PENDING.pop(key, None)
    _LINKEDIN_PENDING.pop(key, None)
    _primo_workshop_set(key, stage="await_mode")
    await _primo_send(
        adapter,
        incoming,
        (
            "Let's make a Primo X post. How do you want to cook it?\n\n"
            "1. Cook Together: bring the rough idea and we'll shape the post and visual.\n"
            "2. Run It for Me: Primo picks the angle and builds the full draft.\n\n"
            "Nothing posts until you approve the exact card."
        ),
        components=[
            MessageComponent(
                label="Cook Together",
                custom_id="primo_flow:mode:cook",
                style="primary",
            ),
            MessageComponent(
                label="Run It for Me",
                custom_id="primo_flow:mode:run",
                style="success",
            ),
            MessageComponent(
                label="Cancel",
                custom_id="primo_flow:cancel",
                style="danger",
            ),
        ],
    )


async def _send_primo_topic_prompt(adapter: Any, incoming: Any) -> None:
    await _primo_send(
        adapter,
        incoming,
        (
            "What should Primo post about? Send the rough idea, market observation, "
            "build receipt, crypto/AI lesson, link, or a few bullets."
        ),
    )


async def _send_primo_media_picker(
    adapter: Any,
    incoming: Any,
    *,
    mode: str,
    topic: str | None,
) -> None:
    from models import MessageComponent

    key = _primo_channel_key(incoming)
    _primo_workshop_set(
        key,
        stage="await_media",
        mode=mode,
        topic=topic,
    )
    await _primo_send(
        adapter,
        incoming,
        (
            "What media should this Primo post use?\n\n"
            "Text Only: fastest.\n"
            "Add Image: requires a finished 16:9 Primo visual before approval.\n"
            "Auto-Decide: tries the visual and safely falls back to text if rendering is unavailable."
        ),
        components=[
            MessageComponent(
                label="Text Only",
                custom_id="primo_flow:media:none",
                style="secondary",
            ),
            MessageComponent(
                label="Add Image",
                custom_id="primo_flow:media:image",
                style="primary",
            ),
            MessageComponent(
                label="Auto-Decide",
                custom_id="primo_flow:media:auto",
                style="success",
            ),
            MessageComponent(
                label="Cancel",
                custom_id="primo_flow:cancel",
                style="danger",
            ),
        ],
    )


def _primo_attachment(post: Any) -> list:
    from models import Attachment

    media_path = str(getattr(post, "media_path", "") or "")
    if not media_path or not Path(media_path).is_file():
        return []
    suffix = Path(media_path).suffix.lower()
    mimetype = "image/png"
    if suffix in {".jpg", ".jpeg"}:
        mimetype = "image/jpeg"
    elif suffix == ".webp":
        mimetype = "image/webp"
    elif suffix == ".gif":
        mimetype = "image/gif"
    return [
        Attachment(
            filename=Path(media_path).name,
            mimetype=mimetype,
            url=media_path,
            size_bytes=Path(media_path).stat().st_size,
        )
    ]


async def _send_primo_preview(adapter: Any, incoming: Any, post: Any) -> None:
    from models import MessageComponent

    attachments = _primo_attachment(post)
    media_note = "16:9 image ready" if attachments else "text only"
    components = [
        MessageComponent(
            label="Approve & Post",
            custom_id=f"social:approve:{post.id}",
            style="success",
        ),
        MessageComponent(
            label="Cook the Copy",
            custom_id=f"primo_flow:revise:{post.id}",
            style="primary",
        ),
        MessageComponent(
            label="Redo Image" if attachments else "Add Image",
            custom_id=f"primo_flow:image:{post.id}",
            style="secondary",
        ),
    ]
    if attachments:
        components.append(
            MessageComponent(
                label="Remove Image",
                custom_id=f"primo_flow:remove:{post.id}",
                style="secondary",
            )
        )
    components.extend(
        [
            MessageComponent(
                label="Reject",
                custom_id=f"social:reject:{post.id}",
                style="danger",
            ),
            MessageComponent(
                label="Start Over",
                custom_id="primo_flow:restart",
                style="secondary",
            ),
        ]
    )
    await _primo_send(
        adapter,
        incoming,
        (
            f"PRIMO X DRAFT #{post.id} ({media_note})\n\n{post.body}\n\n"
            "Reply with copy edits, start with `image:` to direct the visual, "
            "or use the buttons below."
        ),
        attachments=attachments,
        components=components,
    )


async def _send_primo_image_failure(
    adapter: Any, incoming: Any, *, post_id: int
) -> None:
    from models import MessageComponent

    await _primo_send(
        adapter,
        incoming,
        (
            f"The copy for Primo draft #{post_id} is ready, but the required image did not render. "
            "Nothing can be approved from this message. Retry the image or explicitly switch to text only."
        ),
        components=[
            MessageComponent(
                label="Retry Image",
                custom_id=f"primo_flow:retry:{post_id}",
                style="primary",
            ),
            MessageComponent(
                label="Use Text Only",
                custom_id=f"primo_flow:textonly:{post_id}",
                style="secondary",
            ),
            MessageComponent(
                label="Reject",
                custom_id=f"social:reject:{post_id}",
                style="danger",
            ),
        ],
        is_error=True,
    )


async def _generate_primo_workshop_draft(
    adapter: Any,
    incoming: Any,
    *,
    topic: str | None,
    mode: str,
    media_mode: str,
) -> None:
    key = _primo_channel_key(incoming)
    _primo_workshop_set(
        key,
        stage="generating",
        topic=topic,
        mode=mode,
        media_mode=media_mode,
    )
    media_label = "copy" if media_mode == "none" else "copy and Primo visual"
    await _primo_send(adapter, incoming, f"Cooking the {media_label} now...")
    from social.primo_workshop import PrimoImageRequiredError, create_primo_draft

    try:
        post = await asyncio.to_thread(
            create_primo_draft,
            topic=topic,
            mode=mode,
            media_mode=media_mode,
        )
    except PrimoImageRequiredError as exc:
        _primo_workshop_set(
            key,
            stage="image_failed",
            post_id=exc.post_id,
            media_mode=media_mode,
        )
        await _send_primo_image_failure(adapter, incoming, post_id=exc.post_id)
        return
    except Exception as exc:
        _primo_workshop_set(key, stage="await_media", mode=mode, topic=topic)
        await _primo_send(
            adapter,
            incoming,
            f"Primo draft generation failed: {type(exc).__name__}: {exc}",
            is_error=True,
        )
        return
    _primo_workshop_set(
        key,
        stage="await_review",
        post_id=post.id,
        mode=mode,
        media_mode=media_mode,
    )
    await _send_primo_preview(adapter, incoming, post)


async def _revise_primo_workshop_draft(
    adapter: Any,
    incoming: Any,
    *,
    post_id: int,
    feedback: str,
) -> None:
    key = _primo_channel_key(incoming)
    _primo_workshop_set(key, stage="generating", post_id=post_id)
    await _primo_send(adapter, incoming, "Reworking Primo's copy...")
    try:
        from social.primo_workshop import revise_primo_copy

        post = await asyncio.to_thread(revise_primo_copy, post_id, feedback)
    except Exception as exc:
        _primo_workshop_set(key, stage="await_review", post_id=post_id)
        await _primo_send(
            adapter,
            incoming,
            f"Copy revision failed: {type(exc).__name__}: {exc}",
            is_error=True,
        )
        return
    _primo_workshop_set(key, stage="await_review", post_id=post.id)
    await _send_primo_preview(adapter, incoming, post)


async def _regenerate_primo_workshop_image(
    adapter: Any,
    incoming: Any,
    *,
    post_id: int,
    direction: str,
) -> None:
    key = _primo_channel_key(incoming)
    _primo_workshop_set(key, stage="generating", post_id=post_id)
    await _primo_send(adapter, incoming, "Rendering a fresh Primo image...")
    from social.primo_workshop import PrimoImageRequiredError, regenerate_primo_image

    try:
        post = await asyncio.to_thread(regenerate_primo_image, post_id, direction)
    except PrimoImageRequiredError:
        _primo_workshop_set(key, stage="image_failed", post_id=post_id)
        await _send_primo_image_failure(adapter, incoming, post_id=post_id)
        return
    except Exception as exc:
        _primo_workshop_set(key, stage="await_review", post_id=post_id)
        await _primo_send(
            adapter,
            incoming,
            f"Image revision failed: {type(exc).__name__}: {exc}",
            is_error=True,
        )
        return
    _primo_workshop_set(key, stage="await_review", post_id=post.id)
    await _send_primo_preview(adapter, incoming, post)


async def _remove_primo_workshop_image(
    adapter: Any, incoming: Any, *, post_id: int
) -> None:
    key = _primo_channel_key(incoming)
    try:
        from social.primo_workshop import remove_primo_image

        post = await asyncio.to_thread(remove_primo_image, post_id)
    except Exception as exc:
        await _primo_send(
            adapter,
            incoming,
            f"Image removal failed: {type(exc).__name__}: {exc}",
            is_error=True,
        )
        return
    _primo_workshop_set(key, stage="await_review", post_id=post.id, media_mode="none")
    await _send_primo_preview(adapter, incoming, post)


async def handle_primo(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str | None:
    """Guided Primo X workshop backed by the real social approval queue."""

    text = (args or "").strip()
    if not text:
        await _send_primo_mode_picker(adapter, incoming)
        return None
    lowered = text.lower()
    key = _primo_channel_key(incoming)
    if lowered in {"cancel", "stop"}:
        _PRIMO_PENDING.pop(key, None)
        return "Primo workshop cancelled."
    if lowered in {"run", "run it", "auto", "surprise me"}:
        await _send_primo_media_picker(
            adapter, incoming, mode="run", topic=None
        )
        return None
    if lowered == "cook":
        _primo_workshop_set(key, stage="await_topic", mode="cook")
        await _send_primo_topic_prompt(adapter, incoming)
        return None
    if lowered.startswith("cook "):
        text = text[5:].strip()
    await _send_primo_media_picker(adapter, incoming, mode="cook", topic=text)
    return None


async def handle_primo_button(
    adapter: Any,
    incoming: Any,
    custom_id: str,
) -> None:
    """Handle authenticated workshop buttons; publishing remains social:* owned."""

    raw_event = getattr(incoming, "raw_event", None) or {}
    if raw_event.get("interaction_type") != "button":
        await _primo_send(
            adapter,
            incoming,
            "Primo workshop actions only run from the displayed buttons.",
            is_error=True,
        )
        return
    key = _primo_channel_key(incoming)

    if custom_id == "primo_flow:mode:cook":
        _primo_workshop_set(key, stage="await_topic", mode="cook")
        await _send_primo_topic_prompt(adapter, incoming)
        return
    if custom_id == "primo_flow:mode:run":
        await _send_primo_media_picker(adapter, incoming, mode="run", topic=None)
        return
    if custom_id in {"primo_flow:restart", "primo_flow:cancel"}:
        if custom_id.endswith("cancel"):
            _PRIMO_PENDING.pop(key, None)
            await _primo_send(adapter, incoming, "Primo workshop cancelled.")
        else:
            await _send_primo_mode_picker(adapter, incoming)
        return
    if custom_id.startswith("primo_flow:media:"):
        media_mode = custom_id.rsplit(":", 1)[-1]
        if media_mode not in {"none", "image", "auto"}:
            await _primo_send(adapter, incoming, "Unknown Primo media choice.", is_error=True)
            return
        pending = _primo_workshop_get(key)
        if not pending or pending.get("stage") != "await_media":
            await _primo_send(adapter, incoming, _PRIMO_EXPIRED_TEXT, is_error=True)
            return
        await _generate_primo_workshop_draft(
            adapter,
            incoming,
            topic=pending.get("topic"),
            mode=str(pending.get("mode") or "run"),
            media_mode=media_mode,
        )
        return

    parts = custom_id.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await _primo_send(
            adapter,
            incoming,
            f"Malformed Primo workshop action: {custom_id}",
            is_error=True,
        )
        return
    action, post_id = parts[1], int(parts[2])
    if action == "revise":
        _primo_workshop_set(key, stage="await_revision", post_id=post_id)
        await _primo_send(
            adapter,
            incoming,
            "What should change in Primo's copy? Send the direction naturally.",
        )
        return
    if action == "image":
        _primo_workshop_set(key, stage="await_image", post_id=post_id)
        await _primo_send(
            adapter,
            incoming,
            "Describe the new visual direction, or reply `surprise me`.",
        )
        return
    if action == "remove" or action == "textonly":
        await _remove_primo_workshop_image(
            adapter, incoming, post_id=post_id
        )
        return
    if action == "retry":
        await _regenerate_primo_workshop_image(
            adapter, incoming, post_id=post_id, direction="retry"
        )
        return
    await _primo_send(
        adapter,
        incoming,
        f"Unknown Primo workshop action: {action}",
        is_error=True,
    )


async def try_consume_primo_message(adapter: Any, incoming: Any) -> bool:
    """Consume typed input only while an explicit /primo workshop is active."""

    key = _primo_channel_key(incoming)
    pending = _primo_workshop_get(key)
    if not pending:
        return False
    text = (getattr(incoming, "text", "") or "").strip()
    if not text or text.startswith("/") or text.startswith("__"):
        return False
    lowered = text.lower()
    if lowered in {"cancel", "stop"}:
        _PRIMO_PENDING.pop(key, None)
        await _primo_send(adapter, incoming, "Primo workshop cancelled.")
        return True

    stage = pending.get("stage")
    if stage == "await_mode":
        if lowered in {"1", "cook", "cook together"}:
            _primo_workshop_set(key, stage="await_topic", mode="cook")
            await _send_primo_topic_prompt(adapter, incoming)
            return True
        if lowered in {"2", "run", "run it", "run it for me", "auto"}:
            await _send_primo_media_picker(adapter, incoming, mode="run", topic=None)
            return True
        return False
    if stage == "await_topic":
        await _send_primo_media_picker(adapter, incoming, mode="cook", topic=text)
        return True
    if stage == "await_media":
        aliases = {
            "1": "none",
            "text": "none",
            "text only": "none",
            "none": "none",
            "2": "image",
            "image": "image",
            "add image": "image",
            "pic": "image",
            "picture": "image",
            "3": "auto",
            "auto": "auto",
            "auto decide": "auto",
            "auto-decide": "auto",
        }
        media_mode = aliases.get(lowered)
        if media_mode is None:
            return False
        await _generate_primo_workshop_draft(
            adapter,
            incoming,
            topic=pending.get("topic"),
            mode=str(pending.get("mode") or "run"),
            media_mode=media_mode,
        )
        return True
    if stage in {"await_revision", "await_review"}:
        post_id = int(pending.get("post_id") or 0)
        if post_id <= 0:
            _PRIMO_PENDING.pop(key, None)
            await _primo_send(adapter, incoming, _PRIMO_EXPIRED_TEXT)
            return True
        if lowered.startswith("image:"):
            await _regenerate_primo_workshop_image(
                adapter,
                incoming,
                post_id=post_id,
                direction=text.split(":", 1)[1].strip() or "surprise me",
            )
        else:
            await _revise_primo_workshop_draft(
                adapter,
                incoming,
                post_id=post_id,
                feedback=text,
            )
        return True
    if stage == "await_image":
        post_id = int(pending.get("post_id") or 0)
        if post_id <= 0:
            _PRIMO_PENDING.pop(key, None)
            await _primo_send(adapter, incoming, _PRIMO_EXPIRED_TEXT)
            return True
        await _regenerate_primo_workshop_image(
            adapter, incoming, post_id=post_id, direction=text
        )
        return True
    if stage == "generating":
        await _primo_send(adapter, incoming, "Still cooking it. Give me a moment.")
        return True
    if stage == "image_failed":
        await _primo_send(
            adapter,
            incoming,
            "Use the Retry Image or Use Text Only button on the latest message.",
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Social Post Queue (Issue #77)
# ---------------------------------------------------------------------------


def _spawn_social_post_runner(post_id: int) -> str:
    """Claim + hand an approved post to the detached Browser Homie runner.

    The bot never drives the browser on its event loop (the 2026-07-13 wedge:
    a hung agent-browser child froze every adapter, /health, and the liveness
    supervisor at once). This claims the row (CAS — a double-tap or a cron
    race is a no-op) and spawns the one-shot runner; the posted/failed receipt
    arrives cross-process via social.notify.
    """
    import sys

    from social.service import SocialPostService

    svc = SocialPostService()
    post = svc.get_post(post_id)
    if post is None:
        return f"Error: Post {post_id} not found"
    if post.status != "approved":
        return f"Error: Post {post_id} has status '{post.status}', expected 'approved'"
    if not svc.claim_post(post_id):
        return f"Post #{post_id} is already being posted — receipt incoming."

    import social

    runner_script = Path(social.__file__).resolve().parent / "browser_homie_runner.py"
    scripts_dir = runner_script.parent.parent
    log_path = Path(__file__).resolve().parent.parent / "data" / "social_runner.log"
    try:
        from shared import spawn_detached

        runner_pid = spawn_detached(
            [sys.executable, str(runner_script), "--post-id", str(post_id), "--claimed"],
            cwd=str(scripts_dir),
            log_path=log_path,
        )
    except Exception as exc:  # noqa: BLE001 — release the claim so a retry can win it
        svc.clear_claim(post_id)
        return f"Error: could not start the Browser Homie runner: {exc}"
    return (
        f"Post #{post_id} ({post.channel}) queued — Browser Homie is on it "
        f"(runner PID {runner_pid}). Receipt incoming."
    )


async def handle_social(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Social post queue — status, queue, draft, approve, reject, post, cadence."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

    parts = args.strip().split(None, 1) if args.strip() else []
    subcmd = parts[0].lower() if parts else "status"
    rest = parts[1] if len(parts) > 1 else ""

    if subcmd == "status":
        try:
            from social.service import SocialPostService
            from social.channels import list_channels

            svc = SocialPostService()
            counts = svc.count_by_status()
            channels = list_channels()

            lines = ["*Social Post Queue*\n"]
            total = sum(counts.values())
            lines.append(f"Total posts: {total}")
            for status in ("draft", "approved", "posted", "failed", "rejected"):
                c = counts.get(status, 0)
                if c:
                    lines.append(f"  {status}: {c}")
            lines.append(f"\n*Channels ({len(channels)}):*")
            for ch in channels:
                cadence = "ON" if ch.cadence_enabled else "off"
                lines.append(f"  {ch.display_name} [{ch.execution_method}] cadence={cadence}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    elif subcmd == "queue":
        try:
            from social.service import SocialPostService
            svc = SocialPostService()
            posts = svc.list_queue(limit=20)
            if not posts:
                return "No posts in queue."
            lines = ["*Social Post Queue*\n"]
            for p in posts:
                badge = p.status.upper()
                title = p.title[:50] if p.title else "(no title)"
                lines.append(f"  [{badge}] #{p.id} {p.channel} — {title}")
                lines.append(f"    Created: {p.created_at}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    elif subcmd == "draft":
        draft_parts = rest.strip().split(None, 1)
        if len(draft_parts) < 2:
            return "Usage: `/social draft <channel> <idea>`\nExample: `/social draft linkedin Our new AI receptionist handles 100 calls a day`"
        channel_id, topic = draft_parts[0].lower(), draft_parts[1]
        try:
            from social.draft_generator import generate_draft
            pid = generate_draft(channel_id, topic, topic_source="manual")
            if pid:
                from social.service import SocialPostService
                svc = SocialPostService()
                post = svc.get_post(pid)
                preview = post.body[:200] if post else ""
                return f"Draft created: #{pid} ({channel_id})\n\n{preview}{'...' if post and len(post.body) > 200 else ''}\n\nApprove: `/social approve {pid}`"
            return "Draft generation failed. Check logs."
        except Exception as e:
            return f"Error creating draft: {e}"

    elif subcmd == "approve":
        try:
            post_id = int(rest.strip())
        except (ValueError, TypeError):
            return "Usage: `/social approve <id>`"
        try:
            from social.service import SocialPostService
            from social.audit import append_social_audit_record
            svc = SocialPostService()
            post = svc.approve_post(post_id)
            append_social_audit_record(
                channel=post.channel, action="approve", post_id=post_id,
                outcome="approved", operator="operator",
            )
            return f"Post #{post_id} approved ({post.channel}). Dispatch: `/social post {post_id}`"
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"

    elif subcmd == "reject":
        reject_parts = rest.strip().split(None, 1)
        try:
            post_id = int(reject_parts[0]) if reject_parts else 0
        except (ValueError, TypeError):
            return "Usage: `/social reject <id> [reason]`"
        reason = reject_parts[1] if len(reject_parts) > 1 else ""
        try:
            from social.service import SocialPostService
            from social.audit import append_social_audit_record
            svc = SocialPostService()
            post = svc.reject_post(post_id, reason=reason)
            append_social_audit_record(
                channel=post.channel, action="reject", post_id=post_id,
                outcome="rejected", operator="operator",
            )
            return f"Post #{post_id} rejected."
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"

    elif subcmd == "post":
        try:
            post_id = int(rest.strip())
        except (ValueError, TypeError):
            return "Usage: `/social post <id>` (post must be approved first)"
        try:
            # Never dispatch inline: the browser drive is 60-120s of blocking
            # subprocess work and a hung agent-browser child wedges the whole
            # event loop. Claim + spawn the detached Browser Homie runner.
            return _spawn_social_post_runner(post_id)
        except Exception as e:
            return f"Error dispatching post: {e}"

    elif subcmd == "schedule":
        schedule_parts = rest.strip().split(None, 1) if rest.strip() else []
        if len(schedule_parts) < 2:
            return "Usage: `/social schedule <id> <ISO datetime>` (e.g. `/social schedule 5 2026-06-19T10:00:00+00:00`)"
        try:
            post_id = int(schedule_parts[0])
            scheduled_for = schedule_parts[1].strip()
        except (ValueError, TypeError):
            return "Usage: `/social schedule <id> <ISO datetime>`"
        try:
            from social.service import SocialPostService
            svc = SocialPostService()
            post = svc.schedule_post(post_id, scheduled_for)
            return f"Post #{post_id} scheduled for {post.scheduled_for}."
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error scheduling post: {e}"

    elif subcmd in ("dispatch-due", "dispatchdue"):
        try:
            from social.post_executor import dispatch_due_posts
            result = dispatch_due_posts()
            lines = ["*Dispatch Due Posts*\n"]
            lines.append(f"Dispatched: {result['dispatched']}")
            lines.append(f"Failed: {result['failed']}")
            lines.append(f"Blocked: {result['blocked']}")
            if result["errors"]:
                lines.append("\nErrors:")
                for err in result["errors"]:
                    lines.append(f"  {err}")
            if result["dispatched"] == 0 and result["failed"] == 0:
                lines.append("\nNo posts due for dispatch.")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    elif subcmd == "cadence":
        try:
            from social.channels import list_channels
            channels = list_channels()
            lines = ["*Social Cadence Config*\n"]
            for ch in channels:
                status = "ACTIVE" if ch.cadence_enabled else "off"
                lines.append(f"  *{ch.display_name}*: {status}, every {ch.cadence_interval_hours}h, dispatch={ch.execution_method}")
                if ch.topic_pool:
                    lines.append(f"    Topics: {', '.join(ch.topic_pool[:5])}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    else:
        return (
            "*Social Post Queue Commands*\n\n"
            "`/social status` — overview\n"
            "`/social queue` — pending posts\n"
            "`/social draft <channel> <idea>` — create draft\n"
            "`/social approve <id>` — approve for posting\n"
            "`/social reject <id> [reason]` — reject draft\n"
            "`/social post <id>` — dispatch approved post\n"
            "`/social schedule <id> <datetime>` — set dispatch time\n"
            "`/social dispatch-due` — dispatch all scheduled posts due now\n"
            "`/social cadence` — cadence settings"
        )


# ---------------------------------------------------------------------------
# /cofounder — autonomous co-founder project steering (US-015)
# ---------------------------------------------------------------------------


def _cofounder_usage_text() -> str:
    return (
        "*Co-Founder Projects*\n\n"
        "`/cofounder status` — orchestrator overview\n"
        "`/cofounder list` — projects with status\n"
        "`/cofounder show <slug>` — one project in detail\n"
        "`/cofounder steer <slug> <text>` — leave steering for the next pass\n"
        "`/cofounder pause <slug>` — park a project as awaiting-human\n"
        "`/cofounder resume <slug>` — restore a paused project's prior status\n"
        "`/cofounder approve <slug>` — human verdict: flip a park to done + archive\n"
        "`/cofounder agenda` — today's proposed agenda with delegation status\n"
        "`/cofounder run <n>` — approve agenda line n: delegate it to its persona"
    )


def _cofounder_project_path(slug: str, projects_dir: Path) -> Path | None:
    """Resolve a slug to its project file, or None when unknown.

    Mirrors discovery's skip rules (no ``_``-prefix, no README*) and rejects
    any character outside ``[A-Za-z0-9._-]`` so a slug can never traverse out
    of the projects dir (path separators are not in the allowed set).
    """
    if not slug or slug.startswith("_") or slug.lower().startswith("readme"):
        return None
    if not all(ch.isalnum() or ch in "._-" for ch in slug):
        return None
    path = projects_dir / f"{slug}.md"
    return path if path.is_file() else None


def _cofounder_unknown_slug_text(slug: str, projects_dir: Path) -> str:
    from cofounder import project_model

    slugs = [p.slug for p in project_model.discover_projects(projects_dir)]
    known = ", ".join(slugs) if slugs else "none yet"
    return f"Unknown co-founder project '{slug}'. Known projects: {known}."


def _cofounder_project_line(project: Any) -> str:
    fm = project.frontmatter
    gate = " [subjective]" if fm.subjective_gate else ""
    return (
        f"  {project.slug} — {fm.status}{gate} "
        f"(iter {fm.iterations}/{fm.max_iterations})"
    )

async def handle_cofounder(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False
) -> str:
    """Co-founder project steering — status|list|show|steer|pause|resume|approve.

    Steering is file-mediated (prd Phase 6): handlers write markdown through
    the cofounder ownership helpers ([steer] Activity Log lines, frontmatter
    status re-stamps) and the heartbeat pass reads the files on its next
    cycle — no cross-process registry. Deliberately NOT gated on the
    cofounder kill switch: pause/steer are the operator's manual controls
    for stopping the autonomous surface, so they must keep working while
    the switch refuses passes and notifies.
    """
    import config  # scripts-side config (chat process has .claude/scripts on sys.path)
    from cofounder import project_model
    from cofounder import state as state_mod
    from cofounder import status as status_mod

    args = (args or "").strip()
    if not args or args.lower() in {"help", "?"}:
        return _cofounder_usage_text()

    sub, _, rest = args.partition(" ")
    sub = sub.lower()
    rest = rest.strip()

    try:
        settings = config.get_cofounder_settings()
        projects_dir = Path(settings.projects_dir)

        # Cofounder v2 WS3 — the agenda approval surface. `run <n>` is the
        # operator's per-line approval and works while the autonomous flag
        # (COFOUNDER_DELEGATION_ENABLED) is false; the cofounder_delegation
        # kill switch inside delegate.py is the emergency stop for both.
        if sub == "agenda":
            from cofounder import delegate as delegate_mod

            return delegate_mod.render_agenda_status(date=rest or None)

        if sub == "run":
            from cofounder import delegate as delegate_mod

            line_arg, _, _tail = rest.partition(" ")
            try:
                line_number = int(line_arg)
            except (TypeError, ValueError):
                return "Usage: /cofounder run <line-number> (see /cofounder agenda)"
            result = delegate_mod.run_agenda_line(
                line_number, approved_by=str(getattr(incoming, "user_id", "operator"))
            )
            return result.message

        if sub == "status":
            projects = project_model.discover_projects(projects_dir)
            counts: dict[str, int] = {}
            for p in projects:
                counts[p.frontmatter.status] = counts.get(p.frontmatter.status, 0) + 1
            summary = ""
            if counts:
                summary = " (" + ", ".join(
                    f"{k}: {v}" for k, v in sorted(counts.items())
                ) + ")"
            lines = [
                "*Co-Founder Orchestrator*",
                f"Enabled: {'yes' if settings.enabled else 'no (COFOUNDER_ENABLED=false)'}",
                f"Projects dir: {projects_dir}",
                f"Projects: {len(projects)}{summary}",
            ]
            lines.extend(_cofounder_project_line(p) for p in projects)
            return "\n".join(lines)

        if sub == "list":
            projects = project_model.discover_projects(projects_dir)
            if not projects:
                return f"No co-founder projects. Drop a spec file in {projects_dir}."
            return "\n".join(
                ["*Co-founder projects:*"]
                + [_cofounder_project_line(p) for p in projects]
            )

        if sub in {"show", "steer", "pause", "resume", "approve"}:
            slug, _, tail = rest.partition(" ")
            slug = slug.strip()
            tail = tail.strip()
            if not slug:
                extra = " <text>" if sub == "steer" else ""
                return f"Usage: /cofounder {sub} <slug>{extra}"
            path = _cofounder_project_path(slug, projects_dir)
            if path is None:
                return _cofounder_unknown_slug_text(slug, projects_dir)
            project = project_model.parse_project_file(path)
            fm = project.frontmatter

            if sub == "show":
                plan = project.plan.strip() or "(empty)"
                if len(plan) > 600:
                    plan = plan[:600] + " [...]"
                log_lines = [ln for ln in project.activity_log.splitlines() if ln.strip()]
                recent = log_lines[-5:] if log_lines else ["(none)"]
                gate = " [subjective gate]" if fm.subjective_gate else ""
                return "\n".join(
                    [
                        f"*{project.title}* ({project.slug})",
                        f"Status: {fm.status}{gate}",
                        f"Repo: {fm.repo or '-'} | Branch: {fm.branch or '-'}",
                        f"Iterations: {fm.iterations}/{fm.max_iterations}",
                        f"Job: {fm.current_job_id or 'none'}",
                        "",
                        "*Plan:*",
                        plan,
                        "",
                        "*Recent activity:*",
                        *recent,
                    ]
                )

            if sub == "steer":
                if not tail:
                    return "Usage: /cofounder steer <slug> <text>"
                # Activity Log entries are single-line (US-003 ownership
                # contract) — collapse operator newlines/whitespace runs.
                one_line = " ".join(tail.split())
                entry = project_model.append_activity_log(path, f"[steer] {one_line}")
                return (
                    f"Steering noted for '{slug}' — the next pass will read it.\n{entry}"
                )

            if sub == "pause":
                if fm.status == "awaiting-human":
                    return f"'{slug}' is already awaiting-human."
                if status_mod.is_terminal(fm.status):
                    return f"'{slug}' is done — nothing to pause."
                status_mod.transition(fm.status, "awaiting-human")
                # Stash the prior status BEFORE the re-stamp so resume can
                # restore it (a stash with an unchanged status is harmless).
                state_mod.update_project_state(slug, paused_from=fm.status)
                project_model.update_frontmatter(path, status="awaiting-human")
                project_model.append_activity_log(
                    path, f"[pause] paused by operator (was {fm.status})"
                )
                return (
                    f"'{slug}' paused — status awaiting-human (was {fm.status}). "
                    f"Resume with /cofounder resume {slug}."
                )

            if sub == "resume":
                if fm.status != "awaiting-human":
                    return (
                        f"'{slug}' is not paused (status: {fm.status}) — only "
                        "awaiting-human projects can be resumed."
                    )
                stash = state_mod.get_project_state(
                    state_mod.load_state(), slug
                ).get("paused_from")
                # Restore the prior active status; a missing or non-restorable
                # stash (paused from blocked / a rogue non-enum) re-enters the
                # decision loop as `new` — code never writes a non-enum value.
                target = (
                    stash
                    if isinstance(stash, str)
                    and status_mod.can_transition("awaiting-human", stash)
                    else "new"
                )
                project_model.update_frontmatter(path, status=target)
                state_mod.update_project_state(slug, paused_from=None)
                project_model.append_activity_log(
                    path, f"[resume] resumed by operator (status: {target})"
                )
                return f"'{slug}' resumed — status {target}."

            if sub == "approve":
                if fm.status != "awaiting-human":
                    return (
                        f"'{slug}' is not awaiting a human verdict "
                        f"(status: {fm.status}). Approve applies to "
                        "awaiting-human projects."
                    )
                status_mod.transition(fm.status, "done")
                project_model.append_activity_log(
                    path, "[approve] approved by operator -> done"
                )
                project_model.update_frontmatter(path, status="done")
                archived = project_model.archive_to_done(path)
                return (
                    f"'{slug}' approved — done and archived to "
                    f"done/{archived.name}."
                )

        return _cofounder_usage_text()
    except project_model.ProjectParseError as exc:
        return f"Co-founder project file could not be read: {exc}"
    except Exception as exc:  # friendly text, never a stack trace to chat
        return f"Co-founder command failed: {exc}"


# ---------------------------------------------------------------------------
# Operator Automation UX (Phase 2) — /recap, /blueprints, /suggestions
#
# /recap is pure-local zero-LLM over chat.db. /blueprints and /suggestions
# consume the orchestration import contract (blueprint_catalog, suggestions,
# suggestion_catalog) via LAZY imports inside the handler bodies so an import
# failure degrades to a friendly line instead of crashing the router
# (fail-open at the cross-slice seam). Accept flows THROUGH the guarded
# /api/scheduled path via integrations.scheduled_api — never a local create.
# ---------------------------------------------------------------------------


async def handle_recap(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Zero-LLM session recap — turn counts, tool histogram, files touched, last exchange.

    Reads the CURRENT session's recent messages from the chat store and renders
    the pure-local recap. No engine invocation, no tokens. Uses
    ``list_recent_messages`` (latest-N, chronological) — NOT ``list_messages``
    (which returns the OLDEST 200). The scan window is capped at 120 messages;
    ``build_recap`` further windows to the last 20 user/assistant turns.
    """
    store, existing, platform_str, *_ = _get_session(incoming)
    if not existing:
        return build_recap([], platform=platform_str)
    try:
        msgs = store.list_recent_messages(existing.session_id, limit=120)
    except Exception:
        msgs = []
    payload = [
        {"role": m.role, "content": m.content, "tool_calls": m.tool_calls}
        for m in msgs
    ]
    return build_recap(
        payload,
        session_id=existing.session_id,
        platform=platform_str,
    )


def _render_blueprint_list(blueprint_catalog: Any) -> str:
    """Render the catalog as a slug list with usage footer."""
    lines = ["*Automation Blueprints*", ""]
    for bp in getattr(blueprint_catalog, "CATALOG", []):
        lines.append(f"  `{bp.key}` — {bp.title}")
        desc = getattr(bp, "description", "")
        if desc:
            lines.append(f"     {desc}")
    lines.append("")
    lines.append("Show one: `/blueprints <key>`")
    lines.append("Propose: `/blueprints <key> slot=value ...`")
    return "\n".join(lines)


def _render_blueprint_detail(blueprint_catalog: Any, bp: Any) -> str:
    """Render a single blueprint's slots + pre-filled command."""
    lines = [f"*Blueprint: {bp.title}* (`{bp.key}`)"]
    desc = getattr(bp, "description", "")
    if desc:
        lines.append(desc)
    try:
        entry = blueprint_catalog.blueprint_catalog_entry(bp)
    except Exception:
        entry = {}
    sched_human = entry.get("scheduleHuman") if isinstance(entry, dict) else None
    if sched_human:
        lines.append(f"  Schedule: {sched_human}")

    slots = getattr(bp, "slots", None) or []
    if slots:
        lines.append("")
        lines.append("*Slots:*")
        for slot in slots:
            name = getattr(slot, "name", "")
            label = getattr(slot, "label", None) or name
            req = "" if getattr(slot, "optional", False) else " (required)"
            default = getattr(slot, "default", None)
            dflt = f" [default: {default}]" if default not in (None, "") else ""
            options = getattr(slot, "options", None) or ()
            opt = f" — options: {', '.join(map(str, options))}" if options else ""
            lines.append(f"  `{name}` — {label}{req}{dflt}{opt}")

    try:
        cmd = blueprint_catalog.blueprint_slash_command(bp)
    except Exception:
        cmd = None
    if cmd:
        lines.append("")
        lines.append(f"Pre-filled: `{cmd}`")
    lines.append("")
    lines.append("Propose it: `/blueprints <key> slot=value ...`")
    return "\n".join(lines)


async def handle_blueprints(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Automation blueprints — `list` | `<key>` | `<key> slot=val ...` (proposes).

    Filling a blueprint registers a PENDING suggestion — it does NOT auto-create
    a scheduled task (default-deny / propose-don't-auto-create). The operator
    must explicitly `/suggestions accept <n>` to schedule it.
    """
    import hashlib

    try:
        from orchestration import blueprint_catalog, suggestions
    except Exception:
        return "Automation blueprints are unavailable right now."

    raw = (args or "").strip()
    try:
        tokens = shlex.split(raw) if raw else []
    except ValueError:
        tokens = raw.split()

    if not tokens or tokens[0].lower() in {"list", "ls"}:
        return _render_blueprint_list(blueprint_catalog)

    key = tokens[0]
    bp = blueprint_catalog.get_blueprint(key)
    if bp is None:
        return f"No blueprint '{key}'. Use `/blueprints` to list them."

    values: dict[str, str] = {}
    for tok in tokens[1:]:
        if "=" in tok:
            name, _, val = tok.partition("=")
            name = name.strip()
            if name:
                values[name] = val.strip()

    if not values:
        return _render_blueprint_detail(blueprint_catalog, bp)

    # Fill → propose a pending suggestion (no auto-create).
    try:
        spec = blueprint_catalog.fill_blueprint(bp, values)
    except blueprint_catalog.BlueprintFillError as exc:
        return f"Could not fill '{key}': {exc}"
    except Exception as exc:
        return f"Could not fill '{key}': {exc}"

    job_spec = blueprint_catalog.scheduled_kwargs_from_spec(spec)
    dedup_basis = f"{bp.key}|{job_spec.get('schedule', '')}|{job_spec.get('prompt', '')}"
    dedup_key = "blueprint:" + hashlib.sha1(dedup_basis.encode("utf-8")).hexdigest()[:12]
    title = getattr(bp, "title", key)
    description = getattr(bp, "description", "")
    rec = suggestions.add_suggestion(
        title=title,
        description=description,
        source="blueprint",
        job_spec=job_spec,
        dedup_key=dedup_key,
    )
    if rec is None:
        return (
            f"'{title}' is already proposed (or was dismissed). "
            f"Use `/suggestions` to review pending proposals."
        )
    schedule = job_spec.get("schedule", "")
    sched_txt = f" (`{schedule}`)" if schedule else ""
    return (
        f"Proposed '{title}'{sched_txt}. It's pending — review with `/suggestions`, "
        f"then `/suggestions accept <n>` to schedule it through the guard."
    )


def _render_pending_suggestions(pending: list) -> str:
    """Render the pending proposal list with 1-based refs."""
    if not pending:
        return (
            "*Automation Suggestions*\n"
            "No pending proposals. Fill a blueprint with "
            "`/blueprints <key> slot=value ...`"
        )
    lines = ["*Automation Suggestions* (pending)", ""]
    for i, s in enumerate(pending, start=1):
        title = s.get("title") or "(untitled)"
        spec = s.get("job_spec") or {}
        schedule = spec.get("schedule", "")
        head = f"  {i}. {title}" + (f" — `{schedule}`" if schedule else "")
        lines.append(head)
        desc = s.get("description")
        if desc:
            lines.append(f"     {desc}")
    lines.append("")
    lines.append("Accept: `/suggestions accept <n>`   Dismiss: `/suggestions dismiss <n>`")
    return "\n".join(lines)


async def handle_suggestions(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Automation proposals — `list` | `accept <ref>` | `dismiss <ref>`.

    Listing seeds the curated starter catalog on first view (when the store is
    empty). Accept renders the suggestion's job_spec and hands it THROUGH the
    guarded ``/api/scheduled`` path (cross-process, server-side bot-lifecycle
    guard) — a refused prompt returns the guard's verbatim message, never a 500.
    Dismiss latches the dedup_key forever.
    """
    try:
        from orchestration import suggestion_catalog, suggestions
    except Exception:
        return "Automation suggestions are unavailable right now."

    parts = (args or "").split()
    sub = parts[0].lower() if parts else "list"

    if sub in {"list", "ls"}:
        try:
            if not suggestions.load_suggestions():
                suggestion_catalog.seed_catalog_suggestions()
        except Exception:
            pass
        try:
            pending = suggestions.list_pending()
        except Exception:
            pending = []
        return _render_pending_suggestions(pending)

    if sub == "accept":
        ref = parts[1] if len(parts) > 1 else ""
        if not ref:
            return "Usage: `/suggestions accept <n>`"
        from integrations import scheduled_api  # lazy, cross-process, fail-open

        origin = {
            "platform": getattr(getattr(incoming, "platform", None), "value", None),
            "chat_id": getattr(incoming, "chat_id", None),
        }

        async def _create(spec: dict) -> dict:
            return await scheduled_api.create_scheduled_task(spec)

        try:
            job = await suggestions.accept_suggestion_async(
                ref, create_fn=_create, origin=origin,
            )
        except scheduled_api.ScheduledAPIError as exc:
            return exc.friendly_message
        if job is None:
            return "No such pending suggestion. Use `/suggestions` to list them."
        return "Scheduled — it's now an active scheduled task."

    if sub == "dismiss":
        ref = parts[1] if len(parts) > 1 else ""
        if not ref:
            return "Usage: `/suggestions dismiss <n>`"
        try:
            ok = suggestions.dismiss_suggestion(ref)
        except Exception:
            ok = False
        return "Dismissed — won't be offered again." if ok else "No such suggestion."

    return (
        "*Automation Suggestions*\n"
        "`/suggestions` — list pending proposals\n"
        "`/suggestions accept <n>` — schedule it (runs through the guard)\n"
        "`/suggestions dismiss <n>` — never offer it again"
    )


# ---------------------------------------------------------------------------
# Handler lookup — maps command name to handler function
# ---------------------------------------------------------------------------

async def _persona_channel_turn(incoming: Any, instruction: str) -> str:
    """Route a mode-shaped instruction through the channel's bound persona.

    The closer commands (/draft /spar /checkin /debrief) are thin wrappers:
    they reshape the operator's args into a mode instruction and run it as a
    normal persona turn, so the reply carries the persona's identity, memory,
    and skill index — and the turn lands in that persona's learning corpus.
    """
    from dataclasses import replace as dc_replace

    import config
    from discord_channel_bindings import resolve_discord_channel_binding
    from discord_persona_runtime import run_discord_persona_channel_turn
    from session import get_session_store

    binding = resolve_discord_channel_binding(incoming)
    if binding is None:
        return (
            "Run this inside a persona channel (like #YourProduct-salesguy) so the "
            "right homie answers."
        )
    shaped = dc_replace(incoming, text=instruction)
    outgoing = await run_discord_persona_channel_turn(
        incoming=shaped,
        binding=binding,
        session_store=get_session_store(),
        project_root=config.PROJECT_ROOT,
    )
    return getattr(outgoing, "text", "") or "(no reply)"


async def handle_draft(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Draft a client message in the closer voice via the channel's persona."""
    request = args.strip()
    if not request:
        return 'Usage: `/draft <client + goal>` — e.g. `/draft rebecca, nudge the deposit`'
    instruction = (
        "[DRAFT MODE] Draft a client-facing message in our premium client voice "
        f"(closer-playbook doctrine). Request: {request}. Output a copy-paste-ready, "
        "text-length draft with exactly one decision-shaped ask and no em-dashes, "
        "then a one-line [Why it works], and at most one [Alternate]."
    )
    return await _persona_channel_turn(incoming, instruction)


async def handle_spar(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Rebuttal sparring: objection in, ranked in-voice responses out."""
    objection = args.strip()
    if not objection:
        return 'Usage: `/spar <what the prospect said>` — e.g. `/spar she said the price is too high`'
    instruction = (
        f'[SPAR MODE] The prospect said: "{objection}". Give the BEST response in our '
        "client voice, then two ranked alternates. One line each on why it wins. Hold "
        "the premium frame — a response that wins the argument but reads needy or "
        "punitive loses. Every option ends with one decision-shaped ask."
    )
    return await _persona_channel_turn(incoming, instruction)


async def handle_checkin(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Follow-up/courtesy note with an escalation-ladder position check."""
    deal_state = args.strip()
    if not deal_state:
        return 'Usage: `/checkin <client + deal state>` — e.g. `/checkin rebecca, missed her deadline yesterday`'
    instruction = (
        f"[CHECK-IN MODE] Deal state: {deal_state}. First determine the escalation-ladder "
        "position: rung 1 = no courtesy note sent yet, rung 2 = courtesy-note deadline "
        "passed in silence, past rung 2 = already closed out. Then draft the correct next "
        "touch (rung 1 = courtesy note with a real deadline; rung 2 = the cold close-out; "
        "past rung 2 = say that no touch is correct and why). Name the rung you chose."
    )
    return await _persona_channel_turn(incoming, instruction)


async def handle_debrief(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Debrief a won/lost/stalled deal into lessons the persona keeps."""
    outcome = args.strip()
    if not outcome:
        return 'Usage: `/debrief <what happened>` — e.g. `/debrief rebecca paid the full package deposit after the courtesy note`'
    instruction = (
        f"[DEBRIEF MODE] Deal outcome: {outcome}. Extract the lessons: what worked "
        "(1-2 specific bullets), what to do differently (1-2 specific bullets), and "
        "whether the voice doctrine needs an update (propose the exact change, or say "
        "the doctrine held). Keep it tight — this is your sharpening steel."
    )
    return await _persona_channel_turn(incoming, instruction)


CORE_HANDLERS: dict[str, Any] = {
    "draft": handle_draft,
    "spar": handle_spar,
    "checkin": handle_checkin,
    "debrief": handle_debrief,
    "help": handle_help,
    "commands": handle_commands,
    "status": handle_status,
    "diagnostics": handle_diagnostics,
    "cost": handle_cost,
    "clear": handle_clear,
    "new": handle_clear,
    "plan": handle_plan,
    "go": handle_go,
    "execute": handle_go,
    "mode": handle_mode,
    "voice": handle_voice,
    "reload": handle_reload,
    "provider": handle_provider,
    "model": handle_model,
    "restart": handle_restart,
    "autostart": handle_autostart,
    "update": handle_update,
    "gsc": handle_gsc,
    "email": handle_email,
    "personal-email": handle_personal_email,
    "pemail": handle_personal_email,
    "accounts": handle_accounts,
    "browser": handle_browser,
    "browserops": handle_browserops,
    "ghost": handle_ghost,
    "linkedin_profile": handle_linkedin_profile,
    "linkedin": handle_linkedin,
    "primo": handle_primo,
    "linkedin_post": handle_linkedin_post,
    "linkedin_connect": handle_linkedin_connect,
    "reddit": handle_reddit,
    "x": handle_x,
    "video": handle_video,
    "inbox": handle_inbox,
    "cleanup": handle_cleanup,
    "analytics": handle_analytics,
    "signal": handle_signal,
    "stars": handle_stars,
    "budget": handle_budget,
    # Cabinet (Phase 5b) — keys are slashless, matching all other entries
    # (Phase 5 R1 B3 + Codex M2 fix).
    "cabinet": handle_cabinet,
    "standup": handle_standup,
    "discuss": handle_discuss,
    "teamtick": handle_teamtick,
    "teamroom": handle_teamroom,
    "team": handle_team,
    "send": handle_send,
    "brief": handle_brief,
    "working": handle_working,
    "vault": handle_vault,
    # Skill-from-experience loop (WS4) — operator-gated promotion surface.
    # Router-dispatched via the manager (no router.py edit needed); key is
    # slashless to match every other entry.
    "skills": handle_skills,
    # Source-driven skill authoring (Hermes /learn port) — writes an inert
    # draft that graduates through the /skills gate above.
    "learn": handle_learn,
    "watch": handle_watch,
    "extensions": handle_extensions,
    # Native design — Open Design power, no daemon (brief -> artifact -> critique).
    "design": handle_design,
    # Social post queue (Issue #77)
    "social": handle_social,
    # Co-founder projects (US-015) — file-mediated steering; slashless key.
    "cofounder": handle_cofounder,
    # Operator Automation UX (Phase 2) — zero-LLM recap + propose-don't-auto-create.
    "recap": handle_recap,
    "blueprints": handle_blueprints,
    "suggestions": handle_suggestions,
}

try:
    from local_extension_loader import apply_local_extension_hook

    apply_local_extension_hook("register_core_handlers", CORE_HANDLERS)
except ImportError:
    pass
