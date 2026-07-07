import { useMemo, useState } from 'preact/hooks';
import { useLocation } from 'wouter-preact';
import { Search, FileBadge, MessageSquare } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { Modal } from '@/components/Modal';
import { useFetch } from '@/lib/useFetch';
import { apiGet, dashboardChatReadOnly, describeApiError } from '@/lib/api';
import { renderMarkdown } from '@/lib/markdown';
import { pushToast } from '@/lib/toasts';

interface Skill {
  name: string;
  description: string;
  path: string;
}

interface SkillDetail extends Skill {
  frontmatter: Record<string, string>;
  body: string;
  truncated: boolean;
}

interface Draft {
  name: string;
  verdict: string;
  recurrence_count: number;
}

interface SkillsResponse { skills: Skill[]; }
interface DraftsResponse { drafts: Draft[]; }

function verdictTone(verdict: string): string {
  const v = verdict.toLowerCase();
  if (v === 'safe') return 'text-[var(--color-status-done)] border-[color-mix(in_srgb,var(--color-status-done)_50%,transparent)]';
  if (v === 'dangerous') return 'text-[var(--color-status-failed)] border-[color-mix(in_srgb,var(--color-status-failed)_50%,transparent)]';
  if (v === 'caution') return 'text-[var(--color-status-warn)] border-[color-mix(in_srgb,var(--color-status-warn)_50%,transparent)]';
  return 'text-[var(--color-text-muted)] border-[var(--color-border)]';
}

