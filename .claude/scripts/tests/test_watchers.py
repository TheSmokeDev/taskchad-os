"""Tests for the watchers toolkit (Hermes port) — watermark contract,
feed parsing, JSON digging, GitHub flattening.

No network: every test drives pure functions or the state file on disk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Watcher scripts live in their own dir and self-insert it on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "watchers"))

import watch_github
import watch_http_json
import watch_rss
from _watermark import Watermark, format_items_as_markdown


@pytest.fixture()
def state_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WATCHER_STATE_DIR", str(tmp_path))
    return tmp_path


# ── Watermark contract ─────────────────────────────────────


def test_first_run_records_baseline_and_emits_nothing(state_dir: Path):
    wm = Watermark.load("feed")
    items = [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]

    assert wm.is_first_run is True
    assert wm.filter_new(items) == []
    wm.save()

    persisted = json.loads((state_dir / "feed.json").read_text(encoding="utf-8"))
    assert persisted["seen_ids"] == ["1", "2"]
    assert persisted["first_run"] is False


def test_second_run_emits_only_new_items(state_dir: Path):
    wm = Watermark.load("feed")
    wm.filter_new([{"id": "1"}])
    wm.save()

    wm2 = Watermark.load("feed")
    new = wm2.filter_new([{"id": "1"}, {"id": "2", "title": "new"}])
    assert new == [{"id": "2", "title": "new"}]
    wm2.save()

    wm3 = Watermark.load("feed")
    assert wm3.filter_new([{"id": "1"}, {"id": "2"}]) == []


def test_seen_set_is_bounded_to_max_seen(state_dir: Path):
    wm = Watermark.load("feed", max_seen=5)
    wm.filter_new([{"id": str(i)} for i in range(10)])
    # Keeps the most recent tail.
    assert wm.seen == ["5", "6", "7", "8", "9"]


def test_corrupt_state_file_recovers_as_first_run(state_dir: Path):
    (state_dir / "feed.json").write_text("{not json", encoding="utf-8")
    wm = Watermark.load("feed")
    assert wm.is_first_run is True
    # First-run semantics: baseline, no replay.
    assert wm.filter_new([{"id": "1"}]) == []


def test_items_missing_id_key_are_skipped(state_dir: Path):
    wm = Watermark.load("feed")
    wm.filter_new([{"id": "1"}])
    new = wm.filter_new([{"title": "no id"}, {"id": "2"}])
    assert new == [{"id": "2"}]


def test_invalid_watermark_name_rejected(state_dir: Path):
    with pytest.raises(ValueError):
        Watermark("../evil")
    with pytest.raises(ValueError):
        Watermark("")


def test_save_leaves_no_tmp_file(state_dir: Path):
    wm = Watermark.load("feed")
    wm.filter_new([{"id": "1"}])
    wm.save()
    assert (state_dir / "feed.json").exists()
    assert not (state_dir / "feed.tmp").exists()


# ── Markdown rendering ([SILENT] contract) ─────────────────


def test_format_empty_items_is_empty_string():
    assert format_items_as_markdown([]) == ""


def test_format_renders_title_url_and_truncated_body():
    items = [{"title": "T", "url": "https://x", "body": "y" * 600}]
    out = format_items_as_markdown(items, body_key="body", max_body_chars=500)
    assert out.startswith("## T\nhttps://x\n")
    assert "…" in out
    assert len(out) < 620


# ── RSS/Atom parsing ───────────────────────────────────────

RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><guid>g1</guid><title>First</title><link>https://a/1</link>
    <description>d1</description></item>
  <item><link>https://a/2</link><title>GuidFallsBackToLink</title></item>
  <item><title>NoGuidNoLink</title></item>
</channel></rss>"""

ATOM_XML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>a1</id><title>AtomOne</title>
    <link href="https://b/1"/><summary>s1</summary></entry>
</feed>"""


def test_parse_feed_rss_guid_and_link_fallback():
    entries = watch_rss._parse_feed(RSS_XML)
    assert [e["id"] for e in entries] == ["g1", "https://a/2"]
    assert entries[0] == {
        "id": "g1", "title": "First", "url": "https://a/1", "summary": "d1"
    }


def test_parse_feed_atom_namespaced():
    entries = watch_rss._parse_feed(ATOM_XML)
    assert entries == [
        {"id": "a1", "title": "AtomOne", "url": "https://b/1", "summary": "s1"}
    ]


def test_parse_feed_invalid_xml_exits_2():
    with pytest.raises(SystemExit) as exc:
        watch_rss._parse_feed(b"<not-xml")
    assert exc.value.code == 2


# ── JSON endpoint helpers ──────────────────────────────────


def test_dig_dotted_path_and_missing():
    assert watch_http_json._dig({"a": {"b": [1, 2]}}, "a.b") == [1, 2]
    assert watch_http_json._dig({"a": {}}, "a.b") is None
    assert watch_http_json._dig({"a": 1}, "") == {"a": 1}


def test_parse_header_valid_and_invalid():
    assert watch_http_json._parse_header("X-Key: v") == ("X-Key", "v")
    with pytest.raises(Exception):
        watch_http_json._parse_header("no-colon")


# ── GitHub flattening ──────────────────────────────────────


def test_flatten_commit_nested_fields():
    item = {
        "sha": "abc123",
        "html_url": "https://gh/c/abc123",
        "author": {"login": "smoke"},
        "commit": {
            "message": "fix: title line\n\nbody line",
            "author": {"name": "Smoke", "date": "2026-07-03"},
        },
    }
    flat = watch_github._flatten_commit(item)
    assert flat["id"] == "abc123"
    assert flat["title"] == "fix: title line  (smoke)"
    assert flat["body"] == "body line"
    assert flat["created_at"] == "2026-07-03"


def test_flatten_issue_uses_id_title_user():
    item = {
        "id": 42,
        "title": "Bug",
        "html_url": "https://gh/i/42",
        "body": " b ",
        "state": "open",
        "user": {"login": "smoke"},
        "created_at": "2026-07-01",
    }
    flat = watch_github._flatten_issue_or_release(item)
    assert flat == {
        "id": "42", "title": "Bug", "url": "https://gh/i/42", "body": "b",
        "state": "open", "author": "smoke", "created_at": "2026-07-01",
    }
