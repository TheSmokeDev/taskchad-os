"""Tests for cognition.regions — 9-region prompt assembly."""

from __future__ import annotations

from cognition.regions import (
    DEFAULT_REGION_BUDGETS,
    PromptRegion,
    apply_process_weights,
    assemble_regions,
    truncate_region,
)


def test_truncate_over_budget():
    """10K content, 500 token budget -> truncated with warning."""
    content = "Line of text here.\n" * 500  # ~9500 chars
    region = PromptRegion("test", content, 500)  # 500 tokens = 2000 chars
    result = truncate_region(region)

    assert len(result) < len(content)
    assert "[TRUNCATED:" in result
    assert "test" in result  # Region name in warning


def test_truncate_within_budget():
    """Short content, large budget -> unchanged."""
    content = "Short content here."
    region = PromptRegion("test", content, 2000)
    result = truncate_region(region)

    assert result == content
    assert "[TRUNCATED:" not in result


def test_truncate_empty():
    region = PromptRegion("test", "", 1000)
    assert truncate_region(region) == ""


def test_truncate_zero_budget():
    region = PromptRegion("test", "some content", 0)
    assert truncate_region(region) == "some content"  # 0 budget = unlimited


def test_assemble_order():
    """Regions assembled in list order."""
    regions = [
        PromptRegion("identity", "I am the bot", 4000),
        PromptRegion("user_model", "User is YourUser", 3000),
        PromptRegion("recalled_memory", "Past fact", 4000),
    ]
    result = assemble_regions(regions)

    # Check order: identity before user_model before recalled_memory
    idx_identity = result.find("Identity")
    idx_user = result.find("User Model")
    idx_recalled = result.find("Recalled Memory")

    assert idx_identity < idx_user < idx_recalled


def test_empty_regions_skipped():
    """Empty content -> region omitted."""
    regions = [
        PromptRegion("identity", "I am the bot", 4000),
        PromptRegion("continuity", "", 2000),  # Empty — should be skipped
        PromptRegion("recalled_memory", "Past fact", 4000),
    ]
    result = assemble_regions(regions)

    assert "Continuity" not in result
    assert "Identity" in result
    assert "Recalled Memory" in result


def test_assemble_with_separators():
    """Regions separated by ---."""
    regions = [
        PromptRegion("identity", "content1", 4000),
        PromptRegion("user_model", "content2", 3000),
    ]
    result = assemble_regions(regions)
    assert "---" in result


def test_assemble_single_region():
    regions = [PromptRegion("identity", "I am the bot", 4000)]
    result = assemble_regions(regions)
    assert "Identity" in result
    assert "I am the bot" in result
    assert "---" not in result  # No separator for single region


def test_assemble_all_empty():
    regions = [
        PromptRegion("a", "", 1000),
        PromptRegion("b", "   ", 1000),
    ]
    result = assemble_regions(regions)
    assert result == ""


# === Move 3: apply_process_weights tests ===


def test_apply_process_weights_default():
    """Empty weights = unchanged budgets."""
    adjusted = apply_process_weights(DEFAULT_REGION_BUDGETS, {})
    assert adjusted == DEFAULT_REGION_BUDGETS


def test_apply_process_weights_planning():
    weights = {"durable_memory": 1.5, "prefetched_context": 0.7}
    adjusted = apply_process_weights(DEFAULT_REGION_BUDGETS, weights)
    assert adjusted["durable_memory"] == 24000  # 16000 * 1.5
    assert adjusted["prefetched_context"] == 16800  # 24000 * 0.7
    assert adjusted["identity"] == 16000  # Unchanged (no weight)


def test_apply_process_weights_clamped_high():
    adjusted = apply_process_weights({"identity": 16000}, {"identity": 5.0})
    assert adjusted["identity"] == 32000  # Clamped at 2.0x


def test_apply_process_weights_clamped_low():
    adjusted = apply_process_weights({"identity": 16000}, {"identity": 0.1})
    assert adjusted["identity"] == 8000  # Clamped at 0.5x


def test_apply_process_weights_custom_bounds():
    adjusted = apply_process_weights(
        {"x": 1000}, {"x": 10.0}, min_weight=0.2, max_weight=3.0
    )
    assert adjusted["x"] == 3000  # 1000 * 3.0


def test_apply_process_weights_zero_budget():
    adjusted = apply_process_weights({"recent_conversation": 0}, {"recent_conversation": 2.0})
    assert adjusted["recent_conversation"] == 0  # 0 * 2.0 = 0
