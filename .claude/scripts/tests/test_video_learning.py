from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import runtime.gemini_cli as gemini_cli
import runtime.openai_codex as openai_codex
from runtime.auth_profiles import AuthProfileStatus
from runtime.base import RUNTIME_LANE_GENERIC, RuntimeRequest, RuntimeResult
from runtime.capabilities import TOOL_REASONING
from runtime.profiles import RuntimeProfile
from runtime.prompt_builder import render_cli_prompt
from video_learning.analyze import AnalysisResult, apply_approved_proposal
from video_learning.extract import VideoSourceError, parse_vtt, validate_source
from video_learning.models import (
    ExtractionResult,
    TranscriptSegment,
    VideoLearningRequest,
    VideoMetadata,
)
from video_learning.service import VideoLearningService
from video_learning.store import VideoJobStore


def test_validate_source_rejects_private_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "video_learning.extract.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(VideoSourceError, match="private network"):
        validate_source("https://example.com/video", allow_local=False)


def test_validate_source_allows_public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "video_learning.extract.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    assert validate_source("https://example.com/video", allow_local=False) == (
        "url",
        "https://example.com/video",
    )


def test_validate_source_rejects_local_path_for_remote_channel(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    with pytest.raises(VideoSourceError, match="public http"):
        validate_source(str(video), allow_local=False)


def test_parse_vtt_collapses_rolling_caption_duplicates() -> None:
    rows = parse_vtt(
        """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello world

00:00:03.000 --> 00:00:05.000
Hello world again

00:00:05.000 --> 00:00:07.000
Next point
"""
    )
    assert [row.text for row in rows] == ["Hello world", "again", "Next point"]
    assert rows[0].timestamp == "00:00:01"


def test_caption_provenance_prefers_metadata_over_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from video_learning import extract as extraction

    caption = tmp_path / "captions.en.vtt"
    caption.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAutomatic words\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        extraction,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    _, source = extraction._remote_captions(
        "https://example.com/video", tmp_path, {"subtitles": {}, "automatic_captions": {"en": []}}
    )
    assert source == "automatic captions"


def test_job_store_marks_active_jobs_interrupted_after_restart(tmp_path: Path) -> None:
    store = VideoJobStore(tmp_path)
    row = store.create({"source": "https://example.com/video"})
    store.update(row["job_id"], status="analyzing")
    same_process = VideoJobStore(tmp_path)
    assert same_process.get(row["job_id"])["status"] == "analyzing"
    store.update(row["job_id"], status="analyzing", owner_pid=999_999_999)

    restarted = VideoJobStore(tmp_path)
    assert restarted.get(row["job_id"])["status"] == "interrupted"
    retry = restarted.retry(row["job_id"])
    assert retry is not None
    assert retry["retried_from"] == row["job_id"]


def test_read_only_tool_prompt_hides_write_integrations(tmp_path: Path) -> None:
    prompt = render_cli_prompt(
        RuntimeRequest(
            prompt="Inspect frame.jpg",
            cwd=tmp_path,
            task_name="visual",
            capability=TOOL_REASONING,
            read_only_tools=True,
            image_paths=[tmp_path / "frame.jpg"],
        )
    )
    assert "read-only evidence analysis" in prompt
    assert "Key integrations available" not in prompt
    assert "full access to read and write" not in prompt


def test_workspace_edit_prompt_hides_external_integrations(tmp_path: Path) -> None:
    prompt = render_cli_prompt(RuntimeRequest(
        prompt="Apply approved docs proposal",
        cwd=tmp_path,
        task_name="apply",
        capability=TOOL_REASONING,
        workspace_write_tools=True,
    ))
    assert "explicitly approved local-workspace proposal" in prompt
    assert "Key integrations available" not in prompt


@pytest.mark.asyncio
async def test_codex_approved_application_uses_workspace_write_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = openai_codex.OpenAICodexRuntime(RuntimeProfile(
        key="test-codex", provider="openai-codex", model="gpt-test",
        command="codex", auth_profile="default",
    ))
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        openai_codex, "codex_auth_status",
        lambda _profile=None: AuthProfileStatus(True, "ok"),
    )

    async def fake_exec(*args, **_kwargs):
        captured["args"] = args
        output = Path(args[args.index("--output-last-message") + 1])

        class Process:
            returncode = 0

            async def communicate(self, _data):
                output.write_text("applied", encoding="utf-8")
                return b"", b""

        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.run(RuntimeRequest(
        prompt="apply",
        cwd=tmp_path,
        task_name="apply",
        capability=TOOL_REASONING,
        workspace_write_tools=True,
    ))
    args = captured["args"]
    assert args[args.index("--sandbox") + 1] == "workspace-write"


