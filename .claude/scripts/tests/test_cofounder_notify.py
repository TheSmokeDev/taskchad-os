"""Tests for the gated co-founder Telegram notify path (cofounder/notify.py).

Path map (one test per distinct path, adversarial first):
  - level filter: non-configured level = False, zero HTTP, zero audit rows
  - kill switch disabled = refused + counted + audit row, zero HTTP
  - capability denied = False + audit row, zero HTTP (send-gate refusal)
  - registration: cofounder.notify declared (send, internal-only exposure)
  - missing creds = False + audit row, zero HTTP
  - happy send = True, correct request, audit row with message_id,
    chat_thread round-tripped into the project file frontmatter
  - HTTP error with a REAL token-shaped payload = token redacted from the
    log AND the audit row (Security-test rule)
  - fail-open: stamp failure still True; settings failure False, no raise;
    audit-write failure still True
  - Rule 1: COFOUNDER_NOTIFY_LEVELS resolved from env at call time
  - text stays within the Telegram 4096 limit
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import config
from cofounder import notify as notify_mod
from cofounder import project_model
from security import kill_switches

COFOUNDER_ENV_KEYS = (
    "COFOUNDER_ENABLED",
    "COFOUNDER_PROJECTS_DIR",
    "COFOUNDER_MAX_ITERATIONS",
    "COFOUNDER_MAX_WALL_CLOCK_HOURS",
    "COFOUNDER_MAX_CONCURRENT",
    "COFOUNDER_NOTIFY_LEVELS",
    "COFOUNDER_ZOMBIE_STALE_MINUTES",
    "COFOUNDER_ARCHON_DB",
    "COFOUNDER_WORKFLOW_PROVIDER",
    "COFOUNDER_WORKFLOW_MODEL",
)

# A real Telegram-token-shaped payload (Security-test rule: attack payloads,
# not stubs). Not a live credential.
REAL_SHAPED_TOKEN = "8123456789:AAH9zqXvB0aBcDeFgHiJkLmNoPqRsTuVw-Y"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No COFOUNDER_*/kill-switch/Telegram env leaks from the operator .env
    (config runs load_dotenv(override=True) at import)."""
    for key in COFOUNDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


