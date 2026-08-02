"""Steering-gate locks for the Archon workflows the Homie actually dispatches.

Issue #255 / PRD-archon-execution-spine F4: steering rides Archon's own
primitives, so "can the operator correct this run mid-flight?" reduces to "does
this workflow have an authored pause point?". These tests are the lock on that
answer for the dispatched set — they read the PHYSICAL yaml, not a registry or
a doc table, because a doc claiming a gate exists is exactly the derived state
that goes stale first.

Two gate flavours, and the difference is load-bearing (same ruling as the
client-site-deploy gate, Gotcha #1):

  * SPEND gate  — guards an irreversible spend. NO ``on_reject``: a reject must
    CANCEL the run, not rework it.
  * STEER gate  — guards a dead-end or a machine-authored artifact. Carries
    ``on_reject`` so a reject buys one corrective round and re-asks.

The other invariant these lock is the one that would fail SILENTLY: every gate
is ``when``-guarded onto a specific branch, and the node it guards reaches it
through the DAG. A gate that loses its guard starts pausing unattended campaign
runs; a node re-parented around its gate keeps running with no pause at all.
Neither shows up as an error — only as a workflow that quietly stopped meaning
what it said.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".archon" / "workflows"

# The dispatched set this ticket gates, and what each gate is for.
# (workflow, gate node id, guarded node id, flavour)
SPEND_GATES = [
    ("archon-clutch", "execute-gate", "execute"),
    ("image-node-factory", "render-gate", "render"),
    ("codex-image-asset-factory", "render-gate", "render"),
    ("client-site-factory", "image-render-gate", "image-render"),
    ("client-site-factory", "render-video-gate", "render-video"),
]
#: Gates gated on PUBLISHING rather than spending. Same hole, different verb,
#: and the widest blast radius in the repo: `vercel deploy --prod` publishes
#: the entire YourProduct-client project surface. Operator rule (2026-07-27):
#: preview deploys are free, production needs the tap.
PUBLISH_GATES = [
    ("client-site-deploy", "deploy-gate", "record-approval", "APPROVE DEPLOY"),
]
STEER_GATES = [
    ("archon-ralph-dag", "prd-gate", "implement"),
    ("epic-piv-ticket", "exhausted-gate", "exhausted-report"),
    ("epic-piv-ticket-codex", "exhausted-gate", "exhausted-report"),
]
ALL_GATES = SPEND_GATES + STEER_GATES

# Every workflow this ticket touched, including the steering smoke workflow.
GATED_WORKFLOWS = sorted({name for name, _, _ in ALL_GATES} | {"spike-echo-gated"})


def _load(workflow: str) -> dict:
    path = WORKFLOWS_DIR / f"{workflow}.yaml"
    assert path.is_file(), f"missing workflow yaml: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _nodes(workflow: str) -> dict[str, dict]:
    return {node["id"]: node for node in _load(workflow)["nodes"]}


def _reaches(nodes: dict[str, dict], start: str, goal: str) -> bool:
    """Walk depends_on edges backwards from ``start`` looking for ``goal``."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(nodes.get(current, {}).get("depends_on", []) or [])
    return False


@pytest.mark.parametrize(("workflow", "gate_id", "guarded_id"), ALL_GATES)
def test_gate_is_a_real_approval_node(workflow: str, gate_id: str, guarded_id: str) -> None:
    """The pause point exists and is an APPROVAL node with a non-empty message.

    Archon's schema makes approval/prompt/bash/command/loop mutually exclusive;
    catching a violation here costs a millisecond instead of a dispatched run
    that dies at load time.
    """
    node = _nodes(workflow)[gate_id]
    approval = node.get("approval")
    assert isinstance(approval, dict), f"{workflow}:{gate_id} is not an approval node"
    assert approval.get("message", "").strip(), f"{workflow}:{gate_id} has an empty message"
    assert approval.get("capture_response") is True, (
        f"{workflow}:{gate_id} must capture_response — the operator's words are the "
        "steering payload, not just an unblock signal"
    )
    exclusive = [k for k in ("prompt", "bash", "command", "loop", "loop_group", "script")
                 if k in node]
    assert exclusive == [], (
        f"{workflow}:{gate_id} carries {exclusive} alongside approval — Archon's "
        "dagNodeSchema rejects the workflow at load time"
    )


