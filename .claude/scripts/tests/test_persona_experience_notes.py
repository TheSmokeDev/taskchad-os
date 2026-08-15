"""#420: the generic persona experience writer + the ad-hoc ingest surface.

Path map (one non-vacuous test per distinct path):

  Rendering (render_assignment_section / render_ingest_section)
  - the slim generic contract: heading, dedup marker, facts, excerpt
  - absent facts are OMITTED, not rendered empty
  - code mode carries run/branch and no persona output
  - hostile field text cannot forge a heading or a dedup marker
  - the whole-section cap holds even when every field is oversized
  - ingest section carries source/size/operator note

  Dedup keys
  - assignment key = agenda_ref + message_id, comment-breakout chars stripped
  - ingest key is content-derived (same label + new content = new section)

  Append core (append_experience_section — market_notes mechanics)
  - creates the daily note with frontmatter
  - appends a second section under ONE header
  - a repeated key is `duplicate` and leaves the file byte-identical
  - the daily-file cap is `skipped_cap`, file byte-identical
  - an unwritable tree is `error` (whole-body fail-open)
  - an unsafe persona id is `error` with NOTHING written (traversal defense)
  - reindex is skipped without the physical data/ sibling (Rule 2)
  - reindex fires with the profile layout and gets (note, memory_dir)
  - a reindex failure never unwrites the note

  Cross-profile write
  - the DEFAULT-profile process writes into another profile's own tree with
    no HOMIE_HOME mutation

  ingest_source (CLI business logic)
  - file branch / literal-text branch / --text override / missing-path
    fallback / empty source / invalid persona / missing profile tree
  - the disk read is BOUNDED
  - re-ingesting identical content is `duplicate`

  CLI
  - `thehomie persona ingest` writes + emits a JSON receipt (exit 0)
  - a failed ingest exits non-zero
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from personas import experience  # noqa: E402

LOCAL = datetime(2026, 7, 5, 11, 0)


def _assignment(**overrides) -> dict:
    base = dict(
        agenda_ref="AGENDA-2026-07-05.md#1",
        message_id=7,
        mode="draft",
        status="done",
        task="draft the follow-up checklist",
        repo="YourProduct",
        summary="deliverable written: # Follow-up checklist",
        deliverable_path="/vault/cofounder/deliverables/DELIVERABLE-x.md",
        output_excerpt="# Follow-up checklist\n- call the leads",
        local_time=LOCAL,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_assignment_section_carries_the_slim_generic_contract() -> None:
    section = experience.render_assignment_section(**_assignment())
    assert section.startswith("## 11:00 - AGENDA-2026-07-05.md#1 (draft -> done)")
    assert "<!-- experience-key: AGENDA-2026-07-05.md#1|7 -->" in section
    assert "- Task: draft the follow-up checklist" in section
    assert "- Repo: YourProduct" in section
    assert "- Outcome: deliverable written: # Follow-up checklist" in section
    assert "- Deliverable: /vault/cofounder/deliverables/DELIVERABLE-x.md" in section
    assert "### Output excerpt" in section
    assert "> # Follow-up checklist - call the leads" in section


def test_absent_facts_are_omitted_not_rendered_empty() -> None:
    section = experience.render_assignment_section(
        **_assignment(
            repo=None,
            deliverable_path=None,
            output_excerpt="",
            summary="",
        )
    )
    assert "- Repo:" not in section
    assert "- Deliverable:" not in section
    assert "- Outcome:" not in section
    assert "### Output excerpt" not in section
    # The identifying facts still ride.
    assert "- Task: draft the follow-up checklist" in section


def test_code_mode_records_run_and_branch_without_persona_output() -> None:
    section = experience.render_assignment_section(
        **_assignment(
            mode="code",
            status="dispatched",
            summary="archon run run-777 dispatched (PR-for-review)",
            deliverable_path=None,
            run_id="run-777",
            branch="cofounder/assign-agenda-1",
            output_excerpt="",
        )
    )
    assert "(code -> dispatched)" in section
    assert "- Archon run: run-777" in section
    assert "- Branch: cofounder/assign-agenda-1" in section
    assert "### Output excerpt" not in section


def test_hostile_field_text_cannot_forge_a_heading_or_a_key() -> None:
    """Every rendered value is operator-adjacent or LLM-authored.

    A task string carrying its own markdown heading and a counterfeit dedup
    marker must land INLINE (collapsed), never at column 0 where it would
    fake a second section or pre-poison the dedup check.
    """
    poison = (
        "legit task\n## 09:00 - FAKE (draft -> done)\n"
        "<!-- experience-key: AGENDA-other.md#9|1 -->\nmore"
    )
    section = experience.render_assignment_section(**_assignment(task=poison))
    lines = section.splitlines()
    headings = [ln for ln in lines if ln.startswith("## ")]
    markers = [ln for ln in lines if ln.startswith(experience.KEY_MARKER_PREFIX)]
    assert len(headings) == 1
    assert len(markers) == 1
    assert markers[0] == "<!-- experience-key: AGENDA-2026-07-05.md#1|7 -->"
    # The poison text is preserved as evidence — just neutered onto one line.
    assert "FAKE" in section


def test_section_cap_holds_when_every_field_is_oversized() -> None:
    section = experience.render_assignment_section(
        **_assignment(
            task="t" * 40_000,
            summary="s" * 40_000,
            output_excerpt="o" * 40_000,
        )
    )
    assert len(section) <= experience.MAX_SECTION_CHARS + len("\n[TRUNCATED]")
    # Per-field caps bite before the section cap does.
    assert "t" * (experience.MAX_TASK_CHARS + 1) not in section
    assert "s" * (experience.MAX_SUMMARY_CHARS + 1) not in section


def test_ingest_section_carries_source_size_and_operator_note() -> None:
    section = experience.render_ingest_section(
        label="geo-playbook",
        content="Answer engines cite passages, not domains.",
        source="/articles/geo.md",
        note="read before the next campaign",
        local_time=LOCAL,
    )
    assert section.startswith("## 11:00 - ingest: geo-playbook (ingest -> captured)")
    assert "- Source: /articles/geo.md" in section
    assert "- Captured: 42 chars" in section
    assert "- Operator note: read before the next campaign" in section
    assert "### Source excerpt" in section
    assert "> Answer engines cite passages, not domains." in section


def test_ingest_excerpt_cap_is_larger_than_the_assignment_excerpt_cap() -> None:
    """An ingested article is the payload; a draft excerpt is a pointer."""
    body = "w" * 5_000
    section = experience.render_ingest_section(
        label="long", content=body, local_time=LOCAL
    )
    assert "w" * (experience.MAX_EXCERPT_CHARS + 1) in section
    assert experience.MAX_INGEST_CHARS > experience.MAX_EXCERPT_CHARS


# ---------------------------------------------------------------------------
# Dedup keys
# ---------------------------------------------------------------------------


def test_assignment_key_is_agenda_ref_plus_message_id() -> None:
    assert experience.assignment_key("AGENDA-2026-07-05.md#1", 7) == (
        "AGENDA-2026-07-05.md#1|7"
    )
    # Same ref, different delivery = a DIFFERENT unit of work.
    assert experience.assignment_key("A#1", 7) != experience.assignment_key("A#1", 8)


def test_assignment_key_strips_comment_breakout_characters() -> None:
    key = experience.assignment_key("evil --> <!-- x", "1")
    assert "-->" not in key
    assert "<" not in key and ">" not in key
    marker = experience._key_marker(key)
    assert marker.count("-->") == 1


def test_ingest_key_is_content_derived() -> None:
    same = experience.ingest_key("article", "body one")
    assert same == experience.ingest_key("article", "body one")
    assert same != experience.ingest_key("article", "body two")
    assert same != experience.ingest_key("other", "body one")


# ---------------------------------------------------------------------------
# Append core
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, **overrides) -> dict:
    kwargs = _assignment(**overrides)
    return experience.write_assignment_note(
        persona_id="sales", root=tmp_path, reindex=False, **kwargs
    )


def _note_path(tmp_path: Path) -> Path:
    return tmp_path / "memory" / "experience" / "2026-07-05.md"


def test_write_creates_daily_note_with_frontmatter(tmp_path: Path) -> None:
    receipt = _write(tmp_path)
    path = _note_path(tmp_path)
    assert receipt == {"status": "written", "path": str(path)}
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\ntags: [system, persona, experience]\n")
    assert "date: 2026-07-05" in content
    assert "persona: sales" in content
    assert "# Experience Notes - 2026-07-05" in content
    assert "## 11:00 - AGENDA-2026-07-05.md#1 (draft -> done)" in content


def test_write_appends_second_assignment_under_one_header(tmp_path: Path) -> None:
    _write(tmp_path)
    receipt = _write(
        tmp_path,
        agenda_ref="AGENDA-2026-07-05.md#2",
        message_id=8,
        status="failed",
        summary="provider down",
        local_time=LOCAL.replace(hour=13),
    )
    assert receipt["status"] == "written"
    content = _note_path(tmp_path).read_text(encoding="utf-8")
    assert content.count("---\ntags:") == 1
    assert "## 11:00 - AGENDA-2026-07-05.md#1 (draft -> done)" in content
    assert "## 13:00 - AGENDA-2026-07-05.md#2 (draft -> failed)" in content


def test_write_dedups_on_agenda_ref_plus_message_id(tmp_path: Path) -> None:
    _write(tmp_path)
    path = _note_path(tmp_path)
    before = path.read_text(encoding="utf-8")
    # A re-executed delivery: same ref, same message, different prose.
    receipt = _write(tmp_path, summary="re-ran after a restart")
    assert receipt == {"status": "duplicate", "path": str(path)}
    assert path.read_text(encoding="utf-8") == before


def test_dedup_check_is_immune_to_a_marker_substring_hidden_in_an_earlier_field(
    tmp_path: Path,
) -> None:
    """Review finding: the dedup check used to be a plain substring search
    over the whole file (``marker in existing``). A hostile or coincidental
    field value that happens to literally contain a LATER assignment's exact
    marker text can pre-poison that key — the later, legitimate assignment
    then hits ``marker in existing`` and returns ``duplicate`` without ever
    writing. Every rendered field is prefixed (``- Task: ...``) and
    collapsed onto one line, so an embedded marker can only ever land
    MID-line — never as its own line. Matching exact standalone lines closes
    the hole."""
    poison_key = experience.assignment_key("AGENDA-next.md#2", 99)
    poison_marker = experience._key_marker(poison_key)

    # An EARLIER, unrelated assignment whose task text happens to contain
    # the later assignment's exact marker string.
    _write(tmp_path, task=f"legit task mentioning {poison_marker} inline")
    before = _note_path(tmp_path).read_text(encoding="utf-8")
    assert poison_marker in before  # the poison text really landed ...
    assert not experience._marker_line_present(before, poison_marker)  # ... mid-line only

    # The REAL, later assignment that owns that exact key must still write.
    receipt = _write(tmp_path, agenda_ref="AGENDA-next.md#2", message_id=99)
    assert receipt["status"] == "written"
    content = _note_path(tmp_path).read_text(encoding="utf-8")
    # One mid-line poisoned mention + one genuine standalone marker line.
    assert content.count(poison_marker) == 2
    assert experience._marker_line_present(content, poison_marker)


def test_same_ref_new_message_is_not_a_duplicate(tmp_path: Path) -> None:
    _write(tmp_path)
    receipt = _write(tmp_path, message_id=99)
    assert receipt["status"] == "written"
    content = _note_path(tmp_path).read_text(encoding="utf-8")
    assert content.count("## 11:00 - AGENDA-2026-07-05.md#1") == 2


def test_write_skips_when_daily_file_would_exceed_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _write(tmp_path)["status"] == "written"
    path = _note_path(tmp_path)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(experience, "MAX_NOTE_FILE_CHARS", len(before))
    receipt = _write(tmp_path, agenda_ref="AGENDA-2026-07-05.md#2", message_id=8)
    assert receipt["status"] == "skipped_cap"
    assert path.read_text(encoding="utf-8") == before


def test_write_is_fail_open_when_the_tree_is_unwritable(tmp_path: Path) -> None:
    blocker = tmp_path / "memory"
    blocker.write_text("a file where the memory DIR must go", encoding="utf-8")
    receipt = _write(tmp_path)
    assert receipt["status"] == "error"
    assert receipt["detail"]


def test_unsafe_persona_id_writes_nothing_and_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traversal defense: persona ids arrive from payloads and CLI args."""
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    receipt = experience.write_assignment_note(
        persona_id="../../escape", reindex=False, **_assignment()
    )
    assert receipt["status"] == "error"
    assert "unsafe persona id" in receipt["detail"]
    assert not list(tmp_path.rglob("*.md"))


