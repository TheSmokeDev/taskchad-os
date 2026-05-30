"""Tests for ``_print_status_human()`` render output -- PRP-1c.

Uses click.testing.CliRunner to invoke ``thehomie status`` (no --json flag)
and assert the new capabilities and toolsets sections are rendered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_CHAT_DIR = str(Path(__file__).parent.parent.parent / "chat")
_SCRIPTS_DIR = str(Path(__file__).parent.parent)
if _CHAT_DIR not in sys.path:
    sys.path.insert(0, _CHAT_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from click.testing import CliRunner  # noqa: E402

from cli import main  # noqa: E402


class TestStatusHumanRender:
    def test_status_json_contains_url_free_browser_envelope(self, monkeypatch):
        from diagnostics import DiagnosticsReport

        def _fake_report():
            return DiagnosticsReport(
                timestamp="2026-05-28T00:00:00",
                uptime_seconds=0.0,
                runtime_providers={"openai-codex": "ON"},
                browser={
                    "enabled": True,
                    "status": "ready",
                    "cdp_port": 9222,
                    "cdp_reachable": True,
                    "browser": "Chrome/126",
                    "visible_guard": "visible",
                    "tab_count": 3,
                    "agent_browser_command_source": "path",
                    "reason": "ready",
                },
            )

        monkeypatch.setattr("diagnostics.collect_diagnostics", _fake_report)

        result = CliRunner().invoke(main, ["status", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["browser"]["status"] == "ready"
        assert payload["browser"]["tab_count"] == 3
        assert "https://" not in json.dumps(payload["browser"])
        assert "tabs" not in payload["browser"]

    def test_doctor_surfaces_browser_attention_without_failing(
        self, monkeypatch,
    ):
        from diagnostics import DiagnosticsReport

        def _fake_report():
            return DiagnosticsReport(
                timestamp="2026-05-28T00:00:00",
                uptime_seconds=0.0,
                runtime_providers={"openai-codex": "ON"},
                memory_embedding_status="ready",
                browser={
                    "enabled": False,
                    "status": "attention",
                    "cdp_port": 9222,
                    "cdp_reachable": False,
                    "browser": "unknown",
                    "visible_guard": "unknown",
                    "tab_count": 0,
                    "agent_browser_command_source": "path",
                    "reason": "connection refused",
                },
            )

        monkeypatch.setattr("diagnostics.check_environment", lambda: [])
        monkeypatch.setattr("diagnostics.collect_diagnostics", _fake_report)

        result = CliRunner().invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "Browser: attention" in result.output
        assert "CDP: unreachable on 9222" in result.output

    def test_status_human_renders_capabilities_section(self):
        """``thehomie status`` (default mode) renders a "Capabilities"
        section header and at least one ``runtime.overlay.`` row.

        Also asserts pipe characters are present in the rendered output
        (PRP-1c §575 pipe-table contract). A render that drops the ``|``
        delimiters would silently regress the markdown-table contract;
        this assertion locks the contract.
        """
        runner = CliRunner()
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0, (
            f"status exited non-zero: {result.exit_code}\n"
            f"Output:\n{result.output}\n"
            f"Exception: {result.exception!r}"
        )
        assert "Capabilities:" in result.output, (
            f"Missing 'Capabilities:' header in output:\n{result.output}"
        )
        assert "runtime.overlay." in result.output, (
            f"Missing runtime.overlay.* rows in output:\n{result.output}"
        )
        # Pipe-table contract guard (PRP-1c §575): each rendered row must
        # carry the ``|`` delimiter. Stripping pipes would silently break
        # the markdown-table format.
        assert "|" in result.output, (
            f"Missing pipe-table delimiters in output:\n{result.output}"
        )

    def test_status_human_renders_toolsets_section(self):
        """``thehomie status`` renders a "Toolsets" section header and
        the four PRP-1a/1b toolset names."""
        runner = CliRunner()
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "Toolsets:" in result.output, (
            f"Missing 'Toolsets:' header in output:\n{result.output}"
        )
        # chat_all is the always-present diamond toolset from PRP-1a; locks
        # the Toolsets render content.
        assert "chat_all" in result.output, (
            f"Missing chat_all in toolsets output:\n{result.output}"
        )

    def test_capabilities_render_width_stays_under_120_chars(self):
        """Width regression guard (per M4): future contributors who tighten
        or widen column widths must not silently push past the 120-char
        terminal target. Lines containing ``runtime_overlay`` (the source
        column for runtime overlay caps) must each be <= 120 chars."""
        runner = CliRunner()
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        capability_lines = [
            line for line in result.output.splitlines()
            if "runtime_overlay" in line
        ]

        # We expect at least one runtime_overlay row -- the test would be
        # vacuous otherwise.
        assert len(capability_lines) > 0, (
            f"No runtime_overlay rows rendered:\n{result.output}"
        )
        for line in capability_lines:
            assert len(line) <= 120, (
                f"Capability line exceeds 120 chars ({len(line)}): {line!r}"
            )

    def test_no_breakage_on_empty_capabilities(self, monkeypatch):
        """PRP §550: Empty-state guard. If collect_diagnostics() returns a
        DiagnosticsReport with capabilities=[] and toolsets={}, the render
        must NOT emit Capabilities/Toolsets section headers. Exit code stays 0.
        """
        from diagnostics import DiagnosticsReport

        def _empty_report():
            return DiagnosticsReport(
                timestamp="2026-04-27T00:00:00",
                uptime_seconds=0.0,
            )

        # Patch at the source module: cli.py uses a local ``from diagnostics
        # import collect_diagnostics`` inside the status() function, so the
        # name resolves against the diagnostics module on every call. Patching
        # ``diagnostics.collect_diagnostics`` is the correct seam.
        monkeypatch.setattr("diagnostics.collect_diagnostics", _empty_report)

        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, (
            f"status exited non-zero: {result.exit_code}\n"
            f"Output:\n{result.output}\n"
            f"Exception: {result.exception!r}"
        )
        assert "\nCapabilities:" not in result.output, (
            f"Capabilities header rendered for empty caps:\n{result.output}"
        )
        assert "\nToolsets:" not in result.output, (
            f"Toolsets header rendered for empty toolsets:\n{result.output}"
        )

    def test_capabilities_render_truncates_long_strings(self, monkeypatch):
        """PRP §575 + R1 M4: column widths cap id at 40, display_name at 22,
        source at 16. Inject a fake long capability via monkeypatch and
        assert exact truncation in the rendered output. Without this test
        the existing width-under-120 assertion is vacuous against truncation
        regressions because live capabilities sit below the caps.
        """
        from diagnostics import DiagnosticsReport

        long_cap = {
            "id": "runtime.overlay." + "x" * 50,            # 65 chars > 40 cap
            "display_name": "Long Display Name " * 5,       # 90 chars > 22 cap
            "enabled": True,
            "source": "runtime_overlay_with_extra_padding",  # 35 chars > 16 cap
        }

        def _fake_report():
            return DiagnosticsReport(
                timestamp="2026-04-27T00:00:00",
                uptime_seconds=0.0,
                capabilities=[long_cap],
                toolsets={},
            )

        monkeypatch.setattr("diagnostics.collect_diagnostics", _fake_report)
        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, (
            f"status exited non-zero: {result.exit_code}\n"
            f"Output:\n{result.output}\n"
            f"Exception: {result.exception!r}"
        )

        # The id column caps at 40 chars; original was 65, truncated should be 40.
        rendered_id = ("runtime.overlay." + "x" * 50)[:40]  # 40 chars total
        assert rendered_id in result.output, (
            f"id truncation to 40 chars failed; output: {result.output[-500:]}"
        )

        # display_name caps at 22.
        rendered_name_truncated = ("Long Display Name " * 5)[:22]
        assert rendered_name_truncated in result.output, (
            f"display_name truncation to 22 chars failed:\n{result.output}"
        )

        # source caps at 16.
        rendered_source = ("runtime_overlay_with_extra_padding")[:16]
        assert rendered_source in result.output, (
            f"source truncation to 16 chars failed:\n{result.output}"
        )

        # Width regression guard (kept from old test): no line over 120 chars
        # for the lines that actually rendered the injected capability.
        capability_lines = [
            line for line in result.output.splitlines() if rendered_id in line
        ]
        assert len(capability_lines) > 0, (
            f"injected capability did not render:\n{result.output}"
        )
        for line in capability_lines:
            assert len(line) <= 120, (
                f"line {line!r} exceeds 120 chars ({len(line)})"
            )
