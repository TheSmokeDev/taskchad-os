/** Operator projection of the Python-owned learning ledger. */
export interface LearningRecord {
  id: string;
  kind: string;
  created_at: string;
  persona_id: string;
  payload: Record<string, unknown>;
  links?: Array<{ id: string; label: string }>;
}

export interface LearningSummary {
  persona_id: string;
  paused: boolean;
  enabled: boolean;
  counts: Record<string, number>;
  active_methods: LearningRecord[];
  pending_outcomes: number;
  failures: number;
  lifecycle?: LearningLifecycle;
  tuning?: LearningTuning;
  cognition?: {
    cycles: Record<string, number>;
    understanding: Record<string, number>;
    investigations: Record<string, number>;
    delivered_contexts: number;
    execution_modes?: Record<string, number>;
    recorded_model_calls?: number;
    dispatcher: { state: string; last_success_at?: number | null; error_type?: string | null; adapter_coverage?: unknown };
  };
  queue?: {
    pending: number;
    statuses: Record<string, number>;
    jobs: Array<{ id: string; kind: string; stage: string; status: string; last_error?: string; record_id?: string | null }>;
  };
}

export interface LearningLifecycle {
  persona_id: string;
  pending_scope: string;
  synthesis: Record<string, {
    pending_cycles: number;
    completed_cycles: number;
    consumed_sources: number;
    consumed_characters: number;
    last_completed_at?: string | null;
    latest_request?: { id?: string; status?: string; reason?: string } | null;
    pending_consumers?: Array<{ cycle_id: string; status: string; input_count: number }>;
  }>;
  pending_stages: Array<{ id: string; kind: string; stage: string; status: string; record_id?: string | null; reason?: string | null; available_at?: number | null }>;
  request_statuses: Record<string, number>;
  pending_stages_truncated?: boolean;
  cycles_truncated?: boolean;
  recent_cycles: Array<{
    id: string;
    synthesis_kind: string;
    status: string;
    conclusion?: string;
    result_ids?: string[];
    input_manifest: Array<{ ref: string; revision: string; kind: string; start: number; end: number; complete: boolean }>;
    omitted_manifest: unknown[];
    consumption_status: string;
    projection_status: string;
    partial_inputs: number;
    model_calls: Array<{ id: string; model?: string; provider?: string; success?: boolean; status?: string }>;
  }>;
}

export interface LearningTuning {
  min_cases: number;
  validated_cases: number;
  development_cases: number;
  heldout_cases: number;
  readiness: string;
  reason?: string;
  active_policy?: Record<string, unknown> | null;
  latest_run?: Record<string, unknown> | null;
  latest_evaluation?: Record<string, unknown> | null;
  policies?: Array<Record<string, unknown>>;
}

export interface LearningPage {
  persona_id: string;
  records: LearningRecord[];
  next_cursor: string | null;
}

export interface LearningReport {
  persona_id: string;
  period: { since: string; until: string };
  counts: Record<string, number>;
  records: Array<{ id: string; kind: string; title?: string; question?: string; conclusion?: string; status?: string }>;
  open_investigations: Array<{ id: string; question?: string; status?: string; reason?: string }>;
  records_truncated: boolean;
  has_activity: boolean;
  narrative: string | null;
  narrative_status: string;
  model?: string;
  provider?: string;
}
