---
name: yt-livestream
description: Extract long-form video and Shorts ideas from YouTube livestream transcripts. Use when the user has a livestream transcript and wants content suggestions with timestamps. Triggers on requests like "extract content from this livestream...", "find video ideas in this transcript...", "what can I repurpose from this stream...", or any livestream-to-content request.
---

# Livestream Content Extractor

Extract repurposable content segments from YouTube livestream transcripts. Identify long-form video opportunities and Shorts moments with precise timestamps.

## Philosophy

Livestreams are goldmines of content - hours of authentic teaching, demos, reactions, and insights. The challenge is finding the segments that work as standalone pieces. You're looking for:

**Clear boundaries** - Where a topic starts and ends naturally
**Standalone value** - Segments that make sense without the surrounding context
**High-density moments** - The exciting, insightful, or visually impressive parts

This isn't about summarizing the stream. It's about identifying extractable segments that could become their own content pieces.

## Defaults

| Setting | Default | Notes |
|---------|---------|-------|
| Long-form videos | 3 | Configurable via user request |
| Shorts | 6 | Configurable via user request |
| Output | `Content-Ideation/Livestream/YYYY-MM-DD/` | Single markdown file |

If the user specifies different counts (e.g., "give me 5 video ideas and 10 shorts"), use their numbers.

## Workflow

### 1. Read the Transcript

Read the full transcript file the user provides. Livestreams are typically 2-4 hours long, so the transcript will be substantial.

As you read, mentally note:
- Topic shifts (when the conversation moves to something new)
- Energy peaks (excitement, emphasis, "this is important" moments)
- Demo segments (showing something working, building something)
- Explanatory chunks (clear teaching moments)
- Hot takes or opinions (strong viewpoints that stand alone)
- Results/reveals (impressive outcomes, "and here's what we get")

### 2. Identify Long-Form Video Segments

**Target length:** 5-30 minutes of content (typically 10-20 minutes works best)

Look for segments with **clear topical boundaries** - a natural start and end where the content is self-contained.

**What makes a good long-form segment:**

| Type | What to Look For |
|------|------------------|
| **Tutorial/Build** | Step-by-step walkthrough with setup, implementation, and result |
| **Deep Dive** | Extended exploration of a single topic, tool, or concept |
| **New Tech Discussion** | Introduction and breakdown of a new framework, tool, or approach |
| **Problem → Solution** | Debugging session, architectural decision, or challenge overcome |
| **Comparison/Analysis** | Evaluating options, trade-offs, or approaches |
| **Q&A Segment** | Answering viewer questions with substantial depth |

**Boundary identification tips:**
- Look for natural transitions: "Alright, now let's move on to...", "So that's X, now let's talk about Y"
- Watch for context resets: When you re-explain something for people who just joined
- Identify completion points: "And that's how you do X" or "So that's the overview of Y"
- Note topic introductions: "So let me show you this thing I've been working on"

**Avoid:**
- Segments that heavily reference earlier stream content
- Incomplete thoughts or abandoned tangents
- Content that requires watching something else first
- Segments where you're troubleshooting something that never gets resolved

### 3. Identify Shorts Moments

**Target length:** 15 seconds to 3 minutes (sweet spot: 30-90 seconds)

Shorts can technically be up to 3 minutes, but shorter usually performs better. Aim for the sweet spot unless the content genuinely needs more time.

Shorts need ONE clear idea that delivers value immediately. You're looking for high-density moments within the larger stream.

**What makes a good Short:**

| Type | What to Look For |
|------|------------------|
| **Result Reveal** | "And here's what we get..." - impressive output, demo working |
| **Key Insight** | A single, punchy explanation of something valuable |
| **Hot Take** | Strong opinion delivered with conviction |
| **Quick Tip** | Practical advice in a condensed form |
| **Reaction Moment** | Genuine excitement or surprise at something working |
| **Pattern Interrupt** | Counterintuitive claim or myth-busting |
| **Before/After** | Quick transformation demo |

**How to find them:**
- Scan for energy peaks - when your voice gets more emphatic
- Look for "here's the thing" or "the key insight is" type phrases
- Find moments where you're introducing something (before going deep)
- Identify result reveals and "wow" moments
- Notice punchy statements that could stand alone

