# Document Uploads and Ingest

Status: Shipped, all 3 phases (truthfulness, full reads, explicit /vault-ingest)
Owner: chat slice (`attachment_context.py`, `router.py`, `engine.py`) + scripts slice (`entity_extractor.py`)
Last updated: 2026-06-11

## What It Does

Upload a document to your Homie from Telegram or Discord and the framework
reads it. Prose formats — PDF, DOCX, Markdown, plain text — are extracted
and read in full, up to env-tunable caps, and the extracted text rides the
turn prompt on every runtime lane (claude, codex, gemini). The bot can
answer questions about the document's contents immediately without any slash
command.

CSV and TSV are a deliberate exception. They stay a bounded tabular preview:
first 60 rows, 12 cells per row, 120 characters per cell. This is a design
choice, not a truncation: tabular data typically has structure that a
preview captures well enough and a full-text dump would balloon the prompt
for little gain. The full-read char caps do not apply to CSV/TSV.

Partial reads are disclosed. When extracted text is clipped by a cap the
bot receives a `PARTIAL CONTENT` warning in the attachment context header
and a model instruction to tell the user explicitly that only part of the
document was read. The disclosure is model-level, not a silent footer.

A full 81KB document read is approximately 21K tokens on that turn. If token
cost matters for your deployment, dial the caps down via the env knobs
described in the Configuration section.

## Supported Formats

| Format | Extensions | Behavior |
|---|---|---|
| PDF | `.pdf` | PyMuPDF full text extraction, page-tagged |
| DOCX | `.docx` | paragraph extraction from `word/document.xml` |
| Markdown | `.md`, `.markdown` | plain UTF-8 text read |
| Plain text | `.txt`, `.log` | plain UTF-8 text read |
| CSV / TSV | `.csv`, `.tsv` | 60-row preview (12 cells, 120-char cells) |

Any other file type is left as an attachment but not parsed into model-visible
text. The bot names the file and says it is an unsupported type.

## Truthful Failures

The framework guarantees that when a document turn fails, the bot never lies
about what it did.

If the engine times out while processing an upload, the final response
explicitly names every file by display name and states that the files were
"NOT read, ingested, or saved." That exact phrase is persisted as the
assistant turn record, so a follow-up question like "did you ingest it?"
reads the durable record and returns an explicit no.

This guarantee closes the incident from 2026-06-10: an 81KB upload hit the
180-second engine timeout on the Codex lane, and a follow-up question
received a confabulated yes with a fabricated description of the file
contents. The fix is two-layer: an attachment-aware timeout message that
names the files, and a lane-wide grounding rule injected into every runtime
prompt that forbids claiming unperformed actions.

## /vault-ingest Caption (Telegram)

Uploading a document with the caption exactly `/vault-ingest` triggers a
deterministic ingest pipeline on the router side — no LLM turn is involved.

What the pipeline does per file:

1. Preserves the original file byte-for-byte at
   `{vault}/raw/uploads/{filename}` (where `{vault}` is the active profile's
   memory vault directory). The raw archive is immutable and is never passed
   to the compiler.
2. Generates a `{stem}.ingest.md` companion file with Homie-standard
   frontmatter (`tags: [upload, auto-ingested]`, date, source filename).
   Full text from `extract_document_text` populates the companion body.
3. Extracts entities heuristically from the full text.
4. Compiles those entities into concept pages under `{vault}/concepts/`
   (creates new pages or adds sections to existing ones, per the entity
   extractor contract).
5. Returns a counted confirmation: concepts created/updated, connections,
   contradictions.

**Default-deny is strict.** Only a caption that is exactly `/vault-ingest`
(whitespace-tolerant) triggers the pipeline. The following do NOT trigger:

- A prose caption that mentions vault-ingest ("please vault-ingest this")
- A bare `/vault-ingest` command sent as text without an attached file
- A caption-less upload
- Any text command sent after an upload in a separate message

Albums (multi-file Telegram attachment groups): the caption applies to the
whole batch. The pipeline runs for each supported file in the group.
Unsupported files in the batch are refused by name without aborting
the rest.

**Partial-failure honesty.** When the raw archive lands but the compile
stage fails, the reply says exactly that: "Raw file archived as '...', but
concept compilation FAILED. No concept pages were created or updated." It
does not claim total failure. When the archive itself fails the reply says
"Nothing was saved to the vault for this file."

**Discord route** for `/vault-ingest` is a named follow-up; the Telegram
caption path is the currently shipped surface.

## Adapter Integration

The document upload flow is adapter-owned ingress. Both Telegram and Discord
adapters download uploaded files to local temp directories and attach them
as `IncomingMessage.attachments` with filename, mimetype, size, and local
path. The chat engine builds attachment context from those local paths via
`attachment_context.py`. Local paths never appear in model-visible text or
user-visible replies.

