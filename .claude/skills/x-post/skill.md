---
name: x-post
description: Create X (Twitter) posts for AI/tech content. Use when the user wants to create a tweet, thread, or X content. Triggers on requests like "create a tweet about...", "write a thread on...", or any X/Twitter content request.
---

# X Post Creator

Create X posts that sound like you talking, not like a content template.

## Source Attribution (Required)

Every post file must start with source metadata:

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

## Definition of Done

Post is ready when:
- [ ] Hook stops the scroll (first line is compelling)
- [ ] One clear idea per post
- [ ] Specific numbers/examples (no vague claims)
- [ ] CTA included (if thread)
- [ ] Under character limit (280 for tweet, <250 per thread tweet)
- [ ] No links in main post (move to reply)

## Format Selection

| Content | Format |
|---------|--------|
| Quick insight, observation, opinion | Single tweet (under 280 chars) |
| Multi-step process, deeper story | Thread (keep it tight) |
| Adding to discussion | Quote tweet |

## Philosophy

X is conversational. People scroll fast and pattern-match templates instantly.

**What works:**
- Writing like you'd talk to a friend who's into the same stuff
- Sharing something specific you did, built, or noticed
- Having an actual opinion (not a "hot take" for engagement)
- Genuine enthusiasm or frustration

**What kills posts:**
- Heavy formatting (bullets, numbers, arrows, symbols)
- Template phrases ("Here's the truth:", "Here's what I learned:", "Which one are you trying first?")
- The 🧵 emoji and "thread incoming" energy
- Staccato punchiness ("The result? Mind-blowing.")
- Engagement bait closers

## Anti-Patterns

**Template phrases to avoid:**
- "Unpopular opinion:" / "Hot take:" / "Contrarian take:"
- "Here's what I learned:"
- "Here's the truth:"
- "The [X] trick nobody talks about:"
- "Which one are you trying first?"
- "Follow for more [topic]"
- "🧵" or "Thread:" openers

**Formatting to avoid:**
- Arrows (→, ←) used for emphasis
- Excessive bullet points or numbered lists
- Every line being its own paragraph for "impact"
- Em dashes (LLM giveaway)

**Rhythm problems:**
- Multiple two-word sentences in a row
- Every sentence under 10 words
- Reads like headlines instead of thoughts

One punchy moment is fine. A whole post of staccato fragments isn't.

## What Actually Works

**Start with what you did or noticed:**
- "I spent two months building X" (specific, grounded)
- "Just realized Y about Z" (observation, not announcement)
- "Been using X for a week and..." (experience-based)

**Include specifics:**
- Real numbers, timeframes, tool names
- What actually happened, not abstract claims

**End naturally:**
- Link in reply if relevant
- Or just stop when you're done
- Don't manufacture a closer

## Writing Rules

- **Under 280 chars** for single tweets (shorter can work, but don't force it)
- **Under 250 chars** per tweet in threads
- **No hashtags** - they hurt reach on X
- **No links** in main post - put in reply
- **Line breaks** between ideas
- **Specific numbers** beat vague claims
- **Verify tools/products are current** - AI/tech moves fast; search "[tool] 2026 status" before citing specific frameworks or products

## Algorithm Tips

- Post 9 AM - 2 PM weekdays
- First hour engagement is critical
- Retweets worth 2x likes
- Rich media (images/video) = 40% better performance
- Stay in your niche for consistency

## What NOT to Do

- Links in main tweet (kills reach - put in reply)
- Hashtags (hurt reach on X)
- Vague language ("as needed", "regularly")
- Corporate speak
- Template structures that readers pattern-match instantly