export function Skills() {
  const [, navigate] = useLocation();
  const [query, setQuery] = useState('');
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [detailName, setDetailName] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const readOnly = dashboardChatReadOnly;
  const list = useFetch<SkillsResponse>('/api/skills', 60_000);
  const drafts = useFetch<DraftsResponse>('/api/skills/drafts', 60_000);

  const skills = list.data?.skills ?? [];
  const draftRows = drafts.data?.drafts ?? [];

  // Hermes parity: search is client-side (the endpoint docstring says so) —
  // filter by name/description, no server round-trip.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter(
      (s) => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q),
    );
  }, [query, skills]);

  async function openDetail(name: string) {
    setDetailName(name);
    setDetail(null);
    setDetailLoading(true);
    try {
      const res = await apiGet<SkillDetail>(`/api/skills/detail?name=${encodeURIComponent(name)}`);
      setDetail(res);
    } catch (err) {
      pushToast({ tone: 'error', title: 'Failed to load skill', description: describeApiError(err) });
      setDetailName(null);
    } finally {
      setDetailLoading(false);
    }
  }

  function closeDetail() {
    setDetailName(null);
    setDetail(null);
  }

  /** Promotion NEVER mutates from here — deep-link to Chat with the gated
   *  /skills command prefilled; the default-deny chat path does the rest. */
  function composeSkillCommand(verb: 'promote' | 'reject', name: string) {
    navigate(`/chat?draft=${encodeURIComponent(`/skills ${verb} ${name}`)}`);
  }

  return (
    <div class="flex flex-col h-full min-h-0">
      <TopBar
        title="Skills"
        subtitle={list.data ? `${skills.length} installed · ${draftRows.length} draft${draftRows.length === 1 ? '' : 's'} eligible` : ''}
      />

      <div class="px-6 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg)] flex items-center gap-2">
        <Search size={14} class="text-[var(--color-text-faint)] shrink-0" />
        <input
          type="text"
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
          placeholder="filter skills by name or description..."
          aria-label="Filter skills"
          class="flex-1 max-w-md bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2 py-1 text-[12px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)] placeholder:text-[var(--color-text-faint)]"
        />
      </div>

      <div class="flex-1 overflow-y-auto scroll-safe-bottom">
        {list.loading && !list.data && (
          <div class="flex items-center justify-center h-40"><Spinner /></div>
        )}
        {list.error && <Empty title="Failed to load skills" description={list.error} />}
        {!list.loading && !list.error && skills.length === 0 && (
          <Empty title="No skills installed" description="Skills appear once SKILL.md files exist under .claude/skills/." />
        )}
        {!list.error && skills.length > 0 && filtered.length === 0 && (
          <Empty title="No matches" description={`No skill matches "${query}".`} />
        )}

        {filtered.length > 0 && (
          <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 p-4 md:p-6">
            {filtered.map((s) => (
              <button
                key={s.path}
                type="button"
                onClick={() => openDetail(s.name)}
                class="text-left rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-3 transition-colors hover:border-[var(--color-accent)]"
              >
                <div class="flex items-center gap-2 mb-1">
                  <FileBadge size={14} class="text-[var(--color-accent)] shrink-0" />
                  <span class="text-[13px] font-medium text-[var(--color-text)] truncate">{s.name}</span>
                </div>
                <div class="text-[12px] text-[var(--color-text-muted)] line-clamp-2 min-h-[2.1em]">{s.description || 'No description.'}</div>
                <div class="mt-2 inline-block max-w-full truncate rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-1.5 py-0.5 font-mono text-[10.5px] text-[var(--color-text-faint)]">
                  {s.path}
                </div>
              </button>
            ))}
          </div>
        )}

        {/* Drafts — promotion-eligible generated skills. Read-only projection;
          * promote/reject run through the gated /skills chat command. */}
        <div class="px-4 md:px-6 pb-6">
          <div class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-2 mt-2">
            Promotion-eligible drafts
          </div>
          {drafts.loading && !drafts.data && (
            <div class="flex items-center py-4"><Spinner size={14} /></div>
          )}
          {drafts.error && (
            <Empty title="Failed to load drafts" description={drafts.error} />
          )}
          {!drafts.loading && !drafts.error && draftRows.length === 0 && (
            <div class="text-[12px] text-[var(--color-text-muted)] py-2">
              No promotion-eligible drafts right now. Generated drafts become eligible after enough recurrences.
            </div>
          )}
          {draftRows.length > 0 && (
            <div class="flex flex-col gap-2">
              <div class="text-[11.5px] text-[var(--color-text-muted)]">
                Promotion is gated: these buttons only prefill the <span class="font-mono">/skills</span> chat
                command — the default-deny, security-scanned promotion gate runs in Chat.
              </div>
              {draftRows.map((d) => (
                <div
                  key={d.name}
                  class="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2"
                >
                  <span class="text-[13px] font-medium text-[var(--color-text)]">{d.name}</span>
                  <span class={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10.5px] uppercase tracking-wider ${verdictTone(d.verdict)}`}>
                    scan: {d.verdict || 'unknown'}
                  </span>
                  <span class="text-[11px] text-[var(--color-text-muted)]">
                    {d.recurrence_count} recurrence{d.recurrence_count === 1 ? '' : 's'}
                  </span>
                  {!readOnly && (
                    <div class="ml-auto flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => composeSkillCommand('promote', d.name)}
                        class="inline-flex h-7 items-center gap-1.5 rounded-md border border-[var(--color-accent)] bg-[var(--color-accent-soft)] px-2.5 text-[12px] font-medium text-[var(--color-accent)] transition-opacity hover:opacity-90"
                        title="Prefill /skills promote in Chat (gated command — nothing runs from here)"
                      >
                        <MessageSquare size={12} />
                        Promote
                      </button>
                      <button
                        type="button"
                        onClick={() => composeSkillCommand('reject', d.name)}
                        class="inline-flex h-7 items-center gap-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] px-2.5 text-[12px] text-[var(--color-text-muted)] transition-colors hover:text-[var(--color-text)]"
                        title="Prefill /skills reject in Chat (gated command — nothing runs from here)"
                      >
                        <MessageSquare size={12} />
                        Reject
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <Modal open={detailName !== null} onClose={closeDetail} title={detailName ?? ''} width={720}>
        {detailLoading && (
          <div class="flex items-center justify-center py-8"><Spinner /></div>
        )}
        {!detailLoading && detail && (
          <div class="flex flex-col gap-3">
            <div class="flex flex-wrap items-center gap-2">
              <span class="inline-block rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-1.5 py-0.5 font-mono text-[10.5px] text-[var(--color-text-faint)]">
                {detail.path}
              </span>
              {detail.truncated && (
                <span class="text-[10.5px] uppercase tracking-wider text-[var(--color-status-warn)]">truncated</span>
              )}
            </div>
            {detail.description && (
              <div class="text-[12px] text-[var(--color-text-muted)]">{detail.description}</div>
            )}
            <div
              class="text-[13px] leading-relaxed prose-sm"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(detail.body) }}
            />
          </div>
        )}
      </Modal>
    </div>
  );
}
