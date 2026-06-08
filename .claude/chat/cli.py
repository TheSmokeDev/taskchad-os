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

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

import asyncio  # noqa: E402
import json as json_mod  # noqa: E402
from datetime import datetime  # noqa: E402

import click  # noqa: E402
from engine import ConversationEngine  # noqa: E402
from models import Platform  # noqa: E402, F401
from router import ChatRouter  # noqa: E402
from session import SOURCE_VALUES, get_session_store  # noqa: E402
from cli_session import session as session_group  # noqa: E402

from config import (  # noqa: E402
    CHAT_DB_PATH,
    CHAT_MAX_BUDGET_USD,
    CHAT_MAX_TURNS,
    ENV_FILE,
    EXTENSIONS_ALLOW,
    EXTENSIONS_BUNDLED_PATH,
    EXTENSIONS_DENY,
    EXTENSIONS_ENABLED,
    PROJECT_ROOT,
    ensure_directories,
)
from runtime.model_control import (  # noqa: E402
    apply_runtime_model_choice,
    resolve_runtime_model_choice,
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


main.add_command(session_group)


@main.command()
@click.option("-q", "--query", default=None, help="Single query (non-interactive)")
@click.option("-Q", "--quiet", is_flag=True, help="Quiet/JSON output (for Paperclip)")
@click.option("-m", "--model", default=None, help="Select runtime lane/provider/model (claude/codex/gemini/openrouter/openai/auto, provider:model, or gpt5.5)")
@click.option("-t", "--toolsets", default=None, help="Filter tool access (reserved for future)")
@click.option("--resume", "-r", "resume_id", default=None, help="Resume session by ID")
@click.option("--continue", "-c", "continue_last", is_flag=True, help="Resume most recent session")
@click.option(
    "--voice",
    "voice_path",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    default=None,
    help="Audio file to transcribe via voice cascade and feed as the user message (Phase 4 single-shot voice ingress).",
)
@click.option(
    "--voice-out",
    "voice_out_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Synthesize the agent reply to the given audio path via voice cascade (Phase 4 single-shot voice egress).",
)
@click.option(
    "--source",
    type=click.Choice(SOURCE_VALUES, case_sensitive=True),
    default="interactive",
    show_default=True,
    help=(
        "Session source tag (one of: interactive, tool, cron, hook). "
        "'tool' and 'hook' are hidden from `thehomie session list` by default. "
        "Note: --source on a --resume'd session is ignored (source is set once at create). "
        "Values are case-sensitive (lowercase only)."
    ),
)
def chat(query, quiet, model, toolsets, resume_id, continue_last, voice_path, voice_out_path, source):
    """Chat with The Homie. Interactive REPL or single query (-q)."""
    ensure_directories()

    import os

    from adapters.cli_adapter import CLIAdapter

    # -m: Apply an in-process runtime selection override for this CLI session.
    if model:
        model_arg = model.strip()
        if resolve_runtime_model_choice(model_arg):
            apply_runtime_model_choice(model_arg, environ=os.environ)
        else:
            apply_runtime_selection_choice(model_arg.lower(), environ=os.environ)

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
        voice_path=voice_path,
        voice_out_path=voice_out_path,
        source=source,
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
            # PRP-7d Task 15 / R1 B6 — emit the 12-field locked-order error
            # envelope so consumers (Paperclip, Mission Control) see the same
            # schema on the pre-adapter exception path as on the success path.
            # Pass ``source=source`` so a Paperclip-style
            # ``thehomie chat --source tool -q "x" -Q`` that fails during
            # engine/config setup keeps the operator's --source echo (R1 B6
            # post-build fix F1) instead of being silently downgraded to
            # ``"interactive"``.
            from adapters.cli_adapter import build_quiet_error_envelope

            sys.stdout = real_stdout
            print(build_quiet_error_envelope(exc, source=source), flush=True)
            sys.exit(1)
        else:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)


def _collect_profile_lifecycle_contract() -> dict:
    """F5 — return the per-profile lifecycle contract for the ``status`` command.

    Operators running ``thehomie -p sales status`` need to see WHICH paths /
    mutex / ports the active profile is using. Without this, profile
    isolation is invisible from the operator surface (the PRP names this
    command explicitly as the place to expose the contract).

    Returns a dict with stable keys for both human and JSON output:

      * ``active_profile``             - resolved profile name
      * ``bot_pid_path``               - get_bot_pid_path()
      * ``bot_lock_path``              - get_bot_lock_path()
      * ``bot_mutex_name``             - get_bot_mutex_name() (or ``None`` on POSIX)
      * ``orchestration_api_port``     - get_orchestration_api_port()
      * ``health_check_port``          - get_health_check_port()
      * ``whatsapp_webhook_port``      - get_whatsapp_webhook_port()

    Each call goes through the same Phase 3 service helpers that
    chat/main.py uses at startup, so the values printed here are
    guaranteed to match the values the bot will actually use.

    Errors are caught per-field and replaced with a stringified exception
    so a single broken helper does not blank the entire status output.
    """
    from personas import activity as _activity
    from personas import services as _services

    contract: dict = {}

    def _safe(fn):
        try:
            return fn()
        except Exception as exc:  # pragma: no cover — exercised by F5 test
            return f"<error: {exc}>"

    contract["active_profile"] = _safe(_activity.get_active_profile_name)
    contract["bot_pid_path"] = str(_safe(_services.get_bot_pid_path))
    contract["bot_lock_path"] = str(_safe(_services.get_bot_lock_path))
    contract["bot_mutex_name"] = (
        _safe(_services.get_bot_mutex_name) if sys.platform == "win32" else None
    )
    contract["orchestration_api_port"] = _safe(_services.get_orchestration_api_port)
    contract["health_check_port"] = _safe(_services.get_health_check_port)
    contract["whatsapp_webhook_port"] = _safe(_services.get_whatsapp_webhook_port)
    return contract


@main.command()
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def status(json_mode):
    """Show system health: providers, sessions, adapters."""
    ensure_directories()
    from diagnostics import collect_diagnostics

    if json_mode:
        # Keep stdout machine-clean even when diagnostics imports initialize
        # optional observability providers that may log connection failures.
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            report = collect_diagnostics()
            lifecycle = _collect_profile_lifecycle_contract()
        finally:
            sys.stdout = real_stdout

        import dataclasses

        payload = dataclasses.asdict(report)
        # Merge the lifecycle contract under a stable key. Keeping it as a
        # nested dict preserves backwards compatibility for any consumer
        # that already parses the diagnostics fields.
        payload["profile_lifecycle"] = lifecycle
        print(json_mod.dumps(payload, indent=2))
    else:
        report = collect_diagnostics()
        # F5 — expose the per-profile lifecycle contract (pid path, lock path,
        # mutex name, ports). Operators running ``thehomie -p sales status``
        # need to see which paths/ports the active profile is using.
        lifecycle = _collect_profile_lifecycle_contract()
        _print_status_human(report)
        _print_profile_lifecycle_contract(lifecycle)


