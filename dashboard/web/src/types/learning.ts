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
  queue?: {
    pending: number;
    statuses: Record<string, number>;
    jobs: Array<{ id: string; kind: string; stage: string; status: string; last_error?: string; record_id?: string | null }>;
  };
}

export interface LearningPage {
  persona_id: string;
  records: LearningRecord[];
  next_cursor: string | null;
}
