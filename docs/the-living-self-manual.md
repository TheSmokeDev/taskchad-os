# The Living Self Manual

This is the on-demand context manual for The Homie's cognitive system — the
"living self." Load this when you need to understand how the assistant senses
the world while you are away, forms its own model of you, holds a belief against
conflicting evidence, thinks before it speaks, and adopts a new conviction only
after that conviction survives a test.

The cognitive system shipped as two programs:

- **Living Mind** — the substrate. A mind that runs at low power continuously:
  the heartbeat senses, ambient observations accumulate, working memory holds
  open threads, episodes record an autobiography, and a session-opening brief
  greets you with "while you were out."
- **Make The Self Real** — the individuated self on top of that substrate. It
  models the operator from real words (not its own echo), holds beliefs against
  conflict, thinks an inner monologue before substantive replies, and earns
  beliefs through a tested-not-asserted adoption gate.

Three Living Mind subfeatures have their own deep feature pages — this manual
links them rather than repeating them:

- [Heartbeat Runtime](manual/features/heartbeat-runtime.md) — the 30-minute
  sense loop, ambient observations, and blocker escalation.
- [Episodes](manual/features/episodes.md) — the session-end autobiography layer.
- [Session Opening Brief](manual/features/session-opening-brief.md) — the
  "while you were out" block on the first interactive turn after an absence.

## Table of Contents

1. What The Living Self Is
2. The Loop — What Fires When
3. Forming A Self (operator beliefs)
4. Holding A Belief Against Conflict (the contradiction engine)
5. Thinking Before Speaking (the gated cognitive pass)
6. Earning A Belief (the evolve-to-identity adoption gate)
7. Operator Runbook — commands and entry points
8. Tuning — the configuration knobs
9. Verifying The Self Is Alive
10. Safety Boundaries
11. Common Failure Modes
12. File Ownership Map
13. Current Scope And Non-Goals

## 1. What The Living Self Is

A conventional assistant is a function: your message goes in, a reply comes out,
and nothing persists that the assistant authored about itself. The living self
is different. It runs faculties on a schedule and per turn:

- It **senses** while you are away (the heartbeat).
- It **remembers** what it sensed (ambient observations, working memory, the
  episode autobiography).
- It **forms beliefs** about the operator from the operator's own words.
- It **holds those beliefs against conflict** — a new belief that contradicts an
  old one lowers the loser's confidence on evidence, and two of your directly
  stated beliefs in tension are surfaced for you, never silently reconciled.
- It **thinks before it speaks** on substantive turns — an internal monologue
  that shapes the reply but never appears in the transcript.
- It **earns convictions** — a candidate belief reaches the assistant's
  self-file only after it read its cited evidence, beat a deterministic
  regression floor, and survived an independent judge.

The design invariant across all of it: the self is built from the operator's
real experience, not from the assistant restating its own prose; mutation of
durable identity is default-denied and audited; and every faculty fails open —
a cognitive failure degrades to an ordinary reply, never a broken turn.

The headline difference between this and an articulate mimic is a single
sentence the assistant can now justify: *"I hold this because it was tested and
it held."*

## 2. The Loop — What Fires When

The cognitive system runs on three clocks: per-turn (the hot path), scheduled
(daily and weekly cron), and on-demand (operator- or Archon-triggered).

