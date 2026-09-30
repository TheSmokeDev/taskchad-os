# TaskChad Build + Grow hosted source candidate

This is independent, undeployed source prepared alongside the tested local
TaskChad OS implementation. It includes a portable artifact engine and a Sites
D1/R2 reference adapter. It is not a registered Site or a submission-ready
plugin package.

The user's preferred hosting path is the existing TaskChad customer portal,
which uses Vercel and Supabase. Reuse that portal's verified identity, fresh
membership checks, RLS and private storage when integrating the portable engine.
The D1/R2 store in this reference is not a Supabase adapter. Its trusted-header
authentication is specific to Sites dispatch and must never be carried unchanged
to a public Vercel endpoint. Vercel deployment access and provider readiness are
separate prerequisites; no production migration or deployment has run here.

`worker/index.mjs` exports a Worker-compatible default `fetch` handler with
stateless JSON MCP `POST /mcp`, anonymous initialization/discovery, and nine
signed-in project/artifact tools. Supported protocol revisions are explicitly
2025-06-18 and 2025-03-26; other protocol revisions are not advertised.
It also serves health/status information and authenticated exact-byte downloads.

`worker/store.mjs` uses prepared D1 queries scoped by the trusted Sites
`oai-authenticated-user-id` header. Never expose this Worker through a proxy that
allows clients to spoof that header. Sites dispatch must own authentication and
strip/inject identity; a service bypass credential alone grants no user records.

The logical bindings required are `DB` (D1) and `BUCKET` (R2). The installed
starter must declare them in `.openai/hosting.json` along with `mcp` capability;
its real project ID must come from one successful native registration. No Site
identity or endpoint is invented here. Integrate `db/schema.sql` with the
starter's Drizzle schema and generate inspected migrations before deployment.
The SQL file is a reviewed candidate used by local tests, not a generated
production migration. Runtime requests never create or alter tables.

Generation saves frozen briefs, uploads bytes before publishing artifact
pointers, and atomically commits pointers/job status with lease checks. Failed
known-uncommitted writes are cleaned; an unknown commit outcome preserves blobs
for reconciliation. The first version runs bounded generation synchronously.
Limits: 100 projects and 500 historical jobs per user; 30 audit files / 2 MiB
source text; 3 MiB HTTP body; five-minute generation lease. No custom code,
network crawl, external provider, deployment or purchasing is executed.

`npm test` runs actual SQLite-backed D1 contract tests with a fault-injected R2
adapter and renderer checks. This validates local behavior, not production D1,
Sites OAuth, the installed host plugin, or a Portal saved version. A real hosted
connection and review-case run remain mandatory.

Current release preparation is tracked separately; none of the required remote
endpoint, OAuth connection, public policies, reviewer access, host demo or Portal
saved-version checks has been completed by these local tests.
There is no automatic retention policy or self-service deletion. Complete that
product/policy decision before public release. Do not mark local tests or this
source as proof of a completed deployment or public submission.
