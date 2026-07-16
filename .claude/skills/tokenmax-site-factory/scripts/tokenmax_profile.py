#!/usr/bin/env python3
"""Summarize and gate a TokenMax site profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_profile(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(profile: dict[str, Any], min_confidence: float) -> dict[str, Any]:
    confidence = float(profile.get("confidence", 0.0))
    questions = profile.get("questions") or []
    build_command = (profile.get("build") or {}).get("command")
    ok = confidence >= min_confidence and bool(build_command) and not questions
    return {
        "ok": "true" if ok else "false",
        "confidence": confidence,
        "minConfidence": min_confidence,
        "runMode": profile.get("runMode"),
        "framework": profile.get("framework"),
        "packageManager": profile.get("packageManager"),
        "appRoots": len(profile.get("appRoots") or []),
        "contentSinks": len(profile.get("contentSinks") or []),
        "routeFamilies": len(profile.get("routeFamilies") or []),
        "sitemapFiles": len((profile.get("seoSurfaces") or {}).get("sitemapFiles") or []),
        "internalLinkSurfaces": len(profile.get("internalLinkSurfaces") or []),
        "buildCommand": build_command,
        "questions": questions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate a TokenMax site profile before writes.")
    parser.add_argument("profile", help="Path to .token-max/site-profile.json")
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-fail", action="store_true", help="Print failure but exit 0.")
    args = parser.parse_args()

    summary = summarize(load_profile(Path(args.profile)), args.min_confidence)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"ok: {summary['ok']}")
        print(f"framework: {summary['framework']}")
        print(f"runMode: {summary['runMode']}")
        print(f"confidence: {summary['confidence']}")
        print(f"build: {summary['buildCommand'] or 'missing'}")
        print(f"contentSinks: {summary['contentSinks']}")
        print(f"routeFamilies: {summary['routeFamilies']}")
        if summary["questions"]:
            print("questions:")
            for question in summary["questions"]:
                print(f"- {question}")

    if summary["ok"] != "true" and not args.no_fail:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
