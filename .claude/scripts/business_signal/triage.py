"""Triage stage — pure-Python keyword scoring + threshold filter.

Zero LLM calls. Scores fetched items against a ``ChannelFocus`` profile
and filters out anything below the relevance threshold. This is the
cost gate: everything before triage is free, everything after costs
LLM tokens.
"""

from __future__ import annotations

from business_signal.focus import ChannelFocus
from business_signal.models import SignalItem


def triage_items(
    items: list[SignalItem],
    focus: ChannelFocus,
    threshold: float = 0.3,
) -> list[SignalItem]:
    """Score *items* against *focus* and keep those above *threshold*.

    Sets ``item.relevance_score`` and ``item.tags`` on each item.
    Returns a new list sorted by ``relevance_score`` descending.
    Empty when nothing passes the threshold (triggers SIGNAL_SILENT
    downstream).
    """
    scored: list[SignalItem] = []

    for item in items:
        text = f"{item.title} {item.summary}"
        score, matched = focus.score_relevance(text)
        item.relevance_score = score
        item.tags = matched
        if score >= threshold:
            scored.append(item)

    scored.sort(key=lambda it: it.relevance_score, reverse=True)
    return scored
