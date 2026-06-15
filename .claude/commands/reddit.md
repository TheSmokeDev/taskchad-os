# /reddit - Reddit operator (research + approval-gated posting)

`/reddit` drives the visible Chrome CDP session for Reddit. It does not use the Reddit API and never launches a headless browser. It acts as whatever Reddit account is logged into the visible session.

## Subcommands

- `/reddit status` - browser/CDP readiness for the Reddit session.
- `/reddit research <query or r/subreddit>` - read-only. Searches Reddit (or browses a subreddit) and returns the ranked threads so you can find where to help. No approval needed.
- `/reddit comment <thread_url> | <body> | <approval phrase>` - drafts a reply. Blocked by default; it posts only when the FINAL pipe-delimited segment is EXACTLY `post this comment to reddit now`.
- `/reddit post <subreddit> | <title> | <body> | <approval phrase>` - drafts a self-post. Blocked by default; it posts only when the FINAL pipe-delimited segment is EXACTLY `post this to reddit now`.

The approval is a **separate trailing segment**, not text appended to the body. A body that happens to end with the approval phrase can NEVER approve itself — the confirmation must be its own `| <phrase>` segment. The thread URL must be an absolute `https://reddit.com` URL, and the subreddit must match `^[A-Za-z0-9_]{2,21}$` (no slashes, query, or path) or the command is rejected.

## The flow (draft -> approve -> post)

1. Draft the comment or post using the `reddit-post` skill (value-first, no promotion, no em-dashes).
2. Show the draft to the user. Do not post.
3. The user posts only by re-sending the command with the approval phrase as the FINAL pipe-delimited segment (e.g. `/reddit comment <url> | <body> | post this comment to reddit now`).
4. Before driving, the handler refuses if the visible Chrome is not ready (audited `failed`), and rejects an invalid thread URL or subreddit.
5. Every action (blocked, posted, failed, rejected) is written to the sanitized browser audit log. No cookies, tokens, or query strings are ever logged.

## Rules

- Research first. Find a real question you can answer well before drafting anything.
- One approval per post. Never batch, schedule, or loop posts.
- If a captcha or rate-limit appears, STOP and tell the user.
- This command is the generic operator. Business-specific targeting (which subreddits, which voice, what to say) lives in the consuming project's playbook, not here.
