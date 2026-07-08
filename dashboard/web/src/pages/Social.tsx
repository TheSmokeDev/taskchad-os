import { RefreshCw, Link as LinkIcon, Check, X } from 'lucide-preact';
import type { ComponentChildren } from 'preact';
import { useState } from 'preact/hooks';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';
import { apiGet, apiPost, describeApiError } from '@/lib/api';

interface SocialStatusResponse {
  postiz?: {
    configured?: boolean;
    reachable?: boolean;
    auth_ok?: boolean;
    integrations_count?: number;
    error?: string;
  };
  studio_url?: string;
  queue?: Record<string, number>;
  cadence_enabled?: boolean;
}

interface ChannelRow {
  channel_id: string;
  display_name: string;
  execution_method: string;
  cadence_enabled: boolean;
  postiz_integration_id: string;
  postiz_bound: boolean;
}

interface PostizIntegrationRow {
  id: string;
  name: string;
  identifier: string;
  disabled: boolean;
  profile: string;
}

interface ChannelsResponse {
  channels?: ChannelRow[];
  postiz_integrations?: PostizIntegrationRow[];
  postiz_error?: string;
}

interface QueueRow {
  id: number;
  channel: string;
  status: string;
  title: string;
  body: string;
  scheduled_for?: string | null;
  post_url?: string | null;
  error?: string | null;
}

interface QueueResponse {
  posts?: QueueRow[];
  counts?: Record<string, number>;
}

interface PostizPostRow {
  id: string;
  content: string;
  publishDate?: string;
  state?: string;
  releaseURL?: string;
  integration?: { providerIdentifier?: string; name?: string };
}

interface PostizPostsResponse {
  posts?: PostizPostRow[];
  postiz_error?: string;
}

const CONNECT_PROVIDERS = [
  'facebook', 'instagram', 'linkedin', 'linkedin-page', 'youtube',
  'tiktok', 'mastodon', 'bluesky', 'threads', 'reddit', 'pinterest',
  'discord', 'slack', 'x',
];

function pill(tone: 'ok' | 'warn' | 'bad', label: string) {
  const cls = tone === 'ok'
    ? 'border-[color-mix(in_srgb,var(--color-status-done)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-done)_14%,transparent)] text-[var(--color-status-done)]'
    : tone === 'warn'
      ? 'border-[color-mix(in_srgb,var(--color-status-warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-warn)_14%,transparent)] text-[var(--color-status-warn)]'
      : 'border-[color-mix(in_srgb,var(--color-status-failed)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-failed)_14%,transparent)] text-[var(--color-status-failed)]';
  return (
    <span class={`inline-flex items-center rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${cls}`}>
      {label}
    </span>
  );
}

function statusPill(status: string) {
  if (status === 'posted') return pill('ok', 'posted');
  if (status === 'approved') return pill('warn', 'approved');
  if (status === 'draft') return pill('warn', 'draft');
  return pill('bad', status);
}

