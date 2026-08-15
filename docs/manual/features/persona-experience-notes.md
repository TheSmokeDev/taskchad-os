# Persona Experience Notes

Status: Shipped (issue #420 — piece 1 of Universal Persona Cognition)
Owner: Framework (personas slice + co-founder worktick)
Last updated: 2026-08-12

## What It Does

Gives every persona a deterministic record of its own work. When a persona
executes an assignment from the co-founder worktick, one section is appended
to that persona's daily experience note inside its OWN memory tree
(`~/.homie/profiles/<id>/memory/experience/YYYY-MM-DD.md`) and reindexed into
that persona's own memory index the same day. The operator can also drop a
file or a block of text straight onto that trail with
`thehomie persona ingest`.

Nothing here calls a model. The facts come from code (task, repo, mode,
status, deliverable path, run id) and the prose comes from the execution's
EXISTING output. Notes are reindexed into the persona's own `memory.db` the
same day, so they are reachable via that persona's recall immediately.
The nightly distiller does NOT yet walk `PERSONA_NOTE_DIRS` — that
retarget is pending (see Next Slices below), so "work happened" is captured
and searchable today, not yet automatically compounded into a lesson.

This is the generic sibling of the crypto market-note writer: same mechanics,
slim generic section shape, any persona.

## Operator Entry Points

- Chat/Telegram: none — the worktick hook is automatic.
- CLI: `thehomie persona ingest <name> <file|text> [--label ...] [--note ...]
  [--text] [--no-reindex] [--json]`
- Dashboard: none.
- API: none.

## The Section Shape

```markdown
## 11:00 - AGENDA-2026-01-05.md#1 (draft -> done)

<!-- experience-key: AGENDA-2026-01-05.md#1|7 -->

- Task: draft the follow-up checklist
- Repo: example-repo
- Outcome: deliverable written: # Follow-up checklist
- Deliverable: <vault>/cofounder/deliverables/DELIVERABLE-...md

### Output excerpt

> # Follow-up checklist - call the leads
```

The `experience-key` comment is the in-file dedup key: `agenda_ref` plus the
mailbox `message_id` for assignments, `ingest|<label>|<content digest>` for
ingested sources. A re-executed delivery (a process killed between the write
and the ack) returns a `duplicate` receipt and leaves the file byte-identical.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/personas/experience.py` (writer, `PERSONA_NOTE_DIRS`, ingest logic) |
| Python/runtime | `.claude/scripts/cofounder/worktick.py` (`_execute_assignment` hook, `_execute_draft` output passthrough) |
| Chat/router | `.claude/chat/cli.py` (`thehomie persona ingest`) |
| Tests | `.claude/scripts/tests/test_persona_experience_notes.py`, `.claude/scripts/tests/test_cofounder_worktick.py` |
| Docs/proof | this page |

## Receipts

Every write returns a receipt dict; the worktick puts it on the assignment
record as `experience_note`.

| Status | Meaning |
|---|---|
| `written` | The section landed. `reindexed` says whether the recall index was refreshed. |
| `duplicate` | The dedup key was already in today's note. Nothing changed. |
| `skipped_cap` | Appending would blow the daily-file cap. Nothing changed. |
| `error` | Anything went wrong. `detail` carries the exception. Nothing changed. |

## Safety Boundaries

- **The writer is fail-open by contract.** It never raises; a note failure
  can never fail the assignment, the tick, or the ingest that produced it.
  The worktick's call site is fail-open at the import boundary as well.
- **Learning grants memory, never capabilities.** Nothing here posts, sends,
  edits, or connects; every external-mutation gate elsewhere is untouched.
- **Every field is treated as hostile input.** Task text, outcome summaries,
  operator notes and ingested article bodies are collapsed to a single line
  and capped before rendering, so supplied text cannot forge a section
  heading or a dedup key. Persona ids are validated as a single safe path
  segment before any path math (traversal defense).
- **Cross-profile writes resolve paths as an argument.** The worktick runs as
  the default profile and writes into another profile's tree by resolving
  `get_persona_paths(<id>)`, never by mutating `HOMIE_HOME`.
- **The reindex is guarded on physical layout** (Rule 2): it only fires when
  the profile actually has the `memory/` + `data/` sibling pair the DB-path
  resolver keys on. Otherwise a persona's note would be indexed into the
  operator's own index.
- **Co-founder deliverables are not relocated.** They remain operator-review
  artifacts in the main vault; the experience note links to them.
- Ingest reads at most a bounded number of bytes off disk, so pointing it at
  a very large file cannot exhaust memory.

## How To Run It

```powershell
# Ingest a file into a persona's experience trail
thehomie persona ingest sales .\notes\playbook.md --note "read before the next wave"

# Ingest literal text (never touches the filesystem)
thehomie persona ingest sales "always confirm the domain before pitching" --text

# Quiet JSON receipt
thehomie persona ingest sales .\notes\playbook.md --json
```

Assignment notes need no command — they ride the existing worktick.

## How To Test It

```powershell
cd .claude/scripts
uv run python -m pytest tests/test_persona_experience_notes.py tests/test_cofounder_worktick.py -q
```

## Public Export Status

Public-exported. Profile trees and vault content stay private; this page
documents mechanism only.

## Next Slices

- The distiller retarget: fresh files in `PERSONA_NOTE_DIRS` become the
  reflection log corpus so notes compound into the persona's own MEMORY.md.
- The worktick read-back: the persona's memory rides its own draft prompt.
- YouTube link drops ride the curriculum engine, not this writer — two
  surfaces, one pipeline each, never a forked extractor.
