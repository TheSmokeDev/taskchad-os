# Add a Skill to The Homie

Step-by-step checklist. The `skill-creator` skill teaches HOW to write a good skill; this guide teaches WHERE it fits in the framework.

## Steps

### 1. Create the skill directory

```
.claude/skills/<skill-name>/
```

Use kebab-case. The directory name becomes the invocation name (`/<skill-name>`).

### 2. Write SKILL.md with frontmatter

Create `.claude/skills/<skill-name>/SKILL.md` with required YAML frontmatter:

```yaml
---
name: <skill-name>
description: <what it does + when to use it>
---
```

Both `name` and `description` are required. The description is the primary trigger mechanism — Claude reads it to decide when the skill applies. Put all "when to use" info here, not in the body.

Mirror the structure at `skill-creator/SKILL.md:47-62` for the anatomy pattern.

### 3. Add scripts/ if needed

Create `.claude/skills/<skill-name>/scripts/` for executable code that needs deterministic reliability or gets rewritten repeatedly. Test every script by running it.

### 4. Add references/ if needed

Create `.claude/skills/<skill-name>/references/` for documentation loaded into context on demand. Keep SKILL.md lean — move detailed reference material here. One level deep from SKILL.md (no nested references).

### 5. Skill Discovery & Registration (Internals)

The harness auto-discovers skills via this process:

1. **Scan phase** — `.claude/chat/cognition/skills.py:_iter_existing_skills()` walks `.claude/skills/` recursively looking for `SKILL.md` files
2. **Parse phase** — Each `SKILL.md` is parsed for YAML frontmatter (lines 1-3, between `---` markers)
3. **Index phase** — Names + descriptions extracted; indexed in the `procedural_memory` prompt region for availability hints
4. **Invocation phase** — User types `/<skill-name>` → the Skill tool loads the matching `SKILL.md` and serves its body as context

**Key constraint:** Frontmatter `name` and `description` are required. If either is missing, the skill won't be discoverable.

### 6. Validation Command

Run this command to verify your skill is discoverable end-to-end:

```bash
# Standalone validation (no LLM call)
python .claude/chat/cognition/skills.py \
  --validate-skill .claude/skills/<skill-name>/SKILL.md
```

This validates:
- [ ] `SKILL.md` exists at the given path
- [ ] YAML frontmatter is valid (between `---` markers)
- [ ] `name` field is present and non-empty
- [ ] `description` field is present and non-empty
- [ ] Body (after frontmatter) is non-empty (actionable content, not just metadata)
- [ ] Total file size under 25KB (reasonable context budget)

If validation passes, the skill is discoverable by the harness and ready to invoke.

### 7. Test by invoking

Type `/<skill-name>` in a Claude Code session to verify:
- Skill appears in the available skills list (check via `/help` or typeahead)
- SKILL.md body loads on invocation
- Referenced files (scripts/, references/) are readable
- No error messages in the Skill tool output

### 8. Validate structure (Checklist)

Confirm the minimum viable skill:
- [ ] Directory exists at `.claude/skills/<skill-name>/`
- [ ] `SKILL.md` has valid YAML frontmatter (name + description)
- [ ] Body contains actionable instructions (not just metadata)
- [ ] Validation command passes: `python .claude/chat/cognition/skills.py --validate-skill .claude/skills/<skill-name>/SKILL.md`
- [ ] Manual invocation test works: `/<skill-name>` loads the body
- [ ] No extraneous files (README.md, CHANGELOG.md, etc.)
- [ ] Total SKILL.md body under 500 lines (context window is a public good)

### 9. Commit

Stage only the new skill directory. Commit message:

```
feat(skills): add <skill-name> skill

<one-line description of what it does>
```

## Key Principle

The context window is a public good. Claude is already smart — only add context it doesn't have. Challenge each paragraph: "Does this justify its token cost?"

See `skill-creator/SKILL.md:27-33` for the full conciseness principle.
