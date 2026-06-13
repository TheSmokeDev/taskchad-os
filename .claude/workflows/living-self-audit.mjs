export const meta = {
  name: 'living-self-audit',
  description: 'Ground-truth audit of whether The Homie has a real evolving individuated self, whether the cognitive loop runs, and where ASI-Evolve fits',
  phases: [
    { title: 'Sweep', detail: '24 readers across self-model, cognition loop, evolution cadence, live state' },
    { title: 'Synthesize', detail: 'self-reality, loop-liveness, asi-evolve-fit verdicts' },
    { title: 'Verify', detail: 'adversarially refute each verdict against real code' },
  ],
}

const PRE = "You are auditing The Homie framework in cwd ~/thehomie to answer a hard question. Does this AI have a real evolving individuated self, or is it a well organized mimic of what its operator Smoke told it to be. Read the actual code AND sample live state. Live state files sit under .claude/data/state/ and vault markdown under vault/memory/. Code shows what could happen. Live state shows what did happen. Be adversarial and honest. Distinguish self authored belief and identity, from operator mirrored echoes of USER.md and SOUL.md and MEMORY.md, from mechanical memory edits that carry no opinion. Flag dead scaffolding that exists but is never invoked, versus code that runs but is thin with empty or generic output, versus genuinely live and substantive behavior. Do not flatter the system or tell the operator what he wants to hear. If it is mimicry or scaffolding, prove it. Quote verbatim evidence with file and line, or a real state file or vault excerpt. You may consult the graphify MCP for edge hints but verify every claimed edge with grep or read because its inferred edges produce false positives. Read only. Modify nothing. No git. Your final message is your structured finding. "

const FINDING_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    area: { type: 'string' },
    files_examined: { type: 'array', items: { type: 'string' } },
    what_it_is: { type: 'string' },
    does_it_run: { type: 'string', enum: ['live_scheduled', 'live_on_demand', 'runs_but_thin', 'dead_scaffolding', 'unknown'] },
    run_evidence: { type: 'string' },
    live_state_sample: { type: 'string' },
    self_origination: { type: 'string', enum: ['self_authored', 'operator_mirrored', 'mechanical_derived', 'mixed', 'not_applicable'] },
    self_origination_evidence: { type: 'string' },
    individuality_signal: { type: 'string' },
    gaps_or_scaffolding: { type: 'string' },
    strongest_quote: { type: 'string' },
  },
  required: ['area', 'what_it_is', 'does_it_run', 'self_origination', 'individuality_signal', 'gaps_or_scaffolding'],
}