# ---------------------------------------------------------------------------
# Reindex behavior (Rule 2 — physical layout, not a config claim)
# ---------------------------------------------------------------------------


def test_reindex_skipped_without_physical_data_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        experience,
        "_reindex_note",
        lambda path, memory_dir: calls.append((path, memory_dir)),
    )
    receipt = experience.write_assignment_note(
        persona_id="sales", root=tmp_path, reindex=True, **_assignment()
    )
    assert receipt["status"] == "written"
    assert "reindexed" not in receipt
    assert calls == []


def test_reindex_fires_with_profile_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "data").mkdir()
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        experience,
        "_reindex_note",
        lambda path, memory_dir: calls.append((path, memory_dir)),
    )
    receipt = experience.write_assignment_note(
        persona_id="sales", root=tmp_path, reindex=True, **_assignment()
    )
    assert receipt["status"] == "written"
    assert receipt["reindexed"] is True
    assert calls == [(_note_path(tmp_path), tmp_path / "memory")]


def test_reindex_failure_never_unwrites_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "data").mkdir()

    def boom(path: Path, memory_dir: Path) -> None:
        raise RuntimeError("embedding model missing")

    monkeypatch.setattr(experience, "_reindex_note", boom)
    receipt = experience.write_assignment_note(
        persona_id="sales", root=tmp_path, reindex=True, **_assignment()
    )
    assert receipt["status"] == "written"
    assert receipt["reindexed"] is False
    assert "RuntimeError" in receipt["reindex_error"]
    assert _note_path(tmp_path).exists()