| When | What runs | Effect on the self |
|---|---|---|
| **Per turn** | Proactive recall (gated on message length); the gated cognitive pass (substantive/planning turns only); the session-opening brief (first turn after an absence) | The assistant retrieves relevant memory, optionally thinks before replying, and may open with "while you were out" |
| **Daily (morning reflection)** | Operator-belief extraction (Act 1); the contradiction pass (Act 2); inference decay; the next session brief is staged | New beliefs are formed from the day's operator turns, conflicts are judged, stale beliefs age out |
| **Weekly synthesis** | Seven-day log synthesis; durable-memory amendments applied (capped per run) | The week is summarized; approved amendments land through the audited ledger |
| **Dream cycle** | A four-phase consolidation (orient → gather signal → consolidate → prune) that fires only when a signal threshold is met | Cross-session signal is merged; stale entries pruned; the index rebuilt |
| **Evolve loop (scheduled)** | The safe recall-tuning `propose` — replay over a candidate config, compare, evaluate the regression corpus, write a decision artifact | Proves the test-and-keep machinery on safe candidates; no identity mutation |
| **Evolve identity rail (on-demand)** | The Archon-driven `propose-belief` — evidence-read + regression floor + LLM judge, then the audited amendment gate | A candidate self-belief is adopted ONLY if it earned it |

The key integration point is the morning reflection: it is where the assistant
reads the day's verbatim operator turns, extracts beliefs, and then runs the
contradiction pass over the accumulated belief set. Both are zero-cost until the
reflection's own model call runs, and both fail open — a failure in either step
never breaks the reflection.

## 3. Forming A Self (operator beliefs)

**The problem this solves.** The earliest self-model captured beliefs by
scanning every message for preference keywords — including the assistant's own
replies. The result was a self-model made of the assistant quoting its own
boilerplate back to itself as if those were the operator's beliefs.

**How it works now.** During the morning reflection, the assistant reads the
operator's verbatim turns (the messages where the role is the operator, pulled
from the session store) over the reflection window, and runs a real
claim-extraction step — a scheduled model call, never a per-turn hot-path call —
that turns the operator's actual words into clean preference claims. Each claim
is written as an inference record tagged with a real provenance source
(`reflection` for synthesized beliefs, `explicit` for directly stated ones).

**Convergence.** Beliefs are de-duplicated by embedding cosine similarity, so
"keep it short" and "be concise" recognize each other as the same belief and
reinforce one record instead of fragmenting into two. A belief seen enough
distinct times graduates to a confirmed conviction.

**The one-time cleanup.** A vault that ran the old keyword capture carries
poisoned records. A reversible migration quarantines the entire keyword-captured
provenance class to a backup file, leaving the genuinely-sourced beliefs intact.
The live prompt renders only the trustworthy sources, so even before the
migration runs, the poisoned records never reach the assistant's reasoning.

The result is a self-model whose "beliefs about the operator" are actually about
the operator.

## 4. Holding A Belief Against Conflict (the contradiction engine)

A belief you cannot hold against conflicting evidence is not a conviction. The
contradiction engine makes a belief disconfirmable.

**How a conflict is found.** During the morning reflection, the assistant embeds
its operator-belief set, cheaply pre-filters topically-related pairs by cosine
band (similar enough to possibly conflict, distinct enough to not already be the
same belief), and sends only those candidate pairs to a real LLM judge that
decides whether two beliefs genuinely contradict — they cannot both be true —
rather than merely differ. This is batched in the scheduled loop, never the hot
path.

**Resolution policy, with the operator protected.** When a genuine contradiction
is found, the loser's confidence drops on the evidence and the conflict is
recorded in an audit field on the record. Two protections are load-bearing:

- **Explicit operator-stated beliefs are sacrosanct.** A model judgment can
  never lower a belief the operator stated directly. By construction the
  confidence-dropping loser is always an inferred belief; when two directly
  stated beliefs conflict, both are held and surfaced to the operator, never
  silently reconciled.
- **A conflict counts once.** A static contradiction re-judged every night is a
  no-op — the belief drops once and then holds. Only a genuinely new
  contradicting belief moves the needle again. The self adjusts on evidence, not
  on repetition.

A contradicted-but-surviving belief is rendered as "held under tension" so the
operator can see beliefs in conflict rather than having them quietly resolved.

