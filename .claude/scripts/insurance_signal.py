"""Insurance Signal watcher — v1 (deterministic, no LLM calls).

Spec: coding-vault/plans/insurance-signal-lane-2026-08-07.md (tooling decision
RESOLVED 2026-08-07: boring Homie-native build, NOT TrendRadar).

Three intake lanes:
  Tier 1  official CA pages (DOI press room, DOI legal-info hub, DMV
          insurance requirements, CLCA/mylowcostauto) — page change
          detection via sha256 of normalized text. WAF bot-wall responses
          (Incapsula/Cloudflare) are logged as "source down", never hashed.
  Tier 2  competitor sitemap diffing (geico, thezebra, insurify, nerdwallet) —
          new URLs = COMPETITOR-MOVE events. Sitemaps only (robots-safe).
  Feeds   RSS intake via feedparser (present in the venv, so enabled in v1) —
          Insurance Journal national. New entries = NEWS events.

State:  .claude/data/insurance-signal-state.json (hashes + URL sets + last-run).
        First run (or --seed) is baseline seeding: no alerts, state only.
Digest: on runs with change events, writes
        vault/memory/insurance-signal/YYYY-Www.md and regenerates
        INSURANCE-SIGNAL-INDEX.md. No events -> prints "no change events".

Fail-open per lane hard rules: any source down logs "source down: X" and the
run continues; a broken source never blocks the digest.

Classification in v1 is deterministic: source-tier labels + the heuristics in
classify_events(). The LLM classification lane (VACUUM / Bali-criteria scoring
through the Homie runtime) comes later — see the LLM CLASSIFICATION HOOK below.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

from config import DATA_DIR, MEMORY_DIR, now_local  # noqa: E402
from shared import load_state, save_state  # noqa: E402

try:
    import feedparser
except ImportError:  # feeds lane disabled when feedparser is absent
    feedparser = None

STATE_FILE = DATA_DIR / "insurance-signal-state.json"
DIGEST_DIR = MEMORY_DIR / "insurance-signal"
INDEX_FILE = DIGEST_DIR / "INSURANCE-SIGNAL-INDEX.md"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 HomieInsuranceSignal/1.0"
)
TIMEOUT_S = 20
MAX_CHILD_SITEMAPS = 25  # bound per competitor; index children beyond this are skipped

TIER1_SOURCES = [
    {
        "id": "ca-doi-press-releases",
        "name": "CA DOI press room",
        "url": "https://www.insurance.ca.gov/0400-news/0100-press-releases/",
    },
    {
        "id": "ca-doi-legal-info",
        "name": "CA DOI legal information (bulletins/rate-filings hub)",
        # Deep paths (0100-bulletins/, 0200-rate-filing/) all serve the same
        # generic CommonSpot template to plain GETs; the parent hub page is
        # the deepest DOI page with real server-rendered content (2026-08-07).
        "url": "https://www.insurance.ca.gov/0250-insurers/0500-legal-info/",
    },
    {
        "id": "ca-dmv-insurance-requirements",
        "name": "CA DMV insurance requirements",
        # Spec URL (.../driver-licenses-identification-cards/vehicle-registration/...)
        # 404s; this is the live canonical page as of 2026-08-07.
        "url": "https://www.dmv.ca.gov/portal/vehicle-registration/insurance-requirements/",
    },
    {
        "id": "clca-mylowcostauto",
        "name": "CLCA program (mylowcostauto.com)",
        # Incapsula-fronted: plain requests get a block page, which v1 logs
        # as "source down" (fail-open). A browser-backed fetch is a v2 lane.
        "url": "https://www.mylowcostauto.com/",
    },
]

TIER2_SOURCES = [
    {"id": "geico", "name": "GEICO", "sitemap": "https://www.geico.com/sitemap.xml"},
    {"id": "thezebra", "name": "The Zebra", "sitemap": "https://www.thezebra.com/sitemap.xml"},
    {"id": "insurify", "name": "Insurify", "sitemap": "https://insurify.com/sitemap.xml"},
    {
        "id": "nerdwallet",
        "name": "NerdWallet",
        "sitemap": "https://www.nerdwallet.com/sitemaps/us/wp-sitemap.xml",
    },
]

FEEDS = [
    {
        "id": "insurance-journal-national",
        "name": "Insurance Journal (national)",
        "url": "https://www.insurancejournal.com/rss/news/national/",
    },
]


# ---------------------------------------------------------------------------
# Fetch helpers (fail-open: callers catch and log "source down: X")
# ---------------------------------------------------------------------------


def fetch_text(url: str) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_S,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"\s+")


def normalize_page_text(html: str) -> str:
    """Strip scripts/styles/tags and collapse whitespace for a stable hash."""
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _TAGS.sub(" ", text)
    return _WS.sub(" ", text).strip().lower()


def page_hash(html: str) -> str:
    return hashlib.sha256(normalize_page_text(html).encode("utf-8")).hexdigest()


_BLOCK_SIGNATURES = (
    "incapsula incident id",
    "request unsuccessful",
    "attention required! | cloudflare",
    "access denied",
    "are you a robot",
)


def looks_like_block_page(html: str) -> bool:
    """WAF/bot-wall responses are not content — hashing them makes every run
    a false change event (e.g. rotating Incapsula incident IDs)."""
    text = normalize_page_text(html)[:2000]
    return any(sig in text for sig in _BLOCK_SIGNATURES)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def collect_sitemap_urls(sitemap_url: str) -> list[str]:
    """Fetch a sitemap or sitemap index; return the URL set (bounded)."""
    root = ET.fromstring(fetch_text(sitemap_url).encode("utf-8"))
    locs = [
        el.text.strip()
        for el in root.iter()
        if _local_name(el.tag) == "loc" and el.text
    ]
    if _local_name(root.tag) == "urlset":
        return sorted(set(locs))
    # sitemap index: fetch children up to the cap
    urls: set[str] = set()
    for child in locs[:MAX_CHILD_SITEMAPS]:
        try:
            child_root = ET.fromstring(fetch_text(child).encode("utf-8"))
        except Exception:
            print(f"source down: child sitemap {child}", file=sys.stderr)
            continue
        for el in child_root.iter():
            if _local_name(el.tag) == "loc" and el.text:
                urls.add(el.text.strip())
    return sorted(urls)


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------


def classify_events(events: list[dict]) -> list[dict]:
    """Attach a deterministic v1 classification to each raw event.

    --- LLM CLASSIFICATION HOOK (future lane) ---
    When the runtime lane lands, pass `events` through the Homie runtime here
    to classify VACUUM | COMPETITOR-MOVE | NOISE and score the Bali criteria
    (primary source? packaged in EN? in ES? demand phrasing visible?). Keep
    this a pure function over events so the deterministic fallback stays
    testable; route the LLM path through runtime/, never inline provider calls.
    """
    heuristics = (
        "rate", "bulletin", "regulation", "filing", "sr22", "sr-22",
        "minimum", "liability", "limits", "program", "eligib",
    )
    for event in events:
        text = f"{event.get('source', '')} {event.get('detail', '')}".lower()
        if event["kind"] == "TIER1-CHANGE":
            # heuristic: DOI/DMV/CLCA pages are all regulation-shaped, but a
            # keyword hit upgrades the label from a bare page-change signal
            event["classification"] = (
                "VACUUM-CANDIDATE" if any(h in text for h in heuristics) else "TIER1-CHANGE"
            )
        elif event["kind"] == "COMPETITOR-MOVE":
            event["classification"] = "COMPETITOR-MOVE"
        else:
            event["classification"] = "NEWS"
    return events


# ---------------------------------------------------------------------------
# State + intake lanes
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return now_local().isoformat()


def run_tier1(state: dict, down: list[str], *, seed: bool) -> list[dict]:
    events: list[dict] = []
    tier1_state = state.setdefault("tier1", {})
    for source in TIER1_SOURCES:
        try:
            html = fetch_text(source["url"])
            if looks_like_block_page(html):
                raise ValueError("bot-wall page (WAF block), not content")
            digest = page_hash(html)
        except Exception as exc:
            print(f"source down: {source['id']} ({source['url']}) — {exc}", file=sys.stderr)
            # keep any stale baseline entry so recovery still diffs correctly
            down.append(source["id"])
            continue
        prior = tier1_state.get(source["id"], {})
        tier1_state[source["id"]] = {
            "hash": digest,
            "url": source["url"],
            "name": source["name"],
            "checked_at": _now_iso(),
        }
        if not seed and prior.get("hash") and prior["hash"] != digest:
            events.append(
                {
                    "kind": "TIER1-CHANGE",
                    "source": source["name"],
                    "detail": f"page content changed: {source['url']}",
                }
            )
    return events


def run_tier2(state: dict, down: list[str], *, seed: bool) -> list[dict]:
    events: list[dict] = []
    tier2_state = state.setdefault("tier2", {})
    for source in TIER2_SOURCES:
        try:
            urls = collect_sitemap_urls(source["sitemap"])
        except Exception:
            print(f"source down: {source['id']} ({source['sitemap']})", file=sys.stderr)
            # keep any stale baseline entry so recovery still diffs correctly
            down.append(source["id"])
            continue
        prior = tier2_state.get(source["id"], {})
        prior_urls = set(prior.get("urls", []))
        tier2_state[source["id"]] = {
            "urls": urls,
            "sitemap": source["sitemap"],
            "name": source["name"],
            "checked_at": _now_iso(),
        }
        if not seed and prior_urls:
            new_urls = sorted(set(urls) - prior_urls)
            if new_urls:
                events.append(
                    {
                        "kind": "COMPETITOR-MOVE",
                        "source": source["name"],
                        "detail": f"{len(new_urls)} new URLs",
                        "new_urls": new_urls[:50],
                    }
                )
    return events


def run_feeds(state: dict, down: list[str], *, seed: bool) -> list[dict]:
    events: list[dict] = []
    if feedparser is None:
        print("feeds lane skipped: feedparser not installed", file=sys.stderr)
        return events
    feed_state = state.setdefault("feeds", {})
    for feed in FEEDS:
        try:
            parsed = feedparser.parse(
                feed["url"],
                request_headers={"User-Agent": USER_AGENT},
            )
            entries = parsed.entries
        except Exception:
            print(f"source down: feed {feed['id']} ({feed['url']})", file=sys.stderr)
            down.append(feed["id"])
            continue
        prior = feed_state.get(feed["id"], {})
        seen = set(prior.get("seen_ids", []))
        current = []
        new_entries = []
        for entry in entries:
            entry_id = (
                getattr(entry, "id", None) or getattr(entry, "link", None) or entry.get("title", "")
            )
            if not entry_id:
                continue
            current.append(entry_id)
            if entry_id not in seen:
                new_entries.append(entry)
        feed_state[feed["id"]] = {
            "seen_ids": current,
            "url": feed["url"],
            "name": feed["name"],
            "checked_at": _now_iso(),
        }
        if not seed and seen and new_entries:
            events.append(
                {
                    "kind": "NEWS",
                    "source": feed["name"],
                    "detail": f"{len(new_entries)} new feed items",
                    "items": [
                        {
                            "title": e.get("title", ""),
                            "link": e.get("link", ""),
                            "published": e.get("published", ""),
                        }
                        for e in new_entries[:20]
                    ],
                }
            )
    return events


# ---------------------------------------------------------------------------
# Digest notes
# ---------------------------------------------------------------------------


def _week_label(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def write_digest(state: dict, events: list[dict]) -> Path:
    now = now_local()
    week = _week_label(now)
    tier1_events = [e for e in events if e["kind"] == "TIER1-CHANGE"]
    competitor_events = [e for e in events if e["kind"] == "COMPETITOR-MOVE"]
    news_events = [e for e in events if e["kind"] == "NEWS"]
    down = state.get("sources_down_last_run", [])

    lines = [
        "---",
        "tags: [signal, insurance, auto-generated]",
        f"week: {week}",
        f"date: {now.date().isoformat()}",
        f"tier1_changes: {len(tier1_events)}",
        f"competitor_moves: {len(competitor_events)}",
        f"news_items: {len(news_events)}",
        f"sources_down: {len(down)}",
        "classified_by: deterministic-v1",
        "---",
        "",
        f"# Insurance Signal — {week}",
        "",
        "## Tier-1 changes (official sources)",
        "",
    ]
    if tier1_events:
        for e in tier1_events:
            lines.append(f"- **{e['source']}** [{e['classification']}] — {e['detail']}")
    else:
        lines.append("- none")

    lines += ["", "## Competitor moves (Tier-2 sitemap diffs)", ""]
    if competitor_events:
        for e in competitor_events:
            lines.append(f"- **{e['source']}** [{e['classification']}] — {e['detail']}")
            for url in e.get("new_urls", [])[:20]:
                lines.append(f"  - {url}")
    else:
        lines.append("- none")

    lines += ["", "## Feed items", ""]
    if news_events:
        for e in news_events:
            lines.append(f"- **{e['source']}** — {e['detail']}")
            for item in e.get("items", [])[:20]:
                lines.append(f"  - [{item['title']}]({item['link']}) — {item['published']}")
    else:
        lines.append("- none")

    lines += ["", "## Sources down (fail-open)", ""]
    if down:
        for src in down:
            lines.append(f"- {src}")
    else:
        lines.append("- none")

    lines += [
        "",
        "---",
        "",
        "_v1 deterministic classification (source-tier labels + heuristics). "
        "LLM VACUUM/Bali-criteria classification lane pending — see "
        "classify_events() hook in insurance_signal.py._",
        "",
    ]

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    note_path = DIGEST_DIR / f"{week}.md"
    note_path.write_text("\n".join(lines), encoding="utf-8")
    regenerate_index()
    return note_path


def regenerate_index() -> None:
    weeks: list[tuple[str, dict[str, int]]] = []
    for note in sorted(DIGEST_DIR.glob("????-W??.md"), reverse=True):
        counts = {"tier1_changes": 0, "competitor_moves": 0, "news_items": 0}
        text = note.read_text(encoding="utf-8")
        for key in counts:
            match = re.search(rf"^{key}: (\d+)$", text, re.MULTILINE)
            if match:
                counts[key] = int(match.group(1))
        weeks.append((note.stem, counts))

    lines = [
        "---",
        "tags: [system, auto-compiled]",
        "status: current",
        f"date: {now_local().date().isoformat()}",
        'summary: "Auto-generated index of insurance-signal weekly digests."',
        "related:",
        '  - "[[MOC-seo-geo]]"',
        "---",
        "",
        "# Insurance Signal — Lane Index",
        "",
        "_Auto-generated lane index — regenerated by insurance_signal.py on every "
        "digest run. Do not edit by hand._",
        "",
        "",
        f"## Weekly digests ({len(weeks)})",
        "",
        "| Note | Tier-1 changes | Competitor moves | News items |",
        "|---|---|---|---|",
    ]
    for week, counts in weeks:
        lines.append(
            f"| [[{week}]] | {counts['tier1_changes']} | "
            f"{counts['competitor_moves']} | {counts['news_items']} |"
        )
    lines.append("")
    INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_seed() -> int:
    state = load_state(STATE_FILE)
    down: list[str] = []
    run_tier1(state, down, seed=True)
    run_tier2(state, down, seed=True)
    run_feeds(state, down, seed=True)
    state["last_run"] = _now_iso()
    state["last_run_mode"] = "seed"
    state["sources_down_last_run"] = sorted(set(down))
    save_state(state, STATE_FILE)
    tier1_ok = len(state.get("tier1", {}))
    tier2_ok = len(state.get("tier2", {}))
    feeds_ok = len(state.get("feeds", {}))
    print(
        f"[{state['last_run']}] baseline seeded — no alerts (state only): "
        f"{tier1_ok}/{len(TIER1_SOURCES)} tier-1 pages hashed, "
        f"{tier2_ok}/{len(TIER2_SOURCES)} tier-2 sitemaps captured, "
        f"{feeds_ok}/{len(FEEDS)} feeds snapshotted"
    )
    if down:
        print(f"sources down during seed (fail-open): {', '.join(sorted(set(down)))}")
    print(f"state: {STATE_FILE}")
    return 0


def cmd_check() -> int:
    state = load_state(STATE_FILE)
    if not state.get("tier1") and not state.get("tier2") and not state.get("feeds"):
        print("no baseline state found — running as first-run baseline seed (no alerts)")
        return cmd_seed()

    events: list[dict] = []
    down: list[str] = []
    events += run_tier1(state, down, seed=False)
    events += run_tier2(state, down, seed=False)
    events += run_feeds(state, down, seed=False)
    state["sources_down_last_run"] = sorted(set(down))

    state["last_run"] = _now_iso()
    state["last_run_mode"] = "check"
    events = classify_events(events)
    state["last_run_event_count"] = len(events)
    save_state(state, STATE_FILE)

    if not events:
        down_note = f" ({len(down)} sources down)" if down else ""
        print(f"no change events{down_note}")
        return 0

    note_path = write_digest(state, events)
    tier1_n = sum(1 for e in events if e["kind"] == "TIER1-CHANGE")
    comp_n = sum(1 for e in events if e["kind"] == "COMPETITOR-MOVE")
    news_n = sum(1 for e in events if e["kind"] == "NEWS")
    print(
        f"[{state['last_run']}] {len(events)} change events "
        f"(tier-1: {tier1_n}, competitor: {comp_n}, news: {news_n})"
    )
    for e in events:
        print(f"  [{e['classification']}] {e['source']} — {e['detail']}")
    print(f"digest: {note_path}")
    print(f"index: {INDEX_FILE}")
    return 0


def cmd_status() -> int:
    state = load_state(STATE_FILE)
    if not state:
        print("no state — run `--seed` first")
        return 1
    print(f"state file: {STATE_FILE}")
    print(f"last run: {state.get('last_run', 'never')} ({state.get('last_run_mode', 'unknown')})")
    print(f"last run events: {state.get('last_run_event_count', 0)}")
    print(f"tier-1 sources tracked: {len(state.get('tier1', {}))}/{len(TIER1_SOURCES)}")
    for sid, entry in state.get("tier1", {}).items():
        print(f"  {sid}: checked {entry.get('checked_at', '?')}")
    print(f"tier-2 sources tracked: {len(state.get('tier2', {}))}/{len(TIER2_SOURCES)}")
    for sid, entry in state.get("tier2", {}).items():
        print(f"  {sid}: {len(entry.get('urls', []))} URLs, checked {entry.get('checked_at', '?')}")
    print(f"feeds tracked: {len(state.get('feeds', {}))}/{len(FEEDS)}")
    for fid, entry in state.get("feeds", {}).items():
        print(
            f"  {fid}: {len(entry.get('seen_ids', []))} items, "
            f"checked {entry.get('checked_at', '?')}"
        )
    down = state.get("sources_down_last_run", [])
    if down:
        print(f"sources down last run: {', '.join(down)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Insurance Signal watcher (v1, deterministic)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--seed", action="store_true", help="force baseline seeding (no alerts)")
    group.add_argument(
        "--check", action="store_true", help="diff sources, write digest on changes (default)"
    )
    group.add_argument("--status", action="store_true", help="print last-run summary")
    args = parser.parse_args()

    if args.seed:
        return cmd_seed()
    if args.status:
        return cmd_status()
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())
