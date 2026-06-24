---
name: reddit-post
description: Draft Reddit comments and self-posts that read like a real person, not marketing. Use when the user wants to draft a Reddit reply, write a Reddit post, turn knowledge into a community-native comment, or prepare content for the /reddit operator. Triggers on "draft a reddit comment", "write a reddit post", "reply to this thread", or any Reddit content request.
---

# Reddit Post & Comment Drafter

Reddit rewards people who add real value and punishes anything that smells like promotion. Draft like a knowledgeable human helping in a thread, never like a brand.

## Philosophy

**What earns upvotes and trust:**
- A direct answer to the actual question, in the first sentence
- Specifics that could not be templated (numbers, steps, a real tradeoff, "here is what bit me")
- Honest scope ("in my experience", "this varies by X", "I could be wrong about Y")
- Plain language, the way you would explain it to a friend in the thread

**What gets downvoted or removed:**
- Any whiff of self-promotion, a link drop, or a brand name where it was not asked for
- Generic advice with no specifics ("just shop around", "it depends")
- Corporate voice, hype, or a structure that looks copy-pasted
- Answering a question nobody asked so you can pivot to your pitch

## Comment vs post

- **Comment** (default, and the only move until the account has karma and history): find a thread where someone asked a real question you can answer well, and answer it. Most value lives here.
- **Post** (self-post): only once the account has standing. A post must be genuinely useful on its own, titled like a real person wrote it, and answer questions in the comments.

## Universal requirements

**Specificity.** Every draft needs at least two concrete details: a number, a step, a named mechanism, a real example, or a specific tradeoff. If you cannot include specifics, you are abstracting - go get the real detail first.

**Experience attribution.** Be clear about what is yours vs secondhand. "I have seen X" vs "the published rule is Y". Never present a guess as lived experience.

**Honest caveats.** "This varies by state", "I have only done this for X", "double check Y with the official source". Qualification builds trust; weak hedging ("it might perhaps possibly") kills it.

**No em-dashes or en-dashes.** Use " - " (a single hyphen with spaces). Em-dashes are the most reliable AI-slop tell.

**No engagement bait.** No "agree or disagree?", no "what would you add?", no "DM me", no "tag a friend". End on a useful note or just stop.

**Length follows content.** A two-sentence comment that nails the answer beats a padded essay. Some answers need a numbered list; many do not.

## Anti-patterns (Reddit users spot these instantly)

- Emoji bullets, perfectly parallel lists, "broetry" one-line-per-sentence formatting
- Opening with "Great question!" or "Here's the thing"
- A helpful answer that quietly steers toward one product or site
- Mentioning a brand, tool, or site that the asker did not ask about
- Numbered lists when the content is not actually a sequence
- Any sentence you would not say out loud to a person in the thread

## Voice check (before handing off to the operator)

- Would a real person in this subreddit write this, or does it read like content?
- Did I answer the actual question in the first line?
- Are there at least two specifics?
- Zero promotion, zero unrequested links or brand names?
- No em-dashes, no engagement bait?

If any check fails, rewrite. The draft then goes to the `/reddit` operator, which posts only on explicit approval. This skill never posts anything itself - it drafts.
