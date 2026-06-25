#!/usr/bin/env python3
"""SM-2 spaced-repetition scheduler for the flashcards optional skill.

Given a card's current state and a grade (0-5), emit the next interval, ease,
and due offset. Stateless by design — the skill owns persistence.

Usage:
    sm2.py --grade 4 --ease 2.5 --interval 1 --reps 2
"""
from __future__ import annotations

import argparse
import json


def schedule(grade: int, ease: float, interval: int, reps: int) -> dict:
    """Return updated (ease, interval_days, reps) per the SM-2 algorithm."""
    if grade < 3:
        # Failed recall: reset repetition count, review again tomorrow.
        return {"ease": max(1.3, ease), "interval_days": 1, "reps": 0}

    reps += 1
    if reps == 1:
        interval = 1
    elif reps == 2:
        interval = 6
    else:
        interval = round(interval * ease)

    # Adjust ease factor; floor at 1.3.
    ease = ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    ease = max(1.3, round(ease, 4))

    return {"ease": ease, "interval_days": interval, "reps": reps}


def main() -> None:
    p = argparse.ArgumentParser(description="SM-2 scheduler.")
    p.add_argument("--grade", type=int, required=True, choices=range(0, 6))
    p.add_argument("--ease", type=float, default=2.5)
    p.add_argument("--interval", type=int, default=0)
    p.add_argument("--reps", type=int, default=0)
    args = p.parse_args()

    print(json.dumps(schedule(args.grade, args.ease, args.interval, args.reps)))


if __name__ == "__main__":
    main()
