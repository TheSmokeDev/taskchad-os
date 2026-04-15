"""Tests for InferenceTracker wiring into engine._build_frozen_regions().

Move 6c: active user inferences injected into the frozen prompt as a
dedicated region separate from USER.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


def _make_project_root(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(parents=True)
    return project_root


def _write_inferences(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def test_inferences_injected_when_tracker_has_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Active inferences ≥ 0.5 confidence become a user_inferences region."""
    import config
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    inf_path = tmp_path / "inferences.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    _write_inferences(inf_path, [
        {
            "id": "a",
            "inference": "User prefers plain-English breakdowns",
            "observation": "said 'explain like I'm 5' twice",
            "confidence": 0.9,
            "evidence_count": 3,
            "contradiction_count": 0,
            "first_seen": "2026-03-01T00:00:00+00:00",
            "last_updated": "2026-04-01T00:00:00+00:00",
            "source": "auto_capture",
            "status": "confirmed",
        },
        {
            "id": "b",
            "inference": "User works in insurance",
            "observation": "mentioned YourBusiness",
            "confidence": 0.7,
            "evidence_count": 2,
            "contradiction_count": 0,
            "first_seen": "2026-03-15T00:00:00+00:00",
            "last_updated": "2026-04-05T00:00:00+00:00",
            "source": "auto_capture",
            "status": "active",
        },
        {
            "id": "c",
            "inference": "Below-threshold inference",
            "observation": "weak signal",
            "confidence": 0.3,
            "evidence_count": 1,
            "contradiction_count": 0,
            "first_seen": "2026-03-20T00:00:00+00:00",
            "last_updated": "2026-04-01T00:00:00+00:00",
            "source": "auto_capture",
            "status": "active",
        },
    ])

    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", inf_path)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    inference_regions = [
        r for r in convo._frozen_regions if r.name == "user_inferences"
    ]
    assert len(inference_regions) == 1
    region = inference_regions[0]
    assert region.source == "inference-tracker"
    assert region.frozen is True
    assert "Active Beliefs About User" in region.content
    assert "plain-English breakdowns" in region.content
    assert "insurance" in region.content
    # Below-threshold inference (conf=0.3) is filtered out
    assert "Below-threshold" not in region.content
    # Confirmed status renders as [confirmed], active as [conf=X.XX]
    assert "[confirmed]" in region.content
    assert "[conf=0.70]" in region.content


def test_no_inferences_region_when_tracker_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No inference file → no user_inferences region is appended."""
    import config
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    monkeypatch.setattr(
        config, "INFERENCE_STATE_FILE", tmp_path / "missing-inferences.json",
    )
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    assert not any(r.name == "user_inferences" for r in convo._frozen_regions)


def test_no_inferences_region_when_all_decayed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Decayed inferences are filtered by get_active() → no region."""
    import config
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    inf_path = tmp_path / "inferences.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    _write_inferences(inf_path, [
        {
            "id": "d",
            "inference": "Old stale belief",
            "observation": "",
            "confidence": 0.9,
            "evidence_count": 5,
            "contradiction_count": 2,
            "first_seen": "2025-01-01T00:00:00+00:00",
            "last_updated": "2025-06-01T00:00:00+00:00",
            "source": "auto_capture",
            "status": "decayed",
        },
    ])

    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", inf_path)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    assert not any(r.name == "user_inferences" for r in convo._frozen_regions)


# === Sort / cap / threshold / format regression tests (Codex P2 findings) ===


