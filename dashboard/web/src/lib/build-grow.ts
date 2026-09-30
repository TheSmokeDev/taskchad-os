/** Build + Grow contracts. Project records and artifacts are owned by Python. */
export interface BusinessBrief {
  business_name: string;
  description: string;
  industry: string;
  location: string;
  services: { name: string; description: string }[];
  contact_email: string;
  phone: string;
  site_url: string;
  repository_url: string;
  brand_color: string;
}

export interface BuildGrowProject {
  id: number;
  name: string;
  brief_revision: number;
  brief: BusinessBrief;
  created_at: string;
  updated_at: string;
}

export interface BuildGrowDeliverable {
  id: number;
  filename: string;
  media_type: string;
  sha256: string;
  size_bytes: number;
  review_status: 'pending' | 'approved' | 'changes_requested';
}

export interface BuildGrowJob {
  id: number;
  project_id: number;
  kind: 'website' | 'geo';
  status: 'queued' | 'running' | 'ready_for_review' | 'failed';
  brief_revision: number;
  source_job_id?: number | null;
  created_at: string;
  updated_at: string;
  error?: string | null;
  deliverables: BuildGrowDeliverable[];
}

export function emptyBusinessBrief(): BusinessBrief {
  return {
    business_name: '', description: '', industry: '', location: '',
    services: [{ name: '', description: '' }], contact_email: '', phone: '',
    site_url: '', repository_url: '', brand_color: '#eab308',
  };
}
