"""Core command handlers — extracted from router.py's elif chain.

Each handler has the signature:
    async def handle_X(adapter, incoming, args, *, collect_only=False) -> str

Handlers are stateless functions. Access to router-level state (engine, session
store, adapters) is via the _ctx module-level dict, set by the router at startup.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from session_keys import build_session_key, resolve_thread_id

from runtime import routing as runtime_routing
from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE
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

    report = collect_diagnostics()
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
    lines.append("*Sessions*:")
    lines.append(f"  Active: {report.sessions_active}")
    lines.append(f"  Messages: {report.sessions_total_messages}")
    lines.append(f"  Cost: ${report.sessions_total_cost_usd:.4f}")
    lines.append("")
    lines.append("*Adapters*:")
    for name, connected in report.adapters_connected.items():
        lines.append(f"  {name}: {'connected' if connected else 'off'}")
    return "\n".join(lines)


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

        result = clear_session_with_lifecycle(
            store=store,
            session=existing,
            platform=platform_str,
            channel_id=channel_id,
            thread_id=thread_id,
            engine=_ctx.get("engine"),
            source="clear",
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
    return _get_provider_status()


async def handle_model(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Switch runtime provider."""
    return _switch_provider(args.strip().lower() if args else "")


async def handle_restart(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Self-restart the bot."""
    if collect_only:
        return "Cannot chain /restart — use it alone."

    from models import OutgoingMessage

    reply = "Restarting myself... back in a few seconds."
    await adapter.send(
        OutgoingMessage(
            text=reply,
            channel=incoming.channel,
            thread=incoming.thread,
        )
    )
    chat_dir = Path(__file__).resolve().parent
    run_script = chat_dir / "run_chat.sh"
    subprocess.Popen(
        ["bash", str(run_script)],
        cwd=str(chat_dir),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(1)
    print(f"[{datetime.now()}] Self-restart initiated — exiting (PID {os.getpid()})")
    os._exit(0)


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
    """Fetch personal Gmail (owner6392lastname@gmail.com) — read-only."""
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
        return (
            f"Discussion #{ref.id} started — topic: {args}\n"
            f"Watch: http://localhost:3141/cabinet?id={ref.id}"
        )
    except kill_switches.KillSwitchDisabled:
        return "Cabinet is disabled by operator. Reach out for an override."
    except cabinet_api.CabinetAPIError as e:
        return e.friendly_message


async def handle_send(adapter: Any, incoming: Any, args: str, *, collect_only: bool = False) -> str:
    """Send an email draft from the drafts folder."""
    try:
        import shutil

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

            ok = send_email(to=to_email, subject=subject, body=body)
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

    if not (data.open_threads or data.active_hypotheses or data.unresolved_questions):
        lines.append("\n(all sections empty — nothing tracked yet)")

    arch_count = len(data.archived)
    if arch_count:
        lines.append(f"\n_Archive: {arch_count} cold item(s)_")

    return "\n".join(lines)


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

_PROVIDER_ALIASES = {
    "claude": "claude", "anthropic": "claude",
    "codex": "codex", "chatgpt": "codex", "gpt": "codex",
    "gemini": "gemini", "google": "gemini",
    "openrouter": "openrouter",
    "openai": "openai",
    "auto": "auto",
}

_CLAUDE_MODEL_OVERRIDES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
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
        if selection.lane == RUNTIME_LANE_CLAUDE_NATIVE:
            lines.append(
                "  claude model: "
                + os.getenv("SECOND_BRAIN_CLAUDE_MODEL", "claude-sonnet-4-6").strip()
            )
        else:
            preferred = (
                provider_display_name(selection.generic_provider)
                if selection.generic_provider
                else "auto"
            )
            lines.append(f"  generic preferred provider: {preferred}")
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

    content = env_path.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
    else:
        content += f"\n{key}={value}\n"
    env_path.write_text(content, encoding="utf-8")


def _delete_env_var(env_path: Path, key: str) -> None:
    """Delete a key=value pair from the .env file if present."""
    import re

    content = env_path.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(key)}=.*(?:\r?\n)?"
    updated = re.sub(pattern, "", content, flags=re.MULTILINE)
    env_path.write_text(updated.rstrip() + ("\n" if updated.strip() else ""), encoding="utf-8")


def _switch_provider(choice: str) -> str:
    """Switch the runtime lane/provider or Claude model by updating .env."""
    if not choice:
        selection = resolve_runtime_selection()
        current_model = os.getenv("SECOND_BRAIN_CLAUDE_MODEL", "claude-sonnet-4-6").strip()
        model_info = ""
        if selection.lane == RUNTIME_LANE_CLAUDE_NATIVE:
            model_info = f" | Claude model: {current_model}"
        elif selection.generic_provider:
            model_info = (
                " | Preferred generic provider: "
                f"{provider_display_name(selection.generic_provider)}"
            )
        return (
            f"Current selection: {describe_runtime_selection(selection)}{model_info}\n\n"
            "Usage: /model <lane|provider|model>\n"
            "  /model claude - Claude native lane\n"
            "  /model sonnet - Claude Sonnet 4.6\n"
            "  /model opus - Claude Opus 4.6\n"
            "  /model codex - generic runtime lane via Codex\n"
            "  /model gemini - generic runtime lane via Gemini\n"
            "  /model openrouter - generic runtime lane via OpenRouter\n"
            "  /model openai - generic runtime lane via OpenAI-compatible\n"
            "  /model auto - automatic lane/provider routing"
        )

    if choice in _CLAUDE_MODEL_OVERRIDES:
        model_name = _CLAUDE_MODEL_OVERRIDES[choice]
        try:
            from config import ENV_FILE as env_path
            from config import reload_config

            apply_runtime_selection_choice(
                "claude",
                environ=os.environ,
                write_key=lambda key, value: _write_env_var(env_path, key, value),
                delete_key=lambda key: _delete_env_var(env_path, key),
            )
            _write_env_var(env_path, "SECOND_BRAIN_CLAUDE_MODEL", model_name)
            os.environ["SECOND_BRAIN_CLAUDE_MODEL"] = model_name
            reload_config()
            return f"Switched to {model_name}. Next message uses the Claude native lane."
        except Exception as e:
            return f"Failed to switch model: {e}"

    normalized = _PROVIDER_ALIASES.get(choice, choice)
    if normalized not in ("claude", "codex", "gemini", "openrouter", "openai", "auto"):
        return (
            "Unknown runtime selection: "
            f"{choice}. Use: claude, sonnet, opus, codex, gemini, openrouter, openai, or auto"
        )

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
    except Exception as e:
        return f"Failed to switch provider: {e}"


# ---------------------------------------------------------------------------
# Handler lookup — maps command name to handler function
# ---------------------------------------------------------------------------

CORE_HANDLERS: dict[str, Any] = {
    "help": handle_help,
    "status": handle_status,
    "diagnostics": handle_diagnostics,
    "cost": handle_cost,
    "clear": handle_clear,
    "new": handle_clear,
    "plan": handle_plan,
    "go": handle_go,
    "execute": handle_go,
    "mode": handle_mode,
    "reload": handle_reload,
    "provider": handle_provider,
    "model": handle_model,
    "restart": handle_restart,
    "gsc": handle_gsc,
    "email": handle_email,
    "personal-email": handle_personal_email,
    "pemail": handle_personal_email,
    "accounts": handle_accounts,
    "inbox": handle_inbox,
    "cleanup": handle_cleanup,
    "analytics": handle_analytics,
    "budget": handle_budget,
    # Cabinet (Phase 5b) — keys are slashless, matching all other entries
    # (Phase 5 R1 B3 + Codex M2 fix).
    "cabinet": handle_cabinet,
    "standup": handle_standup,
    "discuss": handle_discuss,
    "send": handle_send,
    "brief": handle_brief,
    "working": handle_working,
    "extensions": handle_extensions,
}
