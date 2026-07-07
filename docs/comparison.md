# How The Homie Compares

The rows below reflect the maintainers' reading of neighboring projects at the
time of writing (July 2026). Those projects evolve quickly — check their own
documentation for current capabilities. The Homie is an independent project and
is not affiliated with, sponsored by, or endorsed by any project named here;
see [NOTICE.md](../NOTICE.md).

| | OpenClaw | Hermes Agent | The Homie |
|---|---|---|---|
| **Thesis** | Channel breadth - 25+ adapters | Self-improving skills loop | A real partner - identity + memory + proactive judgment + the nerve to push back |
| **Interface** | Many chat channels | TUI, CLI, gateway, and desktop workbench | CLI, Telegram/Slack/Discord/WhatsApp/web relay, dashboard, and Desktop v0 shell |
| **Runtime** | Adapter-first routing | Broad provider/model support plus terminal backends | Lane-first runtime with `/provider`, `/model`, status/doctor, and quiet JSON contract |
| **Learning loop** | Notes and commands | Skills from experience, skill improvement, memory nudges, session search | Belief/contradiction engine, evidence-gated identity amendments, staged memory promotion, replay-veto safety |
| **Memory** | Plain-text notes | MEMORY.md, user modeling, FTS session search | 9-layer vault: identity, graph traversal + hub boost, dual search, daily/weekly synthesis, staged promotion |
| **Knowledge graph** | No | Not the focus | Entity compilation engine: concept pages, connections, contradictions, Q&A filing, Tier-1 LLM re-ranking |
| **Operator surface** | Bot-style access | Gateway and terminal workbench | Operating Room, Capability Gateway, Team Room, Desktop v0, public manual surfaces |
| **Multi-agent** | No | Subagents and parallel workstreams | Convoy DAGs with dependency-edge parallel release + exactly-once executor callbacks, typed mailbox, team sessions, backend fallback |

## What Using It Feels Like

It's 6:30am. You open a session.

Instead of *"Hi, how can I help you today?"* — you get:

> *"Morning. While you were out — your business had 3 new leads overnight, the loan you flagged is 5 days from maturity, and there's an inbound email from a backlink partner worth reviewing. Yesterday you were mid-decision on the routing refactor. Pick that up, or hit the leads first?"*

You didn't set up a notification. You didn't write a morning brief. The Homie was watching. Its memory isn't a static file you load — it's a living record tended between sessions. Its identity isn't a document you edit — it's a self that amends when the evidence is strong enough.

The load-bearing walls are up. The "while you were out" brief is a shipped feature — the Session Opening Brief composes fresh heartbeat observations, new threads, episodes written while you were away, and applied memory amendments into the first turn after an absence, with zero extra LLM calls (`cognition/proactive_brief.py`, 51 tests in `test_session_brief.py`). Vault, tiered recall, daily reflection, weekly synthesis, dream consolidation, WorkingMemory-owned prompt state, and the self-evolution replay loop all ship today. Ambient monitoring runs on the heartbeat; durable identity amendments only apply after clearing the default-deny evidence + policy gate described in [The Living Self Manual](the-living-self-manual.md).
