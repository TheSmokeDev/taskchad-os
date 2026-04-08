---
name: yt-shorts
description: Create YouTube Shorts scripts for AI/tech educational content. Use when the user wants to create a Short, repurpose long-form content into Shorts, or create quick-tip vertical video content. Triggers on requests like "create a YouTube Short about...", "turn this into a Short", "write a Shorts script", or any vertical video content request.
---

# YouTube Shorts Creator

Create high-performing YouTube Shorts scripts for AI/tech educational content. Shorts are 15-60 second vertical videos optimized for mobile viewing and the Shorts algorithm.

## Source Attribution (Required)

Every Short file must start with source metadata:

**For digest items:**
```
**Source**: Digest #{number} - {title}
**Original**: {URL from topic file}
**Deep Dive**: {path to topic .md file}
```

**For daily notes:**
```
**Source**: Daily Notes - {IMPORTANT if marked}
**Original**: N/A
**Deep Dive**: {path to daily note file}
```

**For web research:**
```
**Source**: {Article/study title that informed the content}
**Original**: {specific URL(s) used}
**Deep Dive**: N/A
```

Get digest info from `DAILY_DIGEST.md` and topic files in `content-engine/output/topics/YYYY-MM-DD/`

## Workflow

1. **Understand the content** - What's the ONE idea, tip, or insight?
2. **Choose the Short type** - Select from templates below
3. **Write the hook** - First 3 seconds that stop the scroll
4. **Structure the body** - Follow the timing guidelines
5. **End abruptly** - No lingering outro, consider loop potential

## Short Specifications

| Element | Requirement |
|---------|-------------|
| **Length** | 15-60 seconds (sweet spot: 30-45 for educational) |
| **Word count** | 75-150 words spoken |
| **Hook** | First 3 seconds, must stop the scroll |
| **Format** | 9:16 vertical (1080x1920) |
| **Pacing** | Visual cut every 2-4 seconds |
| **Captions** | Required - large, high-contrast |
| **Ideas per Short** | ONE (never more) |

## Video Format Context

Videos use a split-screen format:
- **Top 2/3:** Screen recording of what I'm demonstrating
- **Bottom 1/3:** Face cam

This means:
- All visual directions are screen recordings, NOT B-roll
- No attitude/pose/facial expression directions (I handle that naturally)
- Screen directions should describe what I'm showing, not how I'm acting
- Keep directions informal and first-person ("I navigate to...", "I open...")

## The Algorithm: What Matters

1. **Retention rate** - Percentage who watch to the end (aim for 60%+)
2. **Hook rate** - Percentage who watch past 3 seconds (aim for 70%+)
3. **Replay rate** - Even 10% replay rate significantly boosts distribution
4. **Completion** - For sub-20s Shorts, aim for 90%+ completion

## Hook Patterns (Critical - First 3 Seconds)

50-60% of viewers drop off in the first 3 seconds. Your hook must combine:

**Visual Hook:** What's on screen that grabs attention (impressive result, interesting UI, etc.)
**Verbal Hook:** First spoken line - confident, direct, clear

### Hook Formulas That Work

**Pattern Interrupt:**
```
[I show something unexpected on screen]
"Stop. You've been doing [X] wrong."
```

**Contradiction/Shock:**
```
"I made more money after I stopped [common advice]."
"This 'mistake' actually increased my [metric] by 300%."
```

**Curiosity Gap:**
```
"There's a reason [surprising outcome] and nobody talks about it."
"The [tool/technique] that [impressive result] in [short time]."
```

**Result-First (Best for tutorials):**
```
[I show the impressive end result on screen]
"Here's how I did this in [time]."
```

**Bold Claim:**
```
"[Common belief] is completely wrong."
"In 30 seconds, I'll change how you think about [topic]."
```

**Question Hook:**
```
"Why does [surprising thing happen]?"
"What if I told you [counterintuitive claim]?"
```

### What NOT to Do

- Logo fade-ins or slow intros
- "Hi, I'm [name] and today..."
- Generic hooks: "Check this out!" or "Watch until the end!"
- Anything that could apply to any video

## Short Type Templates

### 1. Quick Tip (15-25 seconds)

Best for: Single technique, one insight, surprising fact

```
**Hook (0-3s):** [Pattern interrupt or bold claim]
**Setup (3-8s):** [One sentence of context]
**Tip (8-20s):** [The actual technique/insight]
**End (20-25s):** [Quick result or "Try it"]
```

Example structure:
```
HOOK: "Stop copy-pasting code from ChatGPT."
SETUP: "Here's what senior devs do instead."
TIP: "They use [technique] to [benefit]."
END: "Your code quality just leveled up."
```

### 2. Before/After Transformation (25-40 seconds)

Best for: Tool demos, workflow improvements, results showcase

```
**Hook (0-3s):** [Show the impressive "after" result first]
**Problem (3-10s):** [The "before" - what was painful]
**Solution (10-30s):** [How to get from before to after]
**Result (30-40s):** [Reinforce the transformation]
```

### 3. Myth Buster (20-35 seconds)