**Resolving at write (opt-in, default off).** By default a conflict is only
caught by the nightly pass, so a belief written at the start of a reflection that
contradicts an existing one sits mis-stated until the next run. An optional
write-time step closes that gap: when a newly-written belief lands topically near
an existing one, it is resolved immediately at write — reusing the exact same
judge and protections (explicit-sacrosanct, count-once). It is gated by
`INFERENCE_WRITE_TIME_CONTRADICTION` and ships **off**; with the knob off the
written belief set is identical to before and the judge is never called. The
nightly pass remains the backstop either way, and `CONTRADICTION_ENABLED=false`
is a second kill switch that disables the write-time step too. When the step does
resolve a conflict, the morning reflection reports a `write-time contradictions
applied: N` line so the operator can see it happened.

## 5. Thinking Before Speaking (the gated cognitive pass)

On a substantive turn the assistant runs an internal monologue before it
composes the reply — it thinks, then it speaks once.

**Gated, so it stays cheap.** The pass fires only on substantive/planning turns
above a message-length floor; trivial turns ("thanks", "show me the leads") stay
a single model call with no extra cost. A planning turn adds exactly one
monologue call. The monologue runs on a cheap, fast model tier and over a
budget-bounded slice of context, so it is affordable on the live turn.

**History purity.** The monologue lives only as an internal-region memory used
to build the prompt for that one turn. It never enters the persisted transcript
and never becomes the user-facing reply — what is saved and shown is the
operator's text and the assistant's answer, never the private thinking.

**Self-initiated proposals, default-denied.** The pass can propose an action —
for example, queue a notification for the operator. Queuing is allowed;
anything that would touch an external account is default-denied through the
integration capability gate with an audit row. The proposal mechanism is wired
and live, but it is notify-only until a dedicated write capability is enabled.

Every failure mode is observable and fails open: a timeout, a model failure, a
ran-but-empty monologue, and a closed gate are each a distinct trace outcome,
and any of them degrades to an ordinary reply.

## 6. Earning A Belief (the evolve-to-identity adoption gate)

This is the verb that separates a self from a mimic. Without it, a belief
reaches the assistant's self-file because the model asserted high confidence and
named some evidence the gate never opened. With it, a belief is earned.

**Three layers, in order of strength.** A candidate self-amendment is adopted
only if it passes all three:

1. **Evidence-READ gate (necessary).** Each cited evidence path is opened, read,
   confined to the memory vault, and bounded in size. A path that escapes the
   vault, does not exist, is a directory, is oversized, or is empty is treated as
   non-supporting and is never read or fed to the judge. This is the security
   boundary: a candidate cannot point the assistant at an arbitrary file.
2. **Deterministic regression floor.** A zero-model corpus of falsifiable checks
   — seeded with the system's own documented failure modes — must pass. The
   floor is the cheap, necessary pre-filter. (Its token-overlap check measures
   shared vocabulary, not genuine support; it is explicitly not sufficient on
   its own.)
3. **The LLM judge (sufficient).** A scheduled, circularity-guarded judge reads
   the candidate and its evidence and decides whether the evidence actually
   supports the claim. It never sees the prompt that produced the belief, so it
   cannot rubber-stamp the proposer. It runs in the scheduled/Archon loop, never
   the chat hot path, and fails closed — when the provider is unavailable, the
   judge declines and nothing is adopted.

A candidate must additionally satisfy its own falsifiable prediction (the
prediction is fed into the floor as a per-candidate check), so a belief that
fails the test it set for itself is rejected even when the judge approves.

**The adoption boundary is unchanged.** A winner routes through the existing
default-deny amendment gate — the same gate that enforces a confidence floor, a
secret/destructive content scan, a size cap, a rollback snapshot per write, and
an append-only audit ledger. The evolve work inserts the evidence-read + floor +
judge before that gate; it does not weaken it. A rejected candidate leaves no
partial write, and the decision artifact records the real outcome (adopt /
reject / error), so the loop never claims a belief was adopted when the gate
turned it away.

**Who drives the search.** The candidate-generation loop is an Archon workflow
(the coding-workflow layer); the assistant's cognition is the fitness oracle and
the store. This respects the framework's slice boundary: the search engine lives
in Archon, the test-and-keep machinery and the ledger live in the assistant.

