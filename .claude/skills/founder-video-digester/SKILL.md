---
name: founder-video-digester
description: Digest founder, startup, YC, business, operator, sales, product, and strategy videos or transcripts into reusable operating lessons. Use when the user provides YouTube URLs, video transcripts, podcast/interview notes, founder advice, or asks to pull patterns, compare external founder insights against The Homie, YourBusiness, YourProduct, personal brand, sales, or business operations, identify gaps, and turn those gaps into concrete changes, PRPs, tasks, docs, or implementation plans.
---

# Founder Video Digester

Turn raw founder content into decisions and work. Do not summarize for its own
sake. Extract the useful moves, compare them against the operator's current
systems, and convert real gaps into concrete changes.

## Workflow

1. Intake the source material.
   - Accept YouTube URLs, pasted transcripts, notes, or mixed batches.
   - If URLs are provided, try direct transcript extraction first. Use
     `yt-dlp --write-auto-subs --write-subs --sub-langs "en.*" --skip-download`
     when available. If unavailable, use browser/search only as fallback.
   - If a transcript cannot be retrieved, say that plainly and work only from
     provided notes or ask for the transcript.

2. Build the evidence table.
   - Capture speaker/source, timestamp when available, claim, principle,
     confidence, and whether it is advice, data, anecdote, or opinion.
   - Separate timeless principle from founder theater. Do not overfit to status,
     pedigree, funding context, or survivorship bias.
   - Prefer crisp language: "do this because X", "avoid this because Y".

3. Compare against local context.
   - Search current memory and docs before proposing changes:
     `cd .claude/scripts; uv run python memory_search.py "<topic>" --mode keyword --limit 5`
   - For framework/runtime changes, check `PRPs/active/TRACKER.md`,
     `docs/manual/README.md`, and the owning code slice before recommending
     implementation.
   - For YourBusiness/business changes, distinguish awareness/planning from live
     production operations unless the user explicitly expands scope.

4. Classify each insight.
   - `Already Doing`: current system already covers it; cite the local proof.
   - `Gap`: important and missing or weak.
   - `Experiment`: plausible but not proven; needs a small test.
   - `Reject`: wrong context, bad incentive, too generic, or not worth the cost.
   - `Watch`: interesting but not actionable yet.

5. Convert gaps into work.
   - Produce concrete deltas, not vague lessons.
   - Use the smallest durable artifact that matches the gap:
     PRP, tracker item, manual update, code change, script, dashboard change,
     sales play, content calendar item, SOP, or memory note.
   - If the change spans runtime and GUI, preserve vertical slice ownership:
     `thehomie` for runtime/memory/adapters, `mission-control` for GUI/control
     plane, YourBusiness repos for YourBusiness execution.

6. Return the operator brief.
   - Lead with the highest-leverage moves.
   - Keep proof boundaries explicit: transcript-derived, memory-derived,
     repo-verified, or unverified.
   - End with the single next move unless the user asked for a broad map.

## Output Shape

Use this structure by default:

```md
**Thesis**
One short paragraph on what the batch is really saying.

**Patterns**
- Pattern: source/timestamp -> why it matters here.

**Gap Map**
- Already Doing: local proof.
- Gap: missing behavior -> concrete consequence.
- Experiment: smallest test.
- Reject: why not.

**Concrete Changes**
- Artifact/path or owner: exact change.

**Next Move**
The one move to do first.
```

For a large batch, add a source matrix before the thesis. For implementation
requests, create or update the relevant PRP/tracker/docs after the brief.

## Quality Bar

- Do not worship credentials. YC/famous-founder status is context, not proof.
- Do not flatten the operator's edge into generic startup advice. Preserve the
  real advantage: sales skill, insurance/operator reality, fast shipping, asset
  ownership, and direct customer feedback.
- Do not create abstract "learn more" tasks. Every accepted gap needs a next
  action that can be run, written, tested, sold, or shipped.
- Do not claim transcript access unless it actually succeeded in this turn.
- Ask before sending emails, posting, buying tools, or mutating live production.