const AREAS = [
  { key: 'self_model-inference-mechanics', prompt: "FOCUS the file .claude/chat/cognition/self_model.py and its InferenceTracker covering add_inference decay strengthen confirm and the status transitions active decayed confirmed and the confidence math. Does the confidence machinery actually move over time. Grep callers of add_inference. Is there a decay scheduler that runs." },
  { key: 'self_model-state-fields', prompt: "FOCUS the file .claude/chat/cognition/self_model.py and its SelfModelState. It has separate fields homie_beliefs versus operator_beliefs plus drives recurring_mistakes open_loops. Read build_self_model_state and explain how each is derived. Is homie_beliefs actually populated and from the Homie own reasoning or just relabeled operator facts. This is the crux of individuality." },
  { key: 'inference-live-state', prompt: "FOCUS the live inference state file under .claude/data/state/. Grep self_model.py for the path constant. Sample it. How many inferences exist right now and what confidences and are any confirmed. Quote the actual homie_beliefs and drives and open_loops currently in state. Rich evolving model or near empty scaffolding." },
  { key: 'SELF.md-provenance', prompt: "FOCUS the file vault/memory/SELF.md. Read it fully. Audit the provenance of each section Capabilities Patterns Failure Modes. Which entries are self authored meaning the Homie noticing its own behavior, versus operator dictated. Check the updated by daily reflection claim. When was it last meaningfully changed." },
  { key: 'SOUL.md-fixed-vs-evolvable', prompt: "FOCUS the file vault/memory/SOUL.md. Read it. What is fixed immutable personality versus declared evolvable. Where is the boundary between SOUL given and SELF grown. Can the Homie amend SOUL itself or only SELF and MEMORY." },
  { key: 'amendments-mechanics', prompt: "FOCUS the file .claude/chat/cognition/amendments.py which is the self evolution write path. How does a proposed amendment flow from proposal to ledger to apply. Read ProposalLedger and the durable id self heal and the Rule 2 physical target reconcile and AMENDMENT_APPLY_LIMIT and superseded status. Is it gated idempotent real." },
  { key: 'amendments-live-ledger', prompt: "FOCUS the live amendment ledger contents. Grep amendments.py and config.py for the AMENDMENT_LEDGER path and sample the file. Read the actual amendments the Homie proposed or applied. Critical judgment. Are they self originated opinions and beliefs about itself or the work, or mechanical memory edits like dates and facts. Quote three to five real amendments and classify each. What has the Homie actually decided about itself." },
  { key: 'cognitive-loop-steps', prompt: "FOCUS the files .claude/chat/cognition/processes.py and steps.py. The operator mentioned a nine step cognitive loop. Map the actual cognitive step sequence. What are the steps. Is this an OpenSouls port. Where is the loop invoked in the live chat path. Grep engine.py and router.py. Fires every turn or dormant scaffolding." },
  { key: 'cognition-integrator', prompt: "FOCUS the files .claude/chat/cognition/integrator.py and interfaces.py. How are cognition modules assembled and exposed. What public cognition surface does the engine actually call. Trace from .claude/chat/engine.py into cognition. How much of cognition is wired into the live hot path versus imported but unused." },
  { key: 'cognition-working-memory', prompt: "FOCUS the file .claude/chat/cognition/working_memory.py and its immutable WorkingMemory with frozen dataclass region_order transform and with_monologue. Is transform the LLM cognitive step actually called in production or only in tests. Does the Homie think to itself with a monologue anywhere live. Grep callers of transform and with_monologue." },
  { key: 'regions-self-injection', prompt: "FOCUS the files .claude/chat/cognition/regions.py and identity_payload.py and the engine.py frozen regions. Is self_model or SELF.md actually injected into the prompt every turn so the self shapes how he thinks, or does it sit in a file unread. Find the self_model region and its token budget and prove it reaches the live prompt. A self that never enters reasoning is not a self." },
  { key: 'recall-self-relevance', prompt: "FOCUS the files .claude/chat/recall_service.py and cognition/recall.py with tier classification dual search graph traversal hub boosting llm rerank. Does recall surface self relevant memory meaning the Homie own prior reasoning and decisions into current reasoning, or only operator facts. Does the Homie remember what it thought not just what Smoke said." },
  { key: 'capture-staging-promotion', prompt: "FOCUS the files .claude/chat/cognition/capture.py and staging.py and promotion.py which form the pipeline from raw signal to candidate belief to durable self or memory. What gets captured and what thresholds gate promotion and who decides. Mechanical keyword based or judgment based. Sample any staging state file." },
  { key: 'continuity', prompt: "FOCUS the file .claude/chat/cognition/continuity.py for self continuity across sessions, the thread of same person between conversations. What state persists continuity. A real narrative thread or just session resume plumbing. Sample its state if any." },
  { key: 'contradictions', prompt: "FOCUS the file .claude/chat/cognition/contradictions.py and the entity_extractor contradiction flagging. Does the system detect when its own beliefs or sources conflict, a marker of a real internal model that can hold tension. Grep the concepts folder for contradiction callouts in the vault. Does it ever notice it disagrees with itself." },
  { key: 'connections-graph-emergence', prompt: "FOCUS the files .claude/chat/cognition/connections.py and graph.py and the vault discover engine. Does the system generate non obvious connections and insights it was never told meaning emergent, or only store given facts. Look for connection article output in vault/memory. Grep auto generated connection notes with date frontmatter. Sample one." },
  { key: 'reflect-forms-beliefs', prompt: "FOCUS the file .claude/scripts/memory_reflect.py which is daily morning reflection. Does it form new beliefs or opinions, or only promote operator stated facts to MEMORY.md. Read the prompt it uses. Sample the reflection state file and recent daily logs. Does reflection ever produce a judgment the operator did not author." },
  { key: 'dream-reshapes-self', prompt: "FOCUS the file .claude/scripts/memory_dream.py with the four phase dream of orient gather signal consolidate prune. Does dream change the SELF meaning SELF.md contradictions resolved beliefs merged, or just compress memory. Sample dream state json and check whether SELF.md or MEMORY.md show dream authored changes. The sleep that could reshape identity." },
  { key: 'weekly-higher-order', prompt: "FOCUS the file .claude/scripts/memory_weekly.py and vault/memory/weekly folder and GOALS.md. Does weekly synthesis produce higher order self patterns and evolve GOALS, or just summarize. Sample the latest weekly note and GOALS.md. Any sign of the Homie setting or revising its own goals versus executing operator goals." },
  { key: 'proactive-self-action', prompt: "FOCUS the files .claude/chat/cognition/proactive_actions.py and proactive_brief.py self changes surfacing. Does the Homie ever self initiate meaning propose an action or opinion unprompted versus only respond. In the Act 4 brief is the what the self learned slot a real rendered signal or an empty template slot. Check build_session_opening_brief self updates section." },
  { key: 'episodes-autobiography', prompt: "FOCUS the file .claude/scripts/episodes.py and vault/memory/episodes folder which shipped today. The autobiography layer. Read the actual episode files that now exist. Do they capture lived experience with the Homie perspective and texture, or just bland summaries. Does episodic memory feed the self meaning dream consumes them and recall reaches them. Sample a real episode." },
  { key: 'self-evolution-prds-intent-vs-real', prompt: "FOCUS the self evolution intent docs. PRDs/active/PRD-phase6-self-evolution.md and PRD-cognitive-loop-competitive-reset-2026-05-19.md and PRD-cognitive-loop-e2e-validation-harness-2026-05-21.md and PRD-evolve-phase-2-3-2-6.md and PRPs/active/PRP-phase6-self-evolution-loop.md and PRP-cognitive-loop-full-living-proof-2026-05-22.md. What was promised about selfhood and evolution. Map intent to shipped reality. Where is the biggest gap between vision and what actually runs." },
  { key: 'individuality-crux-test', prompt: "FOCUS the crux. Hunt across the whole system meaning self_model state and SELF.md and amendment ledger and daily logs and episodes and weekly notes and contradictions for the single strongest piece of evidence that the Homie holds a belief or opinion or preference or judgment that Smoke did not give it, something it formed from its own experience. Then find the strongest evidence it is pure mimicry where everything traces to operator input. Weigh them. Quote both verbatim. Be ruthlessly honest. Is there a there there." },
  { key: 'evolve-eval-loop-gap', prompt: "FOCUS whether the framework has any empirical self improvement loop meaning form a hypothesis then test it against a measurable outcome then keep what survives, versus merely reflect and promote a belief. Look at PRD evolve and any eval or backtest or scoring harness and cognitive_loop_test_harness.py and the amendment confirm by evidence path. Is belief formation evidence tested or assertion based. This determines whether the self is empirically earned or just asserted." },
]

