# Verification cases

The automated client tests run against a disposable loopback HTTP fixture. They cover actual request paths and bearer forwarding, stale-review error propagation, bounded response/download handling, unsafe source paths, mutation errors without retries, redirect credential protection, and digest-checked no-clobber downloads. With the OS's FastAPI/uvicorn dependencies installed, an additional integration case executes the real API router, store, website/GEO artifact builders, downloads, and reviews over HTTP with isolated test workspace authentication. It checks that another workspace cannot read or review the artifacts. Production authentication is verified by the OS API tests separately. No test calls an external provider or uses a real token.

Run from the repository root:

```sh
python -m unittest discover -s .claude/scripts/tests -p test_build_grow_plugin.py -v
```

`fixtures/skill-cases.json` contains manual trigger/behavior fixtures. In the target supported local host, install from the repo marketplace and exercise each positive trigger and failure scenario. Inspect whether the agent follows explicit user instructions, reports actual status, scopes records correctly, separates artifact findings from live measurements, and seeks no unnecessary confirmation for authorized work.

Before a customer release, test the end-to-end real API with a disposable project: save a fictional brief, create/run a website job, verify its ZIP digest, inspect the pages, create/run a GEO job referencing that website job, inspect its report, request changes for the exact digest, and read the persisted review. Confirm an authenticated second workspace cannot read or review that project's IDs. Confirm queued/failed state and stale revision/digest conflicts appear clearly.

Local API and package checks are not proof that the plugin is installed in ChatGPT, publicly listed, reachable from web/mobile, or able to bill a user's ChatGPT subscription. Test installation and execution in the actual target host before making those claims. This source package does not submit a public listing.
