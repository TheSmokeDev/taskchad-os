"""Resumable recall tuning on the existing persona learning journal and queue.

Admission is cheap and provider-free. Each worker stage evaluates one frozen
policy/split; per-case receipts survive cancellation and provider deferral.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime

from evolve import policy, relevance
from personas.learning.errors import LearningDeferredError, LearningUnavailableError
from personas.learning.models import LearningError, content_hash


class _StaleCorpusError(LearningError):
    """A frozen evidence revision is no longer available for this experiment."""


def _now(now=None):
    value = now or datetime.now(UTC)
    if isinstance(value, (float, int)):
        value = datetime.fromtimestamp(value, UTC)
    if value.tzinfo is None:
        raise LearningError("tuning time requires timezone")
    return value.astimezone(UTC)


def import_validated_cases(service, cases, *, source_key):
    """Admit independently prepared operator judgments after physical validation."""
    service._require_enabled()
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise LearningError("import requires one through one thousand cases")
    if not isinstance(source_key, str) or not source_key.strip():
        raise LearningError("case import needs a stable source key")
    validated = [relevance.validate_case(case, service.target.memory_dir) for case in cases]
    with service.store.atomic():
        families = {}
        for case in [*service.store.all("tuning_case"), *validated]:
            source = case["source"]["path"]
            if source in families and families[source] != case["source_family"]:
                raise LearningError("source-family leakage across case revisions")
            families[source] = case["source_family"]
        current = {case["case_key"]: case for case in reversed(service.store.all("tuning_case"))}
        for case in validated:
            current[case["case_key"]] = case
        relevance.split_cases(list(current.values()))  # catches cross-import leakage
        records = []
        for case in validated:
            revision = content_hash(case)
            # Import provenance is an event, so reimporting identical reviewed
            # material from another bundle does not create independent cases.
            record = service.store.put(
                "tuning_case",
                case | {"status": "validated", "revision": revision},
                key=f"{case['case_key']}:{revision}",
            )
            service.store.event(
                record["id"], "tuning_import", {"source": source_key}, key=f"import:{source_key}"
            )
            records.append(record)
    return records


def _cases(service):
    latest = {}
    for item in service.store.all("tuning_case"):
        latest.setdefault(item["case_key"], item)
    return sorted(
        (item for item in latest.values() if item.get("status") == "validated"),
        key=lambda item: item["case_key"],
    )


def _evaluation_results(service, evaluation):
    values = {
        event["payload"]["result"]["case_id"]: event["payload"]["result"]
        for event in service.store.events(evaluation["id"])
        if event["event_type"] == "tuning_case_result"
    }
    return [values[key] for key in evaluation["case_ids"] if key in values]


def _evaluation(service, run_id, role):
    return next(
        (
            record
            for record in service.store.all("tuning_evaluation")
            if record["run_id"] == run_id and record["role"] == role
        ),
        None,
    )


def _selected(service, run):
    return next(
        (
            event["payload"]
            for event in service.store.events(run["id"])
            if event["event_type"] == "tuning_selection"
        ),
        None,
    )


def tuning_status(service):
    cases = _cases(service)
    reason = "insufficient_validated_cases"
    try:
        development, heldout = relevance.split_cases(cases)
    except LearningError as exc:
        development, heldout = [], []
        reason = str(exc)
    if len(cases) >= relevance.MIN_CASES and not heldout:
        reason = "insufficient_independent_source_families"
    ready = bool(development and heldout)
    if ready:
        reason = "validated_corpus_ready"
    runs = service.store.all("tuning_run")
    evaluations = service.store.all("tuning_evaluation")
    latest_evaluation = evaluations[0] if evaluations else None
    for evaluation in evaluations:
        comparison = next(
            (
                event["payload"]
                for event in service.store.events(evaluation["id"])
                if event["event_type"] == "tuning_comparison"
            ),
            None,
        )
        if comparison:
            latest_evaluation = evaluation | {"comparison": comparison}
            break
    active = policy.active_policy(service)
    enabled = service.enabled() and os.getenv("EVOLVE_ENABLED", "true").lower() == "true"
    readiness = "ready" if ready else "not_ready"
    if not enabled:
        readiness, reason = "disabled", "paused_or_disabled_by_operator"
    elif (
        ready
        and runs
        and runs[0].get("case_fingerprint") == content_hash([case["id"] for case in cases])
    ):
        if runs[0]["status"] in {"adopted", "no_change"}:
            readiness, reason = "no_new_material", "awaiting_new_validated_material"
        else:
            readiness, reason = "in_progress", "resumable_batch_pending"
    return {
        "persona_id": service.target.persona_id,
        "min_cases": relevance.MIN_CASES,
        "validated_cases": len(cases),
        "development_cases": len(development),
        "heldout_cases": len(heldout),
        "readiness": readiness,
        "reason": reason,
        "latest_run": runs[0] if runs else None,
        "active_policy": active,
        "policies": service.store.all("tuning_policy"),
        "latest_evaluation": latest_evaluation,
    }


def _admit_run(service, now):
    """Serialize daily admission and bind corpus + policy + operator pins."""
    cases = _cases(service)
    if not cases:
        return None  # observing an empty profile must not create a database
    development, heldout = relevance.split_cases(cases)
    fingerprint = content_hash([case["id"] for case in cases])
    day = now.date().isoformat()
    with service.store.atomic():
        runs = service.store.all("tuning_run")
        existing = next((run for run in runs if run["day"] == day), None)
        if existing:
            return existing
        if any(run.get("case_fingerprint") == fingerprint for run in runs):
            return None
        if not development or not heldout:
            return None
        corpus = service.store.put(
            "tuning_corpus",
            {
                "status": "frozen",
                "case_ids": [c["id"] for c in cases],
                "development_ids": [c["id"] for c in development],
                "heldout_ids": [c["id"] for c in heldout],
                "case_fingerprint": fingerprint,
                "split_rule": "source_family_sha256_v1",
                "metric": "ndcg",
            },
            key=fingerprint,
        )
        base = policy.effective_values(service)
        pins = policy.operator_pins(service.target)
        active = policy.active_policy(service)
        return service.store.put(
            "tuning_run",
            {
                "status": "queued",
                "day": day,
                "corpus_id": corpus["id"],
                "case_fingerprint": fingerprint,
                "baseline": base,
                "operator_pins": pins,
                "predecessor_id": active["id"] if active else None,
                "neighbors": policy.neighbors(base, pins=pins),
            },
            key=day,
        )


def discover_tuning_work(service, now=None):
    """Quick queue discovery only, called by the shared foreground-aware worker."""
    if not service.enabled() or os.getenv("EVOLVE_ENABLED", "true").lower() != "true":
        return 0
    from personas.learning.queue import LearningQueue

    queue = LearningQueue(service)
    before = len(queue.list(include_finished=True))
    now = _now(now)
    active = policy.active_policy(service)
    if active:
        day = now.date().isoformat()
        queue.enqueue(
            "tuning_regression",
            f"{active['id']}:{day}",
            payload={"policy_id": active["id"], "day": day},
            now=now.timestamp(),
        )
    run = _admit_run(service, now)
    if run and run["status"] not in {"adopted", "no_change"}:
        queue.enqueue("tuning", run["id"], payload={"run_id": run["id"]}, now=now.timestamp())
    # Deferred work survives midnight even when new data is absent.
    for pending in service.store.all("tuning_run"):
        if pending["status"] in {"queued", "running", "deferred"}:
            queue.enqueue(
                "tuning", pending["id"], payload={"run_id": pending["id"]}, now=now.timestamp()
            )
    return len(queue.list(include_finished=True)) - before


async def tune(service, *, now=None):
    """Operator/HTTP entrypoint: queue existing-worker work, never inline inference."""
    created = discover_tuning_work(service, now=now)
    return tuning_status(service) | {"queued_jobs": created}


async def replay_case(case, values, target):
    """Use ordinary recall with a task-local scoring override and frozen budget."""
    from cognition.recall import format_recall_results
    from recall_service import SearchMode, recall

    budget = case["request_budget"]
    started = time.monotonic()
    with policy.policy_scope(target.memory_dir, values, replay=True):
        response = await recall(
            case["query"],
            memory_dir=target.memory_dir,
            search_mode=SearchMode(budget["search_mode"]),
            max_results=budget["max_results"],
            caller="tuning",
        )
        failures = policy.replay_failures()
        model_calls = policy.replay_model_calls()
    if any(item["unavailable"] for item in failures):
        raise LearningUnavailableError("recall dependency unavailable; retry the same frozen case")
    results = list(response.results)
    # The same fixed K and whole-item context budget apply to every policy.
    while results and len(format_recall_results(results)) > budget["context_chars"]:
        results.pop()
    formatted = format_recall_results(results) if results else ""
    return {
        "paths": [str(item.path) for item in results],
        "latency_ms": (time.monotonic() - started) * 1000,
        "context_chars": len(formatted),
        "error": bool(failures),
        "error_count": len(failures),
        "model_calls": model_calls,
    }


def _boundary(service):
    service._require_enabled()
    if os.getenv("EVOLVE_ENABLED", "true").lower() != "true":
        raise LearningDeferredError("recall tuning is disabled by the operator")
    from personas.learning.worker import _check_stage_boundary

    _check_stage_boundary()


def _runtime_contract():
    """Freeze non-tunable environment at the first worker-side replay."""
    import config

    values = {
        key: getattr(config, key)
        for key in (
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSIONS",
            "RECALL_RERANK_ENABLED",
            "RECALL_RERANK_TOP_N",
            "RECALL_RERANK_TIMEOUT_S",
            "RECALL_MAX_RESULTS",
        )
    }
    values["backend_fingerprint"] = hashlib.sha256(
        str(config.DATABASE_URL or config.DATABASE_PATH).encode()
    ).hexdigest()
    values["model_settings"] = {
        key: os.environ.get(key)
        for key in (
            "SECOND_BRAIN_RUNTIME_LANE",
            "SECOND_BRAIN_RUNTIME_PROVIDER",
            "SECOND_BRAIN_MODEL",
            "SECOND_BRAIN_CLAUDE_MODEL",
            "SECOND_BRAIN_CODEX_MODEL",
            "SECOND_BRAIN_GEMINI_MODEL",
        )
    }
    return values


def _contract_matches(service, run):
    contract = _runtime_contract()
    prior = next(
        (
            event["payload"]
            for event in service.store.events(run["id"])
            if event["event_type"] == "tuning_runtime_contract"
        ),
        None,
    )
    if prior is None:
        service.store.event(run["id"], "tuning_runtime_contract", contract, key="runtime_contract")
        return True
    return prior == contract


async def _evaluate(service, run, role, case_ids, values, *, runner=None):
    runner = runner or replay_case
    evaluation = service.store.put(
        "tuning_evaluation",
        {
            "run_id": run["id"],
            "role": role,
            "status": "running",
            "case_ids": case_ids,
            "values": values,
            "corpus_id": run["corpus_id"],
        },
        key=f"{run['id']}:{role}",
    )
    done = {result["case_id"] for result in _evaluation_results(service, evaluation)}
    for case_id in case_ids:
        _boundary(service)
        if case_id in done:
            continue
        case = service._owned(case_id, "tuning_case")
        # Frozen labels remain auditable if a source later changes. A changed
        # corpus cannot silently supply a different relevance experiment.
        for evidence in [case["source"], *[label["evidence"] for label in case["labels"]]]:
            try:
                relevance._evidence(service.target.memory_dir, evidence, "frozen case")
            except (LearningError, FileNotFoundError, UnicodeDecodeError) as exc:
                service.store.event(
                    case_id,
                    "status",
                    {"status": "invalidated", "reason": "source_revision_changed"},
                    key="source_revision_changed",
                )
                raise _StaleCorpusError(
                    "validated source revision changed; revalidate the corpus"
                ) from exc
        try:
            raw = await runner(case, values, service.target)
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise LearningUnavailableError("recall evaluation infrastructure unavailable") from exc
        result = relevance.score_result(case, raw, service.target.memory_dir)
        service.store.event(
            evaluation["id"], "tuning_case_result", {"result": result}, key=f"case:{case_id}"
        )
    service.store.event(evaluation["id"], "status", {"status": "completed"}, key="completed")
    return service.store.get(evaluation["id"])


def _finish(service, run, reason, comparison=None):
    service.store.event(
        run["id"], "tuning_decision", {"reason": reason, "comparison": comparison}, key="decision"
    )
    service.store.event(
        run["id"], "status", {"status": "no_change", "reason": reason}, key="terminal"
    )


def _activate(service, run, evaluation, baseline_evaluation, comparison):
    with service.store.atomic():
        _boundary(service)
        current = policy.active_policy(service)
        if (current["id"] if current else None) != run["predecessor_id"] or policy.operator_pins(
            service.target
        ) != run["operator_pins"]:
            _finish(service, run, "policy_or_operator_pins_changed")
            return None
        if current is None:
            baseline_development = _evaluation(service, run["id"], "baseline_development")
            current = service.store.put(
                "tuning_policy",
                {
                    "status": "baseline",
                    "values": run["baseline"],
                    "predecessor_id": None,
                    "corpus_id": run["corpus_id"],
                    "baseline_evaluation_id": baseline_evaluation["id"],
                    "evaluation_id": baseline_evaluation["id"],
                    "regression_evaluation_ids": [
                        baseline_development["id"],
                        baseline_evaluation["id"],
                    ],
                    "run_id": run["id"],
                    "operator_pins": run["operator_pins"],
                },
                key=f"baseline:{run['id']}",
            )
        selection = _selected(service, run)
        development = _evaluation(service, run["id"], selection["development_role"])
        accepted = service.store.put(
            "tuning_policy",
            {
                "status": "active",
                "values": evaluation["values"],
                "baseline_values": run["baseline"],
                "predecessor_id": current["id"],
                "corpus_id": run["corpus_id"],
                "baseline_evaluation_id": baseline_evaluation["id"],
                "evaluation_id": evaluation["id"],
                "regression_evaluation_ids": [development["id"], evaluation["id"]],
                "run_id": run["id"],
                "comparison": comparison,
                "operator_pins": run["operator_pins"],
            },
            key=run["id"],
        )
        if current:
            service.store.event(
                current["id"],
                "status",
                {"status": "superseded", "successor_id": accepted["id"]},
                key=f"superseded:{accepted['id']}",
            )
        service.store.set_setting("recall_policy_id", accepted["id"])
        service.store.event(
            accepted["id"],
            "tuning_activation",
            {"run_id": run["id"], "evaluation_id": evaluation["id"]},
            key="activated",
        )
        service.store.event(
            run["id"],
            "tuning_decision",
            {"reason": "accepted", "comparison": comparison, "policy_id": accepted["id"]},
            key="decision",
        )
        service.store.event(run["id"], "status", {"status": "adopted"}, key="terminal")
        return accepted


def rollback_policy(service, *, reason="operator request", expected_policy_id=None):
    with service.store.atomic():
        current = policy.active_policy(service)
        if current is None or (expected_policy_id and current["id"] != expected_policy_id):
            return {"status": "no_change", "reason": "no_matching_active_policy"}
        predecessor_id = current["predecessor_id"]
        if predecessor_id:
            predecessor = service._owned(predecessor_id, "tuning_policy")
            if predecessor.get("status") == "rolled_back":
                raise LearningError("cannot restore a previously failed recall policy")
            service.store.event(
                predecessor_id,
                "status",
                {"status": "active", "restored_from": current["id"]},
                key=f"restore:{current['id']}",
            )
        service.store.event(
            current["id"],
            "rollback",
            {"reason": reason, "predecessor_id": predecessor_id},
            key="rollback",
        )
        service.store.set_setting("recall_policy_id", predecessor_id)
        return {
            "status": "rolled_back",
            "policy_id": current["id"],
            "restored_policy_id": predecessor_id,
            "reason": reason,
        }


async def monitor_active_policy(service, *, now=None, runner=None, policy_id=None):
    current = policy.active_policy(service)
    if not current or (policy_id and current["id"] != policy_id):
        return {"status": "no_change", "reason": "policy_not_active"}
    if policy.operator_pins(service.target) != current["operator_pins"]:
        return {"status": "skipped", "reason": "operator_pins_changed"}
    baselines = [
        service._owned(identifier, "tuning_evaluation")
        for identifier in current.get("regression_evaluation_ids", [current["evaluation_id"]])
    ]
    case_ids = [identifier for baseline in baselines for identifier in baseline["case_ids"]]
    run = service._owned(current["run_id"], "tuning_run")
    if not _contract_matches(service, run):
        return {"status": "skipped", "reason": "runtime_contract_changed"}
    role = f"monitor:{current['id']}:{_now(now).date().isoformat()}"
    monitored = await _evaluate(service, run, role, case_ids, current["values"], runner=runner)
    comparison = relevance.compare(
        [result for baseline in baselines for result in _evaluation_results(service, baseline)],
        _evaluation_results(service, monitored),
        require_gain=False,
    )
    service.store.event(monitored["id"], "tuning_comparison", comparison, key="comparison")
    if not comparison["accepted"]:
        return rollback_policy(
            service, reason="frozen_regression_failed", expected_policy_id=current["id"]
        )
    return {"status": "passed", "policy_id": current["id"], "comparison": comparison}


async def process_tuning_stage(service, job, *, runner=None):
    """Run a durable role and terminate stale corpora with an honest no-change."""
    try:
        return await _process_tuning_stage(service, job, runner=runner)
    except _StaleCorpusError:
        payload = dict(job["payload"])
        if job["kind"] == "tuning":
            run = service._owned(payload["run_id"], "tuning_run")
            _finish(service, run, "source_revision_changed_revalidation_required")
        else:
            service.store.event(
                payload["policy_id"],
                "tuning_monitor_skipped",
                {"day": payload["day"], "reason": "source_revision_changed_revalidation_required"},
                key=f"monitor_skipped:{payload['day']}",
            )
        return "done", payload | {"reason": "source_revision_changed_revalidation_required"}


async def _process_tuning_stage(service, job, *, runner=None):
    """One bounded durable role; called under the shared worker's lease/guard."""
    _boundary(service)
    payload = dict(job["payload"])
    if job["kind"] == "tuning_regression":
        result = await monitor_active_policy(
            service,
            now=datetime.fromisoformat(payload["day"]).replace(tzinfo=UTC),
            runner=runner,
            policy_id=payload["policy_id"],
        )
        return "done", payload | {"result": result}
    run = service._owned(payload["run_id"], "tuning_run")
    if run["status"] in {"adopted", "no_change"}:
        return "done", payload
    if not _contract_matches(service, run):
        _finish(service, run, "runtime_contract_changed")
        return "done", payload
    if policy.operator_pins(service.target) != run["operator_pins"]:
        _finish(service, run, "operator_pins_changed")
        return "done", payload
    corpus = service._owned(run["corpus_id"], "tuning_corpus")
    if set(corpus["development_ids"]) & set(corpus["heldout_ids"]):
        raise LearningError("heldout leakage in frozen tuning corpus")
    roles = [
        ("baseline_development", run["baseline"]),
        *[(f"neighbor_{index}", values) for index, values in enumerate(run["neighbors"])],
    ]
    for role, values in roles:
        evaluation = _evaluation(service, run["id"], role)
        if not evaluation or evaluation["status"] != "completed":
            await _evaluate(service, run, role, corpus["development_ids"], values, runner=runner)
            return "tune", payload
    selection = _selected(service, run)
    if selection is None:
        baseline = _evaluation(service, run["id"], "baseline_development")
        baseline_results = _evaluation_results(service, baseline)
        eligible = []
        for role, values in roles[1:]:
            evaluation = _evaluation(service, run["id"], role)
            comparison = relevance.compare(
                baseline_results, _evaluation_results(service, evaluation)
            )
            if comparison["accepted"]:
                eligible.append((comparison["mean_gain"], role, values))
        if not eligible:
            _finish(service, run, "no_eligible_development_improvement")
            return "done", payload
        winner = sorted(eligible, key=lambda item: (-item[0], item[1]))[0]
        selection = {"development_role": winner[1], "values": winner[2], "corpus_id": corpus["id"]}
        service.store.event(
            run["id"], "tuning_selection", selection, key="selected_once_before_heldout"
        )
    for role, values in (
        ("baseline_heldout", run["baseline"]),
        ("selected_heldout", selection["values"]),
    ):
        evaluation = _evaluation(service, run["id"], role)
        if not evaluation or evaluation["status"] != "completed":
            await _evaluate(service, run, role, corpus["heldout_ids"], values, runner=runner)
            return "tune", payload
    baseline = _evaluation(service, run["id"], "baseline_heldout")
    evaluation = _evaluation(service, run["id"], "selected_heldout")
    comparison = relevance.compare(
        _evaluation_results(service, baseline), _evaluation_results(service, evaluation)
    )
    service.store.event(evaluation["id"], "tuning_comparison", comparison, key="comparison")
    if comparison["accepted"]:
        _activate(service, run, evaluation, baseline, comparison)
    else:
        _finish(service, run, "heldout_rejected_no_second_selection", comparison)
    return "done", payload