def test_inferences_sorted_confirmed_first_then_by_confidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Confirmed records render first, then active sorted by confidence desc."""
    import config
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    inf_path = tmp_path / "inferences.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    _write_inferences(inf_path, [
        {
            "id": "a", "inference": "active-high",
            "observation": "", "confidence": 0.95, "evidence_count": 1,
            "contradiction_count": 0,
            "first_seen": "2026-03-01T00:00:00+00:00",
            "last_updated": "2026-04-10T00:00:00+00:00",
            "source": "auto_capture", "status": "active",
        },
        {
            "id": "b", "inference": "confirmed-mid",
            "observation": "", "confidence": 0.75, "evidence_count": 3,
            "contradiction_count": 0,
            "first_seen": "2026-03-01T00:00:00+00:00",
            "last_updated": "2026-04-01T00:00:00+00:00",
            "source": "auto_capture", "status": "confirmed",
        },
        {
            "id": "c", "inference": "active-mid",
            "observation": "", "confidence": 0.80, "evidence_count": 1,
            "contradiction_count": 0,
            "first_seen": "2026-03-01T00:00:00+00:00",
            "last_updated": "2026-04-05T00:00:00+00:00",
            "source": "auto_capture", "status": "active",
        },
    ])

    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", inf_path)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    region = next(
        r for r in convo._frozen_regions if r.name == "user_inferences"
    )
    idx_confirmed = region.content.index("confirmed-mid")
    idx_active_high = region.content.index("active-high")
    idx_active_mid = region.content.index("active-mid")
    assert idx_confirmed < idx_active_high
    assert idx_confirmed < idx_active_mid
    assert idx_active_high < idx_active_mid


def test_inferences_cap_respects_INFERENCE_PROMPT_CAP(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """INFERENCE_PROMPT_CAP drives the slice — 15 records, cap=3 → 3 lines."""
    import config
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    inf_path = tmp_path / "inferences.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    records = [
        {
            "id": f"r{i}",
            "inference": f"belief-{i:02d}",
            "observation": "",
            "confidence": 0.9,
            "evidence_count": 1,
            "contradiction_count": 0,
            "first_seen": "2026-03-01T00:00:00+00:00",
            "last_updated": f"2026-04-{(i % 28) + 1:02d}T00:00:00+00:00",
            "source": "auto_capture",
            "status": "active",
        }
        for i in range(15)
    ]
    _write_inferences(inf_path, records)

    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", inf_path)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(config, "INFERENCE_PROMPT_CAP", 3)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    region = next(
        r for r in convo._frozen_regions if r.name == "user_inferences"
    )
    rendered = [ln for ln in region.content.splitlines() if ln.startswith("- [")]
    assert len(rendered) == 3


def test_inferences_use_prompt_min_confidence_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Prompt threshold comes from config — 0.6 record filtered when cap=0.7."""
    import config
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    inf_path = tmp_path / "inferences.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    _write_inferences(inf_path, [
        {
            "id": "above", "inference": "surfaces",
            "observation": "", "confidence": 0.8, "evidence_count": 1,
            "contradiction_count": 0,
            "first_seen": "2026-03-01T00:00:00+00:00",
            "last_updated": "2026-04-10T00:00:00+00:00",
            "source": "auto_capture", "status": "active",
        },
        {
            "id": "below", "inference": "stays-hidden",
            "observation": "", "confidence": 0.6, "evidence_count": 1,
            "contradiction_count": 0,
            "first_seen": "2026-03-01T00:00:00+00:00",
            "last_updated": "2026-04-10T00:00:00+00:00",
            "source": "auto_capture", "status": "active",
        },
    ])

    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", inf_path)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(config, "INFERENCE_PROMPT_MIN_CONFIDENCE", 0.7)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    region = next(
        r for r in convo._frozen_regions if r.name == "user_inferences"
    )
    assert "surfaces" in region.content
    assert "stays-hidden" not in region.content


def test_inference_confidence_renders_with_two_decimals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """0.96 must render as conf=0.96 — .1f rounded to 1.0 and looked confirmed."""
    import config
    from engine import ConversationEngine
    from session import SQLiteSessionStore

    inf_path = tmp_path / "inferences.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    _write_inferences(inf_path, [
        {
            "id": "precise", "inference": "precise-belief",
            "observation": "", "confidence": 0.96, "evidence_count": 1,
            "contradiction_count": 0,
            "first_seen": "2026-03-01T00:00:00+00:00",
            "last_updated": "2026-04-10T00:00:00+00:00",
            "source": "auto_capture", "status": "active",
        },
    ])

    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", inf_path)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    region = next(
        r for r in convo._frozen_regions if r.name == "user_inferences"
    )
    assert "conf=0.96" in region.content
    assert "conf=1.00" not in region.content