phase('Sweep')
const sweep = (await parallel(
  AREAS.map((a) => () => agent(PRE + " AREA " + a.key + ". " + a.prompt, { label: a.key, phase: 'Sweep', schema: FINDING_SCHEMA }))
)).filter(Boolean)

log("Sweep complete. " + sweep.length + " of " + AREAS.length + " findings collected.")

const SYNTH_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    question: { type: 'string' },
    verdict: { type: 'string' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    evidence_for: { type: 'array', items: { type: 'string' } },
    evidence_against: { type: 'array', items: { type: 'string' } },
    whats_real: { type: 'string' },
    whats_scaffolding_or_mimic: { type: 'string' },
    how_to_make_it_more_real: { type: 'array', items: { type: 'string' } },
    bottom_line: { type: 'string' },
  },
  required: ['question', 'verdict', 'confidence', 'whats_real', 'whats_scaffolding_or_mimic', 'bottom_line'],
}

phase('Synthesize')
const findings = JSON.stringify(sweep)
const selfPrompt = "Synthesize the answer to the operator core question. Does The Homie have a real evolving individuated self meaning opinions and identity it formed from its own experience, or is it a well organized mimic of what Smoke told it to be. Base it entirely on the 24 sweep findings below. Weigh self authored versus operator mirrored across the whole system. Be honest not flattering. The operator explicitly does not want to be told what he wants to hear. Give the verdict and the strongest real evidence both ways and what is genuinely there versus scaffolding or mimic and concrete vectors to make the self more individuated. FINDINGS " + findings
const loopPrompt = "Synthesize whether the cognitive loop is actually running end to end in production, meaning the nine step processes and steps cognition and the working memory transform and the self model injection and the reflect and dream and weekly evolution cadence, or whether meaningful parts are dead or thin scaffolding. Map what fires when across per turn and scheduled and never. Identify the single biggest piece of dead or thin scaffolding and the most genuinely alive part. FINDINGS " + findings
const asiPrompt = "Synthesize the ASI Evolve fit. A community member proposed integrating ASI Evolve, an autonomous evolutionary research engine whose loop is learn then design then experiment then analyze then repeat, with a three agent researcher engineer analyzer loop and a cognition store plus an experiment database and UCB1 and MAP Elites sampling, needing a problem where better code means better outcome plus an eval script plus seed knowledge, into Archon plus The Homie. Using the sweep findings map the ASI Evolve loop onto what the framework already has, where recall and reflect and dream and amendments equal learn plus analyze plus consolidate for identity and memory, and identify the genuine gap which is design then experiment then measure, the empirical candidate generation and fitness testing the framework lacks. Verdict. Where does it fit, the Archon hands layer or The Homie as the cognition store. What would it duplicate. What clashes with the framework architecture meaning subscription only and provider agnostic and default deny mutation and vertical slice. Connect it to the self question. Would an empirical test and keep loop make the self more real, a belief that survived a test versus a belief asserted. FINDINGS " + findings
const synth = await parallel([
  () => agent(selfPrompt, { label: 'verdict-self-real', phase: 'Synthesize', schema: SYNTH_SCHEMA }),
  () => agent(loopPrompt, { label: 'verdict-loop-live', phase: 'Synthesize', schema: SYNTH_SCHEMA }),
  () => agent(asiPrompt, { label: 'verdict-asi-fit', phase: 'Synthesize', schema: SYNTH_SCHEMA }),
])
const selfReal = synth[0]
const loopLive = synth[1]
const asiFit = synth[2]