def test_reindex_fires_for_real_and_lands_in_the_personas_own_memory_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion is 'reindexed into that persona's memory.db'
    — not just a stubbed ``_reindex_note`` call recorded. Run the REAL
    ``reindex_file`` (embeddings off, for speed — the FTS5 keyword index
    alone proves the note is queryable) and read the row back out through
    ``memory_search``, exactly as recall would."""
    import recall_service

    real_reindex_file = recall_service.reindex_file

    def _reindex_without_embeddings(path, memory_dir, generate_embeddings=True):
        return real_reindex_file(path, memory_dir, generate_embeddings=False)

    monkeypatch.setattr(recall_service, "reindex_file", _reindex_without_embeddings)
    (tmp_path / "data").mkdir()

    sentinel = "zzexperiencereindexsentinel"
    receipt = experience.write_assignment_note(
        persona_id="sales",
        root=tmp_path,
        reindex=True,
        **_assignment(output_excerpt=f"# Checklist\n- {sentinel}"),
    )
    assert receipt["status"] == "written"
    assert receipt["reindexed"] is True

    import memory_search

    rows = memory_search.search_keyword(sentinel, memory_dir=tmp_path / "memory")
    assert rows, "experience note sentinel not reachable via the persona's own memory.db"


def test_write_assignment_note_survives_a_hostile_exception_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open contract, adversarial case: the exception's own ``__str__``
    raises too. A naive ``f"{type(exc).__name__}: {exc}"`` receipt formatter
    would itself raise while formatting the FIRST exception, turning a
    fail-open ``except Exception`` into an unhandled crash."""

    class _EvilExc(RuntimeError):
        def __str__(self):
            raise ValueError("str() explodes too")

    def explode(**kwargs):
        raise _EvilExc("rendering broke")

    monkeypatch.setattr(experience, "render_assignment_section", explode)
    receipt = experience.write_assignment_note(
        persona_id="sales", reindex=False, **_assignment()
    )  # must not raise
    assert receipt["status"] == "error"
    assert "_EvilExc" in receipt["detail"]


