# Persona Tool Calling

Persona tools are model-agnostic. A persona declares toolsets in its profile;
the runtime resolves an exact OpenAI-format definition snapshot and sends every
call back through one Homie-owned dispatcher.

## Transport split

| Lane | Caller-tool transport |
|---|---|
| Claude | In-process SDK bridge |
| Kimi / OpenAI-compatible | Chat-completions tool loop |
| Codex ordinary text/native tools | `codex exec` |
| Codex persona caller tools | Isolated `codex app-server` `dynamicTools` |

Do not change the `codex exec` adapter's
`supports_caller_tool_defs()` result to true. `exec` still cannot carry the
schemas. The provider-level Codex adapter is a composite: requests with empty
`tool_defs` use `exec`; requests with non-empty `tool_defs` use app-server.

## Configured route vs. executable route

A configured runtime route may legitimately contain providers that carry no
caller schemas at all — Gemini CLI has no such surface, and `codex exec` is
deliberately declared false above. Those providers stay fully eligible for
text-only and provider-native tool turns. They are excluded from an
**equipped** turn, and the exclusion happens **before** any provider contact:
`lane_router` asks each adapter's literal `supports_caller_tool_defs()` and
skips the ones that answer anything other than `True`.

So a route has two readings, and they are not the same list:

| Reading | Meaning |
|---|---|
| Configured route | Every provider the operator's selection and fallback contract resolve to, in order |
| Executable route | The subset of that route whose adapters literally carry caller schemas |

Consequences an operator should expect:

- **A provider preference is route ORDER, not exclusive eligibility, for an
  equipped turn.** The preferred provider is still offered the turn first and
  still wins outright when it carries. When it cannot carry, it is skipped
  without contact and the next carrying candidate executes — instead of the
  turn dying with a transport error while a perfectly good carrying fallback
  sat configured and unused.
- **The request's own fallback contract still bounds this.** A request with
  `allow_fallback=False`, or one resuming a session, keeps its single-provider
  route and fails honestly rather than being widened onto a provider the
  caller did not authorize.
- **Turns that carry no caller definitions are untouched.** Text turns and
  provider-native tool turns keep the exact single-provider route a pin gives
  them today. "Carrying tools" means non-empty `tool_defs`, never the
  `TOOL_REASONING` capability tier.
- **No carrying candidate is a loud failure.** The turn raises
  `RuntimeCallerToolTransportError`. A confident text-only answer from a
  provider that silently dropped the schemas is never accepted as success — it
  is indistinguishable from the persona refusing to act, which is the exact
  symptom this design exists to kill.

## Authority boundary

The app-server child runs from an empty temporary directory with an isolated
temporary `CODEX_HOME` containing only subscription auth. It receives empty
workspace roots and environments, an empty MCP map, and explicit feature
disables for shell, file mutation/read surfaces, web, apps, skills,
browser/computer, image generation, hooks, memory, and collaboration.

Only the supplied `dynamicTools` are accepted. Unknown names, malformed
arguments, duplicate call IDs, unexpected server requests, mismatched
thread/turn IDs, and any native-tool event fail closed.

The dynamic call re-enters `RuntimeRequest.tool_dispatch`. Persona scope,
mid-turn kill switch, one-way-door guards, and audit behavior therefore remain
shared with Claude and Kimi.

Persona scope remains default-deny. A blocked persona may request one exact,
operator-approved call without editing its permanent profile; see
[Persona Capability Elevation](persona-capability-elevation.md).

If every selected runtime exhausts caller-tool transport in a Discord persona
channel, that channel retries once as an explicitly text-only turn. The retry
receives no definitions and no dispatcher, cannot claim an action occurred,
and adds a visible no-action notice. Generic runtime, configuration, and
security errors do not silently trigger this downgrade.

## Scope provenance

The runtime rejects:

- unregistered tool names;
- a registered name paired with a schema that does not exactly match the
  registry snapshot;
- unsupported Codex dynamic-tool shapes.

Chat, Cabinet, and Discord carry a deterministic `tool_scope_version` hash of
the persona ID plus exact definition snapshot. Matching persona scopes must
produce matching hashes across all three surfaces.

## Capability plugin kernel boundary

The [Capability Plugin Kernel](capability-plugin-kernel.md) is now the strict,
transactional foundation for future tool, toolset, skill, MCP, and command
plugins. It does **not** replace this chapter's persona equipment or transport
path yet: profile grants, `tool_defs`, `tool_scope_version`, the Homie-owned
dispatcher, and the Claude/Kimi/Codex carrier rules above remain authoritative.

Issue #530 ships only the disabled generic lifecycle kernel and reversible
fixture. Typed contribution adapters begin in #531; profile equipment,
disclosure, migrations, and operator controls are later slices. Until those
slices land, enabling a v2 capability plugin cannot add a tool to any persona,
Talk, Cabinet, Discord, or chat turn.

## Verification

```powershell
cd .claude/scripts
$env:RUN_CODEX_APP_SERVER_INTEGRATION='1'
uv run pytest tests/test_openai_codex_app_server.py tests/test_codex_crypto_acceptance.py -q
```

The integration test uses the real installed Codex binary and subscription
login. It is opt-in so ordinary unit suites do not make provider calls.

No production bot restart, Discord message, or deployment is part of this
verification.