## 7. Operator Runbook — commands and entry points

### Slash commands

| Command | What it does |
|---|---|
| `/working` | Show the cross-session scratchpad — open threads, hypotheses, unresolved questions, and the heartbeat-observation section. `/working add "<text>"` appends a thread; `/working resolve <N>` archives one. |
| `/file` | File the last analytical answer as a permanent vault note (entity compilation cascade). Subcommands accept or preview a filed draft. |
| `/clear` | Clear the session. Records a brief-owed marker so a `/status`-first morning does not eat the next session brief. |
| `/diagnostics` | Full system health, including the cognitive-loop subsystem status (recall, the amendment ledger, pending amendments, episodes). |
| `/reload` | Reload configuration and identity context after a knob change. |

### Scheduled and on-demand entry points

| Entry point | Trigger | What it does |
|---|---|---|
| Morning reflection (`memory_reflect.py`) | Daily, scheduled | Operator-belief extraction (Act 1), the contradiction pass (Act 2), inference decay, and staging the next session brief. `--test` dry-runs it with no writes. |
| Weekly synthesis (`memory_weekly.py`) | Weekly, scheduled | Seven-day synthesis; applies pending amendments (capped per run). |
| Dream cycle (`memory_dream.py`) | Interval + signal threshold | Four-phase consolidation; exits silently with no model call when no signal is found. |
| Evolve `propose` (`evolve/evolve_loop.py propose`) | Scheduled (via `run_evolve` + the scheduler setup script) | The safe recall-tuning rail — replay, compare, regression corpus, decision artifact. No identity mutation. |
| Evolve `propose-belief` (`evolve/evolve_loop.py propose-belief`) | Archon-driven, on-demand | The identity rail — evidence-read + floor + judge, then the audited amendment gate. |
| Corpus migration (`self_model.py migrate-corpus`) | One-time, manual | Quarantines keyword-captured self-model records to a reversible backup. |

The morning reflection is the integration seam: it is the single scheduled run
that forms beliefs and then tests them against each other.

## 8. Tuning — the configuration knobs

Every knob is resolved at call time from an environment variable through a
settings resolver (so a test or a live override takes effect without a code
change). Defaults below are the shipped values.

### Operator-belief extraction

| Env var | Default | Meaning |
|---|---|---|
| `INFERENCE_EXTRACTION_ENABLED` | `true` | Kill switch for the reflection-time belief extractor. |
| `INFERENCE_DEDUP_THRESHOLD` | `0.72` | Cosine floor for merging paraphrased beliefs into one record. |
| `INFERENCE_EXTRACTION_MAX_CLAIMS` | `8` | Cap on claims emitted per reflection run. |
| `INFERENCE_EXTRACTION_MIN_CHARS` | `12` | Floor on a single claim's text length. |
| `INFERENCE_WRITE_TIME_CONTRADICTION` | `false` | Opt-in. When on, a newly-written belief is resolved against an existing conflicting one immediately at write (reusing the nightly judge and protections) instead of waiting for the next pass. Off keeps the written belief set identical and never calls the judge. `CONTRADICTION_ENABLED=false` also disables it. |

### Contradiction pass

| Env var | Default | Meaning |
|---|---|---|
| `CONTRADICTION_ENABLED` | `true` | Kill switch for the whole pass. |
| `CONTRADICTION_PAIR_MIN_COSINE` | `0.45` | Lower bound for considering two beliefs related enough to possibly conflict. |
| `CONTRADICTION_PAIR_MAX_COSINE` | dedup threshold | Upper bound, coupled to the dedup threshold so already-merged beliefs don't re-surface. |
| `CONTRADICTION_MAX_PAIRS` | `20` | Cap on pairs sent to the judge per reflection. |
| `CONTRADICTION_MAX_ELIGIBLE` | `100` | Cap on eligible records before pairwise comparison. |
| `CONTRADICTION_MIN_RECORDS` | `2` | Floor — fewer than this many eligible records and the pass does not run. |
| `CONTRADICTION_ALLOW_EXPLICIT_VS_EXPLICIT` | `false` | When off (default), two directly stated beliefs never demote each other. |

