---
description: Create a Product Requirements Document from conversation
argument-hint: [output-filename]
---

# Create PRD: Generate Product Requirements Document

## Overview

Generate a comprehensive Product Requirements Document (PRD) based on the current conversation context and requirements discussed. Use the structure and sections defined below to create a thorough, professional PRD.

## Output File

Write the PRD to: `$ARGUMENTS` (default: `PRD.md`)

## PRD Structure

Create a well-structured PRD with the following sections. Adapt depth and detail based on available information:

### Required Sections

**1. Intent Statement** (50-100 words)
- One sentence: the actual user need or operator goal
- One sentence: why it matters (business impact, pain point, blocking issue)

**2. Success Hypothesis** (RIGHT Condition)
- Assertive, measurable, binary outcome when the solution works correctly
- Use assertive verbs: "must", "will", "produces", "creates", "returns"
- Must be testable by a machine or a human following a script

**3. WRONG-Condition (Falsifiable Failure)** ⚠️ MANDATORY — SEE VALIDATION GATE BELOW
- Explicit binary conditions that define failure
- When ANY condition is true, the solution has FAILED
- Each entry must be observable (evidence exists: log, test, screenshot, metric)
- Each entry must be binary (it happened or it didn't — no gradients)
- At least 3 entries required
- At least one must describe a silent failure (ships unnoticed)
- No aspirational language, no implementation prescriptions
- Format: `- {concrete failure description}`

**4. Non-Goals** (Anti-Scope)
- What this solution explicitly does NOT address
- Each entry has a one-line rationale: `- **{Thing}** — {Why}`
- At least 1 entry required

**5. Executive Summary**
- Concise product overview (2-3 paragraphs)
- Core value proposition
- MVP goal statement

**6. Target Users**
- Primary user personas
- Technical comfort level
- Key user needs and pain points

**7. Scope** (Positive Deliverables)
- Numbered list of concrete artifacts or behaviors
- Each deliverable is verifiable (noun or verb phrase)
- Avoid vague verbs: "improve", "enhance" — use "add", "create", "implement", "expose"

**8. User Stories**
- Primary user stories (5-8 stories) in format: "As a [user], I want to [action], so that [benefit]"
- Include concrete examples for each story
- Add technical user stories if relevant

**9. Core Architecture & Patterns**
- High-level architecture approach
- Directory structure (if applicable)
- Key design patterns and principles
- Technology-specific patterns

**10. Tools/Features**
- Detailed feature specifications
- If building an agent: Tool designs with purpose, operations, and key features
- If building an app: Core feature breakdown

**11. Technology Stack**
- Backend/Frontend technologies with versions
- Dependencies and libraries
- Optional dependencies
- Third-party integrations

**12. Security & Configuration**
- Authentication/authorization approach
- Configuration management (environment variables, settings)
- Security scope (in-scope and out-of-scope)
- Deployment considerations

**13. API Specification** (if applicable)
- Endpoint definitions
- Request/response formats
- Authentication requirements
- Example payloads

**14. Success Criteria** (Acceptance Gates)
- Step-by-step proof of correctness
- Each criterion references specific commands, files, or observable outcomes
- Must align with Scope deliverables (see Validation Gate)

**15. Implementation Phases**
- Break down into 3-4 phases
- Each phase includes: Goal, Deliverables, Validation criteria
- Realistic timeline estimates

**16. Risks & Mitigations**
- 3-5 key risks with specific mitigation strategies

**17. Future Considerations**
- Post-MVP enhancements
- Integration opportunities
- Advanced features for later phases

**18. Appendix** (if applicable)
- Related documents
- Key dependencies with links
- Repository/project structure

## Validation Gate: WRONG-Condition (MANDATORY)

Before writing the PRD to disk, validate the WRONG-Condition section. If ANY check fails, DO NOT write the file. Instead, output the error message(s) and ask the operator to fix the issues.

### Check 1: Section Exists

If the PRD has no `## WRONG-Condition` section (or equivalent heading like `## WRONG-Condition (Falsifiable Failure)`), reject immediately:

```
ERROR: Missing required section: WRONG-Condition

Every PRD must include a WRONG-Condition section listing observable failure
modes. This section defines when the solution has FAILED, even if it shipped.

Add a section like:
## WRONG-Condition
- {observable failure 1}
- {observable failure 2}
- {observable failure 3 — at least one must be a silent failure}
```

### Check 2: Minimum Entry Count (≥3)

Count bullet points (`- ` or `* ` or `1. `) in the WRONG-Condition section. If fewer than 3:

```
ERROR: WRONG-Condition section has only {N} entries (minimum 3 required).

Each entry should describe a distinct failure mode. Consider:
- What fails silently? (ships unnoticed)
- What fails loudly? (crash, error, test failure)
- What fails on edge-case input?
```

### Check 3: No Aspirational Adjectives (per-entry)

Scan each entry for banned words: `reliably`, `properly`, `well`, `smoothly`, `correctly`, `efficiently`, `effectively`, `sufficiently`, `adequately`, `appropriately`, `robustly`, `cleanly`, `nicely`, `good`, `bad`, `poor`, `great`

If found:

```
ERROR: WRONG-Condition entry {N} uses aspirational language: '{matched_word}'.
Rewrite with observable, binary language.
Example: replace 'doesn't work reliably' with 'fails silently on network
timeout (no error logged)'.
```

### Check 4: Must Be Binary (per-entry)

Scan each entry for gradient/frequency words: `not enough`, `too many`, `too slow`, `too fast`, `partially`, `somewhat`, `mostly`, `sometimes`, `often`, `rarely`, `usually`, `tends to`

If found:

```
ERROR: WRONG-Condition entry {N} is not binary: '{matched_pattern}' implies
a gradient. Rewrite as a concrete yes/no state.
Example: replace 'too slow' with 'response time exceeds 500ms on the
/api/query endpoint'.
```

### Check 5: Must Reference Concrete Subject (per-entry)

Flag entries that start with vague subjects: `The system`, `It `, `Things`, `Everything`, `Nothing` (as the sole subject without a qualifier naming a specific component).

If found:

```
ERROR: WRONG-Condition entry {N} uses a vague subject: '{subject}'.
Name the specific component, file, endpoint, or command that fails.
Example: replace 'The system doesn't validate' with 'The /create-prd skill
accepts PRDs without a WRONG-condition section'.
```

### Check 6: No Aspirational Verbs (per-entry)

Flag entries containing prescriptive verb phrases: `should handle`, `should work`, `should be`, `needs to`, `ought to`, `is supposed to`, `is expected to`

If found:

```
ERROR: WRONG-Condition entry {N} uses prescriptive language: '{matched_phrase}'.
WRONG-conditions describe observed failure, not desired behavior.
Rewrite: 'should handle X' → 'X causes silent failure / crash / wrong output'.
```

### Check 7: No Implementation Prescriptions (per-entry)

Flag entries containing implementation directives: `use X instead`, `implement Y`, `add Z`, `switch to`, `replace with`, `refactor`, `should use`, `must implement`

If found:

```
ERROR: WRONG-Condition entry {N} prescribes an implementation: '{matched_phrase}'.
WRONG-conditions describe failure, not fixes.
Rewrite: describe what breaks (the observable symptom), not how to fix it.
```

### Check 8: At Least One Silent Failure (section-level)

Scan all entries for silent-failure signals: `silently`, `without error`, `without warning`, `no error`, `no alert`, `no log`, `unnoticed`, `goes undetected`, `passes validation` (incorrectly), `accepted` (when it shouldn't be).

If zero entries contain any signal:

```
ERROR: WRONG-Condition section has no silent-failure entry.
At least one condition must describe a failure that ships unnoticed.
Example: 'A PRD is accepted without validation' or 'Failed subtask silently
disappears from the queue'.
Loud failures (crashes, errors) are necessary but not sufficient.
```

### Validation Outcome

- If ALL checks pass: proceed to write the PRD file
- If ANY check fails: output ALL error messages (not just the first), then ask the operator to revise
- Error messages must name the specific entry number, the matched pattern, and include an example of correct language

## Validation Gate: Non-Goals (MANDATORY)

Before writing the PRD to disk, validate the Non-Goals section. If ANY check fails, DO NOT write the file. Instead, output the error message(s) and ask the operator to fix the issues.

### Check NG-1: Section Exists

If the PRD has no `## Non-Goals` section (or equivalent heading like `## Non-Goals (Anti-Scope)`), reject immediately:

```
ERROR: Missing required section: Non-Goals

Every PRD must include a Non-Goals section explicitly scoping what the solution
does NOT address. This prevents scope creep and sets clear boundaries.

Add a section like:
## Non-Goals
- **{Excluded thing}** — {One-line rationale why it's excluded}
- **{Another excluded thing}** — {Rationale}
```

### Check NG-2: Minimum Entry Count (≥1)

Count bullet points (`- ` or `* ` or `1. `) in the Non-Goals section. If fewer than 1:

```
ERROR: Non-Goals section is empty (minimum 1 entry required).

Each entry should name something explicitly excluded from scope and explain why.
Format: - **{Thing}** — {Why it's excluded}
```

### Check NG-3: Each Entry Has a Rationale (per-entry)

Each non-goal entry must include a rationale — indicated by a dash separator (`—`, `--`, or ` - ` after the bolded item) or a parenthetical explanation `(reason)`. Entries that are bare bullets with no explanation fail:

```
ERROR: Non-Goals entry {N} has no rationale.
Each non-goal must explain WHY it's excluded in one line.
Format: - **{Thing}** — {Why it's excluded}
Example: - **Auto-generate PRD sections from keywords** — Manual authorship
forces rigorous thinking; templates prevent rubber-stamping
```

### Check NG-4: No Vague Exclusions (per-entry)

Flag entries that use vague language without naming a concrete thing being excluded: `other stuff`, `misc`, `everything else`, `anything not mentioned`, `out of scope things`

If found:

```
ERROR: Non-Goals entry {N} is too vague: '{matched_pattern}'.
Name a specific feature, behavior, or capability being excluded.
Example: replace 'other stuff' with '**Real-time collaborative editing** —
single-operator tool, multiplayer adds complexity without value'
```

### Non-Goals Validation Outcome

- If ALL checks pass: proceed (non-goals section is valid)
- If ANY check fails: output ALL error messages, then ask the operator to revise
- Error messages must name the specific entry number and include an example of correct format

## Validation Gate: Success Hypothesis (MANDATORY)

Before writing the PRD to disk, validate the Success Hypothesis section. If ANY check fails, DO NOT write the file. Instead, output the error message(s) and ask the operator to fix the issues.

### Check SH-1: Section Exists

If the PRD has no `## Success Hypothesis` section (or equivalent heading like `## Success Hypothesis (RIGHT Condition)`), reject immediately:

```
ERROR: Missing required section: Success Hypothesis

Every PRD must include a Success Hypothesis defining what success looks like
in assertive, testable language.

Add a section like:
## Success Hypothesis
An operator runs X → Y happens. Z produces W. The output must contain A.
```

### Check SH-2: No Hedging Verbs (section-level)

Scan the Success Hypothesis section body for hedging/non-assertive language: `may`, `might`, `could`, `hopefully`, `should`, `would`, `possibly`, `perhaps`, `ideally`, `with luck`, `if all goes well`

If found:

```
ERROR: Success Hypothesis uses hedging language: '{matched_word}'.
A success hypothesis must be assertive — it describes what WILL happen,
not what might happen.
Replace hedging verbs with assertive verbs: 'must', 'will', 'produces',
'creates', 'returns', 'generates', 'outputs', 'completes'.
Example: replace 'should produce a report' with 'produces a report'.
Example: replace 'may improve performance' with 'reduces response time
below 200ms on the /api/query endpoint'.
```

### Check SH-3: Contains Assertive Language (section-level)

Scan the Success Hypothesis section for at least one assertive verb or phrase: `must`, `will`, `produces`, `creates`, `returns`, `generates`, `outputs`, `completes`, `results in`, `establishes`, `enforces`, `validates`, `rejects`, `accepts`

If none found:

```
ERROR: Success Hypothesis lacks assertive language.
The hypothesis must contain at least one assertive verb that commits to
a testable outcome.
Assertive verbs: must, will, produces, creates, returns, generates,
outputs, completes, results in, establishes, enforces, validates.
Example: 'An operator runs /create-prd → the skill produces a PRD file
with all mandatory sections validated.'
```

### Check SH-4: Must Be Testable (section-level)

Flag if the Success Hypothesis contains ONLY abstract/unmeasurable claims with no concrete reference to a command, file, endpoint, metric, or observable artifact. Look for at least one concrete noun: a file path, command name, endpoint, metric name, or specific output format.

If the section contains no concrete nouns (only abstractions like "the system works", "everything is better", "users are happy"):

```
ERROR: Success Hypothesis is not testable — no concrete artifact referenced.
Include at least one specific command, file path, endpoint, metric, or
output format that can be verified.
Example: 'The operator runs `archon workflow run archon-clutch <prd-path>`
→ the workflow creates a git worktree and produces a PR.'
```

### Success Hypothesis Validation Outcome

- If ALL checks pass: proceed (success hypothesis is valid)
- If ANY check fails: output ALL error messages, then ask the operator to revise
- Error messages must quote the matched pattern and include examples of correct assertive language

## Validation Gate: Scope ↔ Acceptance Alignment (WARNING — Overridable)

After validating WRONG-Condition, Non-Goals, and Success Hypothesis, check alignment between Scope deliverables and Success Criteria (acceptance gates). This gate produces WARNINGS, not errors — the operator can override and proceed.

### Check SA-1: Count Scope Deliverables

Count numbered items in the `## Scope` section (lines starting with `1. `, `2. `, etc., or top-level bullets `- ` that describe deliverables). Store as `scope_count`.

### Check SA-2: Count Acceptance Gates

Count top-level items in the `## Success Criteria` (or `## Success Criteria (Acceptance Gates)`) section. Store as `criteria_count`.

### Check SA-3: Ratio Check

Calculate `ratio = criteria_count / scope_count`. If `ratio < 0.8`:

```
WARNING: Scope/Acceptance misalignment detected.
Scope deliverables: {scope_count}
Acceptance gates: {criteria_count}
Coverage ratio: {ratio:.0%} (expected ≥80%)

The following scope deliverables may lack acceptance gates:
{list deliverables without obvious matching criteria}

You can override this warning and proceed, or add acceptance gates
for the uncovered deliverables.
Proceed anyway? [y/N]
```

### Check SA-4: Report Uncovered Deliverables

For each scope deliverable, check if a corresponding acceptance criterion references it (by keyword match on the deliverable's key noun). Report which deliverables have no matching gate:

```
WARNING: These scope deliverables have no matching acceptance gate:
- Scope item {N}: "{deliverable title}" — no criterion references this
- Scope item {M}: "{deliverable title}" — no criterion references this

Consider adding acceptance criteria for these, or confirm they are
covered by existing gates under different wording.
```

### Scope Alignment Validation Outcome

- If ratio ≥ 0.8 and all deliverables have matching gates: proceed silently
- If ratio < 0.8 OR deliverables lack gates: output WARNING (not error)
- Operator can override warning and proceed — this gate does NOT block writing the PRD
- The warning is informational: it flags potential coverage gaps for the operator to consider

## Instructions

### 1. Extract Requirements
- Review the entire conversation history
- Identify explicit requirements and implicit needs
- Note technical constraints and preferences
- Capture user goals and success criteria

### 2. Synthesize Information
- Organize requirements into appropriate sections
- Fill in reasonable assumptions where details are missing
- Maintain consistency across sections
- Ensure technical feasibility

### 3. Write the PRD
- Use clear, professional language
- Include concrete examples and specifics
- Use markdown formatting (headings, lists, code blocks, checkboxes)
- Add code snippets for technical sections where helpful
- Keep Executive Summary concise but comprehensive

### 4. Validate (BEFORE writing to disk)
- Run the WRONG-Condition Validation Gate checks (8 checks) on the WRONG-Condition section
- Run the Non-Goals Validation Gate checks (4 checks) on the Non-Goals section
- Run the Success Hypothesis Validation Gate checks (4 checks) on the Success Hypothesis section
- Run the Scope ↔ Acceptance Alignment check (4 checks) — WARNING only, operator can override
- If any ERROR check fails in any gate, output errors and ask operator to revise — do NOT write the file
- If only WARNINGs remain (alignment gate), ask operator to confirm override before writing
- Only proceed to write when all error checks pass and warnings are acknowledged

### 5. Quality Checks
- ✅ All required sections present (including WRONG-Condition and Non-Goals)
- ✅ WRONG-Condition passes all 8 validation gate checks
- ✅ Non-Goals passes all 4 validation gate checks (section exists, ≥1 entry, rationale per entry, no vague exclusions)
- ✅ Success Hypothesis passes all 4 validation gate checks (section exists, no hedging verbs, assertive language present, testable with concrete artifact)
- ✅ User stories have clear benefits
- ✅ Scope is realistic and well-defined
- ✅ Technology choices are justified
- ✅ Implementation phases are actionable
- ✅ Success criteria are measurable and reference observable outcomes
- ✅ Consistent terminology throughout

## Style Guidelines

- **Tone:** Professional, clear, action-oriented
- **Format:** Use markdown extensively (headings, lists, code blocks, tables)
- **Checkboxes:** Use ✅ for in-scope items, ❌ for out-of-scope
- **Specificity:** Prefer concrete examples over abstract descriptions
- **Length:** Comprehensive but scannable (typically 30-60 sections worth of content)

## Output Confirmation

After creating the PRD:
1. Confirm the file path where it was written
2. Provide a brief summary of the PRD contents
3. Highlight any assumptions made due to missing information
4. Suggest next steps (e.g., review, refinement, planning)

## Notes

- If critical information is missing, ask clarifying questions before generating
- Adapt section depth based on available details
- For highly technical products, emphasize architecture and technical stack
- For user-facing products, emphasize user stories and experience
- This command contains the complete PRD template structure - no external references needed

## Validation Test Cases (Edge Cases)

These test cases verify the validation gates reject bad PRDs and accept good ones.

### Test Case 1: Good PRD → Passes All Validation

**Input PRD snippet:**
```markdown
## Success Hypothesis
An operator runs `/create-prd "build X"` → the skill produces a PRD file with
all mandatory sections. The validation gate rejects PRDs missing WRONG-conditions.

## WRONG-Condition
- A PRD without a WRONG-condition section is accepted without error
- The create-prd skill accepts aspirational language silently (no warning)
- A misaligned scope/acceptance ratio passes without any operator notification

## Non-Goals
- **Auto-generate PRD content from keywords** — Manual authorship forces rigorous thinking
- **Support real-time collaborative editing** — Single-operator tool; multiplayer adds complexity

## Scope
1. Intent-PRD Template
2. Validation Logic

## Success Criteria
1. Template validates correctly
2. Validation rejects bad PRDs
```

**Expected result:** ALL gates pass. File is written.
- WRONG-Condition: 3 entries ✓, no aspirational language ✓, binary ✓, concrete subjects ✓, silent failure present ("accepted without error") ✓
- Non-Goals: section exists ✓, 2 entries ✓, rationales present ✓
- Success Hypothesis: assertive ("produces", "rejects") ✓, no hedging ✓, testable (names `/create-prd`) ✓
- Scope alignment: 2 scope items, 2 criteria → ratio 1.0 ✓

---

### Test Case 2: Bad WRONG-Condition → Rejected

**Input PRD snippet:**
```markdown
## WRONG-Condition
- The system doesn't work reliably under load
- Performance should be good enough for users
```

**Expected result:** REJECTED with multiple errors:
- Check 3 (aspirational adjectives): entry 1 "reliably" → ERROR
- Check 4 (binary): entry 2 "good enough" is gradient → ERROR
- Check 5 (vague subject): entry 1 "The system" → ERROR
- Check 6 (aspirational verbs): entry 2 "should be" → ERROR
- Check 2 (minimum count): only 2 entries (need ≥3) → ERROR
- Check 8 (silent failure): no silent-failure signal → ERROR

---

### Test Case 3: Missing Non-Goals → Rejected

**Input PRD snippet:**
```markdown
## Success Hypothesis
The workflow produces a PR within 5 minutes.

## WRONG-Condition
- The workflow hangs with no error after 10 minutes
- A failed R3 gate still triggers the Execute node
- Subtask state silently reverts to 'pending' after completion

## Scope
1. Workflow YAML
```

**Expected result:** REJECTED
- Check NG-1 (section exists): No `## Non-Goals` heading found → ERROR: "Missing required section: Non-Goals"

---

### Test Case 4: Aspirational Hypothesis → Rejected

**Input PRD snippet:**
```markdown
## Success Hypothesis
The new feature should hopefully improve the user experience and may reduce
support tickets. Users could potentially find it useful.
```

**Expected result:** REJECTED with errors:
- Check SH-2 (hedging verbs): "should" → ERROR
- Check SH-2 (hedging verbs): "hopefully" → ERROR
- Check SH-2 (hedging verbs): "may" → ERROR
- Check SH-2 (hedging verbs): "could" → ERROR
- Check SH-4 (testable): no concrete artifact referenced → ERROR

---

### Test Case 5: Misaligned Scope/Acceptance → Warning (Not Error)

**Input PRD snippet:**
```markdown
## Success Hypothesis
The operator runs `archon workflow run` → all 7 nodes complete and a PR is created.

## WRONG-Condition
- R2 judges block forever waiting for a skipped conditional branch
- Execute node runs without R3 approval
- Worktree is silently deleted before PR creation

## Non-Goals
- **Support parallel workflow runs** — Single-run simplicity first

## Scope
1. Workflow YAML with 7 nodes
2. Command files for all gates
3. Retry logic with exponential backoff
4. Worktree lifecycle management
5. State persistence in prd.json

## Success Criteria
1. Workflow YAML validates without errors
2. R2 judges run in parallel
```

**Expected result:** WARNING (file still written after operator confirms)
- All ERROR gates pass (WRONG-condition, Non-Goals, Success Hypothesis are valid)
- SA-3 (ratio): 2 criteria / 5 scope = 0.4 (< 0.8 threshold) → WARNING
- SA-4 (uncovered): "Retry logic", "Worktree lifecycle", "State persistence" have no matching criteria → WARNING listing them
- Operator prompted to override → if yes, file is written

---

### Test Case 6: Non-Goal Without Rationale → Rejected

**Input PRD snippet:**
```markdown
## Non-Goals
- Real-time collaborative editing
- Auto-deploy to production
- **Support mobile devices** — Not the target audience for CLI tools
```

**Expected result:** REJECTED
- Check NG-3: entry 1 "Real-time collaborative editing" has no rationale (no dash/parenthetical) → ERROR
- Check NG-3: entry 2 "Auto-deploy to production" has no rationale → ERROR
- Entry 3 passes (has rationale after em-dash)

---

### Test Case 7: WRONG-Condition With Implementation Prescriptions → Rejected

**Input PRD snippet:**
```markdown
## WRONG-Condition
- Use Redis instead of SQLite for session storage
- Implement WebSocket instead of polling
- The recall service returns stale results silently without cache invalidation
```

**Expected result:** REJECTED
- Check 7 (prescriptions): entry 1 "Use X instead" → ERROR
- Check 7 (prescriptions): entry 2 "Implement Y" → ERROR
- Entry 3 passes (describes observable failure, not a fix)