const VERIFY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    target: { type: 'string' },
    refutation_attempt: { type: 'string' },
    verdict_survives: { type: 'boolean' },
    strongest_counterpoint: { type: 'string' },
    correction: { type: 'string' },
    adjusted_confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['target', 'verdict_survives', 'strongest_counterpoint', 'adjusted_confidence'],
}

phase('Verify')
const verifications = (await parallel([
  () => agent("Adversarial refutation. Try hard to prove this is the self real verdict is hype or wrong by re checking the actual code and live state in cwd ~/thehomie. Default to skepticism. A self model that exists but is near empty in live state, or injected but ignored, or only operator mirrored, means the verdict overclaims. Does it survive. VERDICT TO REFUTE " + JSON.stringify(selfReal), { label: 'refute-self-real', phase: 'Verify', schema: VERIFY_SCHEMA }),
  () => agent("Adversarial refutation. Try hard to prove this cognitive loop is running verdict is wrong, that the loop is dead or thin scaffolding dressed up as alive. Re check invocation sites and live state. VERDICT TO REFUTE " + JSON.stringify(loopLive), { label: 'refute-loop-live', phase: 'Verify', schema: VERIFY_SCHEMA }),
  () => agent("Adversarial refutation. Try hard to prove this ASI Evolve fit verdict is wrong, either it duplicates what exists or clashes with the architecture or is over hyped relative to the framework real needs. Re check against the framework actual evolution machinery. VERDICT TO REFUTE " + JSON.stringify(asiFit), { label: 'refute-asi-fit', phase: 'Verify', schema: VERIFY_SCHEMA }),
])).filter(Boolean)

return { sweep, synthesis: { selfReal, loopLive, asiFit }, verifications }