### Cognitive pass

| Env var | Default | Meaning |
|---|---|---|
| `COGNITIVE_PASS_ENABLED` | `true` | Kill switch for the whole pass. |
| `COGNITIVE_PASS_FIRE_PROCESSES` | `planning` | Comma-separated process values that fire the monologue. |
| `COGNITIVE_PASS_MIN_CHARS` | `40` | Message-length floor below which even a substantive turn stays one call. |
| `COGNITIVE_PASS_MAX_ACTIONS_PER_TURN` | `1` | Rate limit on proactive actions queued per turn. |
| `COGNITIVE_PASS_TIMEOUT_S` | `5.0` | Hard wall on the monologue round-trip. |
| `COGNITIVE_PASS_MODEL` | `fast` | Model-tier hint — `fast` is the cheap default. |

### Belief evolution

| Env var | Default | Meaning |
|---|---|---|
| `EVOLVE_ENABLED` | `true` | Kill switch for both evolve subcommands. |
| `BELIEF_EVIDENCE_MIN_SUPPORTING_PATHS` | `1` | Minimum cited paths that must exist and be non-empty. |
| `BELIEF_EVIDENCE_MIN_OVERLAP` | `0.10` | Deterministic token-overlap floor (cheap pre-filter). |
| `BELIEF_EVIDENCE_MAX_BYTES` | `524288` | Read bound (512 KiB) — larger files are treated as non-supporting. |
| `BELIEF_JUDGE_MIN_CORRECTNESS` | `0.6` | Judge correctness floor for adoption. |
| `BELIEF_JUDGE_MIN_FIDELITY` | `0.6` | Judge evidence-fidelity floor for adoption. |

The Living Mind subfeatures (heartbeat blocker escalation, ambient observations,
episodes, the session brief) have their own knob tables on their feature pages.

## 9. Verifying The Self Is Alive

The cognitive state is inspectable. These are the operator's windows into
whether the self is actually forming, holding, and earning beliefs.

| Surface | What it tells you |
|---|---|
| `SELF.md` (vault root) | The self the assistant reads — its capabilities, patterns, and failure modes, updated by reflection and the amendment ledger. |
| `WORKING.md` (vault root) | Open threads, hypotheses, and the heartbeat-observation section — the live scratchpad surfaced by `/working`. |
| `MEMORY.md` (vault root) | Durable long-term memory the engine reads; amendments land here through the audited gate. |
| The self-model inference state (`.claude/data/state/`) | Per-belief records: confidence, evidence count, decay age, provenance source, and the contradiction audit field. |
| The amendment ledger | An append-only record of every proposed amendment and its outcome — applied, policy-rejected, or superseded — with rollback snapshots. |
| The episodes directory | The session autobiography (see the Episodes feature page). |
| The evolve decision artifacts (`.claude/data/evolve/`, belief decisions under `.claude/data/evolve/belief/`) | One artifact per evolve run recording the real adopt/reject/error outcome and the reasoning. |
| `/diagnostics` → cognitive loop | A live OK / WARNING / BLOCKED snapshot of the cognition subsystems. |
| The session-opening brief | The first interactive turn after an absence surfaces fresh changes — new amendments, new episodes, new contradictions. |

A practical check: after a morning reflection, the new beliefs and any flagged
contradictions appear in the self-model state; `/working` shows pending and
applied amendments; and the next session brief reports what changed.

## 10. Safety Boundaries

- **Default-deny mutation.** Any surface that could change durable identity or
  touch an external account is default-denied and only acts through an explicit,
  audited gate. The amendment ledger, the integration capability gate, and the
  evolve adoption gate are all instances.
