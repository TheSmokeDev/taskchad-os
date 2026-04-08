---
name: yt-script
description: Create YouTube video scripts from conversation input. Use when the user wants to create a YouTube script, plan a video, or asks for help with video content creation. Triggers on requests like "create a script for...", "help me plan a video about...", "write a YouTube script", or any video content planning request.
---

# YouTube Script Creator

Create YouTube video scripts based on conversation input, using an established template and style guidelines.

## Workflow

1. **Create the script file**
   - Create new file in your notes directory named after the video topic
   - Copy template from your YouTube Script Template (if you have one)

2. **Do web research**
   - Search for relevant documentation, articles, repos related to the topic
   - Gather 1-10 links to include in Research section

3. **Fill each section systematically** (in this order):
   - Tags
   - Source
   - Research
   - Audience Reactions
   - Title
   - Thumbnail Text
   - Thumbnail Ideas
   - Description
   - Links
   - Outline
   - Intro

## Section Requirements

### Tags
Always: `#youtube-script`

### Source (Required)
Reference the original content this script is based on:

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

### Research
- Web research for relevant links
- Format: `- Description: URL` (description on same line, URL on next)
- 1-10 bullet points, keep concise
- Include docs, articles, repos, relevant resources
- **Verify tools/products are current** - AI/tech moves fast; search "[tool] 2026 status" before citing specific frameworks or products

### Audience Reactions
- 3-5 reactions written as YouTube comments
- Capture what viewers should resonate with
- Express gratitude, excitement, value received
- Examples:
  - "Man I can really tell this is the future of AI agents, thanks for leading the charge!"
  - "This makes so much sense, love the examples too."
  - "Best breakdown I've ever seen on YouTube."

### Title
- Generate 3-7 alternatives
- **Bold** only one as the recommended choice
- See [references/style-guide.md](references/style-guide.md) for title patterns

### Thumbnail Text
- 3-6 short options (2-5 words max)
- **Bold** the recommended choice
- Action-oriented or benefit-focused
- Examples: "AI Dev Team", "Agent Evolution", "Ultimate RAG Tool"

### Thumbnail Ideas
- 2-4 visual descriptions
- Reference: screenshots, diagrams, logos, personal image with elements
- GitHub repo style with logo "swoosh" is common
- Excalidraw diagrams work well

### Description
- 2-3 paragraphs (max 4 small ones)
- Strong hook identifying problem/opportunity FIRST
- Professional but approachable tone
- Ends with what viewer will learn
- NOT clickbaity, NOT cringe

### Links
- Sponsor/course links at top
- Relevant resources, documentation, repos
- Use `[link]` placeholder for unknowns
- Format:
  ```
  Resource Name:
  https://example.com
  ```

### Outline
- Start with `- Intro - use description.` or similar
- Bullet points with sub-bullets for details
- Include notes like "show X", "demo Y", "cover Z"
- End with `- Outro`

### Intro
- Based on description but FOR SPEAKING
- More casual, conversational
- Can use **bold** for emphasis on spoken words
- Same hook but worded naturally
- Can include `[Show X]` cues for visuals

## Tone Guidelines

See [references/style-guide.md](references/style-guide.md) for complete style reference.

**Key principles:**
- Direct and confident but not arrogant
- Address viewer with "you" frequently
- Casual but professional
- Self-aware and relatable
- Make bold claims but back them up
- NOT cringe, NOT overly clickbaity, NOT robotic/LLM-sounding

**Characteristic phrases (examples - use naturally, don't force):**
- "And here's the thing"
- "But honestly"
- "I've got you covered"
- "This my friend is..."
- "Let me show you"
- "That's what I'll show you in this video"

These are examples of the creator's voice. Use similar natural, conversational phrases that feel authentic - don't treat this as a required checklist.
