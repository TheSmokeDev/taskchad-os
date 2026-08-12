# Persona Capability Elevation

Status: active, bounded one-call approval path

Persona toolsets stay default-deny. When a persona is blocked by a registered
tool outside its compiled scope, it can call `request_tool` with:

- the exact registered tool name;
- why the current task needs it;
- the exact JSON arguments it intends to use.

`skills_list`, `skill_view`, and `request_tool` are implicit base capabilities
on authenticated named-persona chat turns. Existing profiles do not need
`safe_core` added or their YAML rewritten. Skill discovery is read-only, and
the request bridge can only create a pending request; none of the three grants
or executes the requested domain tool.

The originating Discord or Telegram conversation receives an **Approve once**
and **Deny** card. Approval does not edit `config.yaml`, add a toolset, promote a
skill, or change the persona's future turns. It creates one process-local grant,
automatically retries the original task, and permits one matching call. A
different argument payload or second call is refused.

The card displays the complete canonical argument payload. Requests too large
to display safely are refused instead of showing a truncated approval. Use a
reviewed workflow or a permanent scoped capability for larger operations.

## Voice And Text Approval

Buttons are the normal path. A transcribed voice note or typed message can use
the request-bound phrase shown below:

```text
approve capability 1A2B3C4D5E
deny capability 1A2B3C4D5E
```

Bare `yes`, `approve`, or conversational agreement never carries authority.
The decision must come from the request's originating channel and an
authenticated operator/admin identity.

## Skill Use

Personas discover installed skills with `skills_list` and read the selected
instructions with `skill_view`. If following those instructions needs a tool
outside the persona's scope, the persona requests that exact tool call.

`skill_manage` still creates an inert draft only. Promotion remains a separate
operator action through `/skills promote` and its security scan. One-time tool
approval never promotes or permanently installs a skill.

## Non-Elevatable Authority

Tools are non-elevatable unless their registry entry explicitly opts in. A tool
with `dedicated_gate=true` can never opt in. This keeps real-money actions,
external posting, browser writes, profile mutation, skill promotion, deployment,
and other one-way doors on their existing approval paths.

For generic shell approval, the request card exposes and binds the exact command.
The elevation gate additionally refuses known deployment, publication, remote
mutation, profile-authority, and kill-switch command shapes. This is still a
human-approved shell, not an OS sandbox; approve only commands you recognize.

One-time `write_file` and `patch` grants are confined to the current project.
They cannot write the Homie profile tree.

### Crypto paper trading

Paper trading is a standing Crypto Homie capability and does not require an
approval card or expiring mandate. The bracket path is hard-coded to dry-run
mode and cannot reach a venue. Live trading remains a separate dedicated gate.
For other missing registered tools, Crypto Homie uses the same one-time
request card as every persona; direct operator authorization should cause the
persona to request the capability itself instead of asking the operator to
relay an internal tool name.

## Persistence, Expiry, And Restart

Pending decisions and their lifecycle live in the private SQLite ledger:

```text
.claude/data/persona_elevation.db
```

Append-only receipts live at:

```text
.claude/data/persona_elevation.jsonl
```

Requests default to a ten-minute TTL. Approved grants default to five minutes,
are held only in process memory, and are consumed before the retry begins. A
restart therefore invalidates an approved-but-unclaimed grant instead of
silently carrying authority into a new process.

Every request, grant, denial, expiry, and consumption is persona-attributed.
The dashboard audit receives an additive copy when available.

## Kill Switch

The feature ships on. Disable new elevation requests with:

```text
HOMIE_KILLSWITCH_PERSONA_ELEVATION=disabled
```

This switch does not disable already compiled persona tools; use
`HOMIE_KILLSWITCH_PERSONA_TOOLS=disabled` for the whole persona caller-tool
surface.

Optional TTL controls:

```text
HOMIE_CAPABILITY_REQUEST_TTL_SECONDS=600
HOMIE_CAPABILITY_GRANT_TTL_SECONDS=300
```

## Verification

```powershell
cd .claude\scripts
uv run pytest tests/test_persona_elevation.py `
  tests/test_persona_tool_assembly.py tests/test_tool_registry.py -q
```

The adversarial suite covers origin/persona binding, exact-argument enforcement,
single use, duplicate taps, denial/no-nag behavior, expiry, restart loss,
dedicated-gate refusal, shell/profile boundary checks, and spoofed buttons.

## Related

- [Persona Blueprint Capability Provisioning](persona-blueprints-capability-provisioning.md)
- [Persona Capability Matrix](persona-capability-matrix.md)
- [Persona Team](persona-team.md)
- [Skill From Experience Loop](skill-from-experience-loop.md)
- [Persona Tool Calling](persona-tool-calling.md)
