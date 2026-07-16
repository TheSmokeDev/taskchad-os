from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
CONTROLLER = HERE / "fleet_controller.py"


STUB = r'''from __future__ import annotations
import json
import os
import sys
from pathlib import Path

site = os.environ["TOKENMAX_SITE_ID"]
stage = os.environ["TOKENMAX_STAGE"]
result_path = Path(os.environ["TOKENMAX_STAGE_RESULT"])
result_path.parent.mkdir(parents=True, exist_ok=True)

if site == "already-done" and stage == "audit":
    result = {"outcome": "complete_site", "summary": "live proof already satisfies contract"}
    code = 0
elif site == "bad-build" and stage == "build":
    result = {"outcome": "failed", "summary": "fixture build failure"}
    code = 9
elif site == "bad-live" and stage == "live":
    result = {"outcome": "failed", "summary": "fixture live mismatch"}
    code = 10
else:
    result = {"outcome": "passed", "summary": f"{site}:{stage}:ok", "metrics": {"fixture": True}}
    code = 0

result_path.write_text(json.dumps(result), encoding="utf-8")
raise SystemExit(code)
'''


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stub = self.root / "stub.py"
        self.stub.write_text(STUB, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, sites: list[dict[str, object]]) -> Path:
        config = {
            "schema_version": 1,
            "fleet": {
                "id": "fixture-fleet",
                "state_dir": str(self.root / "state"),
                "lock_file": str(self.root / "fleet.lock"),
            },
            "variables": {"python": sys.executable, "stub": str(self.stub)},
            "stages": [
                {
                    "name": "audit",
                    "command": ["{python}", "{stub}"],
                    "failure_policy": "block_site",
                },
                {
                    "name": "build",
                    "command": ["{python}", "{stub}"],
                    "failure_policy": "block_site",
                },
                {
                    "name": "live",
                    "command": ["{python}", "{stub}"],
                    "failure_policy": "freeze_fleet",
                },
            ],
            "sites": sites,
        }
        path = self.root / "fleet.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def run_cli(self, config: Path, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, str(CONTROLLER), "--config", str(config), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        return completed

    def state(self) -> dict[str, object]:
        return json.loads((self.root / "state" / "fleet-state.json").read_text(encoding="utf-8"))

    def test_passes_all_stages_and_completes_site(self) -> None:
        config = self.write_config([{"id": "good", "priority": 1}])
        self.run_cli(config, "run-next")
        state = self.state()
        self.assertEqual(state["sites"]["good"]["status"], "complete")
        self.assertEqual(state["sites"]["good"]["current_stage"], 3)

    def test_complete_site_result_skips_remaining_stages(self) -> None:
        config = self.write_config([{"id": "already-done", "priority": 1}])
        self.run_cli(config, "run-next")
        state = self.state()
        self.assertEqual(state["sites"]["already-done"]["status"], "complete")
        self.assertNotIn("build", state["sites"]["already-done"]["stages"])

    def test_predeploy_failure_blocks_only_site_and_next_run_advances(self) -> None:
        config = self.write_config(
            [
                {"id": "bad-build", "priority": 1},
                {"id": "good", "priority": 2},
            ]
        )
        self.run_cli(config, "run-next")
        state = self.state()
        self.assertEqual(state["sites"]["bad-build"]["status"], "blocked")
        self.assertFalse(state["frozen"])
        self.run_cli(config, "run-next")
        state = self.state()
        self.assertEqual(state["sites"]["good"]["status"], "complete")

    def test_max_sites_continues_after_predeploy_block(self) -> None:
        config = self.write_config(
            [
                {"id": "bad-build", "priority": 1},
                {"id": "good", "priority": 2},
            ]
        )

        self.run_cli(config, "run-next", "--max-sites", "2")

        state = self.state()
        self.assertEqual(state["sites"]["bad-build"]["status"], "blocked")
        self.assertEqual(state["sites"]["good"]["status"], "complete")
        self.assertFalse(state["frozen"])

    def test_run_next_recovers_interrupted_site_at_current_stage(self) -> None:
        config = self.write_config([{"id": "good", "priority": 1}])
        self.run_cli(config, "init")
        state = self.state()
        site = state["sites"]["good"]
        site.update(
            {
                "status": "running",
                "run_id": "interrupted-run",
                "current_stage": 1,
                "stages": {
                    "build": {
                        "status": "running",
                        "attempt": 1,
                        "started_at": "2026-07-12T00:00:00+00:00",
                    }
                },
            }
        )
        (self.root / "state" / "fleet-state.json").write_text(json.dumps(state), encoding="utf-8")

        completed = self.run_cli(config, "run-next")

        self.assertIn("RECOVERED_INTERRUPTED_SITES=good", completed.stdout)
        state = self.state()
        self.assertEqual(state["sites"]["good"]["status"], "complete")
        self.assertEqual(state["sites"]["good"]["run_id"], "interrupted-run")
        self.assertNotIn("audit", state["sites"]["good"]["stages"])
        self.assertEqual(state["sites"]["good"]["stages"]["build"]["status"], "passed")

    def test_live_failure_freezes_fleet(self) -> None:
        config = self.write_config([{"id": "bad-live", "priority": 1}])
        self.run_cli(config, "run-next", expected=2)
        state = self.state()
        self.assertTrue(state["frozen"])
        self.assertIn("bad-live:live", state["freeze_reason"])

    def test_dry_run_does_not_advance_site(self) -> None:
        config = self.write_config([{"id": "good", "priority": 1}])
        completed = self.run_cli(config, "run-next", "--dry-run")
        self.assertIn('"site": "good"', completed.stdout)
        state = self.state()
        self.assertEqual(state["sites"]["good"]["status"], "queued")
        self.assertEqual(state["sites"]["good"]["current_stage"], 0)

    def test_initial_complete_site_is_not_selected(self) -> None:
        config = self.write_config(
            [
                {"id": "already-done", "priority": 1, "initial_status": "complete"},
                {"id": "good", "priority": 2},
            ]
        )
        completed = self.run_cli(config, "run-next", "--dry-run")
        self.assertNotIn('"site": "already-done"', completed.stdout)
        self.assertIn('"site": "good"', completed.stdout)
        state = self.state()
        self.assertEqual(state["sites"]["already-done"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
