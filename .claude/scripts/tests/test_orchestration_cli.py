"""CLI integration tests for convoy + mailbox commands.

Uses Click's CliRunner — no HTTP, no GUI, pure Python.
Proves GUI-off operation for Phase 0-2.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# Ensure chat dir is on path for cli imports
_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cli import main  # noqa: E402


@pytest.fixture
def runner(tmp_path):
    """Each test gets an isolated orchestration DB via tmp_path."""
    db_path = tmp_path / "test_orch.db"
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        yield CliRunner()


# ── Convoy CLI ─────────────────────────────────────────────────────────────


def test_convoy_create(runner):
    r = runner.invoke(main, ["convoy", "create", "-t", "Test Convoy", "--by", "sb"])
    assert r.exit_code == 0
    assert "Created convoy #" in r.output
    assert "Test Convoy" in r.output


def test_convoy_create_json(runner):
    r = runner.invoke(main, ["convoy", "create", "-t", "JSON Test", "--by", "sb", "--json"])
    assert r.exit_code == 0
    import json
    data = json.loads(r.output)
    assert data["title"] == "JSON Test"
    assert data["status"] == "draft"


def test_convoy_list(runner):
    runner.invoke(main, ["convoy", "create", "-t", "List1", "--by", "sb"])
    runner.invoke(main, ["convoy", "create", "-t", "List2", "--by", "sb"])
    r = runner.invoke(main, ["convoy", "list"])
    assert r.exit_code == 0
    assert "List1" in r.output
    assert "List2" in r.output


def test_convoy_list_empty(runner):
    r = runner.invoke(main, ["convoy", "list"])
    assert r.exit_code == 0
    assert "No convoys" in r.output


def test_convoy_show(runner):
    runner.invoke(main, ["convoy", "create", "-t", "Show Test", "--by", "sb"])
    r = runner.invoke(main, ["convoy", "show", "1"])
    assert r.exit_code == 0
    assert "Show Test" in r.output
    assert "Status: draft" in r.output


def test_convoy_show_not_found(runner):
    r = runner.invoke(main, ["convoy", "show", "9999"])
    assert r.exit_code == 1


def test_convoy_add_task(runner):
    runner.invoke(main, ["convoy", "create", "-t", "Add Task Test", "--by", "sb"])
    r = runner.invoke(main, ["convoy", "add-task", "1", "-t", "New Subtask"])
    assert r.exit_code == 0
    assert "Added subtask" in r.output
    assert "New Subtask" in r.output


def test_convoy_dispatch(runner):
    runner.invoke(main, ["convoy", "create", "-t", "Dispatch", "--by", "sb"])
    runner.invoke(main, ["convoy", "add-task", "1", "-t", "Dispatchable"])
    r = runner.invoke(main, ["convoy", "dispatch", "1"])
    assert r.exit_code == 0
    assert "Dispatched" in r.output


def test_convoy_complete(runner):
    runner.invoke(main, ["convoy", "create", "-t", "Complete", "--by", "sb"])
    runner.invoke(main, ["convoy", "add-task", "1", "-t", "Completable"])
    runner.invoke(main, ["convoy", "dispatch", "1"])
    r = runner.invoke(main, ["convoy", "complete", "1"])
    assert r.exit_code == 0
    assert "Completed" in r.output
    assert "Convoy completed!" in r.output


def test_convoy_fail(runner):
    runner.invoke(main, ["convoy", "create", "-t", "Fail", "--by", "sb"])
    runner.invoke(main, ["convoy", "add-task", "1", "-t", "Failable"])
    runner.invoke(main, ["convoy", "dispatch", "1"])
    r = runner.invoke(main, ["convoy", "fail", "1", "-e", "test error"])
    assert r.exit_code == 0
    assert "Failed" in r.output


def test_convoy_cancel(runner):
    runner.invoke(main, ["convoy", "create", "-t", "Cancel", "--by", "sb"])
    r = runner.invoke(main, ["convoy", "cancel", "1"])
    assert r.exit_code == 0
    assert "Cancelled" in r.output


# ── Mailbox CLI ────────────────────────────────────────────────────────────


def test_mailbox_send(runner):
    r = runner.invoke(main, [
        "mailbox", "send", "--from", "sb", "--to", "agent-b", "-b", "Hello",
    ])
    assert r.exit_code == 0
    assert "Sent message" in r.output
    assert "agent-b" in r.output


def test_mailbox_inbox(runner):
    runner.invoke(main, [
        "mailbox", "send", "--from", "sb", "--to", "agent-b", "-b", "Check inbox",
    ])
    r = runner.invoke(main, ["mailbox", "inbox", "agent-b"])
    assert r.exit_code == 0
    assert "Check inbox" in r.output


def test_mailbox_inbox_empty(runner):
    r = runner.invoke(main, ["mailbox", "inbox", "nobody"])
    assert r.exit_code == 0
    assert "No pending" in r.output


def test_mailbox_claim(runner):
    runner.invoke(main, [
        "mailbox", "send", "--from", "sb", "--to", "agent-b", "-b", "Claim me",
    ])
    r = runner.invoke(main, ["mailbox", "claim", "agent-b"])
    assert r.exit_code == 0
    assert "Claimed 1" in r.output
    assert "claim_token=" in r.output


def test_mailbox_ack(runner):
    import json

    runner.invoke(main, [
        "mailbox", "send", "--from", "sb", "--to", "agent-b", "-b", "Ack me",
    ])
    claim = runner.invoke(main, ["mailbox", "claim", "agent-b", "--json"])
    claimed = json.loads(claim.output)
    agent_b_delivery = [d for d in claimed[0]["deliveries"] if d["recipient_agent"] == "agent-b"][0]
    r = runner.invoke(
        main,
        [
            "mailbox", "ack", str(agent_b_delivery["id"]),
            "--agent", "agent-b",
            "--claim-token", agent_b_delivery["claim_token"],
        ],
    )
    assert r.exit_code == 0
    assert "Acknowledged" in r.output


def test_mailbox_send_json(runner):
    r = runner.invoke(main, [
        "mailbox", "send", "--from", "sb", "--to", "agent-b", "-b", "JSON",
        "--json",
    ])
    assert r.exit_code == 0
    import json
    data = json.loads(r.output)
    assert data["from_agent"] == "sb"
    assert data["body"] == "JSON"


# ── Full Lifecycle (GUI-off Proof) ─────────────────────────────────────────


def test_full_lifecycle_cli_only(runner):
    """Proves convoy + mailbox works from CLI alone, no GUI, no HTTP.

    Flow: create -> add tasks -> dispatch -> complete -> convoy completes
          + send mailbox -> claim -> ack
    """
    # Create convoy
    r = runner.invoke(main, ["convoy", "create", "-t", "E2E CLI", "--by", "sb"])
    assert r.exit_code == 0

    # Add subtask
    r = runner.invoke(main, ["convoy", "add-task", "1", "-t", "Step 1"])
    assert r.exit_code == 0

    # Dispatch
    r = runner.invoke(main, ["convoy", "dispatch", "1"])
    assert r.exit_code == 0

    # Show (should be active)
    r = runner.invoke(main, ["convoy", "show", "1"])
    assert "active" in r.output

    # Complete
    r = runner.invoke(main, ["convoy", "complete", "1"])
    assert r.exit_code == 0
    assert "Convoy completed!" in r.output

    # Show (should be completed)
    r = runner.invoke(main, ["convoy", "show", "1"])
    assert "completed" in r.output

    # Send mailbox message
    r = runner.invoke(main, [
        "mailbox", "send", "--from", "sb", "--to", "agent-b",
        "-b", "E2E mailbox test",
    ])
    assert r.exit_code == 0

    # Claim
    import json

    r = runner.invoke(main, ["mailbox", "claim", "agent-b", "--json"])
    claimed = json.loads(r.output)
    agent_b_delivery = [d for d in claimed[0]["deliveries"] if d["recipient_agent"] == "agent-b"][0]
    assert len(claimed) == 1

    # Ack
    r = runner.invoke(
        main,
        [
            "mailbox", "ack", str(agent_b_delivery["id"]),
            "--agent", "agent-b",
            "--claim-token", agent_b_delivery["claim_token"],
        ],
    )
    assert "Acknowledged" in r.output

    # Inbox is empty
    r = runner.invoke(main, ["mailbox", "inbox", "agent-b"])
    assert "No pending" in r.output
