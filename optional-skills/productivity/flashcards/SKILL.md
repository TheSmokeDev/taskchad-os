---
name: flashcards
description: Turn vault notes, lessons, or a topic into spaced-repetition flashcards and quiz the user on due cards. Use when the user wants to study, review, memorize, retain notes, or asks to be quizzed. Stores cards as a simple JSON deck the agent can schedule reviews against.
version: 1.0.0
author: YourProduct OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  YourProduct:
    category: productivity
    tags: [flashcards, spaced-repetition, study, memory, sm2]
    related_skills: [daily-brief]
    mutates: false
---

# Flashcards

Generate spaced-repetition flashcards from notes and run review sessions.

## Deck format

One JSON file per deck under `vault/flashcards/<deck>.json`:

```json
[
  {
    "id": "uuid",
    "front": "What does the recall pipeline do?",
    "back": "Surfaces durable memory relevant to the current message.",
    "ease": 2.5,
    "interval_days": 0,
    "due": "2026-06-25",
    "reps": 0
  }
]
```

## Generating cards

From a note or topic, extract atomic question/answer pairs — one fact per card,
front phrased as a question, back as a short answer. Avoid compound cards. Aim
for 5–15 cards per note; quality over volume.

## Review session

1. Load the deck, filter to cards where `due <= today`.
2. Show `front`, wait for the user's answer, then reveal `back`.
3. Ask the user to grade 0–5 (0 = blank, 5 = perfect).
4. Update schedule with the SM-2 algorithm in `scripts/sm2.py` and persist.

```bash
uv run python optional-skills/productivity/flashcards/scripts/sm2.py --grade 4 \
  --ease 2.5 --interval 1 --reps 2
```

End the session with a one-line tally (reviewed / again / mastered).
