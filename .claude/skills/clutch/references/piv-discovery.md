# Discovery: From Idea to PRD

## Overview

When a user starts CLUTCH with no PRD and no PRD path, run this discovery process to understand what they want to build, fill in the gaps with your expertise, and generate a PRD automatically.

**Audience:** Vibe coders — people with ideas who can't necessarily articulate architectures or framework choices. Be friendly, conversational, and non-intimidating. Propose answers when the user doesn't know — don't interrogate.

## Discovery Questions

Present these as a single casual message. Skip any the user already answered.

```
Hey! Let's figure out what we're building. A few quick questions:

1. **What's your idea?** Tell me what you want to build — even a rough description works.

2. **Who's gonna use it?** End users, just you, a team?

3. **Any tech preferences?** If you know what language/framework you want, great.
   If not, I'll figure out the best fit and suggest it.

4. **Starting from scratch or adding to something?** If there's an existing
   codebase, point me to it and I'll analyze it.

5. **What does "done" look like?** When you imagine it working, what are 1-3
   things you'd see?

6. **Anything you DON'T want built right now?** Helps me keep scope tight.

7. **How big is this?** Quick weekend project, or multi-phase build?
   (I can propose phases if you're not sure.)
```

## Adaptation Rules

- **If the user already provided context** (e.g., "build me a todo app in React"), skip answered questions — only ask what's still unknown.
- **If the user gives vague answers**, ask 1-2 targeted follow-ups instead of repeating all 7.
- **Match the user's technical level** — concise with experienced devs, more guiding with explorers.
- **Always propose and confirm, never interrogate** — "Here's what I think — does this sound right?"

## AI-Assisted Gap Filling

This is the key differentiator. Don't just ask — help.

| Gap | Action |
|-----|--------|
| User doesn't know tech stack | Research using web search and/or codebase analysis, then **propose** a stack with brief rationale ("I'd go with Next.js + Supabase here because...") |
| Existing codebase exists | Scan package.json / Cargo.toml / requirements.txt / go.mod / etc. and auto-detect stack, patterns, conventions |
| User can't articulate phases | Propose 3-4 phases based on scope complexity |
| Vague description | Ask 1-2 targeted follow-ups, don't re-ask everything |
| User doesn't know what to exclude | Propose sensible scope boundaries based on MVP best practices |

## Discovery-to-PRD Mapping

| Question | PRD Sections Fed |
|----------|-----------------|
| 1. What's your idea? | Executive Summary, Mission |
| 2. Who's gonna use it? | Target Users |
| 3. Tech preferences? | Technology Stack, Core Architecture |
| 4. Scratch or existing? | Core Architecture, triggers codebase analysis |
| 5. What does "done" look like? | Success Criteria, MVP Scope (In Scope) |
| 6. What's out of scope? | MVP Scope (Out of Scope) |
| 7. How big is this? | Implementation Phases |

## Quality Gate

Before generating the PRD, you must have answers (user-provided OR AI-proposed-and-confirmed) for at least:

- **Q1** — What's the idea (must know what we're building)
- **Q3** — Tech stack (user-chosen or AI-proposed)
- **Q5** — Success criteria (must know what "done" looks like)

If any of these three are still unclear after the conversation, ask one more targeted question before proceeding.

## After Discovery

Once you have sufficient answers:

1. Run project setup (create PRDs/, PRPs/templates/, PRPs/planning/ directories)
2. Use the PRD creation process to generate a PRD from discovery answers + your proposals
3. Write PRD to `PROJECT_PATH/PRDs/PRD-{project-name}.md`
4. Set PRD_PATH to the generated file
5. Auto-detect phases from the PRD
6. Continue into normal Phase Workflow
