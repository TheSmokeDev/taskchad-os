# Morning Brief Grounding

Status: Shipped locally 2026-08-14

The wake-up/session-opening brief and co-founder morning agenda share a
bounded operator-context floor before presenting priorities:

- newest daily MDs since the operator's away boundary (up to four files)
- the latest dated `vault/memory/_ops/history.md` receipt
- existing heartbeat observations, episodes, working memory, project state,
  and co-founder outcome receipts

The latest vault-ops `Impact` and `Actions` evidence is ordered before daily
snippets so a verified context pass can supersede stale blockers or queue
labels. Briefing instructions require the runtime to lead with what the
operator actually worked on and to distinguish drafts, generated documents,
delegated tasks, commits, deployments, sends, payments, and provider-verified
outcomes.

The generic `morning-brief` automation blueprint uses the same operating
contract and explicitly requests a `vault-ops orient/context` pass before
reconciling connected calendar, email, and other integration state.

Verification:

```powershell
cd .claude/scripts
uv run pytest tests/test_session_brief.py tests/test_cofounder_agenda.py `
  tests/test_blueprint_catalog.py tests/test_suggestion_catalog.py -q
```

