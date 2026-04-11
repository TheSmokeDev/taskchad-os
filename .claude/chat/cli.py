"""The Homie CLI — agent framework command-line interface.

Usage:
    thehomie chat -q "hello"              # Single query
    thehomie chat -q "hello" -Q           # Quiet/JSON mode (Paperclip)
    thehomie chat                         # Interactive REPL
    thehomie chat --resume <sessionId>    # Resume session
    thehomie chat -c                      # Resume most recent session
    thehomie status                       # System health
    thehomie doctor                       # Deep diagnostics
    thehomie setup --check                # Verify environment
"""

import os
import sys
from pathlib import Path

# CRITICAL: Set up sys.path before any framework imports.
# This mirrors main.py's path setup (main.py:7-12).
_CHAT_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _CHAT_DIR.parent / "scripts"
for p in [str(_CHAT_DIR), str(_SCRIPTS_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

import asyncio  # noqa: E402
import json as json_mod  # noqa: E402
from datetime import datetime  # noqa: E402

import click  # noqa: E402
from engine import ConversationEngine  # noqa: E402
from models import Platform  # noqa: E402, F401
from router import ChatRouter  # noqa: E402
from session import get_session_store  # noqa: E402

from config import (  # noqa: E402
    CHAT_DB_PATH,
    CHAT_MAX_BUDGET_USD,
    CHAT_MAX_TURNS,
    EXTENSIONS_ALLOW,
    EXTENSIONS_BUNDLED_PATH,
    EXTENSIONS_DENY,
    EXTENSIONS_ENABLED,
    PROJECT_ROOT,
    ensure_directories,
)
from runtime.selection import (  # noqa: E402
    apply_runtime_selection_choice,
    provider_display_name,
    resolve_runtime_selection,
    runtime_selection_choice,
)


@click.group(invoke_without_command=True)
@click.version_option(version="1.0.0", prog_name="thehomie")
@click.pass_context
def main(ctx):
    """The Homie — personal AI agent framework."""
    ctx.ensure_object(dict)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("-q", "--query", default=None, help="Single query (non-interactive)")
@click.option("-Q", "--quiet", is_flag=True, help="Quiet/JSON output (for Paperclip)")
@click.option("-m", "--model", default=None, help="Select runtime lane/provider (claude/codex/gemini/openrouter/openai/auto)")
@click.option("-t", "--toolsets", default=None, help="Filter tool access (reserved for future)")
@click.option("--resume", "-r", "resume_id", default=None, help="Resume session by ID")
@click.option("--continue", "-c", "continue_last", is_flag=True, help="Resume most recent session")
def chat(query, quiet, model, toolsets, resume_id, continue_last):
    """Chat with The Homie. Interactive REPL or single query (-q)."""
    ensure_directories()

    import os

    from adapters.cli_adapter import CLIAdapter

    # -m: Apply an in-process runtime selection override for this CLI session.
    if model:
        apply_runtime_selection_choice(model.lower().strip(), environ=os.environ)

    # -t: Toolset filtering is NOT yet wired into the engine
    if toolsets and not quiet:
        click.echo("Warning: --toolsets is reserved for future use, currently ignored")

    # Quiet mode: redirect stdout to stderr so framework logs don't pollute
    # the JSON output. Only the final JSON payload goes to real stdout.
    real_stdout = sys.stdout
    if quiet:
        sys.stdout = sys.stderr

    adapter = CLIAdapter(
        query=query,
        quiet=quiet,
        model=model,
        toolsets=toolsets,
        resume_session=resume_id,
        continue_last=continue_last,
    )

    store = get_session_store(CHAT_DB_PATH)
    engine = ConversationEngine(store, PROJECT_ROOT, CHAT_MAX_TURNS, CHAT_MAX_BUDGET_USD)

    try:
        from runtime.langfuse_setup import init_langfuse

        init_langfuse()
    except Exception:
        pass

    from commands import CATEGORIES, COMMANDS, CORE_INTENTS
    from core_handlers import CORE_HANDLERS, set_context
    from extension_manager import ExtensionManager, set_manager

    manager = ExtensionManager()
    manager.register_core_commands(COMMANDS, CATEGORIES, CORE_HANDLERS)
    manager.register_core_intents(CORE_INTENTS)

    if EXTENSIONS_ENABLED:
        allow = [x.strip() for x in EXTENSIONS_ALLOW.split(",") if x.strip()] if EXTENSIONS_ALLOW else None
        deny = [x.strip() for x in EXTENSIONS_DENY.split(",") if x.strip()] if EXTENSIONS_DENY else None
        manager.configure_allow_deny(allow=allow, deny=deny)

        ext_paths: list[Path] = []
        bundled = Path(EXTENSIONS_BUNDLED_PATH)
        ext_paths.append(bundled)
        global_ext = Path.home() / ".claude" / "extensions"
        if global_ext.exists() and global_ext not in ext_paths:
            ext_paths.append(global_ext)
        manager.discover(ext_paths)

    set_manager(manager)

    router = ChatRouter(engine, manager)
    set_context(
        engine=engine,
        adapters=router.adapters,
        bot_start_time=datetime.now(),
    )
    router.register(adapter)

    async def _run():
        await adapter.connect()
        try:
            async for incoming in adapter.listen():
                await router._handle(adapter, incoming)
        finally:
            await adapter.disconnect()

        session_info = adapter.get_session_info()
        footer = adapter.format_final_output(
            session_info.get("session_id"),
            session_info,
        )
        # Restore real stdout for the JSON payload
        if quiet:
            sys.stdout = real_stdout
        print(footer, flush=True)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if quiet:
            sys.stdout = real_stdout
            print(json_mod.dumps({"success": False, "error": str(exc)}), flush=True)
            sys.exit(1)
        else:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)


@main.command()
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def status(json_mode):
    """Show system health: providers, sessions, adapters."""
    ensure_directories()
    from diagnostics import collect_diagnostics

    report = collect_diagnostics()
    if json_mode:
        import dataclasses

        print(json_mod.dumps(dataclasses.asdict(report), indent=2))
    else:
        _print_status_human(report)


@main.command()
@click.option("--check", is_flag=True, help="Verify environment only (no interactive setup)")
@click.option("--advanced", is_flag=True, help="Full control (prompt every option)")
@click.option("--headless-google", is_flag=True, help="Manual URL copy-paste for Google OAuth")
def setup(check, advanced, headless_google):
    """Onboarding wizard — configure runtime, adapters, and vault.

    Quick mode (default): detect available providers/adapters, accept defaults.
    Advanced mode (--advanced): prompt for every option.
    --check: non-interactive verification only.
    """
    ensure_directories()

    if check:
        from diagnostics import check_environment

        issues = check_environment()
        _print_issues(issues)
        sys.exit(1 if any(i[0] == "error" for i in issues) else 0)

    # Interactive wizard — implemented in Task 15
    _run_setup_wizard(advanced, headless_google)


@main.command()
def doctor():
    """Diagnose issues — like `hermes doctor` / `openclaw doctor`.

    Checks: Python version, deps, .env, API keys, adapters, runtime, vault, memory DB.
    Each issue includes a fix command. Exit 0 = healthy, Exit 1 = errors.
    """
    from diagnostics import check_environment, collect_diagnostics

    click.echo("The Homie — Doctor")
    click.echo("=" * 40)

    issues = check_environment()
    _print_issues(issues)

    report = collect_diagnostics()
    click.echo(
        f"\nRuntime providers: "
        f"{len([v for v in report.runtime_providers.values() if v == 'ON'])} active"
    )
    click.echo(f"Memory DB: {report.memory_doc_count} documents ({report.memory_embedding_status})")
    click.echo(f"Cognition: {'active' if report.cognition_available else 'unavailable'}")
    click.echo(f"Sessions: {report.sessions_active} active")

    # Check for real failures: env errors OR zero runtime providers
    errors = [i for i in issues if i[0] == "error"]
    active_providers = [v for v in report.runtime_providers.values() if v == "ON"]
    has_diagnostics_failure = (
        not active_providers and report.runtime_providers  # providers checked but none ON
    )

    if errors or has_diagnostics_failure:
        problems = len(errors) + (1 if has_diagnostics_failure else 0)
        if has_diagnostics_failure and not errors:
            click.echo("\nNo runtime providers available — check API keys or CLI installs.")
        click.echo(f"\n{problems} issue(s) found. Fix them and re-run `thehomie doctor`.")
        sys.exit(1)
    else:
        click.echo("\nAll checks passed.")


def _get_orchestration_services():
    """Instantiate orchestration DB + services. No HTTP, direct Python calls."""
    from orchestration.db import OrchestrationDB
    from orchestration.convoy_service import ConvoyService
    from orchestration.mailbox_service import MailboxService
    from orchestration.observability import init_orchestration_observability

    from config import ORCHESTRATION_DB_PATH

    ensure_directories()
    init_orchestration_observability()
    db = OrchestrationDB(ORCHESTRATION_DB_PATH)
    return db, ConvoyService(db), MailboxService(db)


def _get_team_services():
    """Instantiate DB + TeamService + MailboxService for team CLI commands."""
    from orchestration.db import OrchestrationDB
    from orchestration.mailbox_service import MailboxService
    from orchestration.observability import init_orchestration_observability
    from orchestration.team_service import TeamService

    from config import ORCHESTRATION_DB_PATH

    ensure_directories()
    init_orchestration_observability()
    db = OrchestrationDB(ORCHESTRATION_DB_PATH)
    return db, TeamService(db), MailboxService(db)


def _fmt_relative_time(ts: int | None) -> str:
    """Format an epoch timestamp as a short 'N units ago' string."""
    if ts is None or ts == 0:
        return "-"
    import time as _t
    delta = int(_t.time()) - int(ts)
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# ── Convoy commands ────────────────────────────────────────────────────────


@main.group()
def convoy():
    """Manage convoys — task DAGs with dependency tracking."""
    pass


@convoy.command("create")
@click.option("--title", "-t", required=True, help="Convoy title")
@click.option("--description", "-d", default=None, help="Description")
@click.option("--branch", "-b", default="main", help="Base branch")
@click.option("--by", default="operator", help="Created by")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def convoy_create(title, description, branch, by, json_mode):
    """Create a new convoy."""
    from orchestration.models import CreateConvoyInput

    _, cs, _ = _get_orchestration_services()
    inp = CreateConvoyInput(title=title, description=description, created_by=by, base_branch=branch)
    result = cs.create_convoy(inp)
    if json_mode:
        import dataclasses
        print(json_mod.dumps(dataclasses.asdict(result.convoy), indent=2))
    else:
        click.echo(f"Created convoy #{result.convoy.id}: {result.convoy.title}")
        click.echo(f"  Status: {result.convoy.status}")
        click.echo(f"  Branch: {result.convoy.base_branch}")


@convoy.command("list")
@click.option("--status", "-s", default=None, help="Filter by status")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def convoy_list(status, json_mode):
    """List convoys."""
    _, cs, _ = _get_orchestration_services()
    convoys = cs.list_convoys(status=status)
    if json_mode:
        import dataclasses
        print(json_mod.dumps([dataclasses.asdict(c) for c in convoys], indent=2))
    else:
        if not convoys:
            click.echo("No convoys found.")
            return
        for c in convoys:
            click.echo(
                f"  #{c.id}  [{c.status}]  {c.title}"
                f"  ({c.completed_subtasks}/{c.total_subtasks} done)"
            )


@convoy.command("show")
@click.argument("convoy_id", type=int)
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def convoy_show(convoy_id, json_mode):
    """Show convoy details with subtasks."""
    import dataclasses

    _, cs, _ = _get_orchestration_services()
    result = cs.get_convoy(convoy_id)
    if not result:
        click.echo(f"Convoy #{convoy_id} not found.", err=True)
        sys.exit(1)
    if json_mode:
        print(json_mod.dumps(dataclasses.asdict(result), indent=2))
    else:
        c = result.convoy
        click.echo(f"Convoy #{c.id}: {c.title}")
        click.echo(f"  Status: {c.status}")
        click.echo(f"  Created by: {c.created_by}")
        click.echo(f"  Branch: {c.base_branch}")
        click.echo(f"  Progress: {c.completed_subtasks}/{c.total_subtasks} done, {c.failed_subtasks} failed")
        if result.subtasks:
            click.echo("\n  Subtasks:")
            for s in result.subtasks:
                agent = f" ({s.assigned_agent_name})" if s.assigned_agent_name else ""
                click.echo(f"    [{s.id}] {s.title} - {s.status}{agent}")
        if result.edges:
            click.echo(f"\n  Dependencies: {len(result.edges)} edges")


@convoy.command("dispatch")
@click.argument("subtask_id", type=int)
def convoy_dispatch(subtask_id):
    """Dispatch a ready subtask for execution."""
    _, cs, _ = _get_orchestration_services()
    try:
        cs.dispatch_subtask(subtask_id)
        click.echo(f"Dispatched subtask #{subtask_id}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@convoy.command("complete")
@click.argument("subtask_id", type=int)
def convoy_complete(subtask_id):
    """Mark a subtask as completed."""
    _, cs, _ = _get_orchestration_services()
    try:
        newly_ready, convoy_done = cs.handle_subtask_completion(subtask_id)
        click.echo(f"Completed subtask #{subtask_id}")
        if newly_ready:
            click.echo(f"  Newly ready: {', '.join(f'#{s.id} {s.title}' for s in newly_ready)}")
        if convoy_done:
            click.echo("  Convoy completed!")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@convoy.command("fail")
@click.argument("subtask_id", type=int)
@click.option("--error", "-e", default=None, help="Error message")
def convoy_fail(subtask_id, error):
    """Mark a subtask as failed."""
    _, cs, _ = _get_orchestration_services()
    try:
        convoy_failed = cs.handle_subtask_failure(subtask_id, error_message=error)
        click.echo(f"Failed subtask #{subtask_id}")
        if convoy_failed:
            click.echo("  Convoy failed!")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@convoy.command("cancel")
@click.argument("convoy_id", type=int)
def convoy_cancel(convoy_id):
    """Cancel a convoy and all non-terminal subtasks."""
    _, cs, _ = _get_orchestration_services()
    try:
        cs.update_convoy_status(convoy_id, "cancelled")
        click.echo(f"Cancelled convoy #{convoy_id}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@convoy.command("add-task")
@click.argument("convoy_id", type=int)
@click.option("--title", "-t", required=True, help="Subtask title")
@click.option("--description", "-d", default=None, help="Description")
@click.option("--depends-on", default=None, help="Comma-separated subtask IDs this depends on")
@click.option("--agent", default=None, help="Assigned agent name")
def convoy_add_task(convoy_id, title, description, depends_on, agent):
    """Add a subtask to an existing convoy."""
    from orchestration.models import AddSubtaskInput

    _, cs, _ = _get_orchestration_services()
    deps = [int(x.strip()) for x in depends_on.split(",")] if depends_on else []
    inp = AddSubtaskInput(
        title=title, description=description,
        depends_on_subtask_ids=deps, assigned_agent_name=agent,
    )
    try:
        added = cs.add_subtasks(convoy_id, [inp])
        click.echo(f"Added subtask #{added[0].id}: {added[0].title} (status: {added[0].status})")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── Mailbox commands ───────────────────────────────────────────────────────


@main.group()
def mailbox():
    """Inter-agent mailbox — send, read, claim messages."""
    pass


@mailbox.command("send")
@click.option("--from", "from_agent", required=True, help="Sender agent ID")
@click.option("--to", "recipients", required=True, help="Comma-separated recipient agent IDs")
@click.option("--body", "-b", required=True, help="Message body")
@click.option("--subject", "-s", default=None, help="Subject line")
@click.option("--type", "msg_type", default="message", help="Message type")
@click.option("--convoy", "convoy_id", type=int, default=None, help="Associated convoy ID")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def mailbox_send(from_agent, recipients, body, subject, msg_type, convoy_id, json_mode):
    """Send a message to one or more agents."""
    from orchestration.models import SendMessageInput

    _, _, ms = _get_orchestration_services()
    rcpt_list = [r.strip() for r in recipients.split(",")]
    inp = SendMessageInput(
        from_agent=from_agent, recipients=rcpt_list, body=body,
        subject=subject, message_type=msg_type, convoy_id=convoy_id,
    )
    msg = ms.send_message(inp)
    if json_mode:
        import dataclasses
        print(json_mod.dumps(dataclasses.asdict(msg), indent=2))
    else:
        click.echo(f"Sent message #{msg.id} from {from_agent} to {', '.join(rcpt_list)}")


@mailbox.command("inbox")
@click.argument("agent_id")
@click.option("--convoy", "convoy_id", type=int, default=None, help="Filter by convoy")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def mailbox_inbox(agent_id, convoy_id, json_mode):
    """Show pending messages for an agent."""
    _, _, ms = _get_orchestration_services()
    messages = ms.get_inbox(agent_id, convoy_id=convoy_id)
    if json_mode:
        import dataclasses
        print(json_mod.dumps([dataclasses.asdict(m) for m in messages], indent=2))
    else:
        if not messages:
            click.echo(f"No pending messages for {agent_id}.")
            return
        for mwd in messages:
            m = mwd.message
            click.echo(f"  #{m.id}  [{m.message_type}]  from {m.from_agent}")
            if m.subject:
                click.echo(f"    Subject: {m.subject}")
            click.echo(f"    {m.body[:80]}{'...' if len(m.body) > 80 else ''}")
            for d in mwd.deliveries:
                if d.recipient_agent == agent_id:
                    click.echo(f"    Delivery #{d.id} status: {d.status}")


@mailbox.command("claim")
@click.argument("agent_id")
@click.option("--convoy", "convoy_id", type=int, default=None, help="Filter by convoy")
@click.option("--limit", "-n", type=int, default=10, help="Max messages to claim")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def mailbox_claim(agent_id, convoy_id, limit, json_mode):
    """Claim pending messages for processing."""
    _, _, ms = _get_orchestration_services()
    claimed = ms.claim_deliveries(agent_id, convoy_id=convoy_id, limit=limit)
    if json_mode:
        import dataclasses
        print(json_mod.dumps([dataclasses.asdict(m) for m in claimed], indent=2))
    else:
        if not claimed:
            click.echo(f"No pending messages to claim for {agent_id}.")
            return
        click.echo(f"Claimed {len(claimed)} message(s):")
        for mwd in claimed:
            m = mwd.message
            click.echo(f"  #{m.id} [{m.message_type}] from {m.from_agent}: {m.body[:60]}")
            for d in mwd.deliveries:
                if d.recipient_agent == agent_id and d.status == "claimed":
                    click.echo(
                        f"    Delivery #{d.id} claim_token={d.claim_token}"
                    )


@mailbox.command("ack")
@click.argument("delivery_id", type=int)
@click.option("--agent", "agent_id", required=True, help="Recipient agent ID")
@click.option("--claim-token", required=True, help="Claim token returned by mailbox claim")
def mailbox_ack(delivery_id, agent_id, claim_token):
    """Acknowledge a claimed delivery."""
    _, _, ms = _get_orchestration_services()
    try:
        ms.ack_delivery(delivery_id, agent_id, claim_token)
        click.echo(f"Acknowledged delivery #{delivery_id}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── Team commands ──────────────────────────────────────────────────────────


@main.group()
def team():
    """Team session operations — list, status, members, shutdown."""
    pass


@team.command("list")
@click.option("--status", "-s", default=None, help="Filter by status (active/idle/shutdown_requested/closed)")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def team_list(status, json_mode):
    """List team sessions."""
    import dataclasses
    from orchestration.observability import orchestration_span, update_observation

    with orchestration_span(
        "team_cli.list",
        metadata={"status_filter": status, "surface": "cli"},
        trace_metadata={"surface": "cli", "feature_phase": 4},
    ):
        _, ts, _ = _get_team_services()
        teams = ts.list_team_sessions(status=status)
        update_observation(metadata={"team_count": len(teams)})
        if json_mode:
            print(json_mod.dumps([dataclasses.asdict(t) for t in teams], indent=2))
            return
        if not teams:
            click.echo("No team sessions found.")
            return
        click.echo(
            f"{'ID':>4}  {'Team':<20} {'Lead':<20} {'Status':<18} {'Last Active':<12} {'Convoy':<7}"
        )
        for t in teams:
            convoy_str = f"#{t.convoy_id}" if t.convoy_id else "-"
            click.echo(
                f"{t.id:>4}  {t.team_name[:20]:<20} {t.lead_agent_id[:20]:<20} "
                f"{t.status:<18} {_fmt_relative_time(t.last_activity_at):<12} {convoy_str:<7}"
            )


@team.command("status")
@click.argument("team_id", type=int)
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def team_status(team_id, json_mode):
    """Show full status of one team session."""
    import dataclasses
    from orchestration.observability import orchestration_span, update_observation

    with orchestration_span(
        "team_cli.status",
        metadata={"team_id": team_id, "surface": "cli"},
        trace_metadata={"surface": "cli", "feature_phase": 4, "team_id": team_id},
        expected_exceptions=(SystemExit,),
    ):
        _, ts, ms = _get_team_services()
        result = ts.get_team_session(team_id)
        if result is None:
            click.echo(f"Team session #{team_id} not found.", err=True)
            sys.exit(1)
        update_observation(
            metadata={
                "team_id": result.session.id,
                "team_name": result.session.team_name,
                "convoy_id": result.session.convoy_id,
                "backend_type": result.session.backend_type,
                "member_count": len(result.members),
            }
        )
        if json_mode:
            print(json_mod.dumps(dataclasses.asdict(result), indent=2))
            return

        s = result.session
        click.echo(f"Team: {s.team_name} (ID: {s.id})")
        click.echo(f"Status: {s.status}")
        lead_display = s.lead_agent_id + (
            f" ({s.lead_agent_name})" if s.lead_agent_name else ""
        )
        click.echo(f"Lead: {lead_display}")
        click.echo(f"Convoy: {('#' + str(s.convoy_id)) if s.convoy_id else '-'}")
        click.echo(f"Backend: {s.backend_type}")
        click.echo(f"Last activity: {_fmt_relative_time(s.last_activity_at)}")

        click.echo(f"\nMembers ({len(result.members)}):")
        for m in result.members:
            subtask_str = f"#{m.subtask_id}" if m.subtask_id else "-"
            click.echo(
                f"  {m.agent_id:<20} {m.role:<8} {m.status:<8} "
                f"last: {_fmt_relative_time(m.last_activity_at):<10} subtask: {subtask_str}"
            )

        convoy_id = result.session.convoy_id
        click.echo("\nMailbox backlog (agent -> unread):")
        backlog: dict[str, int] = {}
        for m in result.members:
            inbox = ms.get_inbox(m.agent_id, convoy_id=convoy_id)
            backlog[m.agent_id] = len(inbox)
            click.echo(f"  {m.agent_id}: {len(inbox)}")
        update_observation(metadata={"mailbox_backlog": backlog})


@team.command("members")
@click.argument("team_id", type=int)
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def team_members(team_id, json_mode):
    """Show member list for a team."""
    import dataclasses
    from orchestration.observability import orchestration_span, update_observation

    with orchestration_span(
        "team_cli.members",
        metadata={"team_id": team_id, "surface": "cli"},
        trace_metadata={"surface": "cli", "feature_phase": 4, "team_id": team_id},
        expected_exceptions=(SystemExit,),
    ):
        _, ts, _ = _get_team_services()
        result = ts.get_team_session(team_id)
        if result is None:
            click.echo(f"Team session #{team_id} not found.", err=True)
            sys.exit(1)
        update_observation(metadata={"team_id": team_id, "member_count": len(result.members)})
        if json_mode:
            print(json_mod.dumps([dataclasses.asdict(m) for m in result.members], indent=2))
            return
        if not result.members:
            click.echo("No members.")
            return
        for m in result.members:
            subtask_str = f"#{m.subtask_id}" if m.subtask_id else "-"
            click.echo(
                f"  {m.agent_id:<20} {m.role:<8} {m.status:<8} subtask: {subtask_str}"
            )


@team.command("shutdown")
@click.argument("team_id", type=int)
@click.option("--force", is_flag=True, default=False, help="Close immediately (skip graceful)")
def team_shutdown(team_id, force):
    """Request graceful team shutdown, or force-close with --force."""
    from orchestration.models import ShutdownRequestPayload
    from orchestration.observability import orchestration_span, update_observation

    with orchestration_span(
        "team_cli.shutdown",
        metadata={"team_id": team_id, "force": force, "surface": "cli"},
        trace_metadata={"surface": "cli", "feature_phase": 4, "team_id": team_id},
        expected_exceptions=(SystemExit,),
    ):
        _, ts, ms = _get_team_services()
        existing = ts.get_team_session(team_id)
        if existing is None:
            click.echo(f"Team session #{team_id} not found.", err=True)
            sys.exit(1)

        if force:
            if not click.confirm(
                f"Force-close team #{team_id} ({existing.session.team_name})?",
                default=False,
            ):
                click.echo("Aborted.")
                update_observation(metadata={"shutdown_aborted": True})
                return
            try:
                result = ts.close_team_session(team_id)
            except ValueError as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)
            update_observation(metadata={"team_id": team_id, "final_status": result.status})
            click.echo(f"Team #{team_id} closed (status: {result.status}).")
            return

        try:
            result = ts.request_shutdown(team_id)
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        sent = 0
        lead = existing.session.lead_agent_id
        for m in existing.members:
            if m.role == "worker" and m.status == "active":
                ms.send_shutdown_request(
                    lead, m.agent_id, ShutdownRequestPayload(),
                    convoy_id=existing.session.convoy_id,
                )
                sent += 1
        update_observation(
            metadata={
                "team_id": team_id,
                "convoy_id": existing.session.convoy_id,
                "msg_type": "shutdown_request",
                "shutdown_messages_sent": sent,
                "final_status": result.status,
            }
        )
        click.echo(
            f"Team #{team_id} shutdown requested (status: {result.status}). "
            f"Sent {sent} shutdown_request message(s)."
        )


@team.command("ping")
@click.argument("team_id", type=int)
@click.option("--agent", "agent_id", default=None, help="Specific member to ping")
def team_ping(team_id, agent_id):
    """Update last_activity_at for the team (or a specific member)."""
    from orchestration.observability import orchestration_span, update_observation

    with orchestration_span(
        "team_cli.ping",
        metadata={"team_id": team_id, "agent_id": agent_id, "surface": "cli"},
        trace_metadata={"surface": "cli", "feature_phase": 4, "team_id": team_id},
        expected_exceptions=(SystemExit,),
    ):
        _, ts, _ = _get_team_services()
        try:
            ts.ping_activity(team_id, agent_id=agent_id)
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        update_observation(metadata={"team_id": team_id, "agent_id": agent_id})
        target = f" agent={agent_id}" if agent_id else ""
        click.echo(f"Pinged team #{team_id}{target}.")


@team.command("close")
@click.argument("team_id", type=int)
def team_close(team_id):
    """Soft-close a team session (idempotent)."""
    from orchestration.observability import orchestration_span, update_observation

    with orchestration_span(
        "team_cli.close",
        metadata={"team_id": team_id, "surface": "cli"},
        trace_metadata={"surface": "cli", "feature_phase": 4, "team_id": team_id},
        expected_exceptions=(SystemExit,),
    ):
        _, ts, _ = _get_team_services()
        try:
            result = ts.close_team_session(team_id)
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        update_observation(metadata={"team_id": team_id, "final_status": result.status})
        click.echo(f"Team #{team_id} closed (status: {result.status}).")


def _print_issues(issues):
    if not issues:
        click.echo("  All checks passed.")
        return
    for level, msg, hint in issues:
        icon = "X" if level == "error" else "!" if level == "warn" else "i"
        click.echo(f"  [{icon}] {msg}")
        if hint:
            click.echo(f"      Fix: {hint}")


def _print_status_human(report):
    """Format DiagnosticsReport for terminal output."""
    click.echo("The Homie — System Status")
    click.echo("=" * 40)
    click.echo(f"Uptime: {report.uptime_seconds:.0f}s")
    click.echo(f"Cognition: {'active' if report.cognition_available else 'unavailable'}")

    if report.cognition_moves:
        for move, active in report.cognition_moves.items():
            click.echo(f"  {move}: {'ON' if active else 'OFF'}")

    click.echo("\nRuntime lanes:")
    for name, status in report.runtime_lanes.items():
        click.echo(f"  {name}: {status}")

    click.echo(f"\nSelected lane: {report.runtime_selected_lane}")
    preferred_generic = (
        provider_display_name(report.runtime_selected_generic_provider)
        if report.runtime_selected_generic_provider
        else "auto"
    )
    click.echo(f"Generic preferred provider: {preferred_generic}")
    if report.runtime_generic_text_route:
        click.echo(
            "Generic text route: "
            + " -> ".join(
                provider_display_name(provider)
                for provider in report.runtime_generic_text_route
            )
        )
    if report.runtime_generic_tool_route:
        click.echo(
            "Generic tool route: "
            + " -> ".join(
                provider_display_name(provider)
                for provider in report.runtime_generic_tool_route
            )
        )

    click.echo("\nRuntime providers:")
    for name, status in report.runtime_providers.items():
        click.echo(f"  {name}: {status}")

    click.echo(f"\nMemory: {report.memory_doc_count} docs ({report.memory_embedding_status})")
    click.echo(f"Sessions: {report.sessions_active} active")
    click.echo(f"Total cost: ${report.sessions_total_cost_usd:.4f}")

    if report.adapters_connected:
        click.echo("\nAdapters:")
        for name, connected in report.adapters_connected.items():
            click.echo(f"  {name}: {'connected' if connected else 'disconnected'}")


def _run_setup_wizard(advanced: bool, headless_google: bool):
    """Interactive onboarding wizard — Hermes/OpenClaw style.

    Detect → ask → configure → verify → next steps.
    """
    from dotenv import load_dotenv

    from config import GOOGLE_CREDENTIALS_FILE, MEMORY_DIR, MEMORY_FILE, SOUL_FILE, USER_FILE

    env_path = _SCRIPTS_DIR / ".env"

    # Ensure .env exists
    if not env_path.exists():
        try:
            from setup_wizard import create_env_from_template

            create_env_from_template()
        except ImportError:
            env_path.write_text("# The Homie configuration\n")

    load_dotenv(env_path, override=True)
    env_values = _read_env_map()

    click.echo("The Homie — Setup Wizard\n")

    # Step 1: Runtime Selection
    click.echo("Step 1/4: Runtime Selection\n")
    providers_found = _detect_providers(env_values)
    current_selection = resolve_runtime_selection(env_values)

    if any(providers_found.values()):
        for name, available in providers_found.items():
            icon = "OK" if available else "--"
            click.echo(f"  [{icon}] {name}")

        default_primary = runtime_selection_choice(current_selection)
        if default_primary == "auto":
            default_primary = next((k for k, v in providers_found.items() if v), "auto")
        available_choices = ["auto", *[k for k, v in providers_found.items() if v]]
        if advanced:
            primary = click.prompt(
                "  Runtime selection",
                type=click.Choice(available_choices),
                default=default_primary,
            )
            apply_runtime_selection_choice(
                primary,
                environ=os.environ,
                write_key=_write_env_key,
                delete_key=_delete_env_key,
            )
        else:
            if current_selection.is_auto:
                apply_runtime_selection_choice(
                    default_primary,
                    environ=os.environ,
                    write_key=_write_env_key,
                    delete_key=_delete_env_key,
                )
            click.echo(f"  Using: {default_primary} (auto-detected)")
    else:
        click.echo("  No runtime provider detected.\n")
        if click.confirm("  Add OpenRouter API key?", default=True):
            key = click.prompt("  OPENROUTER_API_KEY", hide_input=True)
            _write_env_key("OPENROUTER_API_KEY", key)
            click.echo("  [OK] OpenRouter configured")
        elif click.confirm("  Add OpenAI API key?", default=False):
            key = click.prompt("  OPENAI_API_KEY", hide_input=True)
            _write_env_key("OPENAI_API_KEY", key)
            click.echo("  [OK] OpenAI configured")

        load_dotenv(env_path, override=True)
        env_values = _read_env_map()

    # Step 2: Chat Channels
    click.echo("\nStep 2/4: Chat Channels\n")
    channels = {
        "Telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS"),
        "Slack": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        "Discord": ("DISCORD_BOT_TOKEN",),
        "WhatsApp": ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"),
    }

    for channel_name, env_keys in channels.items():
        current = {key: env_values.get(key, "") for key in env_keys}
        existing = any(current.values())

        if existing and not advanced:
            click.echo(f"  [OK] {channel_name}: already configured")
            continue

        if existing and advanced:
            action = click.prompt(
                f"  {channel_name} is already configured",
                type=click.Choice(["keep", "edit", "disable"]),
                default="keep",
            )
        elif advanced or click.confirm(
            f"  Enable {channel_name}?", default=(channel_name == "Telegram")
        ):
            action = "edit"
        else:
            action = "skip"

        if action == "keep":
            click.echo(f"  [OK] {channel_name}: keeping existing configuration")
            continue
        if action == "disable":
            for key in env_keys:
                _write_env_key(key, "")
            click.echo(f"  [--] {channel_name}: disabled")
            load_dotenv(env_path, override=True)
            env_values = _read_env_map()
            continue
        if action == "skip":
            click.echo(f"  [--] {channel_name}: skipped")
            continue

        # action == "edit"
        for key in env_keys:
            default_value = current.get(key, "")
            val = click.prompt(
                f"    {key}",
                default=default_value if default_value else "",
                show_default=bool(default_value),
                hide_input=("TOKEN" in key or "KEY" in key),
            )
            if val:
                _write_env_key(key, val)

        load_dotenv(env_path, override=True)
        env_values = _read_env_map()
        click.echo(f"  [OK] {channel_name}: configured")

    # Step 3: Memory Vault
    click.echo("\nStep 3/4: Memory Vault\n")
    if MEMORY_DIR.exists():
        click.echo(f"  [OK] Vault found at {MEMORY_DIR}")
        for f, name in [(SOUL_FILE, "SOUL.md"), (USER_FILE, "USER.md"), (MEMORY_FILE, "MEMORY.md")]:
            if f.exists():
                click.echo(f"  [OK] {name}")
            else:
                click.echo(f"  [--] {name} missing — creating stub")
                f.write_text(f"# {name.replace('.md', '')}\n\n_Edit this file to configure._\n")
    else:
        click.echo(f"  Vault directory not found at {MEMORY_DIR}")
        if click.confirm("  Create it with stub files?", default=True):
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            for f, name in [
                (SOUL_FILE, "SOUL"),
                (USER_FILE, "USER"),
                (MEMORY_FILE, "MEMORY"),
            ]:
                f.write_text(f"# {name}\n\n_Edit this file to configure._\n")
            click.echo(f"  [OK] Created vault at {MEMORY_DIR}")

    # Step 4: Verification
    click.echo("\nStep 4/4: Verification\n")
    load_dotenv(env_path, override=True)
    env_values = _read_env_map()

    try:
        from setup_wizard import check_prerequisites, validate_tokens

        prereqs = check_prerequisites()
        if prereqs:
            for issue in prereqs:
                click.echo(f"  [!!] {issue}")
        else:
            click.echo("  [OK] Prerequisites")

        token_status = validate_tokens()
        for plat, plat_status in token_status.items():
            icon = "OK" if ("OK" in plat_status or plat_status == "configured") else "--"
            click.echo(f"  [{icon}] {plat}: {plat_status}")
    except ImportError:
        click.echo("  [!!] setup_wizard.py not found — skipping token validation")
        prereqs = []
        token_status = {}

    # Optional deeper auth validation
    auth_checks: dict[str, bool] = {}
    if env_values.get("SLACK_BOT_TOKEN", ""):
        try:
            from importlib import reload

            import setup_auth as _setup_auth

            reload(_setup_auth)
            auth_checks["Slack API"] = _setup_auth.check_slack(check_only=True)
        except (ImportError, Exception):
            pass

    if GOOGLE_CREDENTIALS_FILE.exists() or advanced:
        if click.confirm(
            "  Run Google auth status check?", default=GOOGLE_CREDENTIALS_FILE.exists()
        ):
            try:
                from importlib import reload

                import setup_auth as _setup_auth

                reload(_setup_auth)
                auth_checks["Google OAuth"] = _setup_auth.check_google(
                    check_only=True,
                    headless=headless_google,
                )
            except (ImportError, Exception):
                pass

    for name, ok in auth_checks.items():
        click.echo(f"  [{'OK' if ok else '!!'}] {name}")

    providers = _detect_providers(env_values)
    active = [k for k, v in providers.items() if v]
    click.echo(f"  [OK] Runtime: {', '.join(active) if active else 'NONE - add a provider'}")

    click.echo(f"\n{'=' * 50}")
    if prereqs or not active:
        click.echo("Setup incomplete. Run `thehomie doctor` to see what's missing.")
    else:
        click.echo("All set! Start chatting:\n")
        click.echo("  thehomie chat              # Interactive REPL")
        click.echo("  thehomie chat -q 'hello'   # Quick test")
        click.echo("  thehomie doctor            # Deep diagnostics")


def _read_env_map() -> dict[str, str]:
    """Read effective .env values."""
    from dotenv import dotenv_values

    env_path = _SCRIPTS_DIR / ".env"
    if not env_path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(env_path).items()}


def _detect_providers(env_values: dict[str, str]) -> dict[str, bool]:
    """Detect available runtime providers."""
    import shutil

    return {
        "claude": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
        "gemini": shutil.which("gemini") is not None,
        "openrouter": bool(env_values.get("OPENROUTER_API_KEY", "")),
        "openai": bool(env_values.get("OPENAI_API_KEY", "")),
    }


def _write_env_key(key: str, value: str) -> None:
    """Upsert a key=value pair in .env file, deduplicating existing entries."""
    env_path = _SCRIPTS_DIR / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    new_lines = []
    wrote = False
    for line in lines:
        stripped = line.strip()
        candidate = stripped.lstrip("#").strip()
        if candidate.startswith(f"{key}="):
            if not wrote:
                new_lines.append(f"{key}={value}")
                wrote = True
            continue
        new_lines.append(line)
    if not wrote:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines).rstrip() + "\n")


def _delete_env_key(key: str) -> None:
    """Remove a key=value pair from .env if it exists."""
    env_path = _SCRIPTS_DIR / ".env"
    if not env_path.exists():
        return
    lines = env_path.read_text().splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        candidate = stripped.lstrip("#").strip()
        if candidate.startswith(f"{key}="):
            continue
        new_lines.append(line)
    if new_lines:
        env_path.write_text("\n".join(new_lines).rstrip() + "\n")
    else:
        env_path.write_text("")


if __name__ == "__main__":
    main()