**What makes a Short hook:**
The first 3 seconds must stop the scroll. Look for moments that start with:
- A surprising result on screen
- A bold claim or contradiction
- A visual demo of something impressive
- A direct, confident statement

**Avoid for Shorts:**
- Segments that require context from earlier
- Explanations that need the full walkthrough to make sense
- Moments where you're still setting up (find the payoff instead)
- Content that trails off without a punch

### 4. Generate Output

Create output folder: `Content-Ideation/Livestream/YYYY-MM-DD/`

Create a single markdown file: `{livestream-topic-slug}.md`

## Output Format

```markdown
# Livestream Content Extraction - {Livestream Title/Topic}

**Source**: {Path to transcript file}
**Stream Date**: {Date of livestream if known}
**Extracted**: {Today's date}

---

## Long-Form Video Ideas

### 1. {Video Title Idea}

**Timestamps**: `{HH:MM:SS}` - `{HH:MM:SS}`
**Estimated Length**: {X} minutes
**Type**: {Tutorial / Deep Dive / New Tech / Problem-Solution / Comparison / Q&A}

**What it covers:**
{2-3 sentences describing the content}

**Why it works as standalone:**
{1-2 sentences on why this segment has clear boundaries and complete value}

**Potential hook:**
{Opening line/angle for this video}

---

### 2. {Video Title Idea}
{Same structure}

---

### 3. {Video Title Idea}
{Same structure}

---

## Shorts Ideas

### 1. {Short Title/Hook Idea}

**Timestamps**: `{HH:MM:SS}` - `{HH:MM:SS}`
**Estimated Length**: {X} seconds
**Type**: {Result Reveal / Key Insight / Hot Take / Quick Tip / Reaction / Pattern Interrupt / Before-After}

**The moment:**
{1-2 sentences describing what happens in this clip}

**Why it works:**
{Why this is a high-impact, standalone moment}

**Hook angle:**
{The first 3 seconds - what grabs attention}

---

### 2. {Short Title/Hook Idea}
{Same structure}

---

{Continue for all Shorts...}

---

## Additional Notes

{Any observations about the stream - other potential segments that didn't make the cut, themes that could be explored further, etc. Keep this brief.}
```

## Tips for Better Extractions

### For Long-Form Videos

**The 10-second test:** Can someone understand the first 10 seconds of this segment without watching anything before it? If not, find a better starting point or skip it.

**Look for natural setups:** Great segments often start with you re-explaining something, setting context, or introducing a topic fresh. These are natural entry points.

**Complete arcs:** The best segments have setup → content → payoff. Avoid segments that are all middle.

**Demo-heavy is good:** Segments with visible on-screen action (coding, configuring, building) tend to work better as standalone videos.

### For Shorts

**Energy markers:** Look for moments where you naturally get more animated - these often indicate high-value content.

**The "clip test":** Would you share this 30-second clip with a friend who knows nothing about the stream? If yes, it's probably a good Short.

**Result-first works:** Some of the best Shorts start at the end of an explanation - showing the impressive result, then briefly explaining how.

**Hooks in the transcript:** Look for strong opening lines that are already in the transcript - "Stop doing X", "Here's what nobody tells you about Y", "This is the key insight".

### Flexibility in Length

**Long-form:** Don't force a 10-minute segment if the natural boundaries give you 7 minutes or 25 minutes. Let the content dictate the length.

**Shorts:** 30-90 seconds is the sweet spot for educational content. Can go up to 3 minutes if the content justifies it, but shorter usually performs better. A punchy 15-second tip or a 2-minute mini-tutorial are both valid - let the content dictate.

## What NOT to Extract

- **Housekeeping:** "Let me check the chat", "Give me one second", troubleshooting stream issues
- **Incomplete tangents:** Started discussing something but never finished
- **Heavy context dependency:** "As I showed earlier..." or "Remember when we..."
- **Low-energy stretches:** Waiting for things to load, reading documentation silently
- **Inside jokes:** References that only make sense to stream regulars

## Quality Check

Before finalizing:

- [ ] Each long-form segment has clear start/end boundaries
- [ ] Long-form segments don't require watching earlier content
- [ ] Each Short has a strong hook in the first 3 seconds
- [ ] Shorts are single-idea moments, not mini-summaries
- [ ] Timestamps are accurate to the transcript
- [ ] Reasoning explains why each segment works standalone
- [ ] Title ideas are compelling (not just topic labels)