@pytest.mark.asyncio
async def test_gemini_approved_application_uses_edit_only_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = gemini_cli.GeminiCliRuntime(RuntimeProfile(
        key="test-gemini", provider="gemini-cli", model="gemini-test",
        command="gemini", auth_profile="oauth-personal", candidate_models=("gemini-test",),
    ))
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        gemini_cli, "gemini_auth_status",
        lambda _profile=None: AuthProfileStatus(True, "ok"),
    )

    async def fake_exec(*args, **_kwargs):
        captured["args"] = args

        class Process:
            returncode = 0

            async def communicate(self, _data):
                return b"applied", b""

        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.run(RuntimeRequest(
        prompt="apply",
        cwd=tmp_path,
        task_name="apply",
        capability=TOOL_REASONING,
        workspace_write_tools=True,
    ))
    args = captured["args"]
    assert "auto_edit" in args
    assert "--yolo" not in args
    assert "read_file,write_file,replace,glob,grep_search,list_directory" in args


@pytest.mark.asyncio
async def test_service_saves_sourced_note_not_raw_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    vault = tmp_path / "vault"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    service = VideoLearningService(data_dir=data, memory_dir=vault, workspace=workspace)
    extraction = ExtractionResult(
        metadata=VideoMetadata(
            source="https://example.com/watch?v=abc",
            source_type="url",
            video_id="abc",
            title="Useful Strategy",
            channel="Example Founder",
            webpage_url="https://example.com/watch?v=abc",
        ),
        segments=[TranscriptSegment(1.0, 2.0, "RAW_TRANSCRIPT_SHOULD_NOT_ENTER_NOTE")],
        transcript_source="creator captions",
        artifact_dir=data / "artifact",
    )
    runtime = RuntimeResult(
        text="",
        runtime_lane=RUNTIME_LANE_GENERIC,
        provider="openai-codex",
        model="gpt-test",
    )

    async def fake_extract(*_args, **_kwargs):
        return extraction

    async def fake_analyze(*_args, **_kwargs):
        return AnalysisResult("# Executive takeaway\nUse a tighter feedback loop.", runtime)

    async def fake_recall(*_args, **_kwargs):
        return "existing context"

    async def fake_index(_path: Path) -> None:
        return None

    monkeypatch.setattr("video_learning.service.extract_video", fake_extract)
    monkeypatch.setattr("video_learning.service.analyze_video", fake_analyze)
    monkeypatch.setattr(service, "_recall_context", fake_recall)
    monkeypatch.setattr(service, "_index_note", fake_index)

    row = service.create_job(
        VideoLearningRequest(
            source="https://example.com/watch?v=abc",
            conversation_context="we are building a feedback system",
            workspace=workspace,
        )
    )
    result = await service.run(row["job_id"])

    assert result.success is True
    note = Path(result.note_path)
    body = note.read_text(encoding="utf-8")
    assert "Use a tighter feedback loop" in body
    assert "RAW_TRANSCRIPT_SHOULD_NOT_ENTER_NOTE" not in body
    assert 'source: "https://example.com/watch?v=abc"' in body
    assert service.status(row["job_id"])["status"] == "ready"

    # Proves _save_note's real lane-index wiring fires, not just the primitive.
    index_path = note.parent / "WATCH-DOSSIER-INDEX.md"
    assert index_path.exists()
    index_text = index_path.read_text(encoding="utf-8")
    assert f"[[{note.stem}]]" in index_text
    assert "Useful Strategy" in index_text  # title column resolved from real frontmatter
    assert "Example Founder" in index_text  # channel column resolved from real frontmatter


