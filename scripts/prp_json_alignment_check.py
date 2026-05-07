#!/usr/bin/env python3
"""PRP↔JSON workstream alignment self-check (R3 NB5 + R4 NB2 + R5 NB2 + R6 carve-out).

Runs 7 checks. Exit 0 on PASS, exit 1 on FAIL with diff output.

R4 NB2 fix (carried into R5):
- Check 5 (WS file lists) is now a strict GATE: mismatches FAIL by default.
- Optional `--allowlist <file>` arg for intentional drift cases.
- Canonical-section parser (only "Files to create:" / "Files to modify:" /
  `filesCreate:` / `filesModify:` blocks) — NOT broad backtick prose scanning.
- Reports counts (criteria, workstreams, framework_endpoints) on success.

R5 NB2 fix — alignment script now hard-fails on stale-anchor drift that
survived R4:
- Check 6 — stale-anchor grep gate: 30s keepalive (should be 20s),
  12-endpoint contract (should be 30-endpoint), audit_log empty in Phase 3
  (should ship Phase 3 hard-delete writers), bare avatar.png (should be
  avatar.{png,jpg,webp}), convoy_service.list_subtasks(assigned_agent=...)
  (should be list_subtasks_by_agent(agent_id=...)).
- Check 7 — Phase-7 audit-log claims without the hard-delete exception
  (negative-lookahead — `Phase 7 ... audit log writes` MUST mention
  hard-delete OR `EXPANDED`).

R6 Minor 4 — Check 6 + Check 7 now strip `## R<N> Disposition` blocks
from PRP/JSON body BEFORE running stale-anchor + Phase-7 audit-log greps.
Disposition narratives may legitimately quote stale phrases verbatim
(e.g. "R5 said `30s keepalive` — closed in Edit 1.2"); without this
carve-out the script would hard-fail on its own retro narrative.
Heading-based exclusion via `_strip_disposition_sections()` is
forward-compatible (works for R4, R5, R6, ... without per-round
hard-coding). JSON files have no markdown headings, so the function is
a no-op for them (defensive symmetry).

R5 Minor 2 — success-text endpoint count clarity: now prints
`framework_endpoints=N (criteria; routes=M including K bundled PUT+DELETE
pairs)` so the criterion-vs-route distinction is explicit.

R4 NB2 also swept stale prose hits across PRP and JSON (still verified
historically; keep here as the rosetta of past sweeps):
  ~50 criteria → 64 criteria
  ~25 endpoints → 30 endpoints
  5-7 day(s) → 7-9 day(s)
  ~42 criteria → 64 criteria (no instances found, but searched)

Verification grep AFTER applying the R5 sweep — should return ZERO matches
in PRPs/active/PRP-prd-8-phase-3-dashboard-port.md AND PRPs/contracts/prd-8-phase-3.json:
  rg -nE "30s\\s+keepalive|12-endpoint|avatar\\.png(?![,a-z{])|convoy_service\\.list_subtasks\\s*\\(\\s*assigned_agent\\s*="
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prp", required=True, type=Path)
    parser.add_argument("--json", dest="contract", required=True, type=Path)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="Optional file with one filename per line for intentional PRP/JSON drift",
    )
    args = parser.parse_args()

    prp = args.prp.read_text(encoding="utf-8")
    contract = json.loads(args.contract.read_text(encoding="utf-8"))

    failures: list[str] = []

    # Check 1 — workstream name+order match
    prp_ws = re.findall(r"### Workstream (\d+):\s*(\S+)", prp)
    prp_ws_names = [name for _, name in prp_ws]
    json_ws_names = [ws["name"] for ws in contract["workstreams"]]
    if prp_ws_names != json_ws_names:
        failures.append(
            f"[1] WS name/order drift: PRP={prp_ws_names} JSON={json_ws_names}"
        )

    # Check 2 — endpoint count match
    # Note: PUT+DELETE avatar are bundled into a SINGLE criterion
    # (`framework_endpoint_put_and_delete_avatar`) but count as TWO endpoints
    # in the PRP narrative. Tolerate that 1-criterion-covers-2-endpoints case.
    framework_ep_count = sum(
        1 for c in contract["criteria"] if c["name"].startswith("framework_endpoint_")
    )
    bundled_pair_count = sum(
        1
        for c in contract["criteria"]
        if c["name"].startswith("framework_endpoint_")
        and ("put_and_delete" in c["name"] or "and_delete" in c["name"])
    )
    expected_route_count = framework_ep_count + bundled_pair_count
    prp_count_match = re.search(r"(\d+)\s+(?:framework HTTP )?endpoints", prp)
    if prp_count_match:
        prp_ep_count = int(prp_count_match.group(1))
        if prp_ep_count != expected_route_count:
            failures.append(
                f"[2] Endpoint count drift: PRP says {prp_ep_count} JSON has "
                f"{framework_ep_count} framework_endpoint_* criteria + "
                f"{bundled_pair_count} bundled PUT+DELETE pairs = "
                f"{expected_route_count} routes"
            )

    # Check 3 — criteria count match
    json_criteria_count = len(contract["criteria"])
    prp_criteria_match = re.search(r"All\s+(\d+)\s+criteria", prp)
    if prp_criteria_match:
        prp_criteria_count = int(prp_criteria_match.group(1))
        if prp_criteria_count != json_criteria_count:
            failures.append(
                f"[3] Criteria count drift: PRP says {prp_criteria_count} "
                f"JSON has {json_criteria_count}"
            )

    # Check 4 — DB path mention sweep.
    # Allow narrative/historical references to the bad path (sentences that
    # discuss the prior bug or describe the check itself). Flag only matches
    # that appear in genuine config-shaped contexts (e.g. `=` assignment, or
    # default-value-prose like "defaults to HOMIE_HOME / 'dashboard.db'"
    # WITHOUT a preceding "was" / "stale" / "incorrectly" / "fix" keyword on
    # the same line).
    bad_path = "HOMIE_HOME / 'dashboard.db'"
    narrative_indicators = (
        "stale",
        "was incorrectly",
        "was citing",
        "was citied",
        "was cited",
        "ADDRESSED",
        "fix",
        "drift",
        "matches",
        "swept",
        "sweep",
        "references",
        "remaining",
        "zero",
    )

    def _count_active_bad_path(text: str) -> int:
        n = 0
        for line in text.splitlines():
            if bad_path not in line:
                continue
            lower = line.lower()
            if any(ind.lower() in lower for ind in narrative_indicators):
                continue  # narrative mention, not an active config drift
            n += 1
        return n

    prp_bad = _count_active_bad_path(prp)
    json_bad = _count_active_bad_path(args.contract.read_text(encoding="utf-8"))
    if prp_bad or json_bad:
        failures.append(
            f"[4] DB path drift: PRP has {prp_bad} active `{bad_path}` "
            f"match(es); JSON has {json_bad} (narrative/historical mentions "
            f"that include 'stale'/'fix'/'drift'/etc are excluded)"
        )

    # Check 5 — WS file list set-equality (R4 NB2 fix Part A — strict gate)
    # Parse ONLY the canonical workstream file-list sections, NOT broad prose
    # backticks. The PRP's authoritative file list per workstream is in the
    # "Files to create:" / "Files to modify:" lines (or `filesCreate:` /
    # `filesModify:` keys). Random backticked file paths in PRP prose are NOT
    # part of the contract and must not be compared against JSON arrays.
    #
    # If your workstream legitimately needs to mention a file in prose without
    # adding it to the canonical file list, pass the file via --allowlist.

    # Optional allowlist for legitimate prose-only mentions
    allowlist: set[str] = set()
    if args.allowlist and args.allowlist.is_file():
        allowlist = {
            line.strip()
            for line in args.allowlist.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

    # Strict canonical-section parser: look for the explicit file-list sections
    # under each "### Workstream N:" heading. Accepts either of two prose shapes:
    #   - "Files to create:" / "Files to modify:" markdown sections (each followed
    #     by `- `code`-bulleted file paths)
    #   - "filesCreate:" / "filesModify:" YAML-shaped keys
    # Anything outside these sections is prose — ignored.
    ws_blocks = re.findall(
        r"### Workstream \d+:.*?(?=### Workstream|\Z)", prp, re.DOTALL
    )
    file_path_re = re.compile(
        r"^\s*[-*]\s+`([^`]+\.(?:py|toml|md|ts|tsx|json))`", re.MULTILINE
    )
    canonical_section_re = re.compile(
        r"(?:Files to (?:create|modify)|filesCreate|filesModify)\s*:?\s*\n"
        r"((?:\s*[-*]\s+.+\n?)+)",
        re.IGNORECASE,
    )
    for i, block in enumerate(ws_blocks):
        # Extract files ONLY from canonical sections, not prose backticks
        canonical_chunks = canonical_section_re.findall(block)
        prp_files = set()
        for chunk in canonical_chunks:
            prp_files.update(file_path_re.findall(chunk))

        if i >= len(contract["workstreams"]):
            failures.append(
                f"[5] WS{i + 1} present in PRP but missing from JSON workstreams[]"
            )
            continue
        json_ws = contract["workstreams"][i]
        json_files = {
            re.split(r"\s+\(", entry, maxsplit=1)[0]
            for entry in (
                json_ws.get("filesCreate", []) + json_ws.get("filesModify", [])
            )
        }
        only_prp = (prp_files - json_files) - allowlist
        only_json = (json_files - prp_files) - allowlist
        if only_prp or only_json:
            failures.append(
                f"[5] WS{i + 1} file-list mismatch (canonical sections): "
                f"PRP-only={sorted(only_prp)} JSON-only={sorted(only_json)} "
                f"(use --allowlist <file> for intentional drift)"
            )

    # R6 Minor 4 — section-aware filter for Check 6 + Check 7
    # Strip `## R<N> Disposition` blocks from PRP/JSON body BEFORE running
    # stale-anchor and Phase-7 audit-log greps. Disposition narratives may
    # legitimately quote stale phrases verbatim (e.g. "R4 said `12-endpoint
    # contract`" — reporting a fixed problem); without this carve-out,
    # disposition prose looks like an active spec drift to the script.
    # Heading-based exclusion (forward-compatible — works for R4, R5, R6, ...
    # without per-round hard-coding).
    def _strip_disposition_sections(body: str) -> str:
        """Remove ## R<N> Disposition heading + content up to next ## heading.

        Operates on the markdown body line-by-line; preserves all other
        sections unchanged. JSON files have no markdown headings so the
        function is a no-op for them (returns input verbatim).
        """
        lines = body.splitlines(keepends=True)
        out: list[str] = []
        in_disposition = False
        disposition_re = re.compile(r"^\s*##\s+R\d+\s+Disposition\b", re.IGNORECASE)
        next_section_re = re.compile(r"^\s*##\s+(?!R\d+\s+Disposition\b)")
        for line in lines:
            if disposition_re.match(line):
                in_disposition = True
                continue
            if in_disposition and next_section_re.match(line):
                in_disposition = False
                # Fall through — current line is a NEW non-disposition section
            if not in_disposition:
                out.append(line)
        return "".join(out)

    # Check 6 — Stale-anchor grep gate (R5 NB2 Part B)
    # The R4 sweep made the alignment script catch the prose-text drift class
    # that R4 NB2 found surviving the prior gate. Each pattern below is a hard
    # FAIL with a specific hint about which R5 edit closes it.
    stale_anchor_patterns: list[tuple[str, str]] = [
        (
            r"30s\s+keepalive",
            "stale anchor: SSE keepalive is 20s (R5 NB2 Edits 1.2 + 1.3)",
        ),
        (
            r"30s\s+'?ping'?\s+keepalive",
            "stale anchor: SSE keepalive is 20s comment-line (R5 NB2 Edits 1.2 + 1.3)",
        ),
        (
            r"12-endpoint\s+contract|all\s+12\s+endpoints|\b12\s+endpoint\b",
            "stale anchor: contract publishes 30 endpoints (R5 NB2 Edit 1.4)",
        ),
        (
            r"audit_log[^\n]*empty\s+in\s+Phase\s+3",
            "stale anchor: audit_log TABLE ships Phase 3 with hard-delete WRITERS (R5 NB2 Edit 1.5 + JSON criterion dashboard_db_audit_log_table_has_required_columns_phase3)",
        ),
        (
            r"avatar\.png(?![,a-z{`\s])",
            "stale anchor: avatar should be avatar.{png,jpg,webp} (R5 NB2 Edits 1.7 + 1.8 + 1.9 + criterion framework_endpoint_put_and_delete_avatar)",
        ),
        (
            r"convoy_service\.list_subtasks\s*\(\s*assigned_agent\s*=",
            "stale anchor: method is list_subtasks_by_agent(agent_id=...) (R5 NM3 Edit 2.1 + criterion convoy_service_list_subtasks_by_agent_method_added)",
        ),
        # Codex iter2 NF3 — stale criteria-count anchors. The contract has 64
        # criteria (R4 T10 + R5/R6/R7 unchanged). Active prose, JSON checklist
        # text, and JSON descriptions must say "64 criteria" — the prior "28"
        # / "62" anchors leaked into validation prose at PRP:1384, PRP:1830,
        # JSON:747, JSON:772. Disposition narratives (## R<N> Disposition)
        # are stripped before this scan, so legitimate retro-quotes survive.
        (
            r"\b28\s+criteria\b",
            "stale anchor: contract has 64 criteria (Codex iter2 NF3 — was 28 in v1)",
        ),
        (
            r"\b62\s+criteria\b",
            "stale anchor: contract has 64 criteria (Codex iter2 NF3 — was 62 pre-R4-T10)",
        ),
    ]
    # R6 Minor 4 — strip ## R<N> Disposition blocks from PRP body before
    # stale-anchor + Phase-7 audit-log greps. JSON has no markdown headings,
    # so the strip is a no-op there (defensive — keeps the function symmetric).
    _prp_filtered = _strip_disposition_sections(prp)
    _contract_filtered = _strip_disposition_sections(args.contract.read_text(encoding="utf-8"))
    stale_targets = [(args.prp, _prp_filtered), (args.contract, _contract_filtered)]
    for path, body in stale_targets:
        for line_no, line in enumerate(body.splitlines(), start=1):
            for pattern, hint in stale_anchor_patterns:
                if re.search(pattern, line):
                    failures.append(
                        f"[6] stale anchor in {path}:{line_no} — {hint}\n"
                        f"      offending: {line.strip()[:200]}"
                    )

    # Check 7 — Phase 7 audit-log line MUST include the hard-delete exception
    # (R5 NB2 Edit 1.10). The bare phrase "audit log writes" in a Phase-7-OOS
    # context without acknowledging Phase 3 hard-delete writers is a stale
    # anchor.
    phase7_audit_pattern = re.compile(
        r"Phase\s*7[^\n]*audit\s*log\s*writes(?![^\n]*(hard[-\s]*delete|EXPANDED|expand))",
        re.IGNORECASE,
    )
    for path, body in stale_targets:
        for line_no, line in enumerate(body.splitlines(), start=1):
            if phase7_audit_pattern.search(line):
                failures.append(
                    f"[7] Phase-7-only audit-log claim in {path}:{line_no} — Phase 3 ships hard-delete writers (R5 NB2 Edit 1.10)\n"
                    f"      offending: {line.strip()[:200]}"
                )

    if failures:
        print("FAIL: PRP↔JSON alignment", file=sys.stderr)
        for f in failures:
            print("  " + f, file=sys.stderr)
        return 1

    # R5 Minor 2 — make the bundled PUT+DELETE distinction explicit in the
    # success line. framework_endpoints counts CRITERIA (one criterion may
    # cover multiple HTTP routes — e.g. framework_endpoint_put_and_delete_avatar
    # is one criterion bundling PUT and DELETE). The route count is the
    # criterion count plus the bundled-pair count.
    route_count = framework_ep_count + bundled_pair_count
    print(
        f"OK alignment ws_count={len(json_ws_names)} "
        f"criteria_count={json_criteria_count} "
        f"framework_endpoints={framework_ep_count} (criteria; "
        f"routes={route_count} including {bundled_pair_count} bundled "
        f"PUT+DELETE pair{'s' if bundled_pair_count != 1 else ''})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
