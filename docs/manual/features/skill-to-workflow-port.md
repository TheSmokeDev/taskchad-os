# Skill to Workflow Port

How to make an agent skill usable inside an Archon workflow node without the
workflow quietly citing something it never read.

## The failure this prevents

A skill's `SKILL.md` tells an agent to "read the reference and use the nearest
example cases." Some of what it points at is real bytes on disk. Some of it is a
URL, a link to a file the installer did not ship, or a live API.

A workflow node usually runs with web search disabled, without credentials, and
without network egress. When the node reaches a pointer, nothing happens. The
agent does not error. It improvises, and then stamps the artifact with the name
of the source it never opened.

That is a citation with no referent. The run looks grounded, the report names an
engine, the artifact carries example ids, and none of it resolves. The bug is
invisible precisely because the output is plausible.

**Rule: a skill reference that is a pointer degrades silently to zero inside a
node that cannot fetch, while still being cited.**

## The shape

Three nodes. The agent decides, a script retrieves, the agent consumes. The agent
never fetches.

```
decide     command:  (AI)      emits a retrieval REQUEST: ids, filters, k.
   |                           no network. cites ids only, never their contents.
retrieve   script:   (uv/bun)  PURE and OFFLINE. reads a pinned local cache,
   |                           re-hashes the bytes, resolves ids to real records.
consume    command:  (AI)      reads the resolved records, writes fresh output,
                               stamps provenance if and only if it resolved.
```

A `script:` node is the load-bearing piece. It runs no model, its stdout becomes
`$node.output` and is parsed as JSON, and a non-zero exit fails the node and skips
every dependant. That gives a deterministic, testable gate that an AI node cannot
talk its way around.

## Invariants

**I-1. Classify every reference.** For each thing the skill tells an agent to read,
label it DATA (bytes present in the installed skill) or POINTER (a URL, a link to
an uninstalled file, a live API). Only pointers need porting. Do not port what is
already local, or the node will hold two versions of the truth.

**I-2. Provisioning is not retrieval.** Network access happens in an out-of-band
`prime` step, run once per machine. The in-DAG node is pure and offline. A node
that fetches on a cache miss has reintroduced the exact fragility the port exists
to remove.

**I-3. Fail closed on a hollow citation, not on empty data.** Three states, not two:

| State | Exit | Behavior |
|---|---|---|
| cache absent or corrupt | 1 | provisioning bug. Skip every dependant. Say how to fix it. |
| cache valid, zero matches | 0 | `grounded: false`. Drop the citation. Mark the output self-authored. |
| cache valid, matches | 0 | `grounded: true`. Stamp provenance and the resolved ids only. |

Dying on an empty result set punishes a legitimate novel request. Stamping an
unresolved id is the actual defect. Separate them.

**I-4. Pin once, single source of truth.** One upstream commit per ported corpus.
Any enum the workflow validates against is generated from that pin or asserted
against it in a test. An id list duplicated across a YAML schema, a prompt, and the
corpus will drift, and the drift will surface mid-run as an unrelated-looking
schema failure.

**I-5. The cache is repo-independent and physically verified.** A global workflow
runs from any repository, so a cache under one repo's tree is invisible from the
others. Key it by upstream pin under the agent's home. Decide validity by
re-hashing the cached bytes on every read, never by trusting a sidecar marker: a
fetch killed halfway leaves a truncated file that a `downloaded: true` flag would
happily bless.

**I-6. Ported bytes never enter git or a shipped artifact.** The fetcher ships. The
corpus does not. Keep the cache outside every repo tree, carry the upstream license
next to it, and stamp source, pin, digest, and license onto anything the run
produces. Learning structure from third-party text is not the same as
redistributing it.

**I-7. On providers where node-scoped skills are cosmetic, say which source wins.**
Some providers auto-load every installed skill on every node regardless of what the
YAML declares. When that happens the node sees both the stale installed index and
the freshly pinned cache. Name the ported source authoritative in the node prompt
and name the skill's dangling references non-authoritative, or the model keeps
following the dead link.

## The one test that proves a port is real

Everything else is hygiene. This is the property whose absence is the bug:

- **positive:** with a fixture cache, every stamped id resolves to a record with
  non-empty content.
- **negative:** with an empty match set, the artifact carries no engine name and no
  ids at all.

If both hold, the port cannot cite what it cannot produce.

## Worked example

`image-node-factory` cites example cases from an image prompt-style library. The
installed skill carries the taxonomy (DATA) and links to the worked cases
(POINTER). The workflow runs with web search disabled, so the case ids resolved to
nothing while the pack claimed a prompt engine.

The port:

- `.archon/scripts/style-corpus.py` (`prime | verify | select | template | ground | stats`)
- a `ground` node between `select` and `prompt-pack`
- the corpus cached at `~/.archon/cache/skill-ports/<skill>/<pin>/`, digest-checked
- `image-node-select.md` emits integer ids and is told the gallery is non-authoritative
- `image-node-prompt-pack.md` reads the resolved exemplars, writes fresh wording,
  and stamps provenance only when grounded
- `image-node-qa.md` fails the run if a stamped id is unresolved

```bash
# once per machine, online
uv run .archon/scripts/style-corpus.py prime

# offline, deterministic
uv run .archon/scripts/style-corpus.py verify
uv run .archon/scripts/style-corpus.py select --template-id realistic-photography --k 5
```

Retrieval is a deterministic filter over the library's own taxonomy, in the order
its docs prescribe: category, then style tag, then scene tag, then the template's
nearest cases. Cited ids act as anchors and taxonomy tops the slate up to `k`. No
embedding model is involved, which matters here because the framework embedder is
English-only and the corpus is bilingual: vector search would have been confidently
wrong.

## Porting a different kind of skill

A skill whose data is a live API is the purest pointer. The same contract applies,
with the API call moved into `prime`:

- **decide:** emit a query spec. Do not call anything.
- **prime:** call the API out of band, with credentials, honoring any cost gate.
  Write the rows and a provenance record into the cache.
- **retrieve:** read the cached rows offline. If the query was never primed, report
  `grounded: false`. Never call the API from inside the run.
- **consume:** write the analysis. If ungrounded, say that no live data was
  available rather than citing numbers that were never fetched.

The lesson is provider-agnostic. Whenever a node cannot perform the retrieval a
skill assumes, the citation must resolve against pinned local bytes, or it must not
exist.
