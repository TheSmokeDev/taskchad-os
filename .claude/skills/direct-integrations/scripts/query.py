"""
Interactive CLI wrapper for direct platform integrations.

Used by the direct-integrations Claude Code skill to query Gmail, Calendar,
Asana, Slack, Google Sheets, Google Docs, and Google Drive from interactive sessions.

Usage:
    python query.py gmail list --max 5
    python query.py calendar today
    python query.py asana overdue
    python query.py slack channels
    python query.py sheets read <spreadsheet_id> [--range "Sheet1!A1:Z100"]
    python query.py docs read <document_id>
    python query.py drive find "search term" [--type spreadsheet]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add the scripts directory to Python path for integration imports
SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def cmd_gmail(args: argparse.Namespace) -> None:
    """Handle Gmail commands."""
    from integrations.gmail import (
        check_for_urgent_emails,
        format_emails_for_context,
        get_email_details,
        get_gmail_service,
        get_unread_count,
        list_emails,
    )

    if args.action == "list":
        # Default to 24h window when no query specified (recent inbox view)
        # but no time filter when searching (user wants to find old emails too)
        hours = args.hours if args.hours is not None else (None if args.query else 24)
        emails = list_emails(
            max_results=args.max,
            query=args.query or "",
            unread_only=args.unread,
            hours_ago=hours,
        )
        print(format_emails_for_context(emails))

    elif args.action == "urgent":
        urgent = check_for_urgent_emails(hours_ago=args.hours)
        if urgent:
            print(f"Found {len(urgent)} potentially urgent emails:\n")
            print(format_emails_for_context(urgent))
        else:
            print("No urgent emails found")

    elif args.action == "unread":
        count = get_unread_count()
        print(f"Unread emails: {count}")

    elif args.action == "read":
        if not args.message_id:
            print("Error: message_id required for read command")
            sys.exit(1)
        service = get_gmail_service()
        email = get_email_details(service, args.message_id, include_body=True)
        if email:
            print(f"Subject: {email.subject}")
            print(f"From: {email.sender} <{email.sender_email}>")
            print(f"Date: {email.date}")
            print(f"Labels: {', '.join(email.labels)}")
            print(f"\n{email.body or email.snippet}")
        else:
            print("Email not found")


def cmd_calendar(args: argparse.Namespace) -> None:
    """Handle Calendar commands."""
    from integrations.calendar_api import (
        check_for_upcoming_meetings,
        format_events_for_context,
        get_today_events,
        get_upcoming_events,
    )

    if args.action == "today":
        events = get_today_events()
        print(format_events_for_context(events))

    elif args.action == "upcoming":
        events = get_upcoming_events(hours_ahead=args.hours)
        print(format_events_for_context(events))

    elif args.action == "soon":
        events = check_for_upcoming_meetings(hours_ahead=4)
        print(format_events_for_context(events))


def cmd_asana(args: argparse.Namespace) -> None:
    """Handle Asana commands."""
    from integrations.asana_api import (
        add_comment,
        complete_task,
        create_task,
        format_tasks_for_context,
        get_due_soon_tasks,
        get_my_tasks,
        get_overdue_tasks,
        get_project_tasks,
        move_task,
    )

    assignee = getattr(args, "assignee", None)

    if args.action == "my-tasks":
        tasks = get_my_tasks(max_results=args.max, assignee=assignee)
        print(format_tasks_for_context(tasks))

    elif args.action == "project":
        tasks = get_project_tasks(project_gid=args.project_id, max_results=args.max)
        print(format_tasks_for_context(tasks))

    elif args.action == "overdue":
        tasks = get_overdue_tasks(assignee=assignee)
        if tasks:
            print(f"Found {len(tasks)} overdue tasks:\n")
            print(format_tasks_for_context(tasks))
        else:
            print("No overdue tasks")

    elif args.action == "due-soon":
        tasks = get_due_soon_tasks(days=args.days, assignee=assignee)
        if tasks:
            print(f"Found {len(tasks)} tasks due in next {args.days} days:\n")
            print(format_tasks_for_context(tasks))
        else:
            print(f"No tasks due in next {args.days} days")

    elif args.action == "complete":
        if not args.project_id:
            print("Error: task_gid required for complete command")
            sys.exit(1)
        task = complete_task(args.project_id)
        print(f"Completed: {task.name}")

    elif args.action == "create":
        name = getattr(args, "name", None)
        if not name:
            print("Error: --name required for create command")
            sys.exit(1)
        task = create_task(
            name=name,
            due_on=getattr(args, "due", None),
            assignee=assignee,
            project=getattr(args, "project", None),
            notes=getattr(args, "notes", None),
        )
        due_str = task.due_on.strftime("%Y-%m-%d") if task.due_on else "No due date"
        print(f"Created: **{task.name}** (GID: {task.gid})")
        print(f"  Assignee: {task.assignee or 'me'} | Due: {due_str}")
        if task.project:
            print(f"  Project: {task.project}")

    elif args.action == "comment":
        task_gid = args.project_id
        comment_text = getattr(args, "comment", None)
        if not task_gid or not comment_text:
            print("Error: task_gid (positional) and --comment required")
            sys.exit(1)
        story_gid = add_comment(task_gid, comment_text)
        print(f"Comment added to task {task_gid} (story GID: {story_gid})")

    elif args.action == "move":
        task_gid = args.project_id
        to_proj = getattr(args, "to_project", None)
        from_proj = getattr(args, "from_project", None)
        if not task_gid or not to_proj:
            print("Error: task_gid (positional) and --to-project required")
            sys.exit(1)
        move_task(task_gid, to_project=to_proj, from_project=from_proj)
        print(f"Moved task {task_gid} to project {to_proj}")


def cmd_slack(args: argparse.Namespace) -> None:
    """Handle Slack commands."""
    from integrations.slack_api import (
        check_for_important_messages,
        format_messages_for_context,
        get_channel_id,
        get_recent_messages,
        get_slack_client,
        send_notification,
    )

    if args.action == "channels":
        client = get_slack_client()
        result = client.conversations_list(types="public_channel", limit=100)
        for ch in result.get("channels", []):
            print(f"  #{ch['name']} ({ch['id']})")

    elif args.action == "messages":
        if not args.channel:
            print("Error: channel name required")
            sys.exit(1)
        ch_id = get_channel_id(args.channel)
        if not ch_id:
            print(f"Channel not found: {args.channel}")
            sys.exit(1)
        msgs = get_recent_messages(ch_id, hours_ago=args.hours, limit=20)
        print(format_messages_for_context(msgs))

    elif args.action == "send":
        if not args.channel or not args.message:
            print("Error: channel and message required")
            sys.exit(1)
        result = send_notification(args.channel, args.message)
        print(f"Sent! (ts={result['ts']})" if result else "Failed to send")

    elif args.action == "check":
        important = check_for_important_messages(hours_ago=args.hours)
        if important:
            print(f"Found {len(important)} important messages:\n")
            print(format_messages_for_context(important))
        else:
            print("No important messages found")


def cmd_sheets(args: argparse.Namespace) -> None:
    """Handle Google Sheets commands."""
    from integrations.sheets_api import (
        append_to_spreadsheet,
        format_spreadsheet_for_context,
        get_spreadsheet_info,
        read_spreadsheet,
        write_spreadsheet,
    )

    if args.action == "read":
        if not args.target_id:
            print("Error: spreadsheet_id required")
            sys.exit(1)
        data = read_spreadsheet(
            args.target_id,
            range_notation=args.range or "",
            max_rows=args.max_rows,
        )
        print(format_spreadsheet_for_context(data))

    elif args.action == "info":
        if not args.target_id:
            print("Error: spreadsheet_id required")
            sys.exit(1)
        info = get_spreadsheet_info(args.target_id)
        print(format_spreadsheet_for_context(info))

    elif args.action == "write":
        if not args.target_id or not args.values or not args.range:
            print("Error: spreadsheet_id, --range, and --values required")
            sys.exit(1)
        parsed = json.loads(args.values)
        result = write_spreadsheet(args.target_id, args.range, parsed)
        print(json.dumps(result, indent=2))

    elif args.action == "append":
        if not args.target_id or not args.values or not args.range:
            print("Error: spreadsheet_id, --range, and --values required")
            sys.exit(1)
        parsed = json.loads(args.values)
        result = append_to_spreadsheet(args.target_id, args.range, parsed)
        print(json.dumps(result, indent=2))


def cmd_docs(args: argparse.Namespace) -> None:
    """Handle Google Docs commands."""
    from integrations.docs_api import (
        format_document_for_context,
        get_document_info,
        read_document,
    )

    if args.action == "read":
        if not args.target_id:
            print("Error: document_id required")
            sys.exit(1)
        data = read_document(args.target_id)
        print(format_document_for_context(data, max_chars=args.max_chars))

    elif args.action == "info":
        if not args.target_id:
            print("Error: document_id required")
            sys.exit(1)
        data = get_document_info(args.target_id)
        char_count = len(data.body_text)
        print(f"Title: {data.title}")
        print(f"ID: {data.id}")
        print(f"URL: {data.url}")
        print(f"Content length: ~{char_count} chars")


def cmd_personal_gmail(args: argparse.Namespace) -> None:
    """Handle personal Gmail commands (read-only)."""
    from integrations.personal_gmail import (
        format_personal_emails_for_context,
        get_personal_email,
        get_personal_unread_count,
        is_personal_gmail_configured,
        list_personal_emails,
    )

    if not is_personal_gmail_configured():
        print("Personal Gmail not configured. Run: uv run python setup_auth.py --personal")
        sys.exit(1)

    if args.action == "list":
        emails = list_personal_emails(max_results=args.max, query=args.query or "", hours_ago=args.hours)
        print(format_personal_emails_for_context(emails))

    elif args.action == "unread":
        count = get_personal_unread_count()
        print(f"Unread: {count}")
        emails = list_personal_emails(max_results=args.max, unread_only=True)
        print(format_personal_emails_for_context(emails))

    elif args.action == "read":
        if not args.message_id:
            print("Error: message_id required for read command")
            sys.exit(1)
        email = get_personal_email(args.message_id)
        if email:
            print(f"Subject: {email.subject}")
            print(f"From: {email.sender} <{email.sender_email}>")
            print(f"Date: {email.date}")
            print(f"\n{email.body or email.snippet}")
        else:
            print("Email not found")


def cmd_circle(args: argparse.Namespace) -> None:
    """Handle Circle commands (read-only)."""
    from integrations.circle_api import (
        format_chat_rooms_for_context,
        format_messages_for_context,
        format_notifications_for_context,
        format_posts_for_context,
        format_spaces_for_context,
        get_chat_messages,
        get_chat_rooms,
        get_member_posts,
        get_notifications,
        get_post,
        get_posts,
        get_spaces,
        search_posts,
    )

    if args.action == "spaces":
        spaces = get_spaces()
        print(format_spaces_for_context(spaces))

    elif args.action == "posts":
        if not args.target_id:
            print("Error: space_id required. Run 'circle spaces' first.")
            sys.exit(1)
        posts = get_posts(int(args.target_id), max_results=args.max)
        print(format_posts_for_context(posts))

    elif args.action == "post":
        if not args.target_id:
            print("Error: post_id required")
            sys.exit(1)
        post = get_post(int(args.target_id))
        if post:
            print(format_posts_for_context([post]))
        else:
            print("Post not found")

    elif args.action == "search":
        if not args.query:
            print("Error: search query required")
            sys.exit(1)
        posts = search_posts(args.query, max_results=args.max)
        print(format_posts_for_context(posts))

    elif args.action == "dms":
        rooms = get_chat_rooms(max_results=args.max)
        print(format_chat_rooms_for_context(rooms))

    elif args.action == "dm":
        if not args.target_id:
            print("Error: chat_room_uuid required. Run 'circle dms' first.")
            sys.exit(1)
        messages = get_chat_messages(args.target_id, max_results=args.max)
        print(format_messages_for_context(messages))

    elif args.action == "notifications":
        notifications = get_notifications(max_results=args.max)
        print(format_notifications_for_context(notifications))

    elif args.action == "feed":
        posts = get_member_posts(max_results=args.max)
        print(format_posts_for_context(posts))


def cmd_search_console(args: argparse.Namespace) -> None:
    """Handle Google Search Console commands."""
    from integrations.search_console_api import (
        format_pages_for_context,
        format_queries_for_context,
        format_stats_for_context,
        get_overall_stats,
        get_top_pages,
        get_top_queries,
    )

    if args.action == "top-queries":
        queries = get_top_queries(days=args.days, max_results=args.max)
        print(format_queries_for_context(queries))
    elif args.action == "top-pages":
        pages = get_top_pages(days=args.days, max_results=args.max)
        print(format_pages_for_context(pages))
    elif args.action == "overview":
        stats = get_overall_stats(days=args.days)
        print(format_stats_for_context(stats))


def cmd_analytics(args: argparse.Namespace) -> None:
    """Handle Google Analytics (GA4) commands."""
    from integrations.analytics_api import (
        format_overview_for_context,
        format_pages_for_context,
        format_realtime_for_context,
        format_sources_for_context,
        get_overview,
        get_realtime,
        get_top_pages,
        get_traffic_sources,
    )

    if args.action == "overview":
        data = get_overview(days=args.days)
        print(format_overview_for_context(data))
    elif args.action == "top-pages":
        pages = get_top_pages(days=args.days, max_results=args.max)
        print(format_pages_for_context(pages))
    elif args.action == "traffic-sources":
        sources = get_traffic_sources(days=args.days, max_results=args.max)
        print(format_sources_for_context(sources))
    elif args.action == "realtime":
        data = get_realtime()
        print(format_realtime_for_context(data))


def cmd_drive(args: argparse.Namespace) -> None:
    """Handle Google Drive commands."""
    from integrations.drive_api import (
        find_files,
        format_files_for_context,
        get_file_by_id,
        list_files,
    )

    if args.action == "find":
        if not args.query:
            print("Error: search query required")
            sys.exit(1)
        files = find_files(args.query, file_type=args.file_type, max_results=args.max)
        print(format_files_for_context(files))

    elif args.action == "list":
        files = list_files(file_type=args.file_type, max_results=args.max)
        print(format_files_for_context(files))

    elif args.action == "get":
        if not args.query:
            print("Error: file ID required")
            sys.exit(1)
        file = get_file_by_id(args.query)
        if file:
            print(format_files_for_context([file]))
        else:
            print("File not found")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Direct Platform Integrations")
    subparsers = parser.add_subparsers(dest="service", required=True)

    # Gmail
    gmail_parser = subparsers.add_parser("gmail", help="Gmail operations")
    gmail_parser.add_argument("action", choices=["list", "urgent", "unread", "read"])
    gmail_parser.add_argument("message_id", nargs="?", default=None)
    gmail_parser.add_argument("--max", type=int, default=10)
    gmail_parser.add_argument("--query", default=None)
    gmail_parser.add_argument("--hours", type=int, default=None)
    gmail_parser.add_argument("--unread", action="store_true")

    # Calendar
    cal_parser = subparsers.add_parser("calendar", help="Calendar operations")
    cal_parser.add_argument("action", choices=["today", "upcoming", "soon"])
    cal_parser.add_argument("--hours", type=int, default=24)

    # Asana
    asana_parser = subparsers.add_parser("asana", help="Asana operations")
    asana_parser.add_argument("action", choices=["my-tasks", "project", "overdue", "due-soon", "complete", "create", "comment", "move"])
    asana_parser.add_argument("project_id", nargs="?", default=None)
    asana_parser.add_argument("--max", type=int, default=20)
    asana_parser.add_argument("--days", type=int, default=3)
    asana_parser.add_argument("--assignee", type=str, default=None, help="User name (e.g. 'sydney') or GID")
    asana_parser.add_argument("--name", type=str, default=None, help="Task name for create")
    asana_parser.add_argument("--due", type=str, default=None, help="Due date YYYY-MM-DD for create")
    asana_parser.add_argument("--project", type=str, default=None, help="Project GID for create")
    asana_parser.add_argument("--notes", type=str, default=None, help="Description/notes for create")
    asana_parser.add_argument("--comment", type=str, default=None, help="Comment text for comment action")
    asana_parser.add_argument("--to-project", type=str, default=None, help="Destination project GID for move")
    asana_parser.add_argument("--from-project", type=str, default=None, help="Source project GID for move (optional)")

    # Slack
    slack_parser = subparsers.add_parser("slack", help="Slack operations")
    slack_parser.add_argument("action", choices=["channels", "messages", "send", "check"])
    slack_parser.add_argument("channel", nargs="?", default=None)
    slack_parser.add_argument("message", nargs="?", default=None)
    slack_parser.add_argument("--hours", type=int, default=2)

    # Google Sheets
    sheets_parser = subparsers.add_parser("sheets", help="Google Sheets operations")
    sheets_parser.add_argument("action", choices=["read", "info", "write", "append"])
    sheets_parser.add_argument("target_id", nargs="?", default=None, help="Spreadsheet ID")
    sheets_parser.add_argument("--range", default=None, help="A1 notation range")
    sheets_parser.add_argument("--values", default=None, help="JSON 2D array for write/append")
    sheets_parser.add_argument("--max-rows", type=int, default=500)

    # Google Docs
    docs_parser = subparsers.add_parser("docs", help="Google Docs operations")
    docs_parser.add_argument("action", choices=["read", "info"])
    docs_parser.add_argument("target_id", nargs="?", default=None, help="Document ID")
    docs_parser.add_argument("--max-chars", type=int, default=4000)

    # Circle
    circle_parser = subparsers.add_parser("circle", help="Circle community operations (read-only)")
    circle_parser.add_argument("action", choices=["spaces", "posts", "post", "search", "dms", "dm", "notifications", "feed"])
    circle_parser.add_argument("target_id", nargs="?", default=None, help="space_id, post_id, or chat_room_uuid")
    circle_parser.add_argument("--query", default=None, help="Search query for search action")
    circle_parser.add_argument("--max", type=int, default=10)

    # Google Drive
    drive_parser = subparsers.add_parser("drive", help="Google Drive operations")
    drive_parser.add_argument("action", choices=["find", "list", "get"])
    drive_parser.add_argument("query", nargs="?", default=None, help="Search term or file ID")
    drive_parser.add_argument("--type", dest="file_type", default=None,
                              choices=["spreadsheet", "document", "folder", "presentation", "pdf"])
    drive_parser.add_argument("--max", type=int, default=10)

    # Google Search Console
    gsc_parser = subparsers.add_parser("search-console", help="Google Search Console operations")
    gsc_parser.add_argument("action", choices=["top-queries", "top-pages", "overview"])
    gsc_parser.add_argument("--days", type=int, default=28)
    gsc_parser.add_argument("--max", type=int, default=10)

    # Google Analytics (GA4)
    ga_parser = subparsers.add_parser("analytics", help="Google Analytics (GA4) operations")
    ga_parser.add_argument("action", choices=["overview", "top-pages", "traffic-sources", "realtime"])
    ga_parser.add_argument("--days", type=int, default=28)
    ga_parser.add_argument("--max", type=int, default=10)

    # Personal Gmail (read-only)
    pg_parser = subparsers.add_parser("personal-gmail", help="Personal Gmail read-only (owner6392lastname@gmail.com)")
    pg_parser.add_argument("action", choices=["list", "unread", "read"])
    pg_parser.add_argument("message_id", nargs="?", default=None)
    pg_parser.add_argument("--max", type=int, default=10)
    pg_parser.add_argument("--query", default=None)
    pg_parser.add_argument("--hours", type=int, default=None)

    args = parser.parse_args()

    try:
        if args.service == "gmail":
            cmd_gmail(args)
        elif args.service == "calendar":
            cmd_calendar(args)
        elif args.service == "asana":
            cmd_asana(args)
        elif args.service == "slack":
            cmd_slack(args)
        elif args.service == "sheets":
            cmd_sheets(args)
        elif args.service == "docs":
            cmd_docs(args)
        elif args.service == "circle":
            cmd_circle(args)
        elif args.service == "drive":
            cmd_drive(args)
        elif args.service == "search-console":
            cmd_search_console(args)
        elif args.service == "analytics":
            cmd_analytics(args)
        elif args.service == "personal-gmail":
            cmd_personal_gmail(args)
    except Exception as e:
        print(json.dumps({"error": str(e), "type": "runtime"}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
