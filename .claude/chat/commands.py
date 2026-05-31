"""Central command registry for the chat bot.

Delegates to ExtensionManager for all command queries. The COMMANDS list and
CATEGORIES list define the core commands — they're registered with the manager
at startup (main.py). Other modules that import from here get thin wrappers
that delegate to the singleton.
"""

from __future__ import annotations

from pathlib import Path

# Global slash command instruction files
_COMMANDS_DIR = Path.home() / ".claude" / "commands"

# PIV command -> instruction file path (relative to _COMMANDS_DIR)
PIV_INSTRUCTIONS: dict[str, str] = {
    "prime": "core/prime.md",
    "planning": "core/planning.md",
    "implement": "core/execute.md",       # "/execute" conflicts with mode switch
    "validate": "validation/validate.md",
    "review": "validation/code-review.md",
    "reviewfix": "validation/code-review-fix.md",
    "commit": "core/commit.md",           # upgrades existing engine command
    "prd": "create-prd.md",
    "e2e": "end-to-end-feature.md",
    "sysreview": "validation/system-review.md",
    "execreport": "validation/execution-report.md",
    "clutch": "SKILL",                    # uses Skill tool, not a raw file
    "blog": "blog.md",                    # blog pipeline via Skill("blog-pipeline")
    "quote": "SKILL",                     # TurboRater quote via Skill("turborater-quote")
}

# Each command: (name, description, type, min_role)
# type: "router" = handled instantly by router, "engine" = passed to Agent SDK
# min_role: "viewer" = everyone, "operator" = operator+admin, "admin" = admin only
COMMANDS: list[tuple[str, str, str, str]] = [
    # -- Session & Mode --
    ("plan", "Switch to plan mode — research only, no changes", "router", "viewer"),
    ("go", "Switch to execute mode — implement changes", "router", "viewer"),
    ("execute", "Alias for /go — enable execute mode", "router", "viewer"),
    ("mode", "Show current mode (plan or execute)", "router", "viewer"),
    ("provider", "Show runtime lane status - selection, routes, provider health", "router", "admin"),
    ("model", "Select runtime lane/provider/model - /model claude, sonnet, opus, codex, codex:default, gpt5.5, codex 5.5, gemini, openrouter, openai, auto", "router", "admin"),
    ("reload", "Reload bot config without restarting", "router", "admin"),
    ("restart", "Restart myself — kill this process and start fresh", "router", "admin"),
    ("help", "Show all available commands", "router", "viewer"),
    ("status", "Show session info — messages, cost, uptime", "router", "viewer"),
    ("diagnostics", "Full system health report — cognition, recall, runtime, sessions, adapters", "router", "admin"),
    ("cost", "Show current session cost in USD", "router", "viewer"),
    ("clear", "Clear conversation — start fresh in this chat", "router", "viewer"),
    ("new", "Start a brand new conversation (alias for /clear)", "router", "viewer"),
    ("extensions", "Extension diagnostics — list, doctor, enable/disable", "router", "admin"),
    # -- Integrations (passed to engine as natural language) --
    ("email", "Check Gmail — list, unread, or urgent emails", "router", "admin"),
    ("pemail", "Check personal Gmail (owner) — unread, list, read <id>", "router", "admin"),
    ("cleanup", "Inbox cleanup — dry run both inboxes, /cleanup go to execute", "router", "admin"),
    ("inbox", "Inbox briefing — prioritized TL;DR of what matters", "router", "admin"),
    ("accounts", "Show all social media accounts and connection status", "router", "admin"),
    ("browser", "Browser ops - status, tabs, open, snapshot via visible Chrome CDP", "router", "admin"),
    ("browserops", "Browser Homie specialist context - agent-browser guide, readiness, policy", "router", "admin"),
    ("linkedin_profile", "LinkedIn profile browser ops - status or open via visible Chrome CDP", "router", "admin"),
    ("post", "Post to social media — /post x Hello! or /post facebook Check out our new rates!", "engine", "admin"),
    ("calendar", "Check Google Calendar — today or upcoming events", "engine", "admin"),
    ("tasks", "Check Asana tasks — my tasks, overdue, due soon", "engine", "admin"),
    ("slack", "Check or send Slack messages", "engine", "admin"),
    ("sheets", "Read or write Google Sheets", "engine", "admin"),
    ("docs", "Read a Google Doc", "engine", "admin"),
    ("drive", "Search or list Google Drive files", "engine", "admin"),
    ("circle", "Check Circle community — posts, DMs, feed", "engine", "admin"),
    # -- Analytics & Monitoring --
    ("gsc", "Check Google Search Console — queries, pages, CTR", "router", "operator"),
    ("analytics", "Check Google Analytics — sessions, traffic, pages", "router", "operator"),
    # -- Personal Finance --
    ("budget", "Personal finances — status, bills, loans, transactions, spending, accounts", "router", "admin"),
    # -- Cabinet (Phase 5b) — chat-routed cabinet meetings via localhost:4322 --
    ("cabinet", "Multi-persona meeting — create|list|send <id> <text>|end <id>", "router", "admin"),
    ("standup", "Send a standup question to the cabinet — /standup [question]", "router", "admin"),
    ("discuss", "Start a discussion — /discuss <topic>", "router", "admin"),
    ("teamtick", "Run one autonomous team scheduler tick — /teamtick <team_id>", "router", "admin"),
    ("taskchaddrill", "Run TaskChad multi-role team drill — /taskchaddrill [--runtime]", "router", "admin"),
    ("teamroom", "Run Growth Boardroom team workflow — /teamroom [--v2] [--runtime] <goal>", "router", "admin"),
    ("send", "Send a draft email via Outlook (e.g. /send draft-01)", "router", "operator"),
    ("brief", "Quick briefing — /brief all for full dashboard", "router", "operator"),
    # -- Memory & Search --
    ("search", "Search memory — keyword or semantic over notes", "engine", "admin"),
    ("file", "File the last answer as a vault note with entity compilation", "engine", "admin"),
    ("working", "Show cross-session scratchpad — open threads, hypotheses, questions", "router", "admin"),
    # -- Content Creation --
    ("blog", "Generate a research-backed blog article via the blog-pipeline skill", "engine", "admin"),
    ("quote", "Generate an insurance quote via TurboRater using the turborater-quote skill", "engine", "admin"),
    ("linkedin", "Draft a LinkedIn post", "engine", "admin"),
    ("tweet", "Draft an X (Twitter) post or thread", "engine", "admin"),
    ("instagram", "Create Instagram content — carousel or caption", "engine", "admin"),
    ("yt_script", "Write a YouTube video script", "engine", "admin"),
    ("shorts", "Write a YouTube Shorts script", "engine", "admin"),
    # -- Dev Workflow (PIV Loop) --
    ("prime", "Load codebase context and conventions", "engine", "admin"),
    ("planning", "Create a research-backed implementation plan", "engine", "admin"),
    ("implement", "Execute an implementation plan step by step", "engine", "admin"),
    ("validate", "Run 5-level validation pyramid (lint/types/tests)", "engine", "admin"),
    ("review", "Pre-commit technical code review", "engine", "admin"),
    ("reviewfix", "Fix issues found in code review", "engine", "admin"),
    ("commit", "Create a git commit with conventional format", "engine", "admin"),
    ("prd", "Create a Product Requirements Document", "engine", "admin"),
    ("e2e", "End-to-end feature: prime+plan+implement+commit", "engine", "admin"),
    ("sysreview", "System review — analyze plan vs execution", "engine", "admin"),
    ("execreport", "Generate post-implementation execution report", "engine", "admin"),
    ("clutch", "CLUTCH orchestrator — multi-phase team execution", "engine", "admin"),
    # -- Dev Tools --
    ("diagram", "Create an Excalidraw architecture diagram", "engine", "admin"),
    ("pdf", "Work with PDFs — extract, merge, split, create", "engine", "admin"),
    ("slides", "Generate a PPTX presentation", "engine", "admin"),
    ("sop", "Create a runbook or technical documentation", "engine", "admin"),
]