def _print_profile_lifecycle_contract(contract: dict) -> None:
    """Human-readable rendering of the F5 lifecycle contract block."""
    click.echo("\nProfile lifecycle:")
    click.echo(f"  Active profile:           {contract.get('active_profile')}")
    click.echo(f"  Bot PID path:             {contract.get('bot_pid_path')}")
    click.echo(f"  Bot lock path:            {contract.get('bot_lock_path')}")
    mutex = contract.get("bot_mutex_name")
    if mutex is None:
        click.echo("  Bot mutex:                <hidden — Windows only>")
    else:
        click.echo(f"  Bot mutex:                {mutex}")
    click.echo(f"  Orchestration API port:   {contract.get('orchestration_api_port')}")
    click.echo(f"  Health check port:        {contract.get('health_check_port')}")
    click.echo(f"  WhatsApp webhook port:    {contract.get('whatsapp_webhook_port')}")


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
@click.option("--api-port", default=4322, show_default=True, type=int, help="Python orchestration API port.")
@click.option("--dashboard-port", default=3141, show_default=True, type=int, help="Hono dashboard port.")
@click.option("--web-port", default=5173, show_default=True, type=int, help="Vite web port.")
@click.option("--no-open", "no_open", is_flag=True, help="Do not open the Operating Room in a browser.")
@click.option("--no-vite", "no_vite", is_flag=True, help="Use Hono/static only instead of Vite dev.")
@click.option("--shell", "shell_mode", is_flag=True, help="Launch the Electron Desktop v0 shell.")
@click.option("--dry-run", is_flag=True, help="Print planned local stack commands without starting processes.")
@click.option("--json", "json_mode", is_flag=True, help="JSON output for --dry-run.")
def desktop(api_port, dashboard_port, web_port, no_open, no_vite, shell_mode, dry_run, json_mode):
    """Launch the local desktop/operator stack."""
    from desktop_launcher import (
        DesktopLaunchConfig,
        describe_desktop_launch,
        describe_desktop_shell_launch,
        launch_desktop,
        launch_desktop_shell,
    )

    config = DesktopLaunchConfig(
        api_port=api_port,
        dashboard_port=dashboard_port,
        vite_port=web_port,
        open_browser=not no_open,
        use_vite=not no_vite,
    )
    if dry_run:
        payload = (
            describe_desktop_shell_launch(config)
            if shell_mode
            else describe_desktop_launch(config)
        )
        if json_mode:
            click.echo(json_mod.dumps(payload, indent=2))
            return
        click.echo("The Homie desktop stack:")
        click.echo(f"  Operating Room: {payload['target_url']}")
        for command in payload["commands"]:
            click.echo(f"  {command['name']}:")
            click.echo(f"    cwd: {command['cwd']}")
            click.echo(f"    cmd: {' '.join(command['argv'])}")
        return

    sys.exit(launch_desktop_shell(config) if shell_mode else launch_desktop(config))


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
    if report.runtime_auth_issues:
        click.echo("Runtime auth attention:")
        for provider, issue in report.runtime_auth_issues.items():
            click.echo(f"  {provider}: {issue}")
    click.echo(f"Memory DB: {report.memory_doc_count} documents ({report.memory_embedding_status})")
    click.echo(f"Cognition: {'active' if report.cognition_available else 'unavailable'}")
    _print_cognitive_loop(report.cognitive_loop)
    _print_browser_readiness(report.browser)
    click.echo(f"Sessions: {report.sessions_active} active")
    if report.clear_lifecycle_recent_failures:
        click.echo(
            "Clear lifecycle warnings/errors (recent): "
            f"{report.clear_lifecycle_recent_failures}"
        )
        if report.clear_lifecycle_last_failure:
            click.echo(f"Last clear lifecycle warning: {report.clear_lifecycle_last_failure}")

    # Check for real failures: env errors OR zero runtime providers
    errors = [i for i in issues if i[0] == "error"]
    active_providers = [v for v in report.runtime_providers.values() if v == "ON"]
    auth_issue_count = len(report.runtime_auth_issues)
    has_diagnostics_failure = (
        not active_providers and report.runtime_providers  # providers checked but none ON
    )

    if errors or has_diagnostics_failure or auth_issue_count:
        problems = len(errors) + auth_issue_count + (1 if has_diagnostics_failure else 0)
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


@team.command("tick")
@click.argument("team_id", type=int)
@click.option("--agent", "agent_id", default=None, help="Prefer a specific active member")
@click.option("--runtime", "use_runtime", is_flag=True, default=False, help="Use runtime lane reply")
@click.option("--runtime-lane", default=None, help="Optional runtime lane/provider")
@click.option("--complete-running", is_flag=True, default=False, help="Allow completing running subtasks")
@click.option("--execute-running", is_flag=True, default=False, help="Run an approved executor command for a running subtask")
@click.option("--executor-command", default="git_status", help="Approved executor command preset")
@click.option("--executor-cwd", default=None, help="Optional executor working directory")
@click.option("--complete-on-executor-success", is_flag=True, default=False, help="Complete subtask after successful executor command")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def team_tick(
    team_id,
    agent_id,
    use_runtime,
    runtime_lane,
    complete_running,
    execute_running,
    executor_command,
    executor_cwd,
    complete_on_executor_success,
    json_mode,
):
    """Run one autonomous team scheduler tick."""
    from orchestration.observability import orchestration_span, update_observation
    from orchestration.team_loop import TeamTickService, tick_result_to_dict

    with orchestration_span(
        "team_cli.tick",
        metadata={
            "team_id": team_id,
            "agent_id": agent_id,
            "use_runtime": use_runtime,
            "runtime_lane": runtime_lane,
            "complete_running": complete_running,
            "execute_running": execute_running,
            "executor_command": executor_command,
            "complete_on_executor_success": complete_on_executor_success,
            "surface": "cli",
        },
        trace_metadata={"surface": "cli", "feature_phase": 9, "team_id": team_id},
        expected_exceptions=(SystemExit,),
    ):
        db, _ts, _ms = _get_team_services()
        try:
            result = TeamTickService(db).run_team_tick(
                team_id,
                agent_id=agent_id,
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                complete_running=complete_running,
                execute_running=execute_running,
                executor_command=executor_command,
                executor_cwd=executor_cwd,
                complete_on_executor_success=complete_on_executor_success,
            )
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        finally:
            db.close()

        payload = tick_result_to_dict(result)
        update_observation(
            metadata={
                "team_id": team_id,
                "selected_action": result.selected_action,
                "agent_id": result.agent_id,
                "waited": result.waited,
                "has_error": bool(result.error),
            }
        )
        if json_mode:
            print(json_mod.dumps(payload, indent=2))
            return

        click.echo(f"Team #{team_id} tick: {result.selected_action}")
        if result.agent_id:
            click.echo(f"  Agent: {result.agent_id}")
        if result.subtask_id:
            click.echo(f"  Subtask: #{result.subtask_id}")
        click.echo(f"  Reason: {result.reason}")
        if result.error:
            click.echo(f"  Error: {result.error}")
        elif result.waited:
            click.echo("  Result: waited")
        elif result.step:
            after = result.step.subtask_after.status if result.step.subtask_after else "unknown"
            click.echo(
                f"  Step: {result.step.action}; claimed {len(result.step.claimed)}; status {after}"
            )
            if result.step.runtime:
                click.echo(
                    "  Runtime: "
                    f"{result.step.runtime.runtime_lane} / {result.step.runtime.provider}"
                )
        elif result.executor:
            click.echo(
                "  Executor: "
                f"{result.executor.command_key}; exit {result.executor.exit_code}; "
                f"success={result.executor.success}"
            )
            click.echo(f"  Cwd: {result.executor.cwd}")


@team.group("room")
def team_room():
    """Run bounded team-room workflows."""
    pass


def _clip_team_room_cli_text(text: str, *, max_chars: int = 1800) -> str:
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 14].rstrip() + "\n...[truncated]"


