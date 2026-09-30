# TaskChad Build + Grow

Local developer plugin for business briefs, website drafts, technical SEO/GEO artifact audits, and reviewable delivery history in TaskChad OS. It combines three focused skills with a Python standard-library HTTP client. There is no bundled private GEO corpus, registered app binding, public MCP endpoint, checkout, or deployment command.

## Setup

1. Start TaskChad OS's existing authenticated orchestration API with Build + Grow enabled as documented by the repository.
2. Configure `TASKCHAD_API_URL` in the host that runs the plugin client; the default is `http://127.0.0.1:4322`. Securely configure `TASKCHAD_API_TOKEN` with the existing operator bearer (`ORCHESTRATION_API_TOKEN` on the API) or a provisioned tenant-scoped bearer. Do not paste tokens into chat, command arguments, source files, or committed configuration.
3. For supported local Codex/ChatGPT desktop clients, register this repository as a marketplace, then install **TaskChad Build + Grow** from **TaskChad OS**. From the repository root, supported Codex versions expose `codex plugin marketplace add .`; check the installed CLI's help first. The repo catalog is `.agents/plugins/marketplace.json`. Installation remains a host action; this commit does not install it in your account.

Portable clients discover root `plugin.json` and `skills/`. The package intentionally has no `mcp.json`: there is no verified remote MCP server or registered app ID. The workflows invoke a real local HTTP client through the host's available shell. A skills-only host without shell access can explain the workflow but cannot execute these OS commands. A cloud host's loopback does not point to your laptop; use a reachable authorized HTTPS OS endpoint or run the plugin beside the OS.

## Execute

From this repository root, with credentials already configured:

```sh
python plugins/taskchad-build-grow/scripts/taskchad_client.py projects
python plugins/taskchad-build-grow/scripts/taskchad_client.py create-project --name "Example project" --brief plugins/taskchad-build-grow/fixtures/example-brief.json
```

The fixture is fictional. Replace it with verified customer facts before real use. Continue with `create-job PROJECT_ID --kind website`, `run-job JOB_ID`, `job JOB_ID`, and a digest-checked `download`. See [the workflow reference](references/workflow.md) for all commands and fields. Create a `geo` job with a completed website job ID or a selected artifact JSON map. Requests stay scoped by the API bearer; no command accepts a tenant override.

The local website builder produces reviewable static drafts. The GEO worker inspects provided artifacts; it does not perform a live crawl, guarantee rankings, query Search Console, execute TokenMax, or publish files. Existing repository improvements use the host's connected tools and must be checked in that repository. Contact links and a completed file download do not prove a CRM integration or a public deployment.

## Validate

```sh
python -m unittest discover -s .claude/scripts/tests -p test_build_grow_plugin.py -v
python plugins/taskchad-build-grow/scripts/taskchad_client.py --help
```

See [verification cases](TEST_CASES.md) and [manual skill fixtures](fixtures/skill-cases.json). The root manifest uses the [Agent Plugins 1.0.0 schema](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json), and the listing subtitle fits the 30-character limit. A private developer archive can be created outside the plugin directory with `python -m zipfile -c taskchad-build-grow.zip plugins/taskchad-build-grow`, excluding generated caches beforehand. Public directory submission requires a separate supported hosted integration, reviewer access, listing materials, and actual host verification.

## Commercial limits

This package uses available capabilities in the customer's local host; it neither shares subscription credentials nor enables TaskChad.com to bill AI requests against their ChatGPT plan. Commercial Sign in with ChatGPT subscription access requires the applicable OpenAI partner approval. External APIs, infrastructure, communications, and plan usage limits still apply.

Existing paid customers can access entitled services, but plugin workflows do not sell digital subscriptions, promote upgrades, initiate payments, or add a plugin-access surcharge. Keep independent website purchasing outside the plugin workflow. Selected public instructions are MIT-licensed; private client records and unpublished research are not included.