# Category groupings for help text
CATEGORIES: list[tuple[str, list[str]]] = [
    (
        "Session & Mode",
        ["plan", "go", "execute", "mode", "provider", "model", "reload", "restart",
         "help", "status", "cost", "clear", "new", "extensions"],
    ),
    (
        "Integrations",
        ["email", "pemail", "inbox", "cleanup", "accounts", "post", "calendar", "tasks",
         "browser", "browserops", "linkedin_profile", "slack", "sheets", "docs", "drive", "circle"],
    ),
    ("Analytics & Monitoring", ["gsc", "analytics"]),
    ("Personal Finance", ["budget"]),
    # Cabinet (Phase 5b) — chat-routed cabinet operator surface.
    ("Cabinet", ["cabinet", "standup", "discuss", "teamtick", "taskchaddrill", "teamroom"]),
    ("Communication", ["send", "brief"]),
    ("Memory", ["search", "file", "working"]),
    ("Content Creation", ["blog", "quote", "linkedin", "tweet", "instagram", "yt_script", "shorts"]),
    (
        "PIV Workflow",
        ["prime", "planning", "implement", "validate", "review", "reviewfix",
         "commit", "prd", "e2e", "sysreview", "execreport", "clutch"],
    ),
    ("Dev Tools", ["diagram", "pdf", "slides", "sop"]),
]