@pytest.mark.asyncio
async def test_application_rejects_nonmatching_exact_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match"):
        await apply_approved_proposal(
            proposal="Change docs only.",
            approval_token="wrongtoken",
            workspace=tmp_path,
        )


class TestWatchDossierLaneIndex:
    """The lane index gives every /watch dossier an inbound edge (orphan cure)."""

    WATCH_SECTIONS = [
        {
            "heading": "Video dossiers",
            "glob": "[0-9]*.md",
            "columns": [
                ("Title", "title"),
                ("Channel", "channel"),
                ("Source", "source_type"),
            ],
        }
    ]

    def _regenerate(self, lane_dir):
        from shared import regenerate_lane_index

        return regenerate_lane_index(
            lane_dir=lane_dir,
            index_name="WATCH-DOSSIER-INDEX.md",
            title="Watch Dossiers — Lane Index",
            description="test",
            sections=self.WATCH_SECTIONS,
        )

    def _write_dossier(self, lane_dir, day, slug, title, channel, source_type="youtube"):
        # Mirrors the real _save_note frontmatter (no `date` field — filenames
        # are date-prefixed, so the index sorts newest-first by stem).
        (lane_dir / f"{day}-{slug}.md").write_text(
            f'---\ntype: "video-learning"\ntitle: "{title}"\nchannel: "{channel}"\n'
            f'source_type: "{source_type}"\n---\n\n# {title}\n',
            encoding="utf-8",
        )

    def test_row_per_dossier_newest_first(self, tmp_path: Path) -> None:
        self._write_dossier(tmp_path, "2026-07-01", "alpha", "Alpha Talk", "Chan A")
        self._write_dossier(tmp_path, "2026-07-08", "beta", "Beta Talk", "Chan B")
        text = self._regenerate(tmp_path).read_text(encoding="utf-8")
        assert "[[2026-07-08-beta]]" in text
        assert "[[2026-07-01-alpha]]" in text
        assert text.index("2026-07-08") < text.index("2026-07-01")
        assert "Alpha Talk" in text and "Beta Talk" in text
        assert "[[MOC-thehomie]]" in text

    def test_tolerates_dossier_missing_fields(self, tmp_path: Path) -> None:
        (tmp_path / "2026-06-01-old.md").write_text(
            "---\ntype: video-learning\n---\n\n# old dossier, no fields\n", encoding="utf-8"
        )
        text = self._regenerate(tmp_path).read_text(encoding="utf-8")
        assert "[[2026-06-01-old]]" in text

    def test_index_excludes_itself_and_is_idempotent(self, tmp_path: Path) -> None:
        self._write_dossier(tmp_path, "2026-07-01", "solo", "Solo Talk", "Chan")
        first = self._regenerate(tmp_path).read_text(encoding="utf-8")
        second = self._regenerate(tmp_path).read_text(encoding="utf-8")
        assert first == second
        assert "[[WATCH-DOSSIER-INDEX]]" not in first
        assert "Video dossiers (1)" in first

    def test_missing_lane_dir_returns_none(self, tmp_path: Path) -> None:
        assert self._regenerate(tmp_path / "nope") is None

    def test_index_failure_does_not_block_note_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shared

        monkeypatch.setattr(
            shared,
            "regenerate_lane_index",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        service = VideoLearningService(
            data_dir=tmp_path / "data",
            memory_dir=tmp_path / "vault",
            workspace=tmp_path / "repo",
        )
        extraction = ExtractionResult(
            metadata=VideoMetadata(
                source="https://example.com/watch?v=xyz",
                source_type="url",
                video_id="xyz",
                title="Fail Open Talk",
                channel="Example Channel",
                webpage_url="https://example.com/watch?v=xyz",
            ),
            segments=[],
            transcript_source="creator captions",
            artifact_dir=tmp_path / "artifact",
        )
        analysis = AnalysisResult(
            "# Executive takeaway\nIndex failures never block the note.",
            RuntimeResult(
                text="", runtime_lane=RUNTIME_LANE_GENERIC, provider="openai-codex", model="gpt-test"
            ),
        )
        note_path = service._save_note("job-fail-open", extraction, analysis)
        assert note_path.exists()
        assert "Index failures never block the note" in note_path.read_text(encoding="utf-8")