@team_room.command("run")
@click.option("--workflow", "workflow_id", default="growth_boardroom", help="Workflow ID")
@click.option("--goal", default=None, help="Goal for the team room")
@click.option("--context", default=None, help="Optional short context")
@click.option("--runtime", "use_runtime", is_flag=True, default=False, help="Use runtime lane replies")
@click.option("--lane", "runtime_lane", default=None, help="Optional runtime lane/provider")
@click.option("--runtime-lane", "runtime_lane_alias", default=None, help="Optional runtime lane/provider")
@click.option("--max-rounds", default=None, type=int, help="Cross-talk rounds; >1 enables facilitated V2")
@click.option("--meeting-mode", default=None, help="classic_boardroom or facilitated_boardroom")
@click.option("--v2", "use_v2", is_flag=True, default=False, help="Run the facilitated V2 meeting")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
@click.argument("goal_words", nargs=-1)
def team_room_run(
    workflow_id,
    goal,
    context,
    use_runtime,
    runtime_lane,
    runtime_lane_alias,
    max_rounds,
    meeting_mode,
    use_v2,
    json_mode,
    goal_words,
):
    """Run a Homie-native team room workflow."""
    from orchestration.observability import orchestration_span, update_observation
    from orchestration.team_room import (
        TeamRoomWorkflowService,
        team_room_workflow_result_to_dict,
    )

    if runtime_lane_alias:
        runtime_lane = runtime_lane_alias
    if runtime_lane:
        use_runtime = True
    goal_text = (goal or " ".join(goal_words)).strip()
    if not goal_text:
        click.echo("Error: --goal or goal text is required.", err=True)
        sys.exit(1)

    with orchestration_span(
        "team_cli.room_run",
        metadata={
            "workflow_id": workflow_id,
            "meeting_mode": meeting_mode,
            "v2": use_v2,
            "use_runtime": use_runtime,
            "runtime_lane": runtime_lane,
            "max_rounds": max_rounds,
            "surface": "cli",
        },
        trace_metadata={"surface": "cli", "feature_phase": 12},
        expected_exceptions=(SystemExit,),
    ):
        db, _ts, _ms = _get_team_services()
        try:
            result = TeamRoomWorkflowService(db).run_team_room(
                goal=goal_text,
                workflow_id=workflow_id,
                context=context,
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                max_rounds=max_rounds,
                meeting_mode=(
                    "facilitated_boardroom"
                    if use_v2 and not meeting_mode
                    else meeting_mode
                ),
            )
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        finally:
            db.close()

        payload = team_room_workflow_result_to_dict(result)
        update_observation(
            metadata={
                "team_id": payload["team_id"],
                "convoy_id": payload["convoy_id"],
                "workflow_id": payload["workflow_id"],
                "progress": (
                    f"{payload['progress']['completed']}/"
                    f"{payload['progress']['total']}"
                ),
            },
            output={"final_brief_chars": len(payload["final_brief"])},
        )
        if json_mode:
            print(json_mod.dumps(payload, indent=2))
            return

        runtime_summary = payload["runtime"]
        click.echo("Team Room Workflow")
        click.echo(f"  Workflow: {payload['workflow_id']}")
        click.echo(f"  Mode: {payload['meeting_mode']}")
        click.echo(f"  Rounds: {payload['max_rounds']}")
        click.echo(f"  Goal: {payload['goal']}")
        click.echo(f"  Team: #{payload['team_id']}")
        click.echo(f"  Convoy: #{payload['convoy_id']}")
        click.echo(
            "  Progress: "
            f"{payload['progress']['completed']}/{payload['progress']['total']} subtasks"
        )
        click.echo(f"  Turns: {payload['turn_summary']}")
        synthesis = payload.get("synthesis") or {}
        click.echo(f"  Confidence: {float(synthesis.get('confidence') or 0.0):.2f}")
        click.echo(
            "  Votes / interrupts: "
            f"{len(payload.get('vote_board') or [])} / {len(payload.get('interrupts') or [])}"
        )
        click.echo(f"  Runtime turns: {'on' if use_runtime else 'off'}")
        if runtime_lane:
            click.echo(f"  Runtime lane: {runtime_lane}")
        if use_runtime:
            click.echo(
                "  Runtime metadata: "
                f"{runtime_summary['turn_count']} turns; "
                f"lanes {', '.join(runtime_summary['lanes']) or 'unknown'}; "
                f"providers {', '.join(runtime_summary['providers']) or 'unknown'}; "
                f"models {', '.join(runtime_summary['models']) or 'unknown'}; "
                f"tools {runtime_summary['tool_call_count']}"
            )
        click.echo("\nFinal Brief")
        click.echo(_clip_team_room_cli_text(payload["final_brief"]))


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
    click.echo(
        "Configured model: "
        f"{report.runtime_selected_model or 'auto (route-dependent)'}"
    )
    for warning in report.runtime_model_warnings:
        click.echo(f"Runtime model warning: {warning}")
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

    _print_browser_readiness(report.browser)

    click.echo(f"\nMemory: {report.memory_doc_count} docs ({report.memory_embedding_status})")
    _print_cognitive_loop(report.cognitive_loop)
    click.echo(f"Sessions: {report.sessions_active} active")
    click.echo(f"Total cost: ${report.sessions_total_cost_usd:.4f}")

    if report.adapters_connected:
        click.echo("\nAdapters:")
        for name, connected in report.adapters_connected.items():
            click.echo(f"  {name}: {'connected' if connected else 'disconnected'}")

    if report.capabilities:
        click.echo("\nCapabilities:")
        click.echo(f"  | {'id':<40} | {'display_name':<22} | {'enabled':<7} | {'source':<16} |")
        click.echo(f"  |{'-'*42}|{'-'*24}|{'-'*9}|{'-'*18}|")
        for cap in sorted(report.capabilities, key=lambda c: (c["source"], c["id"])):
            cap_id = cap["id"][:40]
            cap_name = cap["display_name"][:22]
            cap_enabled = "yes" if cap["enabled"] else "no"
            cap_source = cap["source"][:16]
            click.echo(f"  | {cap_id:<40} | {cap_name:<22} | {cap_enabled:<7} | {cap_source:<16} |")

    if report.toolsets:
        click.echo("\nToolsets:")
        click.echo(f"  | {'toolset':<20} | {'count':>5} | {'ids (first 3)...':<30} |")
        click.echo(f"  |{'-'*22}|{'-'*7}|{'-'*32}|")
        for name in sorted(report.toolsets):
            ids = report.toolsets[name]
            count = len(ids)
            preview = ", ".join(ids[:3])
            if count > 3:
                preview += ", ..."
            click.echo(f"  | {name:<20} | {count:>5} | {preview[:30]:<30} |")


def _print_browser_readiness(browser):
    """Render the URL-free browser readiness envelope."""
    if not browser:
        return
    try:
        from browser_control import format_browser_readiness

        click.echo("")
        click.echo(format_browser_readiness(browser))
    except Exception as exc:  # pragma: no cover - defensive render fallback
        click.echo("")
        click.echo("Browser: attention")
        click.echo(f"  Attention: {exc}")


def _print_cognitive_loop(cognitive_loop):
    """Render the cognitive-loop status compactly for human surfaces."""
    if not cognitive_loop:
        return

    overall = str(cognitive_loop.get("overall", "unknown")).upper()
    click.echo("\nCognitive Loop:")
    click.echo(f"  Overall: {overall}")

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
            click.echo(line)

    next_actions = cognitive_loop.get("next_actions", [])
    if next_actions:
        click.echo("  Next actions:")
        for action in next_actions:
            click.echo(f"    - {action}")


def _run_setup_wizard(advanced: bool, headless_google: bool):
    """Interactive onboarding wizard — Hermes/OpenClaw style.

    Detect → ask → configure → verify → next steps.
    """
    from dotenv import load_dotenv

    from config import GOOGLE_CREDENTIALS_FILE, MEMORY_DIR, MEMORY_FILE, SOUL_FILE, USER_FILE

    env_path = ENV_FILE

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

    if not ENV_FILE.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(ENV_FILE).items()}


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
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
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
    ENV_FILE.write_text("\n".join(new_lines).rstrip() + "\n")


def _delete_env_key(key: str) -> None:
    """Remove a key=value pair from .env if it exists."""
    if not ENV_FILE.exists():
        return
    lines = ENV_FILE.read_text().splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        candidate = stripped.lstrip("#").strip()
        if candidate.startswith(f"{key}="):
            continue
        new_lines.append(line)
    if new_lines:
        ENV_FILE.write_text("\n".join(new_lines).rstrip() + "\n")
    else:
        ENV_FILE.write_text("")


# === evolve ===

@main.group()
def evolve():
    """Replay recall queries under candidate configs to measure deltas."""


def _coerce_override_value(raw: str) -> object:
    """Best-effort coerce an override value. JSON first (handles numbers, bools,
    null, arrays, objects), then fall back to raw string for model names and
    other freeform values."""
    import json as _json

    raw = raw.strip()
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        return raw


def _parse_override(override_str: str) -> tuple[str, object]:
    """Parse KEY=VALUE override."""
    if "=" not in override_str:
        raise click.BadParameter(f"override must be KEY=VALUE, got: {override_str}")
    key, raw = override_str.split("=", 1)
    return key.strip(), _coerce_override_value(raw)


@evolve.command("run")
@click.option("--golden/--no-golden", default=True, help="Use built-in golden queries (default)")
@click.option("--override", "-o", "overrides", multiple=True, metavar="KEY=VALUE",
              help="Config override. Repeatable. Example: -o RECALL_MIN_SCORE=0.5")
@click.option("--out", type=click.Path(), default=None, help="Report output directory")
@click.option("--caller", default="replay", help="Caller tag for RecallLog")
@click.option("--max-results", type=int, default=5, help="max_results passed to recall")
@click.option(
    "--trace/--no-trace",
    default=None,
    help=(
        "Phase 2.4: emit Langfuse-tagged spans under user_id=evolve-replay. "
        "Default: EVOLVE_TRACE_REPLAYS env var (false)."
    ),
)
def evolve_run(golden, overrides, out, caller, max_results, trace):
    """Run a replay against the current vault. Writes a ReplayReport JSON."""
    from evolve import load_golden_queries, run_replay_sync, write_report

    # Resolve --trace from env when the flag is not explicitly set on the CLI.
    if trace is None:
        from config import EVOLVE_TRACE_REPLAYS
        trace = EVOLVE_TRACE_REPLAYS

    if not golden:
        click.echo("--no-golden: reading queries from stdin, one per line")
        queries = [line.strip() for line in click.get_text_stream("stdin") if line.strip()]
        if not queries:
            raise click.UsageError("No queries provided on stdin.")
    else:
        queries = load_golden_queries()

    override_dict: dict[str, object] = {}
    for o in overrides:
        k, v = _parse_override(o)
        override_dict[k] = v

    trace_label = "traced" if trace else "untraced"
    click.echo(
        f"Replaying {len(queries)} queries ({trace_label}) with "
        f"overrides={override_dict or '(none)'}"
    )
    report = run_replay_sync(
        queries,
        overrides=override_dict,
        caller=caller,
        max_results=max_results,
        disable_tracing=not trace,
    )

    out_path = write_report(report, out_dir=out)
    s = report.summary
    click.echo("")
    click.echo(f"Report: {out_path}")
    click.echo(f"  experiment_id:   {report.experiment_id}")
    click.echo(f"  hit_rate:        {s.hit_rate:.4f}  ({s.hit_count}/{s.query_count})")
    click.echo(f"  avg_top_score:   {s.avg_top_score:.4f}")
    click.echo(f"  p50/p90 latency: {s.p50_latency_ms:.1f} / {s.p90_latency_ms:.1f} ms")
    click.echo(f"  tier distribution: {dict(s.tier_distribution)}")
    if report.langfuse_trace_url:
        click.echo(f"  langfuse trace:  {report.langfuse_trace_url}")
    if s.error_count:
        click.echo(f"  errors:          {s.error_count}", err=True)


