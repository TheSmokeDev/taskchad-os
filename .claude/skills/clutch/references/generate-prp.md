# Create BASE PRP (Teams Variant)

## Feature: $ARGUMENTS

## PRP Creation Mission

Create a comprehensive PRP that enables **one-pass implementation success** through systematic research and context curation.

**Critical Understanding**: The executing AI agent only receives:

- The PRP content you create
- Its training data knowledge
- Access to codebase files (but needs guidance on which ones)

**Therefore**: Your research and context curation directly determines implementation success. Incomplete context = implementation failure.

## Research Process

- Start by invoking the codebase-analysis subagent if this is a new feature or bugfix in an existing project, then read the markdown file it produces in the PRPs/planning folder.
- When invoking the codebase analysis subagent, prompt it to research the codebase for the specific feature being implemented.

> During the research process, create clear tasks and spawn sub-agents to research in parallel. The deeper research we do here the better the PRP will be. We optimize for chance of success, not speed.

1. **Codebase Analysis in depth**
   - Spawn sub-agents to search the codebase for similar features/patterns
   - Identify all the necessary files to reference in the PRP
   - Note all existing conventions to follow
   - Check existing test patterns for validation approach

2. **External Research at scale**
   - Spawn sub-agents to do deep research for similar features/patterns online
   - Library documentation (include specific URLs)
   - For critical pieces of documentation add a .md file to PRPs/ai_docs and reference it in the PRP with clear reasoning and instructions
   - Implementation examples (GitHub/StackOverflow/blogs)
   - Best practices and common pitfalls found during research

   ### Research Tools (If Available)
   Use whatever research tools your platform provides:
   - **Web search** — for documentation, tutorials, examples
   - **Shell/command runner** — use `gh` CLI for GitHub code search, repo exploration
   - **File search** — search the codebase for patterns, similar implementations

   **If specific tools are unavailable**, rely on PRP context and your training knowledge. Do not block on missing tools.

   **Priority:** Codebase search for patterns > GitHub CLI for code examples > Web search for concepts

3. **User Clarification**
   - Ask for clarification if you need it

## PRP Generation Process

### Step 1: Choose Template

Use the PRP template (`prp_base.md`) as your template structure - it contains all necessary sections and formatting.

### Step 2: Context Completeness Validation

Before writing, apply the **"No Prior Knowledge" test** from the template:
_"If someone knew nothing about this codebase, would they have everything needed to implement this successfully?"_

### Step 3: Research Integration

Transform your research findings into the template sections:

**Goal Section**: Use research to define specific, measurable Feature Goal and concrete Deliverable
**Context Section**: Populate YAML structure with your research findings - specific URLs, file patterns, gotchas
**Implementation Tasks**: Create dependency-ordered tasks using information-dense keywords from codebase analysis
**Validation Gates**: Use project-specific validation commands that you've verified work in this codebase

### Step 4: Information Density Standards

Ensure every reference is **specific and actionable**:

- URLs include section anchors, not just domain names
- File references include specific patterns to follow, not generic mentions
- Task specifications include exact naming conventions and placement
- Validation commands are project-specific and executable

### Step 5: Plan Thoroughly Before Writing

After research completion, create a comprehensive PRP writing plan:

- Plan how to structure each template section with your research findings
- Identify gaps that need additional research
- Create systematic approach to filling template with actionable context

### Step 6: Define Workstreams for Parallel Execution

**CRITICAL FOR TEAMS**: The PRP MUST include a `## Workstreams` section that defines how work can be split across parallel executor teammates.

**Workstream Design Rules:**
- Each workstream should be **file-independent** — no two workstreams should create or modify the same file
- If two features must touch the same file, put them in the same workstream
- Order workstreams by dependency: independent workstreams first, dependent ones later
- Aim for 2-4 workstreams (sweet spot for parallelism vs. coordination overhead)
- If the phase genuinely cannot be parallelized, define only 1 workstream (the orchestrator will fall back to solo execution)

### Contract Chain Analysis

When generating workstreams, also analyze dependencies between them:

1. For each workstream, check: does it PRODUCE interfaces that other workstreams CONSUME?
2. If yes, add `depends_on` to the consumer workstream
3. Document the contract boundary (what shape of data flows between them)
4. Identify cross-cutting concerns that span workstreams (URL conventions, error shapes, etc.)

Example output in PRP:
```markdown
### Workstream 1: api
- **Files owned**: src/api/, src/middleware/
- **Depends on**: none (UPSTREAM)
- **Produces**: REST API contract for frontend workstream
- **Tasks**: endpoints, auth, validation

### Workstream 2: frontend
- **Files owned**: src/components/, src/pages/
- **Depends on**: api (DOWNSTREAM — needs API contract first)
- **Consumes**: REST API contract from api workstream
- **Tasks**: components, routing, state management
```

If ALL workstreams are independent (no `depends_on`): note this explicitly. The orchestrator uses this to decide: parallel spawn (independent) vs staggered spawn (dependent).

Also populate the **Contract Chain** and **Cross-Cutting Concerns** sections in the PRP template when dependencies exist.

**What goes in each workstream:**
- A descriptive name (becomes the teammate's name: `executor-{name}`)
- The specific files it creates and modifies (exclusive ownership)
- Dependencies on other workstreams (if any — minimize these)
- The implementation tasks from the main task list that belong to this workstream

**Think about natural boundaries:**
- Frontend vs. backend
- Different API endpoints or services
- Independent components or modules
- Database layer vs. business logic vs. UI
- Tests can go with the code they test (same workstream)

## Output

Save as: `PRPs/{feature-name}.md` (avoid calling it INITIAL.md)

## PRP Quality Gates

### Context Completeness Check

- [ ] Passes "No Prior Knowledge" test from template
- [ ] All YAML references are specific and accessible
- [ ] Implementation tasks include exact naming and placement guidance
- [ ] Validation commands are project-specific and verified working

### Template Structure Compliance

- [ ] All required template sections completed
- [ ] Goal section has specific Feature Goal, Deliverable, Success Definition
- [ ] Implementation Tasks follow dependency ordering
- [ ] Final Validation Checklist is comprehensive

### Information Density Standards

- [ ] No generic references - all are specific and actionable
- [ ] File patterns point at specific examples to follow
- [ ] URLs include section anchors for exact guidance
- [ ] Task specifications use information-dense keywords from codebase

### Workstream Quality (Teams-Specific)

- [ ] Workstreams section is present and well-defined
- [ ] No file ownership conflicts between workstreams
- [ ] Dependencies between workstreams are minimized and explicit
- [ ] Each workstream has clear, actionable task lists
- [ ] Workstream count is between 1 and 4

## Success Metrics

**Confidence Score**: Rate 1-10 for one-pass implementation success likelihood

**Validation**: The completed PRP should enable an AI agent unfamiliar with the codebase to implement the feature successfully using only the PRP content and codebase access.
