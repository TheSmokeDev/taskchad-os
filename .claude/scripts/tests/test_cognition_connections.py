"""Tests for cognition.connections — emergent connection discovery."""

from __future__ import annotations

from cognition.connections import PotentialConnection


def test_potential_connection_dataclass():
    conn = PotentialConnection(
        note_a="MEMORY.md",
        note_b="USER.md",
        similarity=0.85,
    )
    assert conn.note_a == "MEMORY.md"
    assert conn.note_b == "USER.md"
    assert conn.similarity == 0.85
    assert conn.shared_terms == []


def test_potential_connection_with_terms():
    conn = PotentialConnection(
        note_a="a.md",
        note_b="b.md",
        similarity=0.9,
        shared_terms=["ExampleCorp", "outreach"],
    )
    assert len(conn.shared_terms) == 2
    assert "ExampleCorp" in conn.shared_terms