For the full adapter contract, turn batching, quick-turn buffering, and
Queue/Steer follow-up controls, see
[Multi-Channel Adapters](multi-channel-adapters.md). That page is the
canonical reference for how uploads flow from platform event to normalized
message; this page covers what happens to the document text after
normalization.

## Configuration

All four knobs live in `.claude/scripts/.env` and take effect immediately
after `/reload` — no bot restart required.

| Env var | Default | What it controls |
|---|---|---|
| `CHAT_ATTACHMENT_MAX_BYTES` | `8388608` (8 MiB) | Per-file parser byte limit. Files larger than this are skipped with a named refusal. |
| `CHAT_ATTACHMENT_MAX_CHARS` | `100000` | Per-attachment extracted-text character cap. Reads beyond this are disclosed as PARTIAL. |
| `CHAT_ATTACHMENT_TOTAL_MAX_CHARS` | `120000` | Whole-turn attachment context budget across all files in the turn. |
| `CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS` | `300` | Engine timeout for turns that include attachments (separate from the default turn timeout). |

All four use None-sentinel call-time resolution (Rule 1). Setting them in
`.env` and running `/reload` moves the truncation boundary on the next
upload without a restart.

## Prerequisites and Activation

The attachment parser (`attachment_context.py`) and the router-side ingest
pipeline are active in the codebase. Cron pipelines (`heartbeat.py`,
`memory_reflect.py`, `memory_weekly.py`) pick up code changes immediately on
the next scheduled run.

The live Telegram and Discord bots require a restart to load the new code:

```powershell
# From the repo root (run_chat.sh is the only launcher; Git Bash on Windows)
cd .claude\chat
bash run_chat.sh
```

PDF extraction requires PyMuPDF (`fitz`). DOCX extraction uses only the
Python standard library (`zipfile`, `xml.etree`). CSV/TSV and text formats
have no extra dependencies.

## Failure Modes

| Symptom | Cause and fix |
|---|---|
| "NOT read, ingested, or saved: filename.txt" in the reply | Engine timeout while reading the document. Check `CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS` (default 300s). Large files on slower lanes may need a higher value. |
| "unsupported document type for /vault-ingest" | File format not in the supported list. The reply names the accepted extensions. Re-encode or convert the file. |
| "file exceeds N byte parser limit" | File larger than `CHAT_ATTACHMENT_MAX_BYTES`. Raise the cap or split the file. |
| "PARTIAL CONTENT: only the first N of M characters are included" | File text exceeded `CHAT_ATTACHMENT_MAX_CHARS`. The model was told to disclose this. Raise the cap if you need the full text on every turn. |
| "parser failed: ExceptionType" | Format-level extraction error (corrupted file, encrypted PDF). Re-export the document and retry. |
| "Raw file archived as '...', but concept compilation FAILED" | /vault-ingest pipeline: raw archive landed but `compile_entities` raised. Concept pages were NOT created. Check entity_extractor logs. Re-send with /vault-ingest to retry compile only (the raw archive already exists and a new one will be date-prefixed). |
| "Nothing was saved to the vault for this file" | /vault-ingest pipeline: failure before preserve_raw (missing file reference or unreadable temp path). Re-upload with /vault-ingest caption. |
| Filename displays garbled characters in the reply | Display name is sanitized for safe echo (control characters stripped, newlines removed). The storage name goes through preserve_raw's central sanitizer which additionally strips path traversal sequences and Windows-reserved characters. Both are safe regardless of what the sender named the file. |

## Vertical Slice Architecture

| Layer | File | Role |
|---|---|---|
| Attachment parser | `.claude/chat/attachment_context.py` | Format dispatch, cap enforcement, PARTIAL disclosure, `_clean_filename` |
| Router ingest gate | `.claude/chat/router.py` (`_handle_vault_ingest_document`, `_document_ingest_pipeline`) | Caption gate, default-deny logic, preserve_raw + companion + compile orchestration, honesty replies |
| Engine integration | `.claude/chat/engine.py` | Attachment context injected into `RuntimeRequest.prompt`; grounding rules prefix; attachment timeout |
| Config knobs | `.claude/scripts/config.py` | All four attachment env vars with call-time resolution |
| Entity extractor | `.claude/scripts/entity_extractor.py` (`preserve_raw`, `compile_entities`) | Raw archive, companion compile, concept page updates |
| Tests | `.claude/scripts/tests/test_attachment_context.py`, `test_vault_ingest_document.py`, `test_adapter_telegram.py`, `test_adapter_discord.py` | Full suite covering caps, partial disclosure, ingest pipeline, default-deny, caption gate through the production queue path |

## Public Export Status

This feature page is public-framework safe. Public export goes through
`scripts/sanitize.py`.