@evolve.command("compare")
@click.argument("baseline_report", type=click.Path(exists=True, dir_okay=False))
@click.argument("candidate_report", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--ci",
    "with_ci",
    is_flag=True,
    help=(
        "Phase 2.6: compute paired-bootstrap 95% CI bands on hit_rate_delta "
        "and avg_top_score_delta. Adds [95% CI: lo, hi] suffix to each "
        "metric line so reviewers can distinguish real movement from noise."
    ),
)
@click.option(
    "--ci-seed",
    type=int,
    default=None,
    help="Optional seed for deterministic bootstrap (testing only).",
)
def evolve_compare(baseline_report, candidate_report, with_ci, ci_seed):
    """Diff two ReplayReport JSONs and print the delta table."""
    import json as _json
    from pathlib import Path as _Path

    from evolve import (
        ReplayQueryResult,
        ReplayReport,
        ReplaySummary,
        compare_reports,
        format_delta_table,
    )

    def _load(path: str) -> ReplayReport:
        raw = _json.loads(_Path(path).read_text(encoding="utf-8"))
        summary = ReplaySummary(**raw.get("summary", {}))
        per_query = [ReplayQueryResult(**q) for q in raw.get("per_query", [])]
        return ReplayReport(
            experiment_id=raw["experiment_id"],
            timestamp_utc=raw.get("timestamp_utc", ""),
            overrides=raw.get("overrides", {}),
            config_snapshot=raw.get("config_snapshot", {}),
            per_query=per_query,
            summary=summary,
            memory_dir=raw.get("memory_dir", ""),
            caller=raw.get("caller", "replay"),
            langfuse_trace_url=raw.get("langfuse_trace_url"),
            langfuse_session_url=raw.get("langfuse_session_url"),
        )

    baseline = _load(baseline_report)
    candidate = _load(candidate_report)
    delta = compare_reports(baseline, candidate, with_ci=with_ci, ci_seed=ci_seed)
    click.echo(format_delta_table(delta))


@evolve.command("audit-goldens")
@click.option(
    "--out",
    type=click.Path(),
    default=None,
    help="Optional report output dir (replay JSON) — defaults to data/evolve/reports.",
)
@click.option(
    "--max-results",
    type=int,
    default=5,
    help="max_results passed to recall during the audit replay.",
)
@click.option("--json", "json_mode", is_flag=True, help="Emit JSON drift report instead of table.")
def evolve_audit_goldens(out, max_results, json_mode):
    """Audit the golden corpus for drift. Phase 2.6.

    Re-runs every golden query through recall and flags entries where the
    observed runtime tier no longer matches `tier_expected`, where a
    happy-path query returned zero results, or where a non-error-inducing
    query produced a runtime error. Emits stratification warnings as a
    side effect via the loader.

    Exit codes: 0 = no drift, 1 = drifts present.
    """
    import json as _json

    from evolve import (
        audit_goldens_drift,
        load_golden_queries_full,
        run_replay_sync,
        validate_stratification,
        write_report,
    )

    try:
        entries = load_golden_queries_full(validate=False)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error loading goldens: {exc}", err=True)
        sys.exit(3)

    # Run validation explicitly so we can capture the warning list for
    # JSON output (the loader's auto-validation only logs).
    stratification_warnings = validate_stratification(entries)

    queries = [e["query"] for e in entries]
    if not json_mode:
        click.echo(f"Auditing {len(queries)} golden queries...")
    report = run_replay_sync(
        queries,
        overrides={},
        caller="audit-goldens",
        max_results=max_results,
    )

    if out:
        out_path = write_report(report, out_dir=out)
        if not json_mode:
            click.echo(f"Audit replay saved: {out_path}")

    drifts = audit_goldens_drift(report.per_query, entries)

    if json_mode:
        print(
            json_mod.dumps(
                {
                    "audit_experiment_id": report.experiment_id,
                    "total_queries": len(queries),
                    "drift_count": len(drifts),
                    "stratification_warnings": stratification_warnings,
                    "drifts": drifts,
                },
                indent=2,
            )
        )
    else:
        if stratification_warnings:
            click.echo("")
            click.echo("Stratification warnings (PRD ±10% tolerance):")
            for w in stratification_warnings:
                click.echo(f"  - {w}")
        click.echo("")
        click.echo(f"Drifts: {len(drifts)} of {len(queries)}")
        for d in drifts:
            reasons = "; ".join(d["reasons"])
            click.echo(f"  X {d['query'][:60]:<60} {reasons}")
        if not drifts:
            click.echo("  (no drift detected)")

    sys.exit(0 if not drifts else 1)


# ── evolve helpers (shared by veto + propose) ──────────────────────────────


def _evolve_load_replay_report(path):
    """Reconstruct a ReplayReport from JSON. Mirrors the inline loader in evolve_compare."""
    import json as _json
    from pathlib import Path as _Path

    from evolve import ReplayQueryResult, ReplayReport, ReplaySummary

    raw = _json.loads(_Path(path).read_text(encoding="utf-8"))
    summary = ReplaySummary(**raw.get("summary", {}))
    per_query = [ReplayQueryResult(**q) for q in raw.get("per_query", [])]
    return ReplayReport(
        experiment_id=raw["experiment_id"],
        timestamp_utc=raw.get("timestamp_utc", ""),
        overrides=raw.get("overrides", {}),
        config_snapshot=raw.get("config_snapshot", {}),
        per_query=per_query,
        summary=summary,
        memory_dir=raw.get("memory_dir", ""),
        caller=raw.get("caller", "replay"),
        langfuse_trace_url=raw.get("langfuse_trace_url"),
        langfuse_session_url=raw.get("langfuse_session_url"),
    )


def _evolve_load_report_delta(path):
    """Thin wrapper around evolve.io.load_report_delta.

    The shared loader fail-fasts on missing required aggregates and
    recomputes verdict_counts + error_count_delta from per_query so a stale
    or hand-edited delta cache cannot fail open. See evolve/io.py.
    """
    from evolve import load_report_delta

    return load_report_delta(path)


def _evolve_compute_exit_code(verdict, force):
    """Thin CLI wrapper around evolve.veto.compute_exit_code (returns plain int)."""
    from evolve import compute_exit_code

    return int(compute_exit_code(verdict, force=force))


@evolve.command("veto")
@click.argument("delta_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--ruleset",
    type=click.Choice(["default", "strict", "permissive"]),
    default=None,
    help="Named preset (default/strict/permissive)",
)
@click.option(
    "--veto-rules",
    "veto_rules_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Custom ruleset JSON path",
)
@click.option("--force", is_flag=True, help="Override soft veto (still logs the failure)")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def evolve_veto(delta_path, ruleset, veto_rules_path, force, json_mode):
    """Evaluate veto rules against a saved ReportDelta JSON.

    Exit codes: 0 adopt, 1 hard veto, 2 soft veto, 3 input/harness error.
    """
    from evolve import (
        ExitCode,
        evaluate_veto,
        format_delta_table,
        format_verdict_table,
        load_ruleset,
    )

    try:
        delta = _evolve_load_report_delta(delta_path)
    except (KeyError, ValueError) as exc:
        click.echo(f"Error loading delta: {exc}", err=True)
        sys.exit(int(ExitCode.ERROR))

    try:
        rs = load_ruleset(veto_rules_path or ruleset)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error loading ruleset: {exc}", err=True)
        sys.exit(int(ExitCode.ERROR))

    verdict = evaluate_veto(delta, rs)

    if json_mode:
        print(
            json_mod.dumps(
                {
                    "ruleset": rs.name,
                    "verdict": verdict.to_dict(),
                    "delta": delta.to_dict(),
                    "force": force,
                },
                indent=2,
            )
        )
    else:
        click.echo(format_delta_table(delta))
        click.echo(format_verdict_table(verdict, ruleset_name=rs.name))
        if force and verdict.soft and not verdict.accepted:
            click.echo("\n[--force] Overriding soft veto for adoption.", err=True)

    sys.exit(_evolve_compute_exit_code(verdict, force))


