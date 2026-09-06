# Homie Dashboard React Foundation Spike

This is the isolated React donor-foundation spike for the OpenBot native
Dashboard transplant. It is not a second Dashboard product and is not yet the
canonical build.

The spike deliberately:

- talks only to the existing local Hono/Python `/api/*` contracts;
- creates no database and owns no persona, session, memory, or approval truth;
- requires no CopilotKit account, CopilotKit Intelligence, or hosted service;
- keeps computer presentation read-only until the native ApprovalGrant exists;
- pins and attributes its OpenBot donor sources in `upstream-openbot.json`.

Run the existing Python API and dashboard Hono server, then:

```powershell
cd dashboard\web-next
npm install
npm run dev
```

Open `http://127.0.0.1:5174`. The canonical Preact Dashboard remains on `5173`
during the spike. Acceptance of this foundation removes the dual-runtime state;
`web-next` then replaces `dashboard/web` rather than remaining beside it.
