# Native Design Capability (`/design`)

Status: Phase 1 + B1 shipped, live-proven. B2-B6 scoped.
Owner: runtime-chat slice (`.claude/chat/`) + design domain (`.claude/scripts/design/`)
Last updated: 2026-06-09

## What It Does

`/design` turns The Homie's own coding-agent runtime into a design engine: from
one chat command it generates a brand-grade, standalone HTML artifact (landing
page, dashboard) in a chosen brand's real design language. It ports Open Design's
power — the design method plus a brand-system library — natively, with no
external design app, no daemon, and no install. Each of the 26 bundled brand
systems (`stripe`, `ferrari`, `brutalism`, ...) binds its REAL compiled tokens
and component shapes, so the same brief produces a different, on-brand design per
system — the diversity engine for a multi-site fleet.

## Operator Entry Points

- Chat/Telegram: `/design html "<brief>"`, `/design system <slug> "<brief>"`,
  `/design systems`, `/design directions`
- CLI: the same `/design ...` through the unified router
- Dashboard: deferred to B4 (preview + annotate + WYSIWYG)
- API: none direct; generation routes through the runtime layer

Flags: `--system <slug>` · `--direction <id>` · `--tone <tone>` · `--accent "<override>"`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/design/` (`directions.py`, `brief.py`, `systems.py`, `artifacts.py`); generation via `.claude/scripts/runtime/lane_router.py::run_with_runtime_lanes` |
| Chat/router | `.claude/chat/core_handlers.py::handle_design`; `.claude/chat/commands.py` (command row + Design category + native menu) |
| Bundled assets | `.claude/scripts/design/_systems/<slug>/` (26 full packages, MIT — see `.claude/scripts/design/THIRD-PARTY-NOTICES.md`) |
| Generated artifacts | `vault/memory/design/<system>-<slug>/<kind>-YYYYMMDD/finalized.html` (gitignored vault) |
| Tests | `.claude/scripts/tests/test_design_brief.py` |
| Docs/proof | `PRDs/active/PRD-native-design-capability-open-design-port-2026-06-09.md`, `PRPs/active/PRP-native-design-B1-system-package-harvest-2026-06-09.md` |

## How It Works (the loop)

1. **Resolve the look.** A named `system` loads the full package
   (`DESIGN.md` + `tokens.css` + components); otherwise a built-in `direction`
   is picked by tone.
2. **Assemble one self-contained brief** (`build_design_brief`): the request +
   `USAGE` → `DESIGN.md` → `tokens.css` (pasted byte-for-byte) → components
   summary + anti-slop rules + a 5-dimensional self-critique + the exact output
   path. All of it goes in `RuntimeRequest.prompt` (lane-agnostic — it survives a
   Claude → Codex → Gemini fallback; the brief is the task, not a Claude-only
   side channel).
3. **Run through the runtime** (`run_with_runtime_lanes`, `TOOL_REASONING`,
   tools `Read/Write/Edit/Glob/Grep`, `cwd` = the artifact dir). Whatever
   coding-agent lane is live becomes the design engine.
4. **The agent writes** a complete standalone `finalized.html` to the vault
   artifact dir.
5. **The handler returns** provider/model/cost + the path + the agent's
   self-grade.

## Safety Boundaries

- Generation runs with `cwd` = the artifact dir and **no `Bash` tool** (a
  standalone HTML task needs no shell) — this bounds the obvious write-escape.
  A full write sandbox is future hardening.
- Brand systems load only from `_systems/<slug>/` behind a path-traversal +
  symlink-escape guard; a slug cannot read arbitrary files.
- `tokens.css` is injected byte-for-byte (the binding contract), never
  paraphrased.
- A chosen brand system's palette overrides the house "no blue" default. The
  anti-slop floor is not yet hard — see Next Slices (B2).
- Generated artifacts live under the gitignored, sanitizer-denied vault — client
  work never ships. The bundled `_systems/` library is MIT and ships publicly
  with attribution and a not-affiliated / inspired-by disclaimer.

## How To Run It

```
/design system stripe "a landing page for Focusly: hero, 3 features, pricing, footer"
/design html "a dark dashboard UI for an SMB fintech, dense KPIs" --tone tech
/design systems        # list bundled brand systems
/design directions     # list the 5 built-in visual directions
```

## How To Test It

```powershell
cd .claude\scripts
uv run python -m py_compile design\systems.py design\brief.py ..\chat\core_handlers.py
uv run pytest tests/test_design_brief.py -q
```

## Latest Live Proof

- Date: 2026-06-09
- Surface: runtime (Codex/gpt-5.5 lane), `/design system <slug>`
- Result: the same Focusly brief through `stripe` / `brutalism` / `ferrari`
  produced three distinct design languages, each binding its real tokens
  (`#533afd` / `#ffef5a` / `#dc0000`). Diversity proven; `stripe` leaked 52
  em-dashes (the B2 floor target).
- Proof: the three artifacts under `vault/memory/design/`; PRD live-proof
  criteria.

## Public Export Status

Public: the `/design` code and the bundled MIT brand systems ship via
`scripts/sanitize.py` with attribution and an inspired-by / not-affiliated
disclaimer. Private: generated artifacts and the vault (sanitizer `DENY_DIR`).

## Next Slices

- **B2** — anti-slop charter as a hard floor + seed → layouts → checklist
  preflight (kills the em-dash leak).
- **B3** — fixed deck framework → `/design deck` → PDF/PPTX export.
- **B4** — dashboard canvas: preview, annotate → re-prompt, WYSIWYG edit.
- **B5** — HyperFrames / video + a unified media dispatcher.
- **B6** — critique auto-iterate (devloop) on Convoy until the score bar clears.