# ---------------------------------------------------------------------------
# Cross-profile write from the default process
# ---------------------------------------------------------------------------


def test_default_process_writes_into_another_profiles_own_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `root=`, no HOMIE_HOME games — the id resolves the path.

    This is the worktick's real shape: the tick runs as the DEFAULT profile
    and must land the note in `sales`'s vault, not the operator's.
    """
    homie = tmp_path / ".homie"
    (homie / "profiles" / "sales" / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    monkeypatch.setattr(experience, "_reindex_note", lambda path, memory_dir: None)

    receipt = experience.write_assignment_note(
        persona_id="sales", **_assignment()
    )
    assert receipt["status"] == "written"
    expected = (
        homie / "profiles" / "sales" / "memory" / "experience" / "2026-07-05.md"
    )
    assert Path(receipt["path"]) == expected
    assert expected.is_file()


# ---------------------------------------------------------------------------
# ingest_source
# ---------------------------------------------------------------------------


@pytest.fixture
def ingest_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    homie = tmp_path / ".homie"
    (homie / "profiles" / "sales" / "memory").mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    return homie / "profiles" / "sales"


def test_ingest_reads_a_file_and_labels_it_from_the_stem(
    tmp_path: Path, ingest_profile: Path
) -> None:
    article = tmp_path / "geo-playbook.md"
    article.write_text("Answer engines cite passages.", encoding="utf-8")
    receipt = experience.ingest_source(
        "sales", str(article), local_time=LOCAL, reindex=False
    )
    assert receipt["status"] == "written"
    assert receipt["source_kind"] == "file"
    assert receipt["label"] == "geo-playbook"
    assert receipt["chars"] == 29
    content = Path(receipt["path"]).read_text(encoding="utf-8")
    assert "## 11:00 - ingest: geo-playbook (ingest -> captured)" in content
    assert str(article) in content
    assert "> Answer engines cite passages." in content


def test_ingest_accepts_literal_text(ingest_profile: Path) -> None:
    receipt = experience.ingest_source(
        "sales",
        "always confirm the domain before pitching",
        label="pitch-rule",
        note="from the 07-27 wave",
        local_time=LOCAL,
        reindex=False,
    )
    assert receipt["status"] == "written"
    assert receipt["source_kind"] == "text"
    content = Path(receipt["path"]).read_text(encoding="utf-8")
    assert "- Source: text (inline)" in content
    assert "- Operator note: from the 07-27 wave" in content
    assert "> always confirm the domain before pitching" in content


def test_force_text_never_reads_the_filesystem(
    tmp_path: Path, ingest_profile: Path
) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("FILE_CONTENT_MUST_NOT_APPEAR", encoding="utf-8")
    receipt = experience.ingest_source(
        "sales", str(secret), force_text=True, local_time=LOCAL, reindex=False
    )
    assert receipt["source_kind"] == "text"
    content = Path(receipt["path"]).read_text(encoding="utf-8")
    assert "FILE_CONTENT_MUST_NOT_APPEAR" not in content
    assert str(secret) in content  # the path itself IS the ingested text


def test_missing_path_falls_back_to_literal_text(ingest_profile: Path) -> None:
    receipt = experience.ingest_source(
        "sales", "./no/such/file.md", local_time=LOCAL, reindex=False
    )
    assert receipt["status"] == "written"
    assert receipt["source_kind"] == "text"


def test_ingest_refuses_empty_source(ingest_profile: Path) -> None:
    receipt = experience.ingest_source("sales", "   ", local_time=LOCAL, reindex=False)
    assert receipt["status"] == "error"
    assert "no text" in receipt["detail"]
    assert not (ingest_profile / "memory" / "experience").exists()


def test_ingest_rejects_an_invalid_persona_name(ingest_profile: Path) -> None:
    receipt = experience.ingest_source(
        "../../escape", "hello", local_time=LOCAL, reindex=False
    )
    assert receipt["status"] == "error"
    assert "Invalid persona name" in receipt["detail"]


def test_ingest_requires_a_physical_profile_tree(ingest_profile: Path) -> None:
    """Rule 2 — the guard reads the filesystem, not a roster or a config."""
    receipt = experience.ingest_source(
        "marketing", "hello", local_time=LOCAL, reindex=False
    )
    assert receipt["status"] == "error"
    assert "no profile tree" in receipt["detail"]


def test_ingest_read_is_bounded(
    tmp_path: Path, ingest_profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(experience, "MAX_INGEST_READ_BYTES", 64)
    big = tmp_path / "huge.md"
    big.write_text("z" * 100_000, encoding="utf-8")
    receipt = experience.ingest_source(
        "sales", str(big), local_time=LOCAL, reindex=False
    )
    assert receipt["status"] == "written"
    assert receipt["chars"] == 64


def test_reingesting_identical_content_is_a_duplicate(
    tmp_path: Path, ingest_profile: Path
) -> None:
    article = tmp_path / "geo.md"
    article.write_text("same body", encoding="utf-8")
    first = experience.ingest_source(
        "sales", str(article), local_time=LOCAL, reindex=False
    )
    assert first["status"] == "written"
    before = Path(first["path"]).read_text(encoding="utf-8")
    second = experience.ingest_source(
        "sales", str(article), local_time=LOCAL, reindex=False
    )
    assert second["status"] == "duplicate"
    assert Path(first["path"]).read_text(encoding="utf-8") == before
    # A changed article under the same label DOES land.
    article.write_text("revised body", encoding="utf-8")
    third = experience.ingest_source(
        "sales", str(article), local_time=LOCAL, reindex=False
    )
    assert third["status"] == "written"


# ---------------------------------------------------------------------------
# CLI — `thehomie persona ingest`
# ---------------------------------------------------------------------------


def test_cli_ingest_writes_and_emits_a_receipt(
    tmp_path: Path, ingest_profile: Path
) -> None:
    import json

    from cli import main
    from click.testing import CliRunner

    article = tmp_path / "brief.md"
    article.write_text("the wedge is the narrowest buyer", encoding="utf-8")
    result = CliRunner().invoke(
        main, ["persona", "ingest", "sales", str(article), "--no-reindex", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert payload["persona_id"] == "sales"
    assert payload["source_kind"] == "file"
    note = Path(payload["path"])
    assert note.is_file()
    assert "the wedge is the narrowest buyer" in note.read_text(encoding="utf-8")
    assert note.parent == ingest_profile / "memory" / "experience"


def test_cli_ingest_with_reindex_lands_in_the_personas_own_memory_db(
    tmp_path: Path, ingest_profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion is 'lands a sourced note + reindex receipt'
    — every existing CLI test passes ``--no-reindex``, so the reindex half
    has never actually run end to end. Drop the flag, run the REAL
    ``reindex_file`` (embeddings off, for speed), and read the note back out
    of the persona's own ``data/memory.db``."""
    import json

    import recall_service
    from cli import main
    from click.testing import CliRunner

    real_reindex_file = recall_service.reindex_file

    def _reindex_without_embeddings(path, memory_dir, generate_embeddings=True):
        return real_reindex_file(path, memory_dir, generate_embeddings=False)

    monkeypatch.setattr(recall_service, "reindex_file", _reindex_without_embeddings)
    (ingest_profile / "data").mkdir()

    sentinel = "zzcliingestreindexsentinel"
    article = tmp_path / "brief.md"
    article.write_text(f"the wedge is the {sentinel} buyer", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["persona", "ingest", "sales", str(article), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert payload["reindexed"] is True

    import memory_search

    rows = memory_search.search_keyword(
        sentinel, memory_dir=ingest_profile / "memory"
    )
    assert rows, "ingested sentinel not reachable via the persona's own memory.db"


def test_cli_ingest_json_mode_survives_a_noisy_reindex(
    tmp_path: Path, ingest_profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``reindex_file()`` prints operator-receipt lines to
    stdout on its embedding-dim-drift rebuild path (by design — see
    ``test_reindex_file_detects_drift_and_rebuilds`` in
    test_dim_drift_guard.py). Reproduce that noisy-stdout shape directly
    (a real dim-migration is heavy to stage here) and prove the CLI's
    ``--json`` output still parses — it used to land the print lines BEFORE
    the JSON payload and break any parser reading stdout."""
    import json

    import recall_service
    from cli import main
    from click.testing import CliRunner

    def _noisy_reindex(path, memory_dir, generate_embeddings=True):
        print(
            "reindex_file: embedding dim mismatch (vec schema=512 vs "
            "config=768), forcing full rebuild...",
            flush=True,
        )
        print("reindex_file: rebuild complete (3 chunks)", flush=True)
        return 3

    monkeypatch.setattr(recall_service, "reindex_file", _noisy_reindex)
    (ingest_profile / "data").mkdir()

    article = tmp_path / "brief.md"
    article.write_text("the wedge is the narrowest buyer", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["persona", "ingest", "sales", str(article), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)  # raises if the noisy prints leaked into stdout
    assert payload["status"] == "written"
    assert payload["reindexed"] is True


def test_cli_ingest_exits_non_zero_on_failure(ingest_profile: Path) -> None:
    from cli import main
    from click.testing import CliRunner

    result = CliRunner().invoke(
        main, ["persona", "ingest", "marketing", "hello", "--no-reindex"]
    )
    assert result.exit_code == 1
    assert "no profile tree" in result.output