Best for: Contrarian takes, common misconceptions

```
**Hook (0-3s):** "[Common belief] is wrong."
**Why It's Wrong (3-15s):** [Quick evidence/reasoning]
**The Truth (15-30s):** [What actually works]
**End (30-35s):** [Punchy conclusion]
```

### 4. Mini Tutorial (40-60 seconds)

Best for: Step-by-step guides, how-to content

```
**Hook (0-3s):** [Show end result or make bold promise]
**Context (3-8s):** [Why this matters - one sentence]
**Step 1 (8-20s):** [First action]
**Step 2 (20-35s):** [Second action]
**Step 3 (35-50s):** [Third action]
**Result (50-60s):** [Show it working, abrupt end]
```

### 5. Hot Take (15-30 seconds)

Best for: Opinions, predictions, industry commentary

```
**Hook (0-3s):** [Controversial statement]
**Reasoning (3-20s):** [2-3 quick supporting points]
**Conclusion (20-30s):** [Double down or invite debate]
```

### 6. Tool/Feature Spotlight (30-45 seconds)

Best for: New tools, features, announcements

```
**Hook (0-3s):** [Impressive capability shown]
**What It Is (3-10s):** [One sentence explanation]
**Demo (10-35s):** [Show it in action]
**Why It Matters (35-45s):** [Impact for viewer]
```

## Script Format

Write scripts in this format:

```markdown
# [Short Title]

**Type:** [Quick Tip / Before-After / Myth Buster / Mini Tutorial / Hot Take / Tool Spotlight]
**Length:** [Target seconds]
**One Idea:** [The single takeaway in one sentence]

---

## Script

**[0-3s] HOOK:**
[Here I'll show X on screen]
"[Spoken words]"

**[3-Xs] BODY:**
[Now I'll navigate to/click/demonstrate Y]
"[Spoken words]"

**[Xs-End] CLOSE:**
[Here I'll show the result]
"[Spoken words]"

---

## Screen Prep Notes
- [Specific screens/pages to have ready]
- [Any prep needed before recording]
- [Tabs or windows to have open]
```

### Screen Direction Guidelines

Screen directions mark **major screen changes only** - not every click or action. Use them sparingly. The spoken words carry the content; brackets just indicate when you're switching to show something different.

**How many brackets?**
- 15-60 second Short: 1-3 screen directions max
- 2-3 minute Short: 3-5 screen directions max
- If you're adding a bracket every few sentences, you're overdoing it

**GOOD - Sparse, major transitions only:**
```
[Here I'll open the Nova Act playground]

"You describe what you want in plain English. Watch it run. And when you're ready, download that as a Python script."

[Now I'll show VS Code with the extension]

"Install the extension and you can debug step by step..."
```

**BAD - Over-specified (don't do this):**
```
[Here I'll type a command]
"Type what you want..."
[Here I'll show it running]
"Watch it execute..."
[Here I'll click download]
```

**Also never use:**
- Attitude/pose directions
- B-roll or "failed attempt" visuals
- "TEXT:" or "Screen:" labels
- Emotion directions

Keep brackets minimal. Let the spoken script flow naturally.

### Sponsorship for Dedicated Shorts

For sponsored Shorts (where the whole video is sponsored):
- Thank the sponsor at the END, not the beginning
- Use casual language: "Thanks [Sponsor] for working with me on this"
- Avoid corporate "sponsored by" - too stiff for Shorts

## Repurposing from Long-Form

When converting YouTube scripts or other content to Shorts:

1. **Extract ONE idea** - Not a summary, a single actionable insight
2. **Find the hook moment** - What's the most surprising/valuable 3 seconds?
3. **Cut ruthlessly** - If it doesn't serve the one idea, remove it
4. **Front-load value** - Viewers won't wait, deliver immediately
5. **Adapt the pacing** - What works in 10 minutes doesn't work in 30 seconds

## Tone Guidelines

Same voice as long-form, but compressed:

**DO:**
- Get to the point immediately
- Use simple, punchy sentences
- Speak with confidence and energy
- Make bold claims (but back them up)
- End abruptly - no "thanks for watching"

**DON'T:**
- Ramble or over-explain
- Use filler words
- Apologize or hedge
- Add unnecessary context
- Say "link in bio" or "follow for more" (algorithm penalty)
- Cite outdated tools/products - verify current status before naming specific frameworks

## Technical Checklist

Before finalizing:

- [ ] Hook stops scroll in first 3 seconds
- [ ] ONE clear idea only
- [ ] 75-150 words total
- [ ] Screen directions are informal and first-person
- [ ] No B-roll, attitude directions, or "TEXT:" labels
- [ ] No slow intro or lingering outro
- [ ] Could loop naturally? (bonus, not required)

## Algorithm Optimization

- **Post timing:** 9 AM - 2 PM weekdays, 10 AM - 12 PM weekends
- **Title:** Include keywords, keep under 40 characters
- **Description:** Keyword-rich, 100-150 characters
- **No watermarks:** From TikTok or other platforms
- **Consistency:** Regular posting builds algorithm trust
