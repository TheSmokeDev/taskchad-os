# Local workflow connection

This developer package uses the existing TaskChad OS API. It is not an independently hosted public service. The host running the client must be able to reach that API. `localhost` in a cloud host refers to that host, not the user's computer.

Set `TASKCHAD_API_URL` to the API origin, default `http://127.0.0.1:4322`. Set `TASKCHAD_API_TOKEN` securely to the existing operator API bearer or a provisioned tenant-scoped bearer. The local API's operator bearer is configured as `ORCHESTRATION_API_TOKEN`; the client's variable forwards that same value. The server derives the workspace from authenticated credentials. The client never accepts a workspace override. Use HTTPS for a remote connection; HTTP is restricted to loopback.

Resolve `CLIENT` as the absolute path to this plugin's `scripts/taskchad_client.py`. Run commands through structured shell arguments or correctly quoted literal arguments. Keep brief/source JSON in an explicit file or stdin; do not interpolate its contents into a shell command. Outputs are JSON on stdout; errors go to stderr with a nonzero exit status. Mutation calls are not retried automatically.

```text
python CLIENT projects
python CLIENT project PROJECT_ID
python CLIENT create-project --name "Business project" --brief business-brief.json
python CLIENT update-brief PROJECT_ID --brief business-brief.json --expected-revision 1
python CLIENT jobs PROJECT_ID
python CLIENT create-job PROJECT_ID --kind website
python CLIENT run-job JOB_ID
python CLIENT job JOB_ID
python CLIENT create-job PROJECT_ID --kind geo --source-job-id WEBSITE_JOB_ID
python CLIENT create-job PROJECT_ID --kind geo --source-files selected-site-files.json
python CLIENT download DELIVERABLE_ID --output draft.zip --expected-sha256 SHA256
python CLIENT review DELIVERABLE_ID --decision changes_requested --expected-sha256 SHA256 --note "Replace the draft contact details."
```

`CLIENT`, IDs, and `SHA256` above are placeholders resolved from actual paths/API responses. `--brief -` and `--source-files -` read JSON from stdin. `download --force` explicitly replaces an existing output file; obtain authorization before overwriting unrelated user files.

Brief JSON fields: `business_name`, `description`, `industry`, `location`, `services` (objects containing `name` and `description`), `contact_email`, `phone`, `site_url`, `repository_url`, `brand_color`. Consult `../fixtures/example-brief.json` for a fictional example; do not reuse its business details for a real customer. Omit unknown optional values and ask for facts that block a requested deliverable.

Artifact GEO input is a JSON object mapping selected relative file paths to text. Supported suffixes: `.html`, `.css`, `.json`, `.txt`, `.xml`; at least one HTML file; at most 30 files and 2 MiB of UTF-8 content. Hidden, credential, absolute, and parent paths are rejected. Explicit source job and source files are mutually exclusive. The first version inspects provided artifacts and does not fetch URLs, perform live search measurement, run TokenMax, or deploy.

API base path: `/api/build-grow`. Project reads return `id`, `name`, `brief_revision`, `brief`, `created_at`, and `updated_at`. Project lists wrap `projects`; job lists wrap `jobs`. Job responses return `id`, `project_id`, `kind`, `brief_revision`, `source_job_id`, `status`, timestamps, `error`, and `deliverables`. Artifact metadata includes `id`, `filename`, `media_type`, `sha256`, `size_bytes`, and `review_status`. Do not infer an artifact is ready from the existence of a job ID.

Use the current `brief_revision` for brief updates and actual artifact `sha256` for reviews. Approval is a recorded artifact decision, never a release command. No command in this client merges, deploys, purchases, sends messages, or performs account billing.
