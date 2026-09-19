"""Thin Click registration for the Python-owned recall tuning lifecycle."""

from __future__ import annotations

import asyncio
import json

import click


def register_tuning_commands(evolve_group):
    def service_for(persona):
        from personas import get_active_profile_name
        from personas.learning.service import LearningService

        return LearningService.for_persona(persona or get_active_profile_name())

    def emit(action, json_out):
        from personas.learning import operator

        try:
            result = operator._safe(action())
        except Exception as exc:
            message = operator.safe_text(str(exc))
            if json_out:
                click.echo(json.dumps({"success": False, "error": message}))
                raise click.exceptions.Exit(1) from exc
            raise click.ClickException(message) from exc
        click.echo(json.dumps(result, indent=None if json_out else 2))

    @evolve_group.command("tune")
    @click.option("--persona", default=None, help="Canonical profile id; default means main.")
    @click.option(
        "--cases",
        type=click.Path(exists=True, dir_okay=False),
        help="Prepared independent relevance judgments as a JSON array.",
    )
    @click.option(
        "--source-key", default=None, help="Stable import provenance key (required with --cases)."
    )
    @click.option("--json", "json_out", is_flag=True, help="Emit one redacted JSON object.")
    def tune_command(persona, cases, source_key, json_out):
        """Admit labeled cases and queue a guarded recall tuning batch."""
        from evolve import tuning

        def action():
            service = service_for(persona)
            if cases:
                if not source_key:
                    raise click.UsageError("--source-key is required with --cases")
                with open(cases, encoding="utf-8") as stream:
                    tuning.import_validated_cases(service, json.load(stream), source_key=source_key)
            return asyncio.run(tuning.tune(service))

        emit(action, json_out)

    @evolve_group.command("status")
    @click.option("--persona", default=None)
    @click.option("--json", "json_out", is_flag=True)
    def status_command(persona, json_out):
        """Inspect corpus readiness, comparisons and active recall version."""
        from evolve import tuning

        emit(lambda: tuning.tuning_status(service_for(persona)), json_out)

    @evolve_group.command("rollback")
    @click.option("--persona", default=None)
    @click.option("--reason", default="operator request")
    @click.option("--json", "json_out", is_flag=True)
    def rollback_command(persona, reason, json_out):
        """Restore the active recall policy's recorded predecessor."""
        from evolve import tuning

        emit(lambda: tuning.rollback_policy(service_for(persona), reason=reason), json_out)