# Core data intents: (keywords, command, included_in_brief)
CORE_INTENTS: list[tuple[list[str], str, bool]] = [
    (["email", "inbox", "mail", "outlook", "gmail"], "email", True),
    (["analytics", "traffic", "ga4", "sessions", "pageviews"], "analytics", False),
    (["gsc", "search console", "seo ranking", "queries", "impressions"], "gsc", False),
    (["social media accounts", "social accounts"], "accounts", False),
    (["agent browser", "agent-browser", "browser automation", "browserops", "browser homie",
      "open browser", "open up your browser", "web browser", "visible chrome", "cdp",
      "browse the web", "browse this", "go online", "go to linkedin", "open linkedin",
      "check linkedin", "to linkedin", "linkedin profile", "inspect website", "check this website",
      "open this website", "use the browser"], "browserops", False),
    (["budget", "bills", "finances", "paid", "paycheck", "loan status",
      "what do i owe", "transactions", "spending", "bank balance", "account balance"], "budget", True),
    (["working memory", "open threads", "what was i working on",
      "where did i leave off", "active hypotheses"], "working", True),
    # Cabinet (Phase 5b) — broad-query intents that spawn LLM workloads,
    # NOT data fetches. `included_in_brief=False` so a "show me everything"
    # broad query doesn't kick off a cabinet meeting.
    (["group chat", "all agents discuss", "cabinet meeting"], "cabinet", False),
    (["standup", "team standup", "rotating speakers"], "standup", False),
    (["debate", "discuss this with the team", "open debate"], "discuss", False),
    (["team tick", "team scheduler", "run team scheduler"], "teamtick", False),
    (["taskchad drill", "taskchad team drill", "run taskchad drill"], "taskchaddrill", False),
    (["team room", "growth boardroom", "boardroom workflow"], "teamroom", False),
]


# ---------------------------------------------------------------------------
# Helper functions — delegate to ExtensionManager when available, fall back
# to static COMMANDS list for backward compatibility (import time, tests).
# ---------------------------------------------------------------------------

def _try_manager():
    """Try to get the ExtensionManager singleton. Returns None if not initialized."""
    try:
        from extension_manager import get_manager
        mgr = get_manager()
        # Check if it has any commands registered (it might be a bare instance)
        if mgr._commands:
            return mgr
    except Exception:
        pass
    return None


def get_router_commands() -> list[str]:
    """Return list of command names handled by the router (instant response)."""
    mgr = _try_manager()
    if mgr:
        return list(mgr.get_router_commands())
    return [name for name, _, typ, _ in COMMANDS if typ == "router"]


def get_all_command_names() -> list[str]:
    """Return list of all command names (router + engine)."""
    mgr = _try_manager()
    if mgr:
        return mgr.get_all_command_names()
    return [name for name, _, _, _ in COMMANDS]


def get_telegram_bot_commands() -> list[tuple[str, str]]:
    """Return list of (command, description) tuples for Telegram's setMyCommands."""
    mgr = _try_manager()
    if mgr:
        return [(n, s.description) for n, s in mgr._commands.items()]
    return [(name, desc) for name, desc, _, _ in COMMANDS]


def get_engine_command_description(name: str) -> str | None:
    """Return the description for an engine command, or None if not found."""
    mgr = _try_manager()
    if mgr:
        return mgr.get_engine_command_description(name)
    for cmd_name, desc, typ, _ in COMMANDS:
        if cmd_name == name and typ == "engine":
            return desc
    return None


def get_command_min_role(name: str) -> str:
    """Return the minimum role required to execute a command."""
    mgr = _try_manager()
    if mgr:
        return mgr.get_command_min_role(name)
    for cmd_name, _, _, min_role in COMMANDS:
        if cmd_name == name:
            return min_role
    return "viewer"


def get_piv_instruction(name: str, args: str = "") -> str | None:
    """Load the full instruction markdown for a PIV command."""
    rel_path = PIV_INSTRUCTIONS.get(name)
    if not rel_path or rel_path == "SKILL":
        return None
    filepath = _COMMANDS_DIR / rel_path
    if not filepath.exists():
        return None
    content = filepath.read_text(encoding="utf-8")
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    content = content.replace("$ARGUMENTS", args if args else "(no arguments provided)")
    return content


def get_help_text(user_role: str = "admin") -> str:
    """Return a formatted help string grouped by category."""
    mgr = _try_manager()
    if mgr:
        return mgr.get_help_text(user_role=user_role)

    # Fallback — static COMMANDS list
    role_level = {"viewer": 0, "operator": 1, "admin": 2}
    user_level = role_level.get(user_role, 0)

    lookup = {name: desc for name, desc, _, _ in COMMANDS}
    role_lookup = {name: min_role for name, _, _, min_role in COMMANDS}

    lines = ["*Available Commands*\n"]
    for cat_name, cmd_names in CATEGORIES:
        visible = [n for n in cmd_names if n in lookup and user_level >= role_level.get(role_lookup.get(n, "viewer"), 0)]
        if not visible:
            continue
        lines.append(f"*{cat_name}*")
        for name in visible:
            lines.append(f"  /{name} — {lookup[name]}")
        lines.append("")

    return "\n".join(lines).strip()