@evolve.command("propose")
@click.option(
    "--overrides",
    "overrides_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help='JSON file with config override dict (e.g. {"RECALL_MIN_SCORE": 0.5})',
)
@click.option(
    "--baseline",
    "baseline_path",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
    help="Existing baseline ReplayReport JSON",
)
@click.option(
    "--candidate",
    "candidate_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Pre-built candidate ReplayReport (skips fresh replay; mainly for tests)",
)
@click.option(
    "--ruleset",
    type=click.Choice(["default", "strict", "permissive"]),
    default=None,
)
@click.option(
    "--veto-rules",
    "veto_rules_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
)
@click.option("--force", is_flag=True, help="Override soft veto (still logs the failure)")
@click.option("--out", type=click.Path(), default=None, help="Candidate report output dir")
@click.option("--max-results", type=int, default=5)
@click.option("--caller", default="propose", help="Caller tag for RecallLog")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
@click.option(
    "--trace/--no-trace",
    default=None,
    help=(
        "Phase 2.4: emit Langfuse-tagged spans for the candidate replay "
        "under user_id=evolve-replay; trace URL is recorded on the decision "
        "artifact. Default: EVOLVE_TRACE_REPLAYS env var (false). Ignored "
        "when --candidate is supplied (no fresh replay runs)."
    ),
)
@click.option(
    "--no-regression",
    is_flag=True,
    default=False,
    help=(
        "Phase 2.6.1 (Codex review 2026-04-26 finding 2): explicitly skip "
        "the regression corpus check. Without this flag, a missing or "
        "malformed regression_queries.json is a HARD ERROR (exit 3) — "
        "the regression hard-veto floor must be either enforced or "
        "explicitly opted-out. Use with care; --no-regression disables "
        "structural protection against re-introducing known-fixed bugs."
    ),
)
def evolve_propose(
    overrides_path,
    baseline_path,
    candidate_path,
    ruleset,
    veto_rules_path,
    force,
    out,
    max_results,
    caller,
    json_mode,
    trace,
    no_regression,
):
    """Run replay + compare + veto in one shot — the autonomous adoption path.

    Either supply --overrides (and optionally --out) to run a fresh replay,
    or --candidate to evaluate a pre-built report. Exit codes match `evolve veto`.
    """
    import json as _json
    from pathlib import Path as _Path

    from evolve import (
        ExitCode,
        compare_reports,
        evaluate_veto,
        format_delta_table,
        format_verdict_table,
        load_ruleset,
        run_replay_sync,
        write_decision_artifact,
        write_report,
    )

    # Codex review (2026-04-25) Finding 1: --candidate and --overrides are
    # alternative input modes; passing both is ambiguous and historically
    # silently used --candidate while ignoring --overrides — a footgun where
    # automation could adopt a config that was never replayed.
    if candidate_path and overrides_path:
        click.echo(
            "Error: --candidate and --overrides are mutually exclusive "
            "(pass one or the other; --candidate skips fresh replay).",
            err=True,
        )
        sys.exit(int(ExitCode.ERROR))

    # Resolve --trace from env when the flag is not explicitly set on the CLI.
    if trace is None:
        from config import EVOLVE_TRACE_REPLAYS
        trace = EVOLVE_TRACE_REPLAYS

    try:
        baseline = _evolve_load_replay_report(baseline_path)
    except (KeyError, ValueError) as exc:
        click.echo(f"Error loading baseline: {exc}", err=True)
        sys.exit(int(ExitCode.ERROR))

    overrides_used: dict | None = None
    if candidate_path:
        try:
            candidate = _evolve_load_replay_report(candidate_path)
        except (KeyError, ValueError) as exc:
            click.echo(f"Error loading candidate: {exc}", err=True)
            sys.exit(int(ExitCode.ERROR))
        overrides_used = candidate.overrides
    else:
        if overrides_path is None:
            click.echo("Error: --overrides or --candidate is required", err=True)
            sys.exit(int(ExitCode.ERROR))
        try:
            overrides_used = _json.loads(
                _Path(overrides_path).read_text(encoding="utf-8")
            )
        except _json.JSONDecodeError as exc:
            click.echo(f"Error parsing overrides JSON: {exc}", err=True)
            sys.exit(int(ExitCode.ERROR))
        queries = [r.query for r in baseline.per_query]
        if not json_mode:
            trace_label = "traced" if trace else "untraced"
            click.echo(
                f"Replaying {len(queries)} queries ({trace_label}) with "
                f"overrides={overrides_used or '(none)'}"
            )
        candidate = run_replay_sync(
            queries,
            overrides=overrides_used,
            caller=caller,
            max_results=max_results,
            baseline_experiment_id=baseline.experiment_id,
            disable_tracing=not trace,
        )
        if out:
            out_path = write_report(candidate, out_dir=out)
            if not json_mode:
                click.echo(f"Wrote candidate report: {out_path}")

    try:
        delta = compare_reports(baseline, candidate)
    except ValueError as exc:
        click.echo(f"Error comparing reports: {exc}", err=True)
        sys.exit(int(ExitCode.ERROR))

    try:
        rs = load_ruleset(veto_rules_path or ruleset)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error loading ruleset: {exc}", err=True)
        sys.exit(int(ExitCode.ERROR))

    # Phase 2.6: run regression corpus against candidate config and surface
    # any failed entries as hard vetoes via evaluate_veto. Regression queries
    # historically passed under the candidate's CONFIG (the bug fix held);
    # if any drops below min_top_score now, a known-fixed bug regressed.
    #
    # 2.6.1 hardening (Codex review 2026-04-26 finding 2): a missing /
    # malformed / empty regression corpus is a HARD ERROR. Previous
    # behavior caught FileNotFoundError + ValueError and continued with
    # regression_summary=None — silently disabled the hard-veto floor.
    # Opt-out via --no-regression is the only way to skip enforcement.
    regression_summary = None
    if not no_regression:
        try:
            from evolve import (
                evaluate_regression_corpus,
                load_regression_entries,
                load_regression_queries,
            )
            regression_raw = load_regression_queries()
            regression_set = load_regression_entries(regression_raw)
        except (FileNotFoundError, ValueError) as exc:
            err_msg = (
                f"Regression corpus load failed: {exc}. "
                f"Pass --no-regression to skip enforcement (not recommended), "
                f"or fix regression_queries.json."
            )
            if json_mode:
                print(
                    json_mod.dumps(
                        {
                            "error": "regression_corpus_load_failed",
                            "detail": str(exc),
                            "baseline_experiment_id": baseline.experiment_id,
                            "candidate_experiment_id": candidate.experiment_id,
                            "force": force,
                        },
                        indent=2,
                    )
                )
            else:
                click.echo(err_msg, err=True)
            sys.exit(int(ExitCode.ERROR))

        regression_overrides = candidate.overrides if candidate_path else (
            overrides_used or {}
        )
        regression_queries_strs = [e.query for e in regression_set]
        if not json_mode:
            click.echo(
                f"Running {len(regression_queries_strs)} regression queries "
                f"with candidate config..."
            )
        try:
            regression_report = run_replay_sync(
                regression_queries_strs,
                overrides=regression_overrides,
                caller="regression",
                max_results=max_results,
            )
            regression_summary = evaluate_regression_corpus(
                regression_report.per_query, regression_set
            )
        except (ValueError, RuntimeError) as exc:
            err_msg = (
                f"Regression replay/evaluation failed: {exc}. "
                f"Pass --no-regression to skip enforcement (not recommended)."
            )
            if json_mode:
                print(
                    json_mod.dumps(
                        {
                            "error": "regression_replay_failed",
                            "detail": str(exc),
                            "baseline_experiment_id": baseline.experiment_id,
                            "candidate_experiment_id": candidate.experiment_id,
                            "force": force,
                        },
                        indent=2,
                    )
                )
            else:
                click.echo(err_msg, err=True)
            sys.exit(int(ExitCode.ERROR))
    elif not json_mode:
        click.echo(
            "Note: --no-regression set; regression hard-veto floor disabled "
            "for this propose. Known-fixed bugs are NOT structurally "
            "protected this run.",
            err=True,
        )

    verdict = evaluate_veto(delta, rs, regression_summary=regression_summary)

    exit_code = _evolve_compute_exit_code(verdict, force)

    # Codex review (2026-04-25) Finding 5: when --out is set, persist the
    # decision artifact so the audit trail survives without stdout capture.
    # Especially important when --force flips a soft veto to ADOPT — the
    # override is recorded on disk, not just printed. Phase 2.4: also
    # records the candidate's Langfuse trace URL when the replay opted in,
    # so a reviewer can click straight to the per-query span tree.
    if out:
        decision_path = write_decision_artifact(
            out,
            baseline_experiment_id=baseline.experiment_id,
            candidate_experiment_id=candidate.experiment_id,
            ruleset_name=rs.name,
            delta=delta,
            verdict=verdict,
            force=force,
            exit_code=exit_code,
            overrides=overrides_used,
            langfuse_trace_url=candidate.langfuse_trace_url,
        )
        if not json_mode:
            click.echo(f"Wrote decision: {decision_path}")
            if candidate.langfuse_trace_url:
                click.echo(f"Langfuse trace: {candidate.langfuse_trace_url}")

    if json_mode:
        print(
            json_mod.dumps(
                {
                    "ruleset": rs.name,
                    "delta": delta.to_dict(),
                    "verdict": verdict.to_dict(),
                    "force": force,
                    "effective_exit_code": exit_code,
                    "baseline_experiment_id": baseline.experiment_id,
                    "candidate_experiment_id": candidate.experiment_id,
                    "langfuse_trace_url": candidate.langfuse_trace_url,
                },
                indent=2,
            )
        )
    else:
        click.echo(format_delta_table(delta))
        click.echo(format_verdict_table(verdict, ruleset_name=rs.name))
        if force and verdict.soft and not verdict.accepted:
            click.echo("\n[--force] Overriding soft veto for adoption.", err=True)

    sys.exit(exit_code)