@pytest.mark.parametrize(("workflow", "gate_id", "guarded_id"), SPEND_GATES)
def test_spend_gate_carries_no_on_reject(workflow: str, gate_id: str, guarded_id: str) -> None:
    """Gotcha #1, engine-verified: a reject on a spend gate CANCELS the run.

    With ``on_reject`` the engine instead runs a rework prompt and re-pauses, so
    "no, do not build/render this" would silently become "try again" and the
    spend the operator refused happens on the next lap.
    """
    approval = _nodes(workflow)[gate_id]["approval"]
    assert "on_reject" not in approval, (
        f"{workflow}:{gate_id} is a spend gate — on_reject turns a refusal into a retry"
    )


@pytest.mark.parametrize(("workflow", "gate_id", "guarded_id"), STEER_GATES)
def test_steer_gate_reworks_against_the_rejection_reason(
    workflow: str, gate_id: str, guarded_id: str
) -> None:
    """A steer gate must actually consume the operator's direction.

    ``on_reject.prompt`` without ``$REJECTION_REASON`` runs a rework round that
    never reads what the operator asked for — the gate would pause, take input,
    and throw it away.
    """
    approval = _nodes(workflow)[gate_id]["approval"]
    on_reject = approval.get("on_reject")
    assert isinstance(on_reject, dict), f"{workflow}:{gate_id} must carry on_reject"
    assert "$REJECTION_REASON" in on_reject.get("prompt", ""), (
        f"{workflow}:{gate_id} on_reject ignores the operator's direction"
    )
    attempts = on_reject.get("max_attempts")
    assert isinstance(attempts, int) and 1 <= attempts <= 10, (
        f"{workflow}:{gate_id} needs a bounded max_attempts, got {attempts!r}"
    )


@pytest.mark.parametrize(("workflow", "gate_id", "guarded_id"), ALL_GATES)
def test_gate_guard_matches_the_node_it_guards(
    workflow: str, gate_id: str, guarded_id: str
) -> None:
    """The gate is scoped to the SAME branch as the node it protects.

    Losing the ``when`` is the silent failure that matters most: an unguarded
    exhausted-gate pauses every passing PIV campaign run, and an unguarded
    render-gate pauses pack-only image runs that spend nothing.
    """
    nodes = _nodes(workflow)
    gate_when = nodes[gate_id].get("when", "")
    guarded_when = nodes[guarded_id].get("when", "")
    assert gate_when, f"{workflow}:{gate_id} lost its `when` guard"
    assert gate_when == guarded_when or guarded_id == "implement", (
        f"{workflow}:{gate_id} guard {gate_when!r} diverged from {guarded_id} {guarded_when!r}"
    )


@pytest.mark.parametrize(("workflow", "gate_id", "guarded_id"), ALL_GATES)
def test_guarded_node_reaches_its_gate(workflow: str, gate_id: str, guarded_id: str) -> None:
    """Re-parenting around the gate must turn this red.

    The gate only has steering reach if the guarded node's dependency path runs
    THROUGH it. A node that keeps its `when` but drops the gate from its
    ancestry still executes — with no pause and no receipt.
    """
    nodes = _nodes(workflow)
    assert _reaches(nodes, guarded_id, gate_id), (
        f"{workflow}: {guarded_id} no longer depends (transitively) on {gate_id}"
    )


def test_ralph_implement_survives_a_skipped_prd_gate() -> None:
    """The ready-PRD path stays fully unattended.

    prd-gate is `when`-guarded off when the operator supplied their own
    prd.json, so it is SKIPPED — and under the default all_success trigger rule
    a skipped dependency would skip the entire ralph implementation loop. The
    workflow would report success having built nothing.
    """
    implement = _nodes("archon-ralph-dag")["implement"]
    assert "prd-gate" in implement["depends_on"]
    assert implement.get("trigger_rule") == "none_failed_min_one_success", (
        "implement must tolerate a skipped prd-gate or the already-ready path no-ops"
    )


def test_piv_happy_path_never_pauses() -> None:
    """A passing campaign run must reach its draft PR with zero human input.

    Both PIV lanes run unattended for hours. Their only authored pause sits on
    the gate_exhausted branch; draft-pr must not depend on it, transitively or
    otherwise.
    """
    for workflow in ("epic-piv-ticket", "epic-piv-ticket-codex"):
        nodes = _nodes(workflow)
        assert not _reaches(nodes, "draft-pr", "exhausted-gate"), (
            f"{workflow}: the happy path now runs through the exhausted gate"
        )
        assert nodes["exhausted-gate"]["when"] == "$gate-status.output.gates == 'gate_exhausted'"
        assert nodes["draft-pr"]["when"] == "$gate-status.output.gates == 'pass'"


