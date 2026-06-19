# Add a Direct Integration to The Homie

Step-by-step checklist for connecting a new external API. All integrations follow the default-deny mutation policy.

## Steps

### 1. Create the integration module

File: `.claude/scripts/integrations/<service>_api.py`

Implement the API client with auth setup and query methods. Follow existing patterns (e.g., `gmail_api.py`, `slack_api.py`).

### 2. Register in `registry.py`

File: `.claude/scripts/integrations/registry.py`, variable `_REGISTRY` (~line 47).

```python
"myservice": IntegrationInfo(
    name="myservice",
    display_name="My Service",
    auth_type="api_key",          # or "oauth2"
    required_config=["MYSERVICE_API_KEY"],
    module_path="integrations.myservice_api",
),
```

This makes the service visible to `get_integration_status()` and framework health checks.

### 3. Add capability gate in `capabilities.py`

File: `.claude/scripts/integrations/capabilities.py`, variable `_ACTIONS` (~line 72).

```python
_action("myservice", "read_data", "read", exposures=("myservice",)),
_action("myservice", "send_message", "write", exposures=("myservice",)),
```

Every mutating action (write/archive/send) **must** call `require_integration_action("myservice", "send_message")` at the entrypoint. Read actions are gated but typically allowed by default policy.

This is the default-deny gate: actions are denied unless the policy explicitly allows them. See `capabilities.py:require_integration_action()` (~line 319).

### 4. Add env vars to `.env`

File: `.claude/scripts/.env`

```
MYSERVICE_API_KEY=...
```

Add all required config keys from step 2's `required_config` list.

### 5. Wire into heartbeat gather (if applicable)

File: `.claude/scripts/heartbeat.py`

If the integration provides data the heartbeat should monitor (calendar events, emails, tasks), add a gather step in the heartbeat's data collection phase. The heartbeat gathers data in Python BEFORE invoking the runtime.

## Verification

1. `python -c "from integrations.registry import get_integration_status; print(get_integration_status('myservice'))"`
2. Run the integration's read method directly to confirm auth works
3. Test mutating actions hit the capability gate (should raise `IntegrationPolicyError` if not explicitly allowed)

## Key Principle

Default-deny → explicit capability gate → audit row. A new integration ships read-only until a dedicated PRP enables write actions.
