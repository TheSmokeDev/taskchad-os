"""GitHub signal slice — starred-repo backlog resurfacing + trending digest.

Weekly pipeline: fetch starred inventory → split new-vs-backlog by starred_at
watermark → contextual backlog picks (one background-tier LLM call) → trending
garnish → deterministic vault digest + Telegram card. Bot surface: /stars.
"""