def test_steering_smoke_workflow_echoes_the_captured_response() -> None:
    """spike-echo-gated is the live-proof harness; its proof must be real.

    after-gate has to echo the CAPTURED text (proving the resume carried the
    operator's words into the DAG, not merely unblocked it), and it has to do
    that through a QUOTED heredoc — the captured text is operator free text
    spliced into a bash body before the shell parses it.
    """
    nodes = _nodes("spike-echo-gated")
    assert nodes["steer-gate"]["approval"]["capture_response"] is True
    after = nodes["after-gate"]["bash"]
    assert "$steer-gate.output" in after, "after-gate must echo the captured response"
    delimiter = "STEER_EOF_9f13c2"
    assert f"<<'{delimiter}'" in after, (
        "the captured response must ride a QUOTED heredoc (no expansion, no "
        "command substitution)"
    )
    body = after.split(f"<<'{delimiter}'", 1)[1]
    assert delimiter in body.splitlines(), (
        "heredoc terminator must sit at column 0 (an indented terminator never "
        "closes the heredoc and the node dies on EOF)"
    )


@pytest.mark.parametrize(("workflow", "gate_id", "guarded_id"), ALL_GATES)
def test_gate_copy_is_plain_ascii(workflow: str, gate_id: str, guarded_id: str) -> None:
    """Operator-facing gate copy must survive Archon's rendering path.

    Observed live on run ``5a94695395d1``: an em-dash authored in the yaml
    reached the paused-run message as ``\\u00e2\\u20ac\\u201d`` (UTF-8 bytes
    decoded as cp1252). The gate prompt is the ONE message that asks the
    operator for a decision, so it stays plain ASCII. Scoped to the approval
    copy only -- surrounding yaml comments are developer-facing.
    """
    approval = _nodes(workflow)[gate_id]["approval"]
    copy = approval["message"] + (approval.get("on_reject", {}).get("prompt", ""))
    offenders = sorted({ch for ch in copy if ord(ch) > 127})
    assert offenders == [], (
        f"{workflow}:{gate_id} gate copy carries non-ASCII {offenders} -- it will "
        "reach the operator as mojibake"
    )