function Panel({ title, children, actions }: {
  title: string;
  children: ComponentChildren;
  actions?: ComponentChildren;
}) {
  return (
    <section class="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
      <div class="mb-4 flex min-w-0 items-center justify-between gap-3">
        <h2 class="truncate text-[13px] font-semibold text-[var(--color-text)]">{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

function OnboardingCard() {
  return (
    <Panel title="Connect a Postiz instance">
      <div class="space-y-3 text-[13px] text-[var(--color-text-muted)]">
        <p>
          The Social lane publishes through a self-hosted{' '}
          <span class="font-medium text-[var(--color-text)]">Postiz</span> instance
          (open-source multi-platform scheduler). None is configured yet.
        </p>
        <ol class="list-decimal space-y-1 pl-5">
          <li>Run a Postiz instance (its docker-compose runs the app, Postgres, Redis, and Temporal).</li>
          <li>Create a Public API key in Postiz settings.</li>
          <li>
            Set <code class="rounded bg-[var(--color-elevated)] px-1 font-mono text-[12px]">POSTIZ_API_URL</code> and{' '}
            <code class="rounded bg-[var(--color-elevated)] px-1 font-mono text-[12px]">POSTIZ_API_KEY</code> in the framework env, then restart the API.
          </li>
        </ol>
        <p>
          See the manual page <span class="font-mono text-[12px]">social-postiz-integration</span> for
          the full setup, platform matrix, and the approval-pipeline rules.
        </p>
      </div>
    </Panel>
  );
}

export function Social() {
  const status = useFetch<SocialStatusResponse>('/api/social/status', 30_000);
  const channels = useFetch<ChannelsResponse>('/api/social/channels', 60_000);
  const queue = useFetch<QueueResponse>('/api/social/queue', 15_000);
  const postizPosts = useFetch<PostizPostsResponse>('/api/social/posts', 60_000);

  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [provider, setProvider] = useState(CONNECT_PROVIDERS[0]);
  const [composeChannel, setComposeChannel] = useState('');
  const [composeTitle, setComposeTitle] = useState('');
  const [composeBody, setComposeBody] = useState('');
  const [view, setView] = useState<'queue' | 'studio'>('queue');

  const postiz = status.data?.postiz ?? {};
  const configured = Boolean(postiz.configured);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setNotice('');
    try {
      await action();
    } catch (err) {
      setNotice(describeApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const openConnectUrl = () => run(async () => {
    // Fetched on click only — connect URLs are sensitive and expire.
    const res = await apiGet<{ url?: string }>(
      '/api/social/connect-url?provider=' + encodeURIComponent(provider),
    );
    if (res.url) window.open(res.url, '_blank', 'noopener');
    else setNotice('No connect URL returned.');
  });

  /** After a dispatch, chase the live URL: run an on-demand reconcile every
   *  ~8s (list-endpoints only) until the row gets its releaseURL, fails, or
   *  we give up (~2 min). Runs detached so the buttons stay usable. */
  const watchForUrl = async (id: number) => {
    for (let i = 0; i < 15; i++) {
      await new Promise((r) => setTimeout(r, 8000));
      try {
        await apiPost('/api/social/reconcile', {});
        const q = await apiGet<QueueResponse>('/api/social/queue');
        const row = (q.posts ?? []).find((p) => p.id === id);
        if (row?.post_url) {
          setNotice(`Post #${id} is LIVE: ${row.post_url}`);
          queue.refresh();
          return;
        }
        if (row?.status === 'failed') {
          setNotice(`Post #${id} failed on the platform: ${row.error || 'see Postiz'}`);
          queue.refresh();
          return;
        }
      } catch {
        // transient — keep watching
      }
    }
    setNotice(`Post #${id} is still publishing — the URL will appear on its queue row when the platform confirms.`);
    queue.refresh();
  };

  const approve = (id: number) => run(async () => {
    const res = await apiPost<{
      dispatched?: boolean; status?: string; post_url?: string; error?: string;
    }>('/api/social/approve', { post_id: id });
    if (res.dispatched) {
      setNotice(res.post_url
        ? `Post #${id} is LIVE: ${res.post_url}`
        : `Post #${id} sent — publishing now, watching for the live URL…`);
      if (!res.post_url) void watchForUrl(id);
    } else {
      setNotice(`Post #${id} did not publish: ${res.error || res.status || 'unknown error'}`);
    }
    queue.refresh();
  });

  const reject = (id: number) => run(async () => {
    await apiPost('/api/social/reject', { post_id: id, reason: 'Rejected from dashboard' });
    setNotice(`Post #${id} rejected.`);
    queue.refresh();
  });

  const compose = () => run(async () => {
    const res = await apiPost<{ id?: number }>('/api/social/compose', {
      channel: composeChannel,
      title: composeTitle,
      body: composeBody,
    });
    setNotice(`Draft #${res.id} saved. It needs approval before it can publish.`);
    setComposeTitle('');
    setComposeBody('');
    queue.refresh();
  });

  if (status.loading && !status.data) {
    return <div class="flex h-full items-center justify-center"><Spinner /></div>;
  }
  if (status.error) {
    return <Empty title="Failed to load Social" description={status.error} />;
  }

  const queueCounts = status.data?.queue ?? {};
  const channelRows = channels.data?.channels ?? [];
  const integrations = channels.data?.postiz_integrations ?? [];
  const queueRows = queue.data?.posts ?? [];
  const remoteRows = postizPosts.data?.posts ?? [];

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Social"
        subtitle={configured
          ? `Postiz ${postiz.reachable ? (postiz.auth_ok ? 'connected' : 'auth failed') : 'unreachable'} · ${postiz.integrations_count ?? 0} channels · cadence ${status.data?.cadence_enabled ? 'on' : 'off'}`
          : 'Postiz not configured'}
        actions={(
          <div class="flex items-center gap-2">
            {configured && Boolean(status.data?.studio_url) && (
              <div class="flex overflow-hidden rounded-md border border-[var(--color-border)]">
                <button
                  type="button"
                  onClick={() => setView('queue')}
                  class={`px-3 py-1.5 text-[12px] transition-colors ${view === 'queue' ? 'bg-[var(--color-elevated)] text-[var(--color-text)]' : 'bg-[var(--color-card)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]'}`}
                >
                  Queue
                </button>
                <button
                  type="button"
                  onClick={() => setView('studio')}
                  class={`px-3 py-1.5 text-[12px] transition-colors ${view === 'studio' ? 'bg-[var(--color-elevated)] text-[var(--color-text)]' : 'bg-[var(--color-card)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]'}`}
                >
                  Studio
                </button>
              </div>
            )}
            <button
              type="button"
              onClick={() => { status.refresh(); channels.refresh(); queue.refresh(); postizPosts.refresh(); }}
              class="inline-flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[12px] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)]"
            >
              <RefreshCw size={14} />
              <span>Refresh</span>
            </button>
          </div>
        )}
      />

      {view === 'studio' && configured && Boolean(status.data?.studio_url) ? (
        <div class="flex-1 min-h-0 p-2">
          {/* The full Postiz studio (calendar, channels, analytics) embedded
              in-dashboard. Session cookie lives in the browser — log in once
              inside the frame and it sticks. */}
          <iframe
            src={status.data!.studio_url}
            title="Social Studio"
            class="h-full w-full rounded-lg border border-[var(--color-border)] bg-white"
          />
        </div>
      ) : (
      <div class="flex-1 overflow-y-auto p-4 md:p-6">
        <div class="mx-auto max-w-6xl space-y-4">
          {notice && (
            <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[12px] text-[var(--color-text)]">
              {notice}
            </div>
          )}

          {!configured && <OnboardingCard />}

          {configured && (
            <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              <Panel title="Postiz">
                {postiz.reachable
                  ? (postiz.auth_ok ? pill('ok', 'connected') : pill('bad', 'auth failed'))
                  : pill('bad', 'unreachable')}
                {postiz.error && (
                  <p class="mt-2 text-[12px] text-[var(--color-text-muted)]">{postiz.error}</p>
                )}
              </Panel>
              <Panel title="Connected channels">
                <div class="text-[20px] font-semibold">{postiz.integrations_count ?? 0}</div>
              </Panel>
              <Panel title="Queue drafts">
                <div class="text-[20px] font-semibold">{queueCounts['draft'] ?? 0}</div>
              </Panel>
              <Panel title="Cadence">
                {status.data?.cadence_enabled ? pill('ok', 'on') : pill('warn', 'off')}
              </Panel>
            </div>
          )}

          {configured && (
            <Panel
              title="Channels"
              actions={(
                <div class="flex items-center gap-2">
                  <select
                    value={provider}
                    onChange={(e) => setProvider((e.target as HTMLSelectElement).value)}
                    class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-2 py-1 text-[12px]"
                  >
                    {CONNECT_PROVIDERS.map((p) => <option key={p} value={p}>{p}</option>)}
                  </select>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={openConnectUrl}
                    class="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-2 py-1 text-[12px] text-[var(--color-text-muted)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)]"
                  >
                    <LinkIcon size={12} />
                    <span>Connect</span>
                  </button>
                </div>
              )}
            >
              {channels.data?.postiz_error && (
                <p class="mb-3 text-[12px] text-[var(--color-status-warn)]">{channels.data.postiz_error}</p>
              )}
              <div class="grid gap-2 md:grid-cols-2">
                {integrations.map((it) => (
                  <div key={it.id} class="flex min-w-0 items-center justify-between gap-2 rounded bg-[var(--color-elevated)] px-3 py-2">
                    <div class="min-w-0">
                      <div class="truncate text-[12px] font-medium text-[var(--color-text)]">{it.name || it.identifier}</div>
                      <div class="truncate font-mono text-[11px] text-[var(--color-text-faint)]">{it.identifier} · {it.id}</div>
                    </div>
                    {it.disabled ? pill('warn', 'disabled') : pill('ok', 'active')}
                  </div>
                ))}
                {integrations.length === 0 && !channels.data?.postiz_error && (
                  <p class="text-[12px] text-[var(--color-text-muted)]">No channels connected yet — use Connect above.</p>
                )}
              </div>
              <div class="mt-4 overflow-x-auto">
                <table class="w-full text-left text-[12px]">
                  <thead class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
                    <tr>
                      <th class="py-1 pr-3">Framework channel</th>
                      <th class="py-1 pr-3">Method</th>
                      <th class="py-1 pr-3">Cadence</th>
                      <th class="py-1">Postiz binding</th>
                    </tr>
                  </thead>
                  <tbody>
                    {channelRows.map((ch) => (
                      <tr key={ch.channel_id} class="border-t border-[var(--color-border)]">
                        <td class="py-1.5 pr-3 font-medium text-[var(--color-text)]">{ch.display_name}</td>
                        <td class="py-1.5 pr-3 font-mono">{ch.execution_method}</td>
                        <td class="py-1.5 pr-3">{ch.cadence_enabled ? 'on' : 'off'}</td>
                        <td class="py-1.5">
                          {ch.execution_method === 'postiz'
                            ? (ch.postiz_bound ? pill('ok', 'bound') : pill('warn', 'unbound'))
                            : <span class="text-[var(--color-text-faint)]">—</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          )}

          <Panel title="Approval queue">
            {queueRows.length === 0 && <p class="text-[12px] text-[var(--color-text-muted)]">Queue is empty.</p>}
            <div class="space-y-2">
              {queueRows.map((p) => (
                <div key={p.id} class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] p-3">
                  <div class="flex min-w-0 items-center justify-between gap-2">
                    <div class="min-w-0 truncate text-[12px] font-medium text-[var(--color-text)]">
                      #{p.id} · {p.channel} {p.title ? `· ${p.title}` : ''}
                    </div>
                    <div class="flex items-center gap-2">
                      {statusPill(p.status)}
                      {p.status === 'draft' && (
                        <>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => approve(p.id)}
                            class="inline-flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-[11px] text-[var(--color-status-done)] hover:bg-[var(--color-card)]"
                          >
                            <Check size={12} /> Approve & Post
                          </button>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => reject(p.id)}
                            class="inline-flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-[11px] text-[var(--color-status-failed)] hover:bg-[var(--color-card)]"
                          >
                            <X size={12} /> Reject
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                  <p class="mt-1 line-clamp-2 text-[12px] text-[var(--color-text-muted)]">{p.body}</p>
                  {p.error && <p class="mt-1 text-[11px] text-[var(--color-status-failed)]">{p.error}</p>}
                  {p.post_url && (
                    <a href={p.post_url} target="_blank" rel="noopener noreferrer" class="mt-1 block truncate text-[11px] text-[var(--color-accent,var(--color-text))] underline">
                      {p.post_url}
                    </a>
                  )}
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="Compose draft">
            <div class="grid gap-2 md:grid-cols-[180px_1fr]">
              <select
                value={composeChannel}
                onChange={(e) => setComposeChannel((e.target as HTMLSelectElement).value)}
                class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-2 py-1.5 text-[12px]"
              >
                <option value="">Select channel…</option>
                {channelRows.map((ch) => (
                  <option key={ch.channel_id} value={ch.channel_id}>{ch.display_name}</option>
                ))}
              </select>
              <input
                value={composeTitle}
                onInput={(e) => setComposeTitle((e.target as HTMLInputElement).value)}
                placeholder="Title (used by some platforms)"
                class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-2 py-1.5 text-[12px]"
              />
            </div>
            <textarea
              value={composeBody}
              onInput={(e) => setComposeBody((e.target as HTMLTextAreaElement).value)}
              placeholder="Post body…"
              rows={4}
              class="mt-2 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-2 py-1.5 text-[12px]"
            />
            <div class="mt-2 flex items-center justify-between gap-3">
              <p class="text-[11px] text-[var(--color-text-faint)]">
                Saves as a draft — nothing publishes without approval (default-deny).
              </p>
              <button
                type="button"
                disabled={busy || !composeChannel || !composeBody.trim()}
                onClick={compose}
                class="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[12px] text-[var(--color-text)] hover:bg-[var(--color-elevated)] disabled:opacity-50"
              >
                Save draft
              </button>
            </div>
          </Panel>

          {configured && (
            <Panel title="Postiz posts (±7 days)">
              {postizPosts.data?.postiz_error && (
                <p class="mb-2 text-[12px] text-[var(--color-status-warn)]">{postizPosts.data.postiz_error}</p>
              )}
              {remoteRows.length === 0 && !postizPosts.data?.postiz_error && (
                <p class="text-[12px] text-[var(--color-text-muted)]">No Postiz posts in the window.</p>
              )}
              <div class="space-y-2">
                {remoteRows.map((p) => (
                  <div key={p.id} class="flex min-w-0 items-center justify-between gap-2 rounded bg-[var(--color-elevated)] px-3 py-2">
                    <div class="min-w-0">
                      <div class="truncate text-[12px] text-[var(--color-text)]">
                        {(p.integration?.name || p.integration?.providerIdentifier || 'channel')} · {p.content || '(no text)'}
                      </div>
                      {p.releaseURL && (
                        <a href={p.releaseURL} target="_blank" rel="noopener noreferrer" class="block truncate text-[11px] underline">
                          {p.releaseURL}
                        </a>
                      )}
                    </div>
                    {p.state === 'PUBLISHED' ? pill('ok', 'published')
                      : p.state === 'ERROR' ? pill('bad', 'error')
                        : pill('warn', String(p.state || 'queued').toLowerCase())}
                  </div>
                ))}
              </div>
            </Panel>
          )}
        </div>
      </div>
      )}
    </div>
  );
}
