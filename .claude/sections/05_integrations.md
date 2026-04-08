The `direct-integrations` skill provides direct API access to core framework integrations plus any optional deployment-specific integrations configured in this repo. **Always prefer direct integrations over Zapier.**

**Usage:** Run `/direct-integrations` skill or invoke scripts directly:
```bash
python .claude/skills/direct-integrations/scripts/query.py <service> <command> [--flags]
```

Core services: `gmail`, `calendar`, `asana`, `slack`, `sheets`, `docs`, `drive`, `circle`, `search-console`, `analytics`

See the `direct-integrations` skill SKILL.md for the full CLI reference.

### Authentication

| Service | Method | Key Env Vars |
|---------|--------|-------------|
| Google (Gmail, Calendar, Sheets, Docs, Drive, GSC, GA4) | OAuth2 (`your-calendar@gmail.com`) | `google_token.json` (7 scopes) |
| Asana | Personal Access Token | `ASANA_ACCESS_TOKEN` |
| Slack | Bot Token | `SLACK_BOT_TOKEN` |
| Circle | Admin V2 + Headless Auth | `CIRCLE_ADMIN_TOKEN`, `CIRCLE_HEADLESS_TOKEN` |
| Outlook / Microsoft Graph | MS Graph client creds | `GRAPH_CLIENT_ID`, `GRAPH_TENANT_ID` |

- **Setup:** `cd .claude/scripts && uv run python setup_auth.py`
- **Re-auth:** Delete `google_token.json` and re-run `setup_auth.py`
- All account IDs and service preferences are in `vault/memory/USER.md`

### Heartbeat Architecture

The heartbeat gathers data from all integrations in Python BEFORE invoking Claude:
```
heartbeat.py → Python calls APIs → results fed into runtime prompt → runtime reasons
```
Claude no longer needs Skill/MCP tools for heartbeat — data is pre-loaded as context.
