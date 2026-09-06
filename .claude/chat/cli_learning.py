"""Learning inspection and controls for ``thehomie profile learning``."""

from __future__ import annotations

import json
from collections.abc import Callable

import click
from personas.learning import operator


def _output(action: Callable[[], dict], json_mode: bool) -> None:
    try:
        result = action()
    except Exception as exc:
        message = operator.safe_text(str(exc))
        if json_mode:
            click.echo(json.dumps({"success": False, "error": message}))
            raise click.exceptions.Exit(1) from None
        raise click.ClickException(message) from None
    click.echo(json.dumps(result, ensure_ascii=False, indent=None if json_mode else 2))


@click.command("summary")
@click.argument("name", default="default")
@click.option("--json", "json_mode", is_flag=True, help="Emit one JSON object.")
def learning_summary(name: str, json_mode: bool) -> None:
    """Show current methods, pending outcomes, and learning status."""
    _output(lambda: operator.get_learning_operator(name).summary(), json_mode)


@click.command("history")
@click.argument("name", default="default")
@click.option("--kind", default=None, help="Record kind, e.g. candidate or evaluation.")
@click.option("--status", default=None)
@click.option("--limit", type=click.IntRange(1, 100), default=30)
@click.option("--cursor", default=None, help="next_cursor from the preceding response.")
@click.option("--json", "json_mode", is_flag=True)
def learning_history(
    name: str,
    kind: str | None,
    status: str | None,
    limit: int,
    cursor: str | None,
    json_mode: bool,
) -> None:
    """Read a page of this persona's learning history."""
    _output(
        lambda: operator.get_learning_operator(name).list_records(
            kind,
            limit=limit,
            cursor=cursor,
            status=status,
        ),
        json_mode,
    )


@click.command("show")
@click.argument("name")
@click.argument("record_id")
@click.option("--json", "json_mode", is_flag=True)
def learning_show(name: str, record_id: str, json_mode: bool) -> None:
    """Inspect a learning record and its linked evidence ids."""
    _output(
        lambda: operator.get_learning_operator(name).get_record(record_id), json_mode
    )


@click.command("pause")
@click.argument("name", default="default")
@click.option("--json", "json_mode", is_flag=True)
def learning_pause(name: str, json_mode: bool) -> None:
    """Pause background learning without deleting learned methods."""
    _output(lambda: operator.get_learning_operator(name).set_paused(True), json_mode)


@click.command("resume")
@click.argument("name", default="default")
@click.option("--json", "json_mode", is_flag=True)
def learning_resume(name: str, json_mode: bool) -> None:
    """Resume background learning; preserve explicit configuration disables."""
    _output(lambda: operator.get_learning_operator(name).set_paused(False), json_mode)


@click.command("rollback")
@click.argument("name")
@click.argument("activation_id")
@click.option("--json", "json_mode", is_flag=True)
def learning_rollback(name: str, activation_id: str, json_mode: bool) -> None:
    """Revert an activated method for future work, preserving all evidence."""
    _output(
        lambda: operator.get_learning_operator(name).rollback(activation_id), json_mode
    )


def register_learning_commands(group: click.Group) -> None:
    for command in (
        learning_summary,
        learning_history,
        learning_show,
        learning_pause,
        learning_resume,
        learning_rollback,
    ):
        group.add_command(command)
