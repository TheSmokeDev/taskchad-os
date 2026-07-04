"""Tests for orchestration/blueprint_catalog.py — one test per code path.

Covers the ported renderers + fill/validate + the two Homie re-anchors:
  * every CATALOG default fill resolves to a 5-field cron (the ``/api/scheduled``
    guard ``_validate_cron`` regex) — the load-bearing invariant;
  * ``scheduled_kwargs_from_spec`` maps the Hermes-shaped spec to the
    ``/api/scheduled`` create body (drops name/deliver, no next_run).

Pure module — no state/store, no chat imports.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.blueprint_catalog import (  # noqa: E402
    CATALOG,
    AutomationBlueprint,
    BlueprintFillError,
    BlueprintSlot,
    blueprint_catalog_entry,
    blueprint_form_schema,
    blueprint_slash_command,
    fill_blueprint,
    get_blueprint,
    scheduled_kwargs_from_spec,
)

# The exact regex the /api/scheduled guard uses (dashboard_api._CRON_RE).
# Copied literally so this orchestration test stays pure (no FastAPI import).
_CRON_RE = re.compile(r"^[\d\*\-\,\/]+( +[\d\*\-\,\/]+){4}$")

_EXPECTED_KEYS = [
    "morning-brief",
    "important-mail",
    "weekly-review",
    "workday-start",
    "bill-renewal-watch",
    "vault-sweep",
    "news-digest",
    "custom-reminder",
]


# --------------------------------------------------------------------------- #
# Catalog shape
# --------------------------------------------------------------------------- #

def test_catalog_has_eight_expected_blueprints() -> None:
    assert [b.key for b in CATALOG] == _EXPECTED_KEYS
    assert len(CATALOG) == 8


def test_get_blueprint_hit_and_miss() -> None:
    assert get_blueprint("morning-brief") is CATALOG[0]
    assert get_blueprint("does-not-exist") is None


def test_slot_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown slot type"):
        BlueprintSlot(name="x", type="datetime", label="X")


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #

def test_form_schema_shape() -> None:
    schema = blueprint_form_schema(get_blueprint("morning-brief"))
    assert schema["key"] == "morning-brief"
    assert {f["name"] for f in schema["fields"]} == {"time", "deliver"}
    time_field = next(f for f in schema["fields"] if f["name"] == "time")
    assert time_field["type"] == "time"
    assert time_field["default"] == "08:00"


def test_slash_command_uses_plural_prefix_and_quotes_text() -> None:
    # Homie re-anchor: the command prefix is the plural ``/blueprints``.
    cmd = blueprint_slash_command(get_blueprint("custom-reminder"))
    assert cmd.startswith("/blueprints custom-reminder ")
    # Free-text default carries spaces -> must be quoted.
    assert 'what="take a break and stretch"' in cmd


def test_catalog_entry_has_command_and_human_schedule_no_appurl() -> None:
    entry = blueprint_catalog_entry(get_blueprint("morning-brief"))
    assert entry["schedule"] == "{minute} {hour} * * *"
    assert entry["scheduleHuman"] == "daily at 08:00"
    assert entry["command"].startswith("/blueprints morning-brief")
    # hermes:// deep-link was dropped in the Homie port.
    assert "appUrl" not in entry


# --------------------------------------------------------------------------- #
# fill_blueprint — validation paths
# --------------------------------------------------------------------------- #

def test_fill_happy_path_returns_spec() -> None:
    bp = get_blueprint("morning-brief")
    spec = fill_blueprint(bp, {"time": "07:15", "deliver": "origin"})
    assert spec["schedule"] == "15 7 * * *"
    assert spec["name"] == "Morning briefing"
    assert spec["deliver"] == "origin"
    assert "briefing" in spec["prompt"].lower()


def test_fill_unknown_slot_is_named_and_rejected() -> None:
    bp = get_blueprint("morning-brief")
    with pytest.raises(BlueprintFillError, match="unknown slot.*tiem"):
        fill_blueprint(bp, {"tiem": "07:15"})


def test_fill_missing_required_slot_rejected() -> None:
    # custom-reminder requires ``time`` (no default gap when blanked).
    bp = get_blueprint("custom-reminder")
    with pytest.raises(BlueprintFillError, match="missing required value: time"):
        fill_blueprint(bp, {"time": ""})


def test_fill_enum_out_of_options_rejected() -> None:
    bp = get_blueprint("weekly-review")
    with pytest.raises(BlueprintFillError, match="day=.*not allowed"):
        fill_blueprint(bp, {"day": "caturday"})


def test_fill_non_strict_enum_accepts_free_value() -> None:
    # ``deliver`` is strict=False -> a free platform name passes.
    bp = get_blueprint("morning-brief")
    spec = fill_blueprint(bp, {"time": "08:00", "deliver": "matrix"})
    assert spec["deliver"] == "matrix"


# --------------------------------------------------------------------------- #
# _resolve_schedule branches (via fill_blueprint)
# --------------------------------------------------------------------------- #

def test_resolve_time_to_minute_hour() -> None:
    spec = fill_blueprint(get_blueprint("morning-brief"), {"time": "09:05"})
    assert spec["schedule"] == "5 9 * * *"


def test_resolve_recurrence_to_dow() -> None:
    spec = fill_blueprint(
        get_blueprint("custom-reminder"),
        {"time": "14:00", "recurrence": "weekends"},
    )
    assert spec["schedule"] == "0 14 * * 0,6"


def test_resolve_day_to_dow() -> None:
    spec = fill_blueprint(
        get_blueprint("weekly-review"), {"time": "18:00", "day": "monday"}
    )
    assert spec["schedule"] == "0 18 * * 1"


def test_resolve_interval_to_step() -> None:
    spec = fill_blueprint(get_blueprint("important-mail"), {"interval_min": "15"})
    assert spec["schedule"] == "*/15 * * * *"


def test_resolve_bad_time_rejected() -> None:
    with pytest.raises(BlueprintFillError, match="invalid time"):
        fill_blueprint(get_blueprint("morning-brief"), {"time": "25:99"})


# --------------------------------------------------------------------------- #
# The load-bearing invariant: every default fill is a 5-field cron
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("blueprint", CATALOG, ids=[b.key for b in CATALOG])
def test_every_default_fill_is_five_field_cron(blueprint: AutomationBlueprint) -> None:
    spec = fill_blueprint(blueprint, {})
    assert _CRON_RE.match(spec["schedule"]), (
        f"{blueprint.key} default fill produced non-5-field cron "
        f"{spec['schedule']!r} — /api/scheduled would 422 it"
    )


# --------------------------------------------------------------------------- #
# scheduled_kwargs_from_spec — the Homie adapter
# --------------------------------------------------------------------------- #

def test_scheduled_kwargs_drops_name_and_deliver() -> None:
    spec = fill_blueprint(get_blueprint("morning-brief"), {"time": "08:00"})
    kw = scheduled_kwargs_from_spec(spec)
    assert kw == {
        "persona_id": "default",
        "prompt": spec["prompt"],
        "schedule": spec["schedule"],
        "next_run": None,
    }
    assert "name" not in kw and "deliver" not in kw


def test_scheduled_kwargs_honors_persona_id() -> None:
    spec = fill_blueprint(get_blueprint("morning-brief"), {"time": "08:00"})
    kw = scheduled_kwargs_from_spec(spec, persona_id="sales")
    assert kw["persona_id"] == "sales"
