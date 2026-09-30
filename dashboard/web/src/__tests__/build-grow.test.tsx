import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { BuildGrow } from '@/pages/BuildGrow';
import type { BuildGrowProject, BuildGrowJob } from '@/lib/build-grow';

function project(): BuildGrowProject {
  return {
    id: 1, name: 'Cedar Plumbing', brief_revision: 1, created_at: '2026-09-30T12:00:00Z', updated_at: '2026-09-30T12:00:00Z',
    brief: { business_name: 'Cedar Plumbing', description: 'Residential plumbing with clear estimates.', industry: 'Plumbing', location: 'Austin', services: [{ name: 'Drain cleaning', description: 'Clear blocked drains.' }], contact_email: 'hello@cedar.test', phone: '', site_url: '', repository_url: '', brand_color: '#eab308' },
  };
}

function website(): BuildGrowJob {
  return {
    id: 10, project_id: 1, kind: 'website', status: 'ready_for_review', brief_revision: 1,
    created_at: '2026-09-30T12:00:00Z', updated_at: '2026-09-30T12:00:00Z', error: null,
    deliverables: [{ id: 90, filename: 'website.zip', media_type: 'application/zip', sha256: 'a'.repeat(64), size_bytes: 5000, review_status: 'pending' }],
  };
}

describe('Build + Grow workspace', () => {
  let savedProject: BuildGrowProject;
  let jobs: BuildGrowJob[];
  let conflict: boolean;
  let runFailure: boolean;

  beforeEach(() => {
    savedProject = project(); jobs = []; conflict = false; runFailure = false;
    vi.stubGlobal('fetch', vi.fn(async (input, init) => {
      const path = String(input);
      const method = init?.method ?? 'GET';
      let body: unknown = {};
      let status = 200;
      if (path === '/api/build-grow/projects') {
        body = method === 'GET' ? { projects: [savedProject] } : savedProject;
      } else if (path === '/api/build-grow/projects/1') body = savedProject;
      else if (path === '/api/build-grow/projects/1/brief') {
        if (conflict) { body = { detail: 'stale revision' }; status = 409; }
        else {
          savedProject = { ...savedProject, brief: JSON.parse(String(init?.body)).brief, brief_revision: savedProject.brief_revision + 1 };
          body = savedProject;
        }
      } else if (path === '/api/build-grow/projects/1/jobs') {
        if (method === 'GET') body = { jobs };
        else { const queued = { ...website(), status: 'queued' as const, deliverables: [] }; jobs = [queued]; body = queued; }
      } else if (path === '/api/build-grow/jobs/10/run') {
        jobs = [runFailure ? { ...website(), status: 'failed', error: 'Generation failed.', deliverables: [] } : website()];
        body = jobs[0];
      }
      else if (path === '/api/build-grow/jobs/10') body = jobs[0] ?? website();
      else if (path === '/api/build-grow/deliverables/90/review') {
        if (conflict) { body = { detail: 'stale brief' }; status = 409; }
        else { jobs[0].deliverables[0].review_status = JSON.parse(String(init?.body)).decision; body = jobs[0].deliverables[0]; }
      }
      return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
    }));
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('records a website job before running it and enables GEO only after ready artifacts', async () => {
    render(<BuildGrow />);
    await screen.findByRole('button', { name: 'Edit brief' });
    expect(screen.getByRole('button', { name: 'Run SEO/GEO checks' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Generate website' }));
    await screen.findByText('website.zip');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run SEO/GEO checks' })).toBeEnabled());
    const calls = vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === 'POST');
    expect(calls.map(([path]) => path)).toEqual(['/api/build-grow/projects/1/jobs', '/api/build-grow/jobs/10/run']);
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ kind: 'website' });
    expect(screen.getByText(/Acceptance records your review/)).toBeInTheDocument();
  });

  it('submits review against the exact artifact digest and reports stale review conflicts', async () => {
    jobs = [website()]; conflict = true;
    render(<BuildGrow />);
    fireEvent.click(await screen.findByRole('button', { name: /Website draft/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }));
    fireEvent.input(screen.getByLabelText('Review note'), { target: { value: 'Copy looks accurate.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Record review' }));
    await waitFor(() => expect(screen.getAllByRole('alert')[0]).toHaveTextContent('has changed'));
    const call = vi.mocked(fetch).mock.calls.find(([path]) => path === '/api/build-grow/deliverables/90/review');
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ decision: 'approved', expected_sha256: 'a'.repeat(64), note: 'Copy looks accurate.' });
  });

  it('saves edited briefs with optimistic revision protection', async () => {
    render(<BuildGrow />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit brief' }));
    fireEvent.input(screen.getByLabelText('Business description'), { target: { value: 'Confirmed updated business description.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save business brief' }));
    await screen.findByText('Confirmed updated business description.');
    const call = vi.mocked(fetch).mock.calls.find(([path]) => path === '/api/build-grow/projects/1/brief');
    expect(call?.[1]?.method).toBe('PUT');
    expect(JSON.parse(String(call?.[1]?.body)).expected_revision).toBe(1);
    expect(savedProject.brief_revision).toBe(2);
  });

  it('marks outputs from older briefs and disables acceptance and current-brief GEO', async () => {
    savedProject.brief_revision = 2; jobs = [website()];
    render(<BuildGrow />);
    fireEvent.click(await screen.findByRole('button', { name: /Website draft/ }));
    expect(await screen.findByText(/These outputs use an older brief/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Accept' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Run SEO/GEO checks' })).toBeDisabled();
  });

  it('keeps failed job history and reports a builder failure returned with HTTP 200', async () => {
    runFailure = true;
    render(<BuildGrow />);
    fireEvent.click(await screen.findByRole('button', { name: 'Generate website' }));
    await waitFor(() => expect(screen.getAllByRole('alert')[0]).toHaveTextContent('Generation failed.'));
    expect(await screen.findByRole('button', { name: /Website draft/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run SEO/GEO checks' })).toBeDisabled();
    expect(screen.queryByText('website.zip')).not.toBeInTheDocument();
  });
});
