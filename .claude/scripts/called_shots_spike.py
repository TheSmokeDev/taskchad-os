"""Called-Shots detection spike harness (T2 #188 — the architecture doc's Spike 2).

Replays labeled sample turns through the DETERMINISTIC staked-position
detection gate and reports fire/no-fire counts + precision. Zero LLM, zero
network, zero writes.

FRAMING (honest scope): the BUNDLED sample set below is a REGRESSION LOCK on
the detection patterns — it exists so a pattern edit that reintroduces a known
false-positive class fails a test. It is NOT the architecture doc's arming
evidence. The arming bar for ``CALLED_SHOTS_CHALLENGE_MODE=live`` remains the
doc's Spike 2 as written: a replay of REAL historical operator turns (via
``--jsonl``, labeled) with a measured false-positive rate that clears the
decision rule. Until that replay is run and reviewed, live mode stays unarmed.

Usage:
    uv run python called_shots_spike.py                 # bundled regression set
    uv run python called_shots_spike.py --jsonl f.jsonl # {"text":..,"staked":bool} per line
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# (text, staked) — staked=True means a human labeled this a challengeable
# operator position. The negatives deliberately include the near-misses that
# caused the spike (questions, commands, passing remarks, short acks).
BUNDLED_SAMPLES: tuple[tuple[str, bool], ...] = (
    # --- staked positions (should fire) ---
    ("I think raising the price to $50 is the best way to fix our margin problem here", True),
    ("I'm convinced that we need to drop the Etsy channel and go direct only", True),
    ("we should definitely go with SQLite for the ledger instead of the file approach", True),
    ("I've decided to move all outbound calls to the morning block from now on", True),
    ("let's go with the premium tier pricing for every new client going forward", True),
    ("my plan is to run the whole campaign on LinkedIn only this quarter", True),
    ("I believe this landing page copy is the best version we've had so far", True),
    ("the best approach is to rebuild the demo site from scratch this weekend", True),
    ("I'm going to double the ad budget because the numbers look great this week", True),
    ("I want to bet on the voice vertical over the website funnel for Q3 growth", True),
    # --- non-stakes (must NOT fire) ---
    ("what do you think is the best way to price the new tier for agencies?", False),
    ("should I go with SQLite or Postgres for this one, what's your take here?", False),
    ("how are we looking across all the boards this morning, anything urgent?", False),
    ("/budget spending", False),
    ("thanks homie", False),
    ("can you pull the latest call logs from the voice platform for me please", False),
    ("that pricing meeting yesterday ran way too long and we lost the thread", False),
    ("the client said they think our price is too high compared to the others", False),
    ("do you believe the seo numbers we got back from the last crawl are right?", False),
    ("ok sounds good, ship it", False),
    ("I think it might rain later so I'm working from home this afternoon ok", False),
    ("remind me to check the stripe dashboard tomorrow morning before standup", False),
    # --- Codex R1 hostile probes (each seeded a guard; labeled negatives) ---
    ('the client told me "I think we should go with the premium plan for sure" so let me know what you find', False),
    ("here's the snippet from the config:\n```\n# I think we should use SQLite here, the best way\nDB = 'sqlite'\n```\nrun it when you get a chance", False),
    ("If I think the higher price is the best way to fix margins, I'll still wait for the numbers first", False),
    ("I'm convinced we should drop Etsy, right? but also we could keep the kits running for now", False),
    # --- more of each guard class ---
    ("owner said he thinks we should switch to Postgres for the ledger work soon", False),
    ("suppose we should just go with the cheaper hosting for now, hypothetically that saves money", False),
    ("the template literally contains `we should definitely go with X` in the comment section there", False),
    ('> I believe this is the best way forward\nthat was from the old thread, ignore it for now', False),
)


def run_spike(samples) -> dict:
    from cognition.challenge import detect_staked_position

    tp = fp = tn = fn = 0
    misses: list[tuple[str, bool]] = []
    for text, staked in samples:
        fired = detect_staked_position(text) is not None
        if fired and staked:
            tp += 1
        elif fired and not staked:
            fp += 1
            misses.append((text, staked))
        elif not fired and staked:
            fn += 1
            misses.append((text, staked))
        else:
            tn += 1
    total_fired = tp + fp
    return {
        "samples": len(list(samples)),
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "precision": (tp / total_fired) if total_fired else 1.0,
        "misses": misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", help="labeled samples: {'text':..,'staked':bool}")
    args = parser.parse_args()

    if args.jsonl:
        samples = []
        for line in Path(args.jsonl).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            samples.append((str(row["text"]), bool(row["staked"])))
    else:
        samples = list(BUNDLED_SAMPLES)

    report = run_spike(samples)
    print(f"samples:        {report['samples']}")
    print(f"true positive:  {report['true_positive']}")
    print(f"false positive: {report['false_positive']}")
    print(f"true negative:  {report['true_negative']}")
    print(f"false negative: {report['false_negative']}")
    print(f"precision:      {report['precision']:.2f}")
    if report["misses"]:
        print("\nmisclassified:")
        for text, staked in report["misses"]:
            kind = "FN (staked, no fire)" if staked else "FP (fired, not staked)"
            print(f"  [{kind}] {text[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