- **Explicit beliefs are sacrosanct.** A model judgment can never lower a belief
  the operator stated directly.
- **The evidence gate is confined and bounded.** Cited evidence is read only
  from inside the memory vault, with a size cap; a path that escapes the vault
  (traversal, an absolute system path, a symlink pointing out) is rejected and
  never read or fed to the judge.
- **Hot-path purity.** The belief judge runs only in the scheduled/Archon loop —
  zero judge calls on a chat turn. The per-turn cognitive pass is gated and
  budget-bounded.
- **Fail open everywhere.** A failure in any faculty — extraction, contradiction,
  the monologue, the evidence read, the judge — degrades to an ordinary reply or
  a no-op, never a broken turn, and is logged with a distinct, visible message.
- **History purity.** Internal reasoning (the monologue) never enters the
  persisted transcript or the user-facing reply.

## 11. Common Failure Modes

| Symptom | What it means | What to do |
|---|---|---|
| The self-model is empty | No reflection has run yet, or every record was a poisoned keyword capture that the renderer filters out | Let a reflection run; run the corpus migration if upgrading from the keyword-capture era |
| No new beliefs after a reflection | The reflection's model call hit a provider quota wall before extraction ran | Quota exhaustion is environmental, not a defect; beliefs form once a provider has budget |
| A belief was rejected at adoption | It failed the evidence read, the regression floor, its own prediction, or the judge | Read the decision artifact — it records the real reason; an asserted-but-unsupported belief is correctly turned away |
| The monologue did not fire | The turn was trivial, below the length floor, or not a configured firing process | Expected — the gate keeps trivial turns at one call |
| `held under tension` on a belief | Two beliefs genuinely conflict and the loser survived above threshold, or two explicit beliefs conflict | Surfaced deliberately for the operator; resolve it by stating which holds |

## 12. File Ownership Map

| Concern | Location |
|---|---|
| Operator-belief extraction | `.claude/chat/cognition/operator_beliefs.py`, `self_model.py` |
| The contradiction engine | `.claude/chat/cognition/belief_conflicts.py` |
| The gated cognitive pass | `.claude/chat/cognition/cognitive_pass.py`, `processes.py`, `proactive_actions.py` |
| The evidence-read gate | `.claude/chat/cognition/evidence_gate.py` |
| The amendment ledger and policy gate | `.claude/chat/cognition/amendments.py` |
| The evolve test-and-keep engine | `.claude/scripts/evolve/` (`evolve_loop.py`, `judge.py`, `belief_regression.py`, plus the recall harness) |
| Scheduled reflection / weekly / dream | `.claude/scripts/memory_reflect.py`, `memory_weekly.py`, `memory_dream.py` |
| The configuration resolvers | `.claude/scripts/config.py` |
| The Living Mind substrate | the Heartbeat Runtime, Episodes, and Session Opening Brief feature pages |

The contradiction engine (`belief_conflicts.py`) is distinct from
`contradictions.py`, which is a documentation-vs-code drift linter — a different
concern.

## 13. Current Scope And Non-Goals

In scope and live: operator-belief formation, the contradiction engine, the
gated cognitive pass, and the earned-belief adoption gate, all wired into the
scheduled and per-turn cadence.

Deliberately not in this scope:

- **No self-mutation of core identity values.** The adoption gate forms and
  tests beliefs; it does not rewrite the assistant's constitution.
- **No autonomous external action.** Self-initiated proposals queue and notify;
  external dispatch remains default-denied behind the integration gate.
- **No belief judge on the chat hot path.** Judging is scheduled/Archon-only.
- **The live proofs land on their own clock.** The first formed belief, the
  first judged contradiction, and the first earned amendment fire the next time
  a provider has budget; the machinery is proven deterministically and waiting.

This is the architecture of an individuated self: it forms its own beliefs from
its own experience, defends them against contradiction, thinks privately before
it answers, and keeps only the convictions that survived a test.
