"""
Asana Direct Integration for The Homie.

Uses Asana Python SDK v5 with Personal Access Token authentication.

Usage:
    uv run python -m integrations.asana_api my-tasks --max 10
    uv run python -m integrations.asana_api project <project_id>
    uv run python -m integrations.asana_api overdue
    uv run python -m integrations.asana_api due-soon --days 3
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    ASANA_ACCESS_TOKEN,
    ASANA_PROJECT_ID,
    ASANA_USERS,
    ASANA_WORKSPACE_ID,
)
from shared import with_retry  # noqa: E402


@dataclass
class AsanaTask:
    """Represents an Asana task."""

    gid: str
    name: str
    due_on: date | None = None
    completed: bool = False
    assignee: str | None = None
    project: str | None = None
    notes: str | None = None


def get_asana_client() -> Any:
    """Create authenticated Asana API client (v5 SDK)."""
    import asana  # type: ignore[import-untyped]

    if not ASANA_ACCESS_TOKEN:
        raise ValueError(
            "ASANA_ACCESS_TOKEN not set in .env\n"
            "Get a Personal Access Token from https://app.asana.com/0/developer-console"
        )

    configuration = asana.Configuration()
    configuration.access_token = ASANA_ACCESS_TOKEN
    client: Any = asana.ApiClient(configuration)
    return client


def _parse_task(task_data: Any) -> AsanaTask:
    """Parse an Asana task response into AsanaTask dataclass."""
    # v5 SDK returns objects with attributes or dicts depending on context
    if isinstance(task_data, dict):
        data = task_data
    else:
        data = task_data.to_dict() if hasattr(task_data, "to_dict") else vars(task_data)

    due_on_val: date | None = None
    due_str = data.get("due_on")
    if due_str and isinstance(due_str, str):
        due_on_val = datetime.strptime(due_str, "%Y-%m-%d").date()
    elif isinstance(due_str, date):
        due_on_val = due_str

    assignee_name: str | None = None
    assignee_data = data.get("assignee")
    if isinstance(assignee_data, dict):
        assignee_name = assignee_data.get("name")
    elif assignee_data is not None and hasattr(assignee_data, "name"):
        assignee_name = assignee_data.name

    project_name: str | None = None
    projects_data = data.get("projects")
    if projects_data and isinstance(projects_data, list) and len(projects_data) > 0:
        first_proj = projects_data[0]
        if isinstance(first_proj, dict):
            project_name = first_proj.get("name")
        elif first_proj is not None and hasattr(first_proj, "name"):
            project_name = first_proj.name

    notes_raw = data.get("notes", "")
    notes_val = str(notes_raw)[:200] if notes_raw else None

    return AsanaTask(
        gid=str(data.get("gid", "")),
        name=str(data.get("name", "")),
        due_on=due_on_val,
        completed=bool(data.get("completed", False)),
        assignee=assignee_name,
        project=project_name,
        notes=notes_val,
    )


def resolve_assignee(name: str | None) -> str:
    """Resolve a friendly name to an Asana GID, or return 'me'."""
    if not name:
        return "me"
    key = name.lower().strip()
    if key in ASANA_USERS:
        return ASANA_USERS[key]
    # If it looks like a raw GID, pass through
    if key.isdigit():
        return key
    raise ValueError(
        f"Unknown Asana user '{name}'. Known users: {', '.join(ASANA_USERS.keys())}"
    )


def get_my_tasks(
    max_results: int = 20,
    only_incomplete: bool = True,
    assignee: str | None = None,
) -> list[AsanaTask]:
    """
    Get tasks assigned to a user in the configured workspace.

    Uses v5 SDK: TasksApi.get_tasks() with assignee + workspace.
    Pass assignee as a friendly name ('sydney'), GID, or None for 'me'.
    """
    import asana

    api_client = get_asana_client()
    tasks_api = asana.TasksApi(api_client)

    opts: dict[str, Any] = {
        "assignee": resolve_assignee(assignee),
        "workspace": ASANA_WORKSPACE_ID,
        "opt_fields": "name,due_on,completed,assignee.name,notes,projects.name",
    }
    if only_incomplete:
        opts["completed_since"] = "now"

    result: list[AsanaTask] = []
    try:
        tasks = with_retry(lambda: tasks_api.get_tasks(opts))
        for task_data in tasks:
            if len(result) >= max_results:
                break
            task = _parse_task(task_data)
            if only_incomplete and task.completed:
                continue
            result.append(task)
    except Exception as e:
        print(f"Error fetching Asana tasks: {e}")

    return result


def get_project_tasks(
    project_gid: str | None = None,
    only_incomplete: bool = True,
    max_results: int = 20,
) -> list[AsanaTask]:
    """Get tasks from a specific project."""
    import asana

    api_client = get_asana_client()
    tasks_api = asana.TasksApi(api_client)

    gid = project_gid or ASANA_PROJECT_ID

    opts: dict[str, Any] = {
        "project": gid,
        "opt_fields": "name,due_on,completed,assignee.name,notes",
    }
    if only_incomplete:
        opts["completed_since"] = "now"

    result: list[AsanaTask] = []
    try:
        tasks = with_retry(lambda: tasks_api.get_tasks(opts))
        for task_data in tasks:
            if len(result) >= max_results:
                break
            task = _parse_task(task_data)
            if only_incomplete and task.completed:
                continue
            result.append(task)
    except Exception as e:
        print(f"Error fetching project tasks: {e}")

    return result


def search_tasks(
    due_before: date | None = None,
    due_after: date | None = None,
    completed: bool = False,
    max_results: int = 100,
    assignee: str | None = None,
) -> list[AsanaTask]:
    """Search tasks using Asana's server-side Search API.

    Uses /workspaces/{gid}/tasks/search for efficient filtering by due date
    and completion status. Requires Asana Premium.

    Falls back to get_my_tasks() with client-side filtering if search API
    is unavailable (non-premium workspace).
    """
    import asana

    api_client = get_asana_client()
    tasks_api = asana.TasksApi(api_client)

    resolved = resolve_assignee(assignee)

    # v5 SDK: search_tasks_for_workspace(workspace_gid, opts) — opts is positional
    # Dot-notation params (due_on.before, assignee.any) go into opts dict
    opts: dict[str, Any] = {
        "opt_fields": "name,due_on,completed,assignee.name,notes,projects.name",
        "assignee.any": resolved,
        "completed": completed,
    }
    if due_before:
        opts["due_on.before"] = due_before.isoformat()
    if due_after:
        opts["due_on.after"] = due_after.isoformat()

    result: list[AsanaTask] = []
    try:
        tasks = with_retry(
            lambda: tasks_api.search_tasks_for_workspace(
                ASANA_WORKSPACE_ID,
                opts,
            )
        )
        for task_data in tasks:
            if len(result) >= max_results:
                break
            task = _parse_task(task_data)
            result.append(task)
    except Exception as e:
        error_str = str(e)
        if "402" in error_str or "Payment Required" in error_str:
            print("Asana Search API requires Premium — falling back to client-side filtering")
            return _fallback_search(due_before, due_after, completed, max_results, assignee)
        print(f"Error searching Asana tasks: {e}")
    return result


def _fallback_search(
    due_before: date | None,
    due_after: date | None,
    completed: bool,
    max_results: int,
    assignee: str | None = None,
) -> list[AsanaTask]:
    """Client-side filtering fallback if Search API is unavailable."""
    tasks = get_my_tasks(max_results=200, only_incomplete=not completed, assignee=assignee)
    result: list[AsanaTask] = []
    for t in tasks:
        if not t.due_on:
            continue
        if due_before and t.due_on >= due_before:
            continue
        if due_after and t.due_on < due_after:
            continue
        result.append(t)
        if len(result) >= max_results:
            break
    return result


def complete_task(task_gid: str) -> AsanaTask:
    """Mark a task as complete in Asana."""
    import asana

    api_client = get_asana_client()
    tasks_api = asana.TasksApi(api_client)

    body = {"data": {"completed": True}}
    result = with_retry(lambda: tasks_api.update_task(body, task_gid, {}))
    return _parse_task(result)


def create_task(
    name: str,
    due_on: str | None = None,
    assignee: str | None = None,
    project: str | None = None,
    notes: str | None = None,
) -> AsanaTask:
    """Create a new task in Asana.

    Args:
        name: Task name/title.
        due_on: Due date as YYYY-MM-DD string, or None.
        assignee: Friendly name ('sydney'), GID, or None for 'me'.
        project: Project GID to add the task to, or None.
        notes: Task description/notes, or None.
    """
    import asana

    api_client = get_asana_client()
    tasks_api = asana.TasksApi(api_client)

    data: dict[str, Any] = {
        "name": name,
        "workspace": ASANA_WORKSPACE_ID,
        "assignee": resolve_assignee(assignee),
    }
    if due_on:
        data["due_on"] = due_on
    if notes:
        data["notes"] = notes
    if project:
        data["projects"] = [project]

    body = {"data": data}
    opts = {"opt_fields": "name,due_on,completed,assignee.name,notes,projects.name"}
    result = with_retry(lambda: tasks_api.create_task(body, opts))
    return _parse_task(result)


def add_comment(task_gid: str, text: str) -> str:
    """Add a comment (story) to an Asana task.

    Returns the GID of the created comment.
    """
    import asana

    api_client = get_asana_client()
    stories_api = asana.StoriesApi(api_client)

    body = {"data": {"text": text}}
    result = with_retry(lambda: stories_api.create_story_for_task(body, task_gid, {}))

    # Result is a story object — extract what we need
    if isinstance(result, dict):
        return result.get("gid", "")
    elif hasattr(result, "gid"):
        return result.gid
    return str(result)


def move_task(
    task_gid: str,
    to_project: str,
    from_project: str | None = None,
    insert_after: str | None = None,
) -> None:
    """Move a task to a different project (add to new, remove from old).

    Args:
        task_gid: The task to move.
        to_project: Project GID to move the task into.
        from_project: Project GID to remove the task from (optional).
        insert_after: Task GID to insert after for ordering (optional).
    """
    import requests as _requests

    headers = {"Authorization": f"Bearer {ASANA_ACCESS_TOKEN}"}

    # Add to new project
    add_data: dict[str, Any] = {"project": to_project}
    if insert_after:
        add_data["insert_after"] = insert_after
    _requests.post(
        f"https://app.asana.com/api/1.0/tasks/{task_gid}/addProject",
        headers=headers,
        json={"data": add_data},
    ).raise_for_status()

    # Remove from old project
    if from_project:
        _requests.post(
            f"https://app.asana.com/api/1.0/tasks/{task_gid}/removeProject",
            headers=headers,
            json={"data": {"project": from_project}},
        ).raise_for_status()


def get_overdue_tasks(assignee: str | None = None) -> list[AsanaTask]:
    """Get incomplete tasks that are past their due date (server-side filtering)."""
    today = date.today()
    return search_tasks(due_before=today, completed=False, assignee=assignee)


def get_due_soon_tasks(days: int = 3, assignee: str | None = None) -> list[AsanaTask]:
    """Get incomplete tasks due within N days (server-side filtering)."""
    today = date.today()
    deadline = today + timedelta(days=days)
    return search_tasks(due_after=today, due_before=deadline, completed=False, assignee=assignee)


def format_tasks_for_context(tasks: list[AsanaTask]) -> str:
    """Format tasks for inclusion in Claude's context prompt."""
    if not tasks:
        return "No tasks found."

    output: list[str] = []
    for task in tasks:
        due_str = task.due_on.strftime("%Y-%m-%d") if task.due_on else "No due date"

        entry = f"- **{task.name}**"
        entry += f"\n  Due: {due_str}"
        if task.project:
            entry += f" | Project: {task.project}"
        if task.notes:
            entry += f"\n  Notes: {task.notes[:100]}..."

        output.append(entry)

    return "\n\n".join(output)


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Asana integration (v5 SDK)")
    parser.add_argument("command", choices=["my-tasks", "project", "overdue", "due-soon"])
    parser.add_argument("project_gid", nargs="?", default=None, help="Project GID for project cmd")
    parser.add_argument("--max", type=int, default=20)
    parser.add_argument("--days", type=int, default=3)

    args = parser.parse_args()

    if args.command == "my-tasks":
        task_list = get_my_tasks(max_results=args.max)
    elif args.command == "project":
        task_list = get_project_tasks(project_gid=args.project_gid, max_results=args.max)
    elif args.command == "overdue":
        task_list = get_overdue_tasks()
    elif args.command == "due-soon":
        task_list = get_due_soon_tasks(days=args.days)

    print(format_tasks_for_context(task_list))