# ── Profile commands (PRD-7 Phase 2) ───────────────────────────────────────


@main.group()
def profile():
    """Manage persona profiles — create, list, clone, export, import, delete."""
    pass


def _profile_info_to_dict(info) -> dict:
    """Serialize a ProfileInfo dataclass for --json output."""
    import dataclasses
    d = dataclasses.asdict(info)
    # Path fields are not JSON-serializable; coerce to str.
    for k, v in list(d.items()):
        if isinstance(v, Path):
            d[k] = str(v)
    return d


@profile.command("create")
@click.argument("name")
@click.option("--clone", is_flag=True, default=False, help="Light-clone from --from (or default)")
@click.option("--clone-all", is_flag=True, default=False, help="Full-clone from --from (or default)")
@click.option("--from", "clone_from", default=None, help="Source profile name to clone from")
@click.option("--install-launchd", is_flag=True, default=False, help="Install macOS launchd auto-start (darwin only)")
@click.option("--install-systemd", is_flag=True, default=False, help="Install Linux systemd user unit (linux only)")
@click.option("--no-alias", is_flag=True, default=False, help="Skip wrapper alias creation")
@click.option(
    "--best-effort-alias",
    is_flag=True,
    default=False,
    help="If wrapper creation fails, warn and keep the profile (R1 B4 opt-in).",
)
def profile_create(
    name,
    clone,
    clone_all,
    clone_from,
    install_launchd,
    install_systemd,
    no_alias,
    best_effort_alias,
):
    """Create a new persona profile."""
    try:
        from personas.lifecycle import LifecycleError, create_profile
        # Lazy import of cli_root for R1 M2 collision check (must be inside body).
        from cli import main as cli_root

        registered = frozenset(cli_root.commands.keys())
        info = create_profile(
            name,
            clone=clone,
            clone_all=clone_all,
            clone_from=clone_from,
            install_launchd=install_launchd,
            install_systemd=install_systemd,
            no_alias=no_alias,
            best_effort_alias=best_effort_alias,
            registered_subcommands=registered,
        )
        # create_profile returns ProfileInfo; print path for the operator.
        click.echo(f"Created profile '{name}' at {info.path}")
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("list")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def profile_list(json_mode):
    """List all known persona profiles."""
    try:
        from personas.lifecycle import LifecycleError, list_profiles

        infos = list_profiles()
        if json_mode:
            print(json_mod.dumps([_profile_info_to_dict(i) for i in infos], indent=2))
        else:
            if not infos:
                click.echo("No profiles found.")
                return
            for i in infos:
                marker = "*" if i.is_default else " "
                bot = "running" if i.bot_running else "idle"
                click.echo(
                    f"  {marker} {i.name:<16} [{bot}]  {i.path}  "
                    f"skills={i.skill_count} env={'yes' if i.has_env else 'no'}"
                )
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("show")
@click.argument("name")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def profile_show(name, json_mode):
    """Show details for a single profile."""
    try:
        from personas.lifecycle import LifecycleError, show_profile

        info = show_profile(name)
        if json_mode:
            print(json_mod.dumps(_profile_info_to_dict(info), indent=2))
        else:
            click.echo(f"Profile: {info.name}")
            click.echo(f"  Path:        {info.path}")
            click.echo(f"  Default:     {info.is_default}")
            click.echo(f"  Bot running: {info.bot_running}")
            click.echo(f"  Has .env:    {info.has_env}")
            click.echo(f"  Skills:      {info.skill_count}")
            if info.alias_path is not None:
                click.echo(f"  Alias:       {info.alias_path}")
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
def profile_delete(name, yes):
    """Delete a profile (quiesce -> unlink wrapper -> rmtree)."""
    try:
        from personas.lifecycle import LifecycleError, delete_profile

        path = delete_profile(name, yes=yes)
        click.echo(f"Deleted profile '{name}' (was at {path})")
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("use")
@click.argument("name")
def profile_use(name):
    """Set the sticky active profile to <name>."""
    try:
        from personas.lifecycle import LifecycleError, use_profile

        use_profile(name)
        click.echo(f"Active profile set to '{name}'")
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("clone")
@click.argument("src")
@click.argument("dst")
@click.option("--carry-secrets", is_flag=True, default=False, help="Copy .env tokens verbatim (Hermes-faithful)")
@click.option("--no-alias", is_flag=True, default=False, help="Skip wrapper alias creation")
@click.option(
    "--best-effort-alias",
    is_flag=True,
    default=False,
    help="If wrapper creation fails, warn and keep the cloned profile.",
)
def profile_clone(src, dst, carry_secrets, no_alias, best_effort_alias):
    """Light-clone profile <src> into a new profile <dst>.

    R-post-build F4 — routes through ``create_profile(... clone=True,
    clone_from=src, ...)`` so the destination gets the full PRP-7b
    inventory backfill (every ``_REQUIRED_PROFILE_DIRS``,
    ``_REQUIRED_MEMORY_DIRS``, and ``_REQUIRED_IDENTITY_FILES`` entry)
    AND a wrapper alias. The previous direct ``clone_profile()`` call
    bypassed both.
    """
    try:
        from personas.lifecycle import LifecycleError, create_profile
        # Lazy import of cli_root for R1 M2 collision check (must be inside body).
        from cli import main as cli_root

        registered = frozenset(cli_root.commands.keys())
        info = create_profile(
            dst,
            clone=True,
            clone_from=src,
            clone_secrets=carry_secrets,
            no_alias=no_alias,
            best_effort_alias=best_effort_alias,
            registered_subcommands=registered,
        )
        click.echo(f"Cloned '{src}' -> '{dst}' at {info.path}")
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("clone-all")
@click.argument("src")
@click.argument("dst")
@click.option("--carry-secrets", is_flag=True, default=False, help="Copy .env tokens verbatim (Hermes-faithful)")
@click.option("--no-alias", is_flag=True, default=False, help="Skip wrapper alias creation")
@click.option(
    "--best-effort-alias",
    is_flag=True,
    default=False,
    help="If wrapper creation fails, warn and keep the cloned profile.",
)
def profile_clone_all(src, dst, carry_secrets, no_alias, best_effort_alias):
    """Full-clone profile <src> into <dst> (everything including caches).

    R-post-build F4 — routes through ``create_profile(... clone_all=True,
    clone_from=src, ...)`` so the destination gets the full PRP-7b
    inventory backfill AND a wrapper alias.
    """
    try:
        from personas.lifecycle import LifecycleError, create_profile
        from cli import main as cli_root

        registered = frozenset(cli_root.commands.keys())
        info = create_profile(
            dst,
            clone_all=True,
            clone_from=src,
            clone_secrets=carry_secrets,
            no_alias=no_alias,
            best_effort_alias=best_effort_alias,
            registered_subcommands=registered,
        )
        click.echo(f"Full-cloned '{src}' -> '{dst}' at {info.path}")
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("init-archon")
@click.argument("name")
@click.option(
    "--archon-version",
    "archon_version",
    default=None,
    help="Pin a specific Archon version (default: detect from installed binary)",
)
@click.option(
    "--strict-version",
    is_flag=True,
    default=False,
    help="Fail on installed-vs-pinned version drift (default: warn-only)",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing config + re-seed smoke workflow",
)
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
@click.option("-Q", "--quiet", is_flag=True, help="Quiet output (no echo on success)")
def profile_init_archon(name, archon_version, strict_version, force, json_mode, quiet):
    """Initialize the Archon spine layout for a profile (PRP-7e Phase 5).

    Exit codes:
      0 - success
      1 - generic error (LifecycleError / ValueError / FileExistsError / FileNotFoundError /
          ArchonConfigShapeError)
      4 - archon binary not installed (ArchonNotInstalledError)
      7 - version mismatch (ArchonVersionMismatchError) - PRD §12.3 / Q3
    """
    # Subclass-first import + catch order. ArchonNotInstalledError /
    # ArchonVersionMismatchError must be caught BEFORE ArchonError /
    # LifecycleError, otherwise the broader catch swallows the specific
    # exit-code mapping.
    from personas.archon import (
        ArchonError,
        ArchonNotInstalledError,
        ArchonVersionMismatchError,
        init_archon,
    )
    from personas.lifecycle import LifecycleError

    try:
        archon_root = init_archon(
            name,
            archon_version=archon_version,
            strict_version=strict_version,
            force=force,
        )
        smoke_path = archon_root / "workflows" / "profile-isolation-smoke.yaml"
        if json_mode:
            payload = {
                "profile": name,
                "archon_root": str(archon_root),
                "config_path": str(archon_root / "config.yaml"),
                "smoke_workflow": str(smoke_path),
                "smoke_seeded": smoke_path.is_file(),
                "force": force,
                "strict_version": strict_version,
                "archon_version_pinned": archon_version,
            }
            print(json_mod.dumps(payload, indent=2))
        elif not quiet:
            click.echo(
                f"Initialized Archon spine for profile '{name}' at {archon_root}"
            )
            if smoke_path.is_file():
                click.echo("  workflows/profile-isolation-smoke.yaml seeded")
    except ArchonNotInstalledError as exc:
        click.echo(f"Error: Archon not installed: {exc}", err=True)
        sys.exit(4)
    except ArchonVersionMismatchError as exc:
        click.echo(f"Error: Archon version mismatch: {exc}", err=True)
        sys.exit(7)
    except (
        ArchonError,
        LifecycleError,
        ValueError,
        FileExistsError,
        FileNotFoundError,
    ) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("export")
