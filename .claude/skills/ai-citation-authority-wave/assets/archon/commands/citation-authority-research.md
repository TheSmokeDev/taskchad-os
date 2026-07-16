---
description: Build or preserve a receipt-backed GSC, OpenSEO, or live-SERP evidence packet for one bounded authority wave
argument-hint: (none - reads .citation-authority artifacts)
---

# Research one bounded authority wave

Work only in the current website repository. This node may research and write
`.citation-authority/evidence-packet.json`. It must not edit site content, use a
Reddit account, publish, deploy, submit indexing requests, or manufacture a
metric.

## Inputs

Read these files in order:

1. `.citation-authority/run-config.json`
2. `.citation-authority/site-profile.json`
3. `.citation-authority/evidence-request.json`, when present
4. `.citation-authority/evidence-packet.json`, when already present
5. the optional fleet intent map named by `run-config.json`
6. existing content near the configured content sink

If `evidence-packet.json` already has `status: ready` or `status: no_evidence`,
preserve it byte for byte and report `EVIDENCE_PRESERVED`.

## Receipt hierarchy

Prefer existing first-party receipts in this order:

1. GSC queries with impressions or clicks and a stated date range.
2. OpenSEO measurements with a run ID or source URL.
3. A live SERP autopsy performed in this node.

For a SERP autopsy, search the exact candidate query in the site's configured
language and record at least three URLs that were actually returned. Capture
the engine, observation timestamp, titles/positions when visible, the buyer
intent, and the specific gap. A Reddit-modifier receipt should include a real
Reddit discussion URL when one ranks. Merely finding a Reddit thread is not a
claim that Reddit recommends this brand.

Do not convert keyword-tool silence into a made-up zero. `No data` is a valid
note, not a numeric metric. Do not infer GSC data from a public SERP. Do not use
stale receipts older than 90 days.

## Language and fleet boundaries

- `en` targets are researched and written as English-native intents.
- `es` targets are researched as Spanish-native intents, not translations of
  an English list. Use natural terms such as `cotizacion`, not English `quote`.
- Do not put Spanish pages under `/es` on a Spanish-only root-domain profile.
- Respect the fleet intent map. A query already owned by another domain is not
  eligible here.
- Keep the research broad enough to compare Reddit modifiers, direct
  `who helps...` answers, and comparison intents, but do not select targets in
  this node.

## Output

Write valid JSON matching:

`.claude/skills/ai-citation-authority-wave/references/evidence-packet.schema.json`

Use `status: ready` only when at least one real receipt exists. If authenticated
data is unavailable and a live SERP autopsy cannot produce a defensible target,
write:

```json
{
  "schema_version": 1,
  "created_at": "<ISO-8601 UTC>",
  "status": "no_evidence",
  "reason": "<specific reason>",
  "receipts": []
}
```

That is a successful no-op, not permission to guess. End with the receipt IDs
or `NO_EVIDENCE`.