@pytest.fixture()
def no_http(monkeypatch):
    """Any HTTP attempt fails the test (proves the no-HTTP invariants)."""

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP call attempted; this path must never reach Telegram")

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", forbidden)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_fake_telegram(monkeypatch, *, message_id: int = 4242) -> dict:
    """Capture the outgoing request and answer a canned sendMessage payload."""
    captured: dict = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["params"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return _FakeResponse({"ok": True, "result": {"message_id": message_id}})

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    return captured


def set_creds(monkeypatch, token: str = "tok123", user_ids: str = "555, 666"):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", user_ids)


def make_project(projects_dir: Path, slug: str = "alpha") -> Path:
    fm = {"tags": ["system", "cofounder"], "status": "testing"}
    body = (
        f"# {slug}\n\n"
        "## Spec (STATIC - orchestrator MUST NOT rewrite; only the operator edits)\n"
        f"Build {slug}.\n\n"
        "## Plan / Working Memory (MUTABLE - orchestrator may rewrite)\n"
        f"- [ ] plan {slug}\n\n"
        "## Activity Log (APPEND-ONLY - newest at the bottom)\n"
        "- 2026-07-03T08:00:00 created\n"
    )
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body
    projects_dir.mkdir(parents=True, exist_ok=True)
    path = projects_dir / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    return path


def project_obj(path: Path | None = None, slug: str = "alpha") -> SimpleNamespace:
    return SimpleNamespace(slug=slug, path=path)


def audit_rows(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# === level filter ===


def test_non_configured_level_returns_false_without_http_or_audit(
    tmp_path, monkeypatch, no_http
):
    set_creds(monkeypatch)
    audit = tmp_path / "audit.jsonl"
    ok = notify_mod.notify(project_obj(), "routine progress", "progress", audit_path=audit)
    assert ok is False
    assert audit_rows(audit) == []  # not a send attempt: no audit row


def test_level_matching_is_case_insensitive(tmp_path, monkeypatch):
    set_creds(monkeypatch)
    captured = install_fake_telegram(monkeypatch)
    ok = notify_mod.notify(project_obj(), "all green", "DONE", audit_path=tmp_path / "a.jsonl")
    assert ok is True
    assert captured["params"]["chat_id"] == "555"


def test_notify_levels_env_resolved_at_call_time(tmp_path, monkeypatch, no_http):
    """Rule 1: the level set comes from env at CALL time, no reload needed."""
    set_creds(monkeypatch)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("COFOUNDER_NOTIFY_LEVELS", "done")
    assert notify_mod.notify(project_obj(), "stuck", "blocked", audit_path=audit) is False
    assert audit_rows(audit) == []

    monkeypatch.setenv("COFOUNDER_NOTIFY_LEVELS", "done,blocked")
    captured = install_fake_telegram(monkeypatch)
    assert notify_mod.notify(project_obj(), "stuck", "blocked", audit_path=audit) is True
    assert captured["params"]["chat_id"] == "555"


# === kill switch ===


def test_kill_switch_refuses_send_counts_and_audits(tmp_path, monkeypatch, no_http):
    set_creds(monkeypatch)
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    audit = tmp_path / "audit.jsonl"
    ok = notify_mod.notify(project_obj(), "project done", "done", audit_path=audit)
    assert ok is False
    assert kill_switches.get_refusal_counters()["cofounder"] == 1
    rows = audit_rows(audit)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "refused_killswitch"
    assert rows[0]["level"] == "done"


def test_kill_switch_reenable_allows_next_send_without_restart(tmp_path, monkeypatch):
    set_creds(monkeypatch)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    assert notify_mod.notify(project_obj(), "done", "done", audit_path=audit) is False
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "enabled")
    install_fake_telegram(monkeypatch)
    assert notify_mod.notify(project_obj(), "done", "done", audit_path=audit) is True
    assert kill_switches.get_refusal_counters()["cofounder"] == 1


# === capability gate ===


def test_capability_denied_means_no_http_call(tmp_path, monkeypatch, no_http):
    """The send-gate refusal test: policy deny = zero HTTP + audit row."""
    from integrations import capabilities

    set_creds(monkeypatch)

    def deny(*args, **kwargs):
        raise capabilities.IntegrationPolicyError("blocked by policy")

    monkeypatch.setattr(capabilities, "require_integration_action", deny)
    audit = tmp_path / "audit.jsonl"
    ok = notify_mod.notify(project_obj(), "project done", "done", audit_path=audit)
    assert ok is False
    rows = audit_rows(audit)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    assert "blocked by policy" in rows[0]["error"]


def test_cofounder_notify_action_registered_internal_only():
    from integrations import capabilities

    declared = capabilities.get_integration_action("cofounder", "notify")
    assert declared is not None
    assert declared.effect == "send"
    assert declared.is_mutating
    assert declared.exposures == ("internal",)
    # The gate the module actually calls passes...
    capabilities.require_integration_action(
        "cofounder", "notify", surface="internal", caller="test"
    )
    # ...and a model-facing surface is refused.
    with pytest.raises(capabilities.IntegrationPolicyError, match="not exposed"):
        capabilities.require_integration_action(
            "cofounder", "notify", surface="model", caller="test"
        )


def test_capability_gate_called_before_every_send(tmp_path, monkeypatch):
    from integrations import capabilities

    set_creds(monkeypatch)
    install_fake_telegram(monkeypatch)
    calls: list[tuple] = []
    real = capabilities.require_integration_action

    def spy(*args, **kwargs):
        calls.append((args, kwargs.get("surface")))
        return real(*args, **kwargs)

    monkeypatch.setattr(capabilities, "require_integration_action", spy)
    for _ in range(2):
        assert notify_mod.notify(project_obj(), "x", "done", audit_path=tmp_path / "a.jsonl")
    assert len(calls) == 2
    assert all(surface == "internal" for _, surface in calls)


# === credentials ===


def test_missing_creds_fails_open_with_audit_row(tmp_path, no_http):
    audit = tmp_path / "audit.jsonl"
    ok = notify_mod.notify(project_obj(), "done", "done", audit_path=audit)
    assert ok is False
    rows = audit_rows(audit)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed"
    assert "credentials" in rows[0]["error"]


# === happy path + message_id round-trip ===


def test_send_success_request_audit_and_chat_thread_roundtrip(tmp_path, monkeypatch):
    set_creds(monkeypatch, token="tok123", user_ids="555, 666")
    captured = install_fake_telegram(monkeypatch, message_id=4242)
    audit = tmp_path / "audit.jsonl"
    path = make_project(tmp_path / "projects", "alpha")

    ok = notify_mod.notify(
        project_obj(path), "completion check green; project done", "done", audit_path=audit
    )
    assert ok is True
    assert "bottok123/sendMessage" in captured["url"]
    assert captured["params"]["chat_id"] == "555"  # first allowed id
    assert "alpha" in captured["params"]["text"]
    assert "completion check green" in captured["params"]["text"]

    rows = audit_rows(audit)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "sent"
    assert rows[0]["message_id"] == 4242
    assert rows[0]["project"] == "alpha"

    # message_id round-trip: chat_thread stamped through update_frontmatter.
    parsed = project_model.parse_project_file(path)
    assert parsed.frontmatter.chat_thread == 4242
    # Spec ownership intact: the writer path never touches the body.
    assert "Build alpha." in path.read_text(encoding="utf-8")


def test_text_stays_within_telegram_limit(tmp_path, monkeypatch):
    set_creds(monkeypatch)
    captured = install_fake_telegram(monkeypatch)
    ok = notify_mod.notify(
        project_obj(), "x" * 9000, "done", audit_path=tmp_path / "a.jsonl"
    )
    assert ok is True
    assert len(captured["params"]["text"]) <= notify_mod._TG_TEXT_LIMIT


# === token redaction (Security-test rule: real token-shaped payload) ===


def test_token_redacted_from_log_and_audit_on_http_error(tmp_path, monkeypatch, caplog):
    set_creds(monkeypatch, token=REAL_SHAPED_TOKEN, user_ids="555")

    def boom(req, timeout=10):
        # urllib errors echo the token-bearing request URL.
        raise RuntimeError(
            f"HTTP error for https://api.telegram.org/bot{REAL_SHAPED_TOKEN}/sendMessage"
        )

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", boom)
    audit = tmp_path / "audit.jsonl"
    with caplog.at_level("WARNING"):
        ok = notify_mod.notify(project_obj(), "done", "done", audit_path=audit)
    assert ok is False
    assert REAL_SHAPED_TOKEN not in caplog.text
    assert "***" in caplog.text
    raw_audit = audit.read_text(encoding="utf-8")
    assert REAL_SHAPED_TOKEN not in raw_audit
    rows = audit_rows(audit)
    assert rows[0]["outcome"] == "failed"


# === fail-open seams ===


def test_chat_thread_stamp_failure_does_not_unsend(tmp_path, monkeypatch):
    set_creds(monkeypatch)
    install_fake_telegram(monkeypatch)
    missing = tmp_path / "projects" / "gone.md"  # update_frontmatter will raise
    ok = notify_mod.notify(
        project_obj(missing), "done", "done", audit_path=tmp_path / "a.jsonl"
    )
    assert ok is True


def test_project_without_path_still_sends(tmp_path, monkeypatch):
    set_creds(monkeypatch)
    install_fake_telegram(monkeypatch)
    bare = SimpleNamespace(slug="bare")  # no .path attribute at all
    assert notify_mod.notify(bare, "done", "done", audit_path=tmp_path / "a.jsonl") is True


def test_settings_resolution_failure_never_raises(tmp_path, monkeypatch, no_http):
    def broken(*args, **kwargs):
        raise RuntimeError("config exploded")

    monkeypatch.setattr(config, "get_cofounder_settings", broken)
    ok = notify_mod.notify(project_obj(), "done", "done", audit_path=tmp_path / "a.jsonl")
    assert ok is False


def test_audit_write_failure_does_not_break_the_send(tmp_path, monkeypatch):
    set_creds(monkeypatch)
    install_fake_telegram(monkeypatch)
    audit_dir = tmp_path / "audit-as-dir"
    audit_dir.mkdir()  # open(..., "a") on a directory raises
    ok = notify_mod.notify(project_obj(), "done", "done", audit_path=audit_dir)
    assert ok is True


# === audit record shape ===


def test_audit_record_preview_capped_and_single_line(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit_id = notify_mod.append_notify_audit_record(
        project="alpha",
        level="done",
        outcome="sent",
        text_preview="line one\nline two " + "y" * 200,
        message_id=7,
        audit_path=audit,
    )
    rows = audit_rows(audit)
    assert len(rows) == 1
    assert len(rows[0]["text_preview"]) <= 80
    assert "\n" not in rows[0]["text_preview"]
    assert rows[0]["integration"] == "cofounder"
    assert rows[0]["action"] == "notify"
    assert audit_id.endswith("alpha:done:sent")
