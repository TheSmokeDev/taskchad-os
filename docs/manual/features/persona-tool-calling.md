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