@click.argument("name")
@click.option("--output", "-o", default=None, help="Output archive path (default: ~/.homie/exports/<name>-<ts>.tar.gz)")
def profile_export(name, output):
    """Export profile <name> as a .tar.gz archive."""
    try:
        from personas.clone import export_profile
        from personas.lifecycle import LifecycleError

        path = export_profile(name, output_path=output)
        click.echo(f"Exported profile '{name}' to {path}")
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("import")
@click.argument("archive")
@click.option("--as", "as_name", default=None, help="Override imported profile name")
@click.option("--force", is_flag=True, default=False, help="Overwrite existing profile dir")
def profile_import(archive, as_name, force):
    """Import a profile archive (.tar.gz)."""
    try:
        from personas.clone import import_profile
        from personas.lifecycle import LifecycleError

        path = import_profile(archive, as_name=as_name, force=force)
        click.echo(f"Imported profile to {path}")
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@profile.command("migrate-default")
@click.option("--dry-run/--apply", "dry_run", default=True, help="Dry-run prints op list (default); --apply writes journal stub")
def profile_migrate_default(dry_run):
    """Inventory or apply the install-dir -> ~/.homie/profiles/default migration."""
    try:
        from personas.lifecycle import LifecycleError
        from personas.migrate import migrate_default_apply, migrate_default_dry_run

        if dry_run:
            ops = migrate_default_dry_run()
            if not ops:
                click.echo("No migration operations needed.")
                return
            click.echo(f"Would perform {len(ops)} operation(s):")
            for op in ops:
                click.echo(f"  [{op.op_type}] {op.source} -> {op.destination}")
        else:
            # migrate_default_apply prints its own stub message and never raises.
            migrate_default_apply()
            sys.exit(0)
    except (LifecycleError, ValueError, FileExistsError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


# ── Archon runner subgroup (PRP-7e Phase 5 — R4 ARCHON_HOME pivot) ───────────
#
# `thehomie archon list/run/status` is the operator surface for invoking
# Archon workflows scoped to the active profile (resolved by Phase 1's
# `apply_persona_override`). The subgroup is a thin wrapper over the `archon`
# binary — it does NOT re-implement workflow discovery or execution.
#
# R4 pivot: per-profile state lives at <profile>/.archon/ via ARCHON_HOME env
# var (state isolation). --cwd is passed for Archon's git-probe (which fails
# on non-git dirs) — set to `git rev-parse --show-toplevel` of the operator's
# CWD. NO ARCHON_SOURCE_REPO env (R4 dropped).
#
# Profile-aware via existing `-p/--profile` precedence — already pre-parsed
# by `apply_persona_override` at module import (cli.py:27-29). Click's `main`
# group declares no `-p` option.


@main.group()
def archon():
    """Run profile-scoped Archon workflows (list/run/status)."""
    pass


def _resolve_archon_home_for_runner(profile_name: str) -> str:
    """Compute the ARCHON_HOME value (= <profile>/.archon).

    For named profiles: returns <profile_root>/.archon.
    For default: returns <install_root>/.archon.
    """
    from personas import get_default_paths, get_persona_paths

    if profile_name == "default":
        paths = get_default_paths()
    else:
        paths = get_persona_paths(profile_name)
    # paths["archon"] resolves to <profile>/.archon (R3 cascade — Q1)
    return str(paths["archon"])


def _resolve_homie_home_for_runner(profile_name: str) -> str:
    """Compute the HOMIE_HOME value the Archon subprocess actually runs under.

    Phase 5 post-build F2 fix: never returns an empty string. Resolves to a
    concrete path so the shipped smoke YAML's strict
    ``${HOMIE_HOME:?HOMIE_HOME must be set}`` expansion succeeds.

    Resolution rules:
        - default profile  → install repo root (parent of
                              ``get_default_paths()["archon"]``). This is the
                              install-dir that hosts ``.claude/``, ``.archon/``,
                              ``vault/memory/``, etc.
        - named profile    → ``~/.homie/profiles/<name>/`` (the profile dir
                              itself; same value boot.py rank-3 publishes).

    The default-profile choice intentionally aligns with the shape the
    smoke workflow expects: it does ``mkdir -p $HOMIE_HOME/.archon/...`` and
    writes markers under there, which lands at ``<install>/.archon/...`` —
    the same dir ``ARCHON_HOME`` already points at.
    """
    from personas import get_default_paths, get_persona_paths

    if profile_name == "default":
        # Install repo root = parent of <install>/.archon. ``get_default_paths``
        # already resolves the install root via ``Path(__file__).parent.parent``
        # so this stays portable across clones / Windows / WSL.
        archon_root = get_default_paths()["archon"]
        return str(archon_root.parent)
    # Named profile: use the profile dir itself (same value boot.py
    # rank-1 / rank-3 sets HOMIE_HOME to). ``paths["archon"]`` is
    # ``<profile>/.archon`` so its parent is ``<profile>``.
    paths = get_persona_paths(profile_name)
    return str(paths["archon"].parent)


def _resolve_git_repo_for_runner():
    """Compute the --cwd value: operator's git repo root, or None if not in repo."""
    import subprocess as _subprocess

    try:
        toplevel = _subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=_subprocess.DEVNULL,
        ).strip()
        return toplevel or None
    except (_subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


@archon.command("list")
@click.option("--json", "json_mode", is_flag=True, help="JSON output (passes through to archon)")
def archon_list(json_mode):
    """List Archon workflows discoverable for the active profile."""
    import subprocess as _subprocess

    from personas import get_active_profile_name, get_subprocess_env
    from personas.archon import (
        ArchonNotInstalledError,
        detect_archon_binary,
        is_archon_initialized,
    )

    try:
        detect_archon_binary()  # raises ArchonNotInstalledError (exit 4)
        profile_name = get_active_profile_name()
        if not is_archon_initialized(profile_name):
            click.echo(
                f"Error: Archon not initialized for profile '{profile_name}'.\n"
                f"  Run: thehomie profile init-archon {profile_name}",
                err=True,
            )
            sys.exit(1)

        git_repo = _resolve_git_repo_for_runner()
        if git_repo is None:
            click.echo(
                "Error: thehomie archon list must be invoked from inside a git repo "
                "(Archon's --cwd flag triggers a git-repo-root probe).",
                err=True,
            )
            sys.exit(1)

        archon_home = _resolve_archon_home_for_runner(profile_name)
        # F2 post-build fix: resolve a concrete HOMIE_HOME — never empty.
        # The shipped smoke YAML uses strict ``${HOMIE_HOME:?...}`` expansion
        # which would hard-fail on an empty value. boot.py also calls
        # ``os.environ.pop("HOMIE_HOME")`` for the explicit ``-p default``
        # sentinel, so falling back to ``os.environ.get("HOMIE_HOME", "")``
        # produces an empty string for that path (real bug, not theoretical).
        existing_home = os.environ.get("HOMIE_HOME", "").strip()
        homie_home = existing_home or _resolve_homie_home_for_runner(profile_name)
        env_extra = {
            "ARCHON_HOME": archon_home,  # R4 pivot
            "ARCHON_SUPPRESS_NESTED_CLAUDE_WARNING": "1",
            "HOMIE_HOME": homie_home,
            "HOMIE_NAME": os.environ.get("HOMIE_NAME", profile_name),
        }
        cmd = ["archon", "workflow", "list", "--cwd", git_repo]
        if json_mode:
            cmd.append("--json")
        result = _subprocess.run(
            cmd,
            env=get_subprocess_env(env_extra),
            check=False,
        )
        sys.exit(result.returncode)
    except ArchonNotInstalledError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(4)


@archon.command("run", context_settings={"ignore_unknown_options": True})
@click.argument("workflow")
@click.argument("workflow_args", nargs=-1, type=click.UNPROCESSED)
def archon_run(workflow, workflow_args):
    """Run an Archon workflow under the active profile."""
    import subprocess as _subprocess

    from personas import get_active_profile_name, get_subprocess_env
    from personas.archon import (
        ArchonNotInstalledError,
        detect_archon_binary,
        is_archon_initialized,
    )

    try:
        detect_archon_binary()
        profile_name = get_active_profile_name()
        if not is_archon_initialized(profile_name):
            click.echo(
                f"Error: Archon not initialized for profile '{profile_name}'.\n"
                f"  Run: thehomie profile init-archon {profile_name}",
                err=True,
            )
            sys.exit(1)

        # R4 ARCHON_HOME pivot: per-profile state isolation via env var,
        # --cwd points at a git repo to satisfy Archon's git-probe. Resolve
        # git repo from operator's CWD at invocation time.
        git_repo = _resolve_git_repo_for_runner()
        if git_repo is None:
            click.echo(
                "Error: thehomie archon run must be invoked from inside a git repo "
                "(Archon's --cwd flag triggers a git-repo-root probe that fails "
                "on non-git directories).",
                err=True,
            )
            sys.exit(1)

        archon_home = _resolve_archon_home_for_runner(profile_name)
        # F2 post-build fix: resolve a concrete HOMIE_HOME — never empty.
        # See ``archon list`` handler for the full rationale; the same env
        # contract applies to ``archon run``.
        existing_home = os.environ.get("HOMIE_HOME", "").strip()
        homie_home = existing_home or _resolve_homie_home_for_runner(profile_name)
        env_extra = {
            "ARCHON_HOME": archon_home,  # R4 pivot
            "ARCHON_SUPPRESS_NESTED_CLAUDE_WARNING": "1",
            # boot.py already set HOMIE_HOME and HOMIE_NAME for named
            # profiles; be defensive AND fill in the default-profile case.
            "HOMIE_HOME": homie_home,
            "HOMIE_NAME": os.environ.get("HOMIE_NAME", profile_name),
        }
        # R4: --cwd is the operator's git repo (any git repo will do — only
        # used to satisfy Archon's git-probe). State + workflow discovery
        # come from <ARCHON_HOME>/workflows/ via the env var.
        cmd = ["archon", "workflow", "run", workflow, "--cwd", git_repo]
        cmd.extend(workflow_args)
        result = _subprocess.run(
            cmd,
            env=get_subprocess_env(env_extra),
            check=False,
        )
        sys.exit(result.returncode)
    except ArchonNotInstalledError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(4)


@archon.command("status")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def archon_status(json_mode):
    """Show Archon binary + version-lock + config status for the active profile.

    DIAGNOSTIC-ONLY (R3 NM-minor): always exits 0. The body explains the
    state. This makes `thehomie archon status` a useful first-look tool
    even when Archon isn't installed or the profile isn't initialized.
    For gating CI/CD on Archon health, parse the output OR call
    `thehomie archon list` (which exits 4 / 1 on real failures).
    """
    from personas import get_active_profile_name
    from personas.archon import (
        ArchonNotInstalledError,
        detect_archon_binary,
        get_actual_config_shape,
        get_archon_config_path,
        is_archon_initialized,
    )

    profile_name = get_active_profile_name()
    homie_home = os.environ.get("HOMIE_HOME", "<not set>")
    archon_home = _resolve_archon_home_for_runner(profile_name)

    # --- Detection ---
    binary_path: str | None = None
    installed_version: str | None = None
    detection_error: str | None = None
    try:
        bp, installed_version = detect_archon_binary()
        binary_path = str(bp)
    except ArchonNotInstalledError as exc:
        detection_error = str(exc)
        # R3 NM-minor: status is diagnostic. Do NOT exit 4 here.

    # --- Config shape ---
    config_path = get_archon_config_path(profile_name)
    config_state: str  # one of: "OK", "STALE", "MISSING"
    locked_version: str | None = None
    if not config_path.is_file():
        config_state = "MISSING"
    else:
        shape = get_actual_config_shape(profile_name)
        if not is_archon_initialized(profile_name):
            config_state = "STALE"
        else:
            config_state = "OK"
        if shape is not None:
            try:
                locked_version = shape["capabilities"]["archon"].get("archon_version")
            except (KeyError, TypeError, AttributeError):
                locked_version = None

    # --- JSON path ---
    if json_mode:
        version_match: bool | None
        if installed_version is None or locked_version is None:
            version_match = None
        else:
            version_match = installed_version == locked_version
        payload = {
            "profile": profile_name,
            "homie_home": homie_home,
            "archon_home": archon_home,
            "archon_binary": binary_path,
            "installed_version": installed_version,
            "binary_error": detection_error,
            "config_path": str(config_path),
            "config_state": config_state,
            "locked_version": locked_version,
            "version_match": version_match,
            "initialized": config_state == "OK",
        }
        print(json_mod.dumps(payload, indent=2))
        return  # exit 0 (diagnostic)

    # --- Human-readable path ---
    click.echo(f"Profile: {profile_name} (HOMIE_HOME={homie_home})")
    click.echo(f"  ARCHON_HOME:  {archon_home}")
    if binary_path is not None:
        click.echo(f"  archon binary: {binary_path} (v{installed_version})")
    else:
        click.echo(f"  archon binary: NOT INSTALLED ({detection_error})")
    if config_state == "MISSING":
        click.echo(f"  config:        {config_path} - MISSING")
        click.echo(f"                 Run: thehomie profile init-archon {profile_name}")
        return  # exit 0 (diagnostic)
    if config_state == "STALE":
        click.echo(
            f"  config:        {config_path} - STALE (missing required PRD §11.1 fields)"
        )
        click.echo(f"                 Run: thehomie profile init-archon {profile_name}")
        return  # exit 0 (diagnostic)
    click.echo(f"  config:        {config_path} - OK (PRD §11.1 shape)")
    if installed_version is not None and locked_version is not None:
        if locked_version == installed_version:
            click.echo(f"  version-lock:  {locked_version} (matches)")
        else:
            click.echo(
                f"  version-lock:  {locked_version} (installed: {installed_version}) - MISMATCH"
            )
            # R3 NM-minor: status is diagnostic. Do NOT exit 1 here.


@main.group()
def repositories():
    """Validate profile-owned repository config."""
    pass


def _load_repository_report():
    from repository_config import load_repository_config

    return load_repository_config()


@repositories.command("status")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def repositories_status(json_mode):
    """Show profile-owned repository config status."""
    report = _load_repository_report()
    if json_mode:
        click.echo(json_mod.dumps(report.to_dict(), indent=2))
        return

    state = "enabled" if report.enabled else "disabled"
    click.echo(f"Repositories: {state}")
    click.echo(f"Profile: {report.profile}")
    exists = "present" if report.config_exists else "missing"
    click.echo(f"Config: {report.config_path} ({exists})")
    click.echo(f"Valid: {'yes' if report.valid else 'no'}")
    if report.enabled and report.items:
        click.echo(f"Configured: {len(report.items)}")
        for item in report.items:
            archon = "yes" if item.archon_enabled else "no"
            click.echo(
                f"  - {item.slug}: {item.github_repo} "
                f"(branch={item.default_branch}, dispatch={item.dispatch_mode}, "
                f"archon={archon})"
            )
    if report.errors:
        click.echo("Errors:")
        for error in report.errors:
            click.echo(f"  - {error}")
    if report.warnings:
        click.echo("Warnings:")
        for warning in report.warnings:
            click.echo(f"  - {warning}")


@repositories.command("validate")
@click.option("--json", "json_mode", is_flag=True, help="JSON output")
def repositories_validate(json_mode):
    """Validate profile-owned repository config."""
    report = _load_repository_report()
    if json_mode:
        click.echo(json_mod.dumps(report.to_dict(), indent=2))
    elif report.valid:
        state = "enabled" if report.enabled else "disabled"
        click.echo(f"Repository config valid ({state}; {len(report.items)} configured).")
    else:
        click.echo("Repository config invalid:", err=True)
        for error in report.errors:
            click.echo(f"  - {error}", err=True)
    if not report.valid:
        sys.exit(1)


if __name__ == "__main__":
    main()
