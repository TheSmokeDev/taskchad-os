import { useEffect, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import {
  ArrowUpRight, Building2, Check, ChevronRight, Download, FileCheck2,
  Globe, Layers3, Pencil, Play, Plus, RefreshCw, Search, X,
} from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Modal } from '@/components/Modal';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { ApiError, apiGetBlob, apiPost, apiPut, describeApiError } from '@/lib/api';
import { useFetch } from '@/lib/useFetch';
import { pushToast } from '@/lib/toasts';
import {
  emptyBusinessBrief,
  type BusinessBrief, type BuildGrowProject, type BuildGrowJob, type BuildGrowDeliverable,
} from '@/lib/build-grow';

const INPUT = 'w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-[13px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]';
const BUTTON = 'inline-flex items-center justify-center gap-1.5 rounded-md border border-[var(--color-border)] px-3 py-2 text-[12px] text-[var(--color-text)] hover:bg-[var(--color-elevated)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed';
const PRIMARY = 'inline-flex items-center justify-center gap-1.5 rounded-md bg-[var(--color-accent)] px-3 py-2 text-[12px] font-medium text-white hover:bg-[var(--color-accent-hover)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed';
const CARD = 'rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]';

function operationError(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return 'This business brief or deliverable has changed. Refresh the project and generate new work from the current brief before reviewing it.';
  }
  return describeApiError(error);
}

function timeLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function fileSize(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

function Field({ label, children, hint }: { label: string; children: ComponentChildren; hint?: string }) {
  return <label class="block text-[12px] text-[var(--color-text-muted)]">
    <span class="mb-1.5 block">{label}</span>
    {children}
    {hint && <span class="mt-1 block text-[11px] text-[var(--color-text-faint)]">{hint}</span>}
  </label>;
}

function Status({ value }: { value: BuildGrowJob['status'] | BuildGrowDeliverable['review_status'] }) {
  const names: Record<string, string> = {
    queued: 'Queued', running: 'Generating', ready_for_review: 'Ready for review', failed: 'Failed',
    pending: 'Awaiting review', approved: 'Accepted', changes_requested: 'Changes requested',
  };
  const color = value === 'approved' ? 'text-emerald-300 bg-emerald-500/10'
    : value === 'failed' || value === 'changes_requested' ? 'text-amber-300 bg-amber-500/10'
    : value === 'ready_for_review' ? 'text-[var(--color-accent)] bg-[var(--color-accent-soft)]'
    : 'text-[var(--color-text-muted)] bg-[var(--color-elevated)]';
  return <span class={`inline-flex rounded-full px-2 py-1 text-[10px] font-medium whitespace-nowrap ${color}`}>{names[value]}</span>;
}

export function BuildGrow() {
  const projectsState = useFetch<{ projects: BuildGrowProject[] }>('/api/build-grow/projects');
  const [projectId, setProjectId] = useState<number | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const projectState = useFetch<BuildGrowProject>(projectId === null ? null : `/api/build-grow/projects/${projectId}`);
  const jobsState = useFetch<{ jobs: BuildGrowJob[] }>(projectId === null ? null : `/api/build-grow/projects/${projectId}/jobs`, 10_000);
  const jobState = useFetch<BuildGrowJob>(jobId === null ? null : `/api/build-grow/jobs/${jobId}`, 5_000);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<BuildGrowProject | null>(null);
  const [projectName, setProjectName] = useState('');
  const [brief, setBrief] = useState<BusinessBrief>(emptyBusinessBrief);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [review, setReview] = useState<{ deliverable: BuildGrowDeliverable; decision: 'approved' | 'changes_requested' } | null>(null);
  const [reviewNote, setReviewNote] = useState('');
  const [downloadId, setDownloadId] = useState<number | null>(null);

  const projects = projectsState.data?.projects ?? [];
  const project = projectState.data?.id === projectId ? projectState.data : null;
  // useFetch retains the prior response while changing paths. Never show or
  // mutate the previous project's jobs during that transition.
  const jobs = (jobsState.data?.jobs ?? []).filter((job) => job.project_id === projectId);
  const activeJob = jobState.data?.id === jobId && jobState.data.project_id === projectId
    ? jobState.data : jobs.find((job) => job.id === jobId) ?? null;
  const currentWebsite = jobs.find((job) => job.kind === 'website'
    && job.status === 'ready_for_review' && job.brief_revision === project?.brief_revision);
  const readyCount = jobs.filter((job) => job.status === 'ready_for_review').length;

  useEffect(() => {
    if (projectId === null && projects.length) setProjectId(projects[0].id);
  }, [projects, projectId]);

  function selectProject(id: number) {
    setProjectId(id);
    setJobId(null);
    setError(null);
    setReview(null);
  }

  function refresh() {
    projectsState.refresh(); projectState.refresh(); jobsState.refresh(); jobState.refresh();
  }

  function openEditor(existing: BuildGrowProject | null) {
    setEditing(existing);
    setProjectName(existing?.name ?? '');
    setBrief(existing ? { ...existing.brief, services: existing.brief.services.map((service) => ({ ...service })) } : emptyBusinessBrief());
    setError(null);
    setEditorOpen(true);
  }

  function changeBrief<K extends keyof BusinessBrief>(key: K, value: BusinessBrief[K]) {
    setBrief((previous) => ({ ...previous, [key]: value }));
  }

  async function saveBrief(event: Event) {
    event.preventDefault();
    setBusy(true); setError(null);
    const savedBrief = {
      ...brief,
      services: brief.services.filter((service) => service.name.trim()).map((service) => ({
        name: service.name.trim(), description: service.description.trim(),
      })),
    };
    try {
      const saved = editing
        ? await apiPut<BuildGrowProject>(`/api/build-grow/projects/${editing.id}/brief`, {
          brief: savedBrief, expected_revision: editing.brief_revision,
        })
        : await apiPost<BuildGrowProject>('/api/build-grow/projects', {
          name: projectName.trim() || brief.business_name.trim(), brief: savedBrief,
        });
      selectProject(saved.id);
      setEditorOpen(false);
      refresh();
      pushToast({ tone: 'success', title: 'Business brief saved', description: `Revision ${saved.brief_revision} is ready for new work.` });
    } catch (err) {
      setError(operationError(err));
      refresh();
    } finally { setBusy(false); }
  }

  async function runJob(job: BuildGrowJob) {
    setBusy(true); setError(null); setJobId(job.id);
    try {
      const result = await apiPost<BuildGrowJob>(`/api/build-grow/jobs/${job.id}/run`, {});
      if (result.status === 'failed') {
        setError(result.error || 'Generation failed. Your job record is saved; create a new job after resolving the issue.');
      } else if (result.status === 'ready_for_review') {
        pushToast({ tone: 'success', title: 'Deliverables ready', description: 'Download the files and review the result.' });
      }
    } catch (err) { setError(operationError(err)); }
    finally { setBusy(false); jobsState.refresh(); jobState.refresh(); }
  }

  async function generate(kind: 'website' | 'geo') {
    if (!project) return;
    setBusy(true); setError(null);
    try {
      const job = await apiPost<BuildGrowJob>(`/api/build-grow/projects/${project.id}/jobs`, {
        kind, ...(kind === 'geo' && currentWebsite ? { source_job_id: currentWebsite.id } : {}),
      });
      setJobId(job.id);
      jobsState.refresh();
      await runJob(job);
    } catch (err) { setError(operationError(err)); setBusy(false); jobsState.refresh(); }
  }

  async function download(deliverable: BuildGrowDeliverable) {
    setDownloadId(deliverable.id); setError(null);
    try {
      const blob = await apiGetBlob(`/api/build-grow/deliverables/${deliverable.id}/download`);
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = objectUrl; anchor.download = deliverable.filename;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (err) { setError(operationError(err)); }
    finally { setDownloadId(null); }
  }

  async function submitReview(event: Event) {
    event.preventDefault();
    if (!review) return;
    setBusy(true); setError(null);
    try {
      await apiPost(`/api/build-grow/deliverables/${review.deliverable.id}/review`, {
        decision: review.decision, expected_sha256: review.deliverable.sha256, note: reviewNote.trim(),
      });
      setReview(null); jobState.refresh(); jobsState.refresh();
      pushToast({ tone: 'success', title: 'Review recorded' });
    } catch (err) { setError(operationError(err)); refresh(); }
    finally { setBusy(false); }
  }

  const pageError = error ?? projectsState.error ?? projectState.error ?? jobsState.error ?? jobState.error;
  return <div class="flex flex-1 min-h-0 flex-col">
    <TopBar title="Build + Grow" subtitle="Business context, website drafts, and search readiness in one place."
      actions={<button class={BUTTON} onClick={refresh} disabled={busy} aria-label="Refresh Build + Grow"><RefreshCw size={14} /></button>} />
    <div class="flex-1 overflow-y-auto p-4 md:p-6 scroll-safe-bottom">
      <div class="mx-auto max-w-[1280px] space-y-5">
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div class="text-[10px] uppercase tracking-[0.16em] text-[var(--color-accent)]">TaskChad workspace</div>
            <h1 class="mt-2 text-[24px] leading-tight font-semibold">Turn your business brief into progress.</h1>
            <p class="mt-2 max-w-2xl text-[13px] leading-6 text-[var(--color-text-muted)]">Save your business details, create a website draft, and check its SEO/GEO readiness. Review each result before your release process.</p>
          </div>
          <button type="button" class={PRIMARY} onClick={() => openEditor(null)} disabled={busy}><Plus size={15} /> New project</button>
        </div>

        {pageError && <div role="alert" class="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-[12px] leading-5 text-amber-200">{pageError}</div>}

        {projectsState.loading && !projectsState.data ? <div class="flex justify-center py-16"><Spinner /></div> : projects.length === 0 ?
          <div class={`${CARD} p-6`}><Empty title="Start with your business" description="Your saved brief keeps future website and growth work grounded in the same facts."
            action={<button type="button" class={PRIMARY} onClick={() => openEditor(null)}>Create your first project <ArrowUpRight size={14} /></button>} /></div>
          : <div class="grid gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
            <aside class="space-y-4">
              <div class={`${CARD} overflow-hidden`}>
                <div class="px-4 py-3 border-b border-[var(--color-border)] text-[12px] font-medium">Business projects <span class="ml-1 text-[var(--color-text-faint)]">{projects.length}</span></div>
                <div class="p-2 space-y-1">
                  {projects.map((item) => <button type="button" key={item.id} onClick={() => selectProject(item.id)} disabled={busy}
                    class={`w-full flex items-center gap-3 text-left rounded-lg px-3 py-3 transition-colors disabled:opacity-60 ${item.id === projectId ? 'bg-[var(--color-accent-soft)]' : 'hover:bg-[var(--color-elevated)]'}`}>
                    <Building2 size={16} class={item.id === projectId ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-muted)]'} />
                    <div class="min-w-0 flex-1"><div class="truncate text-[12px] font-medium">{item.name}</div><div class="mt-0.5 text-[10px] text-[var(--color-text-muted)]">Brief revision {item.brief_revision}</div></div>
                    {item.id === projectId && <ChevronRight size={14} class="text-[var(--color-accent)]" />}
                  </button>)}
                </div>
              </div>
              <div class="px-2 text-[11px] leading-5 text-[var(--color-text-faint)]">Each job records the brief it used. Updates create a new revision so older outputs remain traceable.</div>
            </aside>

            <section class="space-y-5 min-w-0">
              {!project ? <div class="flex justify-center py-12"><Spinner /></div> : <>
                <div class={`${CARD} p-5`}>
                  <div class="flex flex-wrap items-start justify-between gap-3">
                    <div><div class="flex items-center gap-2"><h2 class="text-[18px] font-semibold">{project.brief.business_name}</h2><span class="text-[10px] text-[var(--color-text-muted)]">Revision {project.brief_revision}</span></div>
                      <div class="mt-1 text-[12px] text-[var(--color-text-muted)]">{[project.brief.industry, project.brief.location].filter(Boolean).join(' · ') || project.name}</div></div>
                    <button type="button" class={BUTTON} onClick={() => openEditor(project)} disabled={busy}><Pencil size={13} /> Edit brief</button>
                  </div>
                  <p class="mt-4 text-[13px] leading-6 text-[var(--color-text-muted)] whitespace-pre-wrap">{project.brief.description}</p>
                  <div class="mt-4 flex flex-wrap gap-2">{project.brief.services.map((service) => <span key={service.name} class="rounded-md bg-[var(--color-elevated)] px-2 py-1 text-[11px] text-[var(--color-text-muted)]">{service.name}</span>)}</div>
                  <div class="mt-5 grid grid-cols-3 gap-3 border-t border-[var(--color-border)] pt-4">
                    {[['Saved brief', `v${project.brief_revision}`], ['Jobs', jobs.length], ['Ready to review', readyCount]].map(([label, value]) => <div key={label}><div class="text-[10px] text-[var(--color-text-faint)]">{label}</div><div class="mt-1 text-[18px] font-semibold">{value}</div></div>)}
                  </div>
                </div>

                <div class="grid gap-4 md:grid-cols-2">
                  <div class={`${CARD} p-5 flex flex-col`}>
                    <Globe size={21} class="text-[var(--color-accent)]" /><h3 class="mt-3 text-[14px] font-semibold">Build your website</h3>
                    <p class="mt-2 flex-1 text-[12px] leading-5 text-[var(--color-text-muted)]">Generate a static website draft and validation report from your saved business facts.</p>
                    <button type="button" class={`${PRIMARY} mt-4 self-start`} onClick={() => generate('website')} disabled={busy}><Play size={13} /> {busy ? 'Working…' : 'Generate website'}</button>
                  </div>
                  <div class={`${CARD} p-5 flex flex-col`}>
                    <Search size={21} class="text-[var(--color-accent)]" /><h3 class="mt-3 text-[14px] font-semibold">Check search readiness</h3>
                    <p class="mt-2 flex-1 text-[12px] leading-5 text-[var(--color-text-muted)]">Check the generated website’s metadata, structured data, and content. Get evidence and prioritized fixes.</p>
                    <button type="button" class={`${BUTTON} mt-4 self-start`} onClick={() => generate('geo')} disabled={busy || !currentWebsite}><Search size={13} /> Run SEO/GEO checks</button>
                    {!currentWebsite && <p class="mt-2 text-[10px] text-[var(--color-text-faint)]">Generate a website from the current brief first.</p>}
                  </div>
                </div>
                <p class="text-[11px] leading-5 text-[var(--color-text-faint)]">Search readiness checks inspect generated files. Live rankings, indexing, and AI citations require separate measurement.</p>

                <div class={`${CARD} overflow-hidden`}>
                  <div class="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3"><h2 class="text-[13px] font-medium flex items-center gap-2"><Layers3 size={14} /> Project work</h2><span class="text-[10px] text-[var(--color-text-faint)]">{jobs.length} jobs</span></div>
                  {jobs.length === 0 ? <Empty title="Your first deliverable starts here" description="Generate a website to create a recorded job with downloadable outputs." /> : <div class="divide-y divide-[var(--color-border)]">
                    {jobs.map((job) => <div key={job.id} class={`flex items-center gap-3 px-4 py-3 ${job.id === jobId ? 'bg-[var(--color-accent-soft)]' : ''}`}>
                      <button type="button" class="flex flex-1 min-w-0 items-center gap-3 text-left" onClick={() => setJobId(job.id)}>
                        {job.kind === 'website' ? <Globe size={17} class="shrink-0 text-[var(--color-text-muted)]" /> : <Search size={17} class="shrink-0 text-[var(--color-text-muted)]" />}
                        <div class="min-w-0"><div class="text-[12px] font-medium">{job.kind === 'website' ? 'Website draft' : 'SEO/GEO report'} <span class="text-[var(--color-text-faint)]">#{job.id}</span></div><div class="mt-1 text-[10px] text-[var(--color-text-muted)]">Brief v{job.brief_revision} · {timeLabel(job.created_at)}{job.brief_revision !== project.brief_revision && <span class="ml-1 text-amber-300">· Older brief</span>}</div></div>
                      </button>
                      <Status value={job.status} />
                      {job.status === 'queued' && <button type="button" class={`${BUTTON} !px-2 !py-1`} onClick={() => runJob(job)} disabled={busy}>Run</button>}
                    </div>)}
                  </div>}
                </div>

                {activeJob && <div class={`${CARD} p-5`}>
                  <div class="flex items-start justify-between gap-3"><div><h2 class="text-[14px] font-semibold flex items-center gap-2"><FileCheck2 size={16} /> {activeJob.kind === 'website' ? 'Website deliverables' : 'SEO/GEO deliverables'}</h2><p class="mt-1 text-[11px] text-[var(--color-text-muted)]">Job #{activeJob.id} · Brief revision {activeJob.brief_revision}</p></div><Status value={activeJob.status} /></div>
                  {activeJob.brief_revision !== project.brief_revision && <div class="mt-4 text-[12px] text-amber-300">These outputs use an older brief. Generate new work before recording acceptance.</div>}
                  {activeJob.error && <div class="mt-4 text-[12px] text-amber-300" role="alert">{activeJob.error}</div>}
                  {activeJob.deliverables.length === 0 ? <p class="py-6 text-[12px] text-[var(--color-text-muted)]">{activeJob.status === 'running' ? 'Generating your deliverables…' : 'This job has no deliverables yet.'}</p> : <div class="mt-4 space-y-3">
                    {activeJob.deliverables.map((deliverable) => <div key={deliverable.id} class="rounded-lg border border-[var(--color-border)] p-3">
                      <div class="flex flex-wrap items-center justify-between gap-2"><div class="min-w-0"><div class="text-[12px] font-medium break-all">{deliverable.filename}</div><div class="mt-1 text-[10px] text-[var(--color-text-faint)]">{fileSize(deliverable.size_bytes)}</div></div><Status value={deliverable.review_status} /></div>
                      <div class="mt-3 flex flex-wrap gap-2">
                        <button type="button" class={`${BUTTON} !py-1.5`} onClick={() => download(deliverable)} disabled={downloadId !== null}><Download size={12} /> {downloadId === deliverable.id ? 'Downloading…' : 'Download'}</button>
                        <button type="button" class={`${BUTTON} !py-1.5`} disabled={busy || activeJob.brief_revision !== project.brief_revision} onClick={() => { setReview({ deliverable, decision: 'approved' }); setReviewNote(''); }}><Check size={12} /> Accept</button>
                        <button type="button" class={`${BUTTON} !py-1.5`} disabled={busy || activeJob.brief_revision !== project.brief_revision} onClick={() => { setReview({ deliverable, decision: 'changes_requested' }); setReviewNote(''); }}>Request changes</button>
                      </div>
                    </div>)}
                  </div>}
                  <p class="mt-4 text-[11px] leading-5 text-[var(--color-text-faint)]">Acceptance records your review of this exact file. Deployment follows your separate release process.</p>
                </div>}
              </>}
            </section>
          </div>}
      </div>
    </div>

    <Modal open={editorOpen} onClose={() => { if (!busy) setEditorOpen(false); }} title={editing ? 'Update business brief' : 'New business project'} width={680}>
      <form onSubmit={saveBrief} class="space-y-4">
        <p class="text-[12px] leading-5 text-[var(--color-text-muted)]">Use confirmed details. Website copy and growth checks will use this saved brief.</p>
        {error && <div role="alert" class="text-[12px] text-amber-300">{error}</div>}
        {!editing && <Field label="Project name"><input class={INPUT} value={projectName} maxLength={160} onInput={(event) => setProjectName(event.currentTarget.value)} placeholder="Optional — defaults to business name" /></Field>}
        <Field label="Business name"><input class={INPUT} required maxLength={160} value={brief.business_name} onInput={(event) => changeBrief('business_name', event.currentTarget.value)} /></Field>
        <Field label="Business description"><textarea class={INPUT} required rows={3} maxLength={4000} value={brief.description} onInput={(event) => changeBrief('description', event.currentTarget.value)} placeholder="What you do, who you serve, and what customers can expect." /></Field>
        <div class="grid gap-4 sm:grid-cols-2"><Field label="Industry"><input class={INPUT} value={brief.industry} maxLength={120} onInput={(event) => changeBrief('industry', event.currentTarget.value)} /></Field><Field label="Location / service area"><input class={INPUT} value={brief.location} maxLength={200} onInput={(event) => changeBrief('location', event.currentTarget.value)} /></Field></div>
        <div class="space-y-2"><div class="text-[12px] text-[var(--color-text-muted)]">Services</div>{brief.services.map((service, index) => <div key={index} class="flex items-start gap-2"><div class="grid flex-1 gap-2 sm:grid-cols-2"><input aria-label={`Service ${index + 1} name`} class={INPUT} required maxLength={160} value={service.name} placeholder="Service name" onInput={(event) => changeBrief('services', brief.services.map((item, i) => i === index ? { ...item, name: event.currentTarget.value } : item))} /><input aria-label={`Service ${index + 1} description`} class={INPUT} required value={service.description} maxLength={2000} placeholder="Short description" onInput={(event) => changeBrief('services', brief.services.map((item, i) => i === index ? { ...item, description: event.currentTarget.value } : item))} /></div>{brief.services.length > 1 && <button type="button" class={`${BUTTON} !p-2`} aria-label={`Remove service ${index + 1}`} onClick={() => changeBrief('services', brief.services.filter((_, i) => i !== index))}><X size={14} /></button>}</div>)}<button type="button" class="text-[11px] text-[var(--color-accent)]" disabled={brief.services.length >= 20} onClick={() => changeBrief('services', [...brief.services, { name: '', description: '' }])}>+ Add service</button></div>
        <div class="grid gap-4 sm:grid-cols-2"><Field label="Contact email"><input type="email" class={INPUT} required maxLength={254} value={brief.contact_email} onInput={(event) => changeBrief('contact_email', event.currentTarget.value)} /></Field><Field label="Phone"><input type="tel" class={INPUT} value={brief.phone} maxLength={60} onInput={(event) => changeBrief('phone', event.currentTarget.value)} /></Field></div>
        <Field label="Current website" hint="Optional HTTPS address without query parameters or fragments."><input type="url" class={INPUT} pattern="https://.*" value={brief.site_url} maxLength={1000} onInput={(event) => changeBrief('site_url', event.currentTarget.value)} placeholder="https://your-business.com" /></Field>
        <Field label="GitHub repository" hint="Optional repository reference for your project."><input type="url" class={INPUT} pattern="https://github\.com/[^/]+/[^/]+/?" value={brief.repository_url} maxLength={1000} onInput={(event) => changeBrief('repository_url', event.currentTarget.value)} placeholder="https://github.com/owner/repository" /></Field>
        <Field label="Brand accent"><div class="flex items-center gap-3"><input type="color" class="h-9 w-12 cursor-pointer rounded border border-[var(--color-border)] bg-transparent" value={brief.brand_color} onInput={(event) => changeBrief('brand_color', event.currentTarget.value)} /><span class="text-[12px]">{brief.brand_color}</span></div></Field>
        {editing && <p class="text-[11px] text-[var(--color-text-faint)]">Saving creates a new revision. Existing jobs retain revision {editing.brief_revision}.</p>}
        <div class="flex justify-end gap-2 border-t border-[var(--color-border)] pt-4"><button type="button" class={BUTTON} disabled={busy} onClick={() => setEditorOpen(false)}>Cancel</button><button type="submit" class={PRIMARY} disabled={busy}>{busy ? 'Saving…' : 'Save business brief'}</button></div>
      </form>
    </Modal>

    <Modal open={review !== null} onClose={() => { if (!busy) setReview(null); }} title={review?.decision === 'approved' ? 'Accept deliverable' : 'Request changes'}>
      <form onSubmit={submitReview} class="space-y-4">
        <p class="text-[12px] text-[var(--color-text-muted)] break-all">{review?.deliverable.filename}</p>
        <p class="text-[12px] leading-5 text-[var(--color-text-muted)]">Your review is recorded for this exact file. Accepted files are ready for your release process.</p>
        {error && <div role="alert" class="text-[12px] text-amber-300">{error}</div>}
        <Field label="Review note"><textarea class={INPUT} rows={3} maxLength={2000} value={reviewNote} onInput={(event) => setReviewNote(event.currentTarget.value)} placeholder={review?.decision === 'changes_requested' ? 'Describe the changes needed.' : 'Optional feedback.'} /></Field>
        <div class="flex justify-end gap-2"><button type="button" class={BUTTON} disabled={busy} onClick={() => setReview(null)}>Cancel</button><button type="submit" class={PRIMARY} disabled={busy}>{busy ? 'Recording…' : 'Record review'}</button></div>
      </form>
    </Modal>
  </div>;
}