@pytest.mark.parametrize("workflow", GATED_WORKFLOWS)
def test_gated_workflow_parses_in_the_archon_engine(workflow: str) -> None:
    """Acceptance leg: Archon's OWN parser accepts the edited definition.

    POST /api/workflows/validate re-serializes the definition and runs the real
    ``parseWorkflow``, so this catches schema drift our structural asserts above
    cannot see. Skipped (not failed) when the local engine is down — the
    structural locks still run offline.
    """
    httpx = pytest.importorskip("httpx")
    base = os.environ.get("ARCHON_API_BASE_URL", "http://127.0.0.1:3090")
    definition = _load(workflow)
    try:
        response = httpx.post(
            f"{base}/api/workflows/validate",
            json={"definition": definition},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:  # engine not running on this box
        pytest.skip(f"Archon engine unreachable at {base}: {exc}")

    assert response.status_code == 200, f"{workflow}: HTTP {response.status_code} {response.text}"
    payload = response.json()
    assert payload.get("valid") is True, f"{workflow}: {payload.get('errors')}"


# A workflow name that collides with one of Archon's OWN bundled defaults never
# resolves the repo's override at dispatch time. Root-caused against
# `workflow-discovery.ts` in Archon's own source (`packages/workflows/src/
# workflow-discovery.ts`, the repo-scope override loop): when a project file's
# FILENAME already carries `source: 'bundled'` from step 1 (bundled defaults are
# loaded before repo discovery), the override loop's `source` stays 'bundled'
# and, empirically (queried live via `GET /api/workflows?cwd=...`), the CONTENT
# served is the bundled definition too -- not this repo's edited file. `archon-
# ralph-dag` is both a stock Archon workflow name and this repo's gated
# filename, so `prd-gate` is authored but NOT live under that name. See
# docs/manual/features/archon-steering-gates.md "Known traps" for the full
# node-by-node proof. This is a documented upstream Archon limitation, not
# something `.archon/workflows/` YAML can fix — this test locks the CURRENT
# (broken) behavior so a change in either direction (Archon fixes the
# precedence, or someone "fixes" this test without reading why) gets a loud
# signal instead of silently drifting from the manual's claim.
_BUNDLED_NAME_COLLISIONS = {"archon-ralph-dag"}


def _fetch_live_workflow(base: str, cwd: str, name: str):
    httpx = pytest.importorskip("httpx")
    try:
        response = httpx.get(f"{base}/api/workflows", params={"cwd": cwd}, timeout=20.0)
    except httpx.HTTPError as exc:
        pytest.skip(f"Archon engine unreachable at {base}: {exc}")
    if response.status_code != 200:
        pytest.skip(f"Archon engine rejected cwd {cwd!r}: HTTP {response.status_code}")
    for entry in response.json().get("workflows", []):
        if entry["workflow"].get("name") == name:
            return entry
    return None


@pytest.mark.parametrize(
    "workflow", [w for w in GATED_WORKFLOWS if w not in _BUNDLED_NAME_COLLISIONS]
)
def test_gated_workflow_resolves_as_project_in_live_registry(workflow: str) -> None:
    """The dispatched workflow must actually BE this repo's edited file.

    `POST /api/workflows/validate` (above) only proves the YAML is schema-valid
    in isolation -- it says nothing about what `run_archon` actually dispatches.
    This queries the same registry `run_archon` resolves against and asserts
    the live `source` is 'project', not 'bundled': a gate authored here but
    silently shadowed by an Archon bundled default of the same name (the
    `archon-ralph-dag` class of bug) is exactly the failure this misses without
    this check. Skipped when the engine is unreachable or `ARCHON_TEST_CWD`
    (a REGISTERED codebase path — `GET /api/workflows?cwd=` 400s on anything
    else) is not set, since that path is operator-machine-specific.
    """
    base = os.environ.get("ARCHON_API_BASE_URL", "http://127.0.0.1:3090")
    cwd = os.environ.get("ARCHON_TEST_CWD")
    if not cwd:
        pytest.skip("ARCHON_TEST_CWD not set — live registry resolution check is opt-in")
    entry = _fetch_live_workflow(base, cwd, workflow)
    assert entry is not None, f"{workflow}: not found in live registry for cwd={cwd!r}"
    assert entry.get("source") == "project", (
        f"{workflow}: live registry source is {entry.get('source')!r}, not 'project' -- "
        "run_archon would dispatch a different definition than this repo's edited file"
    )


def test_archon_ralph_dag_prd_gate_is_authored_but_not_live() -> None:
    """Regression lock for the known, root-caused archon-ralph-dag limitation.

    Confirmed 2026-07-27 by querying the live registry directly: the served
    `archon-ralph-dag` definition has `source: 'bundled'` and its `implement`
    node has neither `prd-gate` in `depends_on` nor the repo's `provider:
    codex` override -- it is byte-for-byte Archon's OWN bundled default, not
    this repo's file. If this ever starts passing (source flips to 'project'),
    that means Archon changed its bundled-name-collision precedence and
    `prd-gate` just went live for the first time -- update
    docs/manual/features/archon-steering-gates.md's "Known traps" section
    (drop the caveat) when it does. Skipped when unreachable/opt-out, same as
    the sibling live check.
    """
    base = os.environ.get("ARCHON_API_BASE_URL", "http://127.0.0.1:3090")
    cwd = os.environ.get("ARCHON_TEST_CWD")
    if not cwd:
        pytest.skip("ARCHON_TEST_CWD not set — live registry resolution check is opt-in")
    entry = _fetch_live_workflow(base, cwd, "archon-ralph-dag")
    assert entry is not None, f"archon-ralph-dag: not found in live registry for cwd={cwd!r}"
    assert entry.get("source") == "bundled", (
        "archon-ralph-dag now resolves as "
        f"{entry.get('source')!r} in the live registry, not the known 'bundled' "
        "shadow -- prd-gate may be live now; verify and update the manual's "
        "Known traps section instead of leaving it in this xfail-shaped lock"
    )
    implement = next(n for n in entry["workflow"]["nodes"] if n["id"] == "implement")
    assert "prd-gate" not in (implement.get("depends_on") or []), (
        "implement now depends on prd-gate in the live registry -- the gate is "
        "live; update the manual instead of leaving this locked as broken"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Spend-gate verbatim-phrase checks (codex R3 blocker: NL refusal approves)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("workflow", "gate_id", "guarded_id"), SPEND_GATES)
def test_spend_gate_has_verbatim_phrase_check(
    workflow: str, gate_id: str, guarded_id: str
) -> None:
    """Archon's NL-approval path approves on ANY non-slash reply, so "no"
    would resume a spend. Every spend gate must therefore be followed by a
    deterministic `<gate>-check` bash node that requires the exact phrase
    APPROVE SPEND in the captured reply, and the guarded node must depend on
    the CHECK (directly), never on the raw gate alone."""
    doc = _load(workflow)
    nodes = {n["id"]: n for n in doc["nodes"]}
    check_id = f"{gate_id}-check"
    assert check_id in nodes, f"{workflow}: missing {check_id}"
    check = nodes[check_id]
    assert gate_id in (check.get("depends_on") or []), (
        f"{workflow}: {check_id} must depend on {gate_id}"
    )
    body = check.get("bash", "")
    assert "APPROVE SPEND" in body, (
        f"{workflow}: {check_id} must enforce the exact phrase APPROVE SPEND"
    )
    assert "exit 1" in body, (
        f"{workflow}: {check_id} must FAIL the run on a non-matching reply"
    )
    guarded_deps = nodes[guarded_id].get("depends_on") or []
    assert check_id in guarded_deps, (
        f"{workflow}: {guarded_id} must depend on {check_id}"
    )
    assert gate_id not in guarded_deps, (
        f"{workflow}: {guarded_id} must not bypass the check by depending on "
        f"{gate_id} directly"
    )
    # The gate copy must state the contract to the operator.
    message = nodes[gate_id]["approval"]["message"]
    assert "APPROVE SPEND" in message, (
        f"{workflow}: {gate_id} message must name the exact phrase"
    )


@pytest.mark.parametrize(("workflow", "gate_id", "guarded_id", "phrase"), PUBLISH_GATES)
def test_publish_gate_has_verbatim_phrase_check(
    workflow: str, gate_id: str, guarded_id: str, phrase: str
) -> None:
    """Same lock as the spend gates, for gates that PUBLISH.

    `client-site-deploy` shipped with a bare approval node in front of
    `vercel deploy --prod`, so a bare "no" at the gate resumed the run and
    published every client slug in the target checkout. The verbatim phrase
    is what actually holds it.
    """
    doc = _load(workflow)
    nodes = {n["id"]: n for n in doc["nodes"]}
    check_id = f"{gate_id}-check"
    assert check_id in nodes, f"{workflow}: missing {check_id}"
    check = nodes[check_id]
    assert gate_id in (check.get("depends_on") or []), (
        f"{workflow}: {check_id} must depend on {gate_id}"
    )
    body = check.get("bash", "")
    assert phrase in body, (
        f"{workflow}: {check_id} must enforce the exact phrase {phrase}"
    )
    assert "exit 1" in body, (
        f"{workflow}: {check_id} must FAIL the run on a non-matching reply"
    )
    guarded_deps = nodes[guarded_id].get("depends_on") or []
    assert check_id in guarded_deps, (
        f"{workflow}: {guarded_id} must depend on {check_id}"
    )
    assert gate_id not in guarded_deps, (
        f"{workflow}: {guarded_id} must not bypass the check by depending on "
        f"{gate_id} directly"
    )
    message = nodes[gate_id]["approval"]["message"]
    assert phrase in message, (
        f"{workflow}: {gate_id} message must name the exact phrase {phrase}"
    )


def test_no_prod_deploy_node_is_reachable_without_the_phrase_check() -> None:
    """Physical sweep: every `--prod` deploy must sit behind a phrase check.

    Reads the DAG rather than trusting the table above, so a NEW production
    deploy node added to any workflow fails this test until it is gated.
    """
    for path in WORKFLOWS_DIR.glob("*.yaml"):
        doc = _load(path.stem)
        nodes = {n["id"]: n for n in doc.get("nodes") or []}
        checks = {
            nid
            for nid, node in nodes.items()
            if "APPROVE DEPLOY" in (node.get("bash") or "")
        }
        for nid, node in nodes.items():
            body = node.get("bash") or ""
            if "deploy --prod" not in body:
                continue
            seen, stack = set(), list(node.get("depends_on") or [])
            while stack:
                dep = stack.pop()
                if dep in seen:
                    continue
                seen.add(dep)
                stack.extend(nodes.get(dep, {}).get("depends_on") or [])
            assert seen & checks, (
                f"{path.stem}: node '{nid}' runs a --prod deploy with no "
                "APPROVE DEPLOY phrase check anywhere upstream"
            )


def test_codex_variant_test_gate_has_the_thirty_minute_timeout() -> None:
    """codex R3 major 3: epic-piv-ticket-codex omitted the sibling's 30-min
    test-gate timeout, restoring the known 120s-default kill that loses the
    entire implementation when a real suite runs long."""
    doc = _load("epic-piv-ticket-codex")
    nodes = {n["id"]: n for n in doc["nodes"]}
    assert nodes["test-gate"].get("timeout") == 1800000
