import { useState } from 'preact/hooks';
import { useRoute } from 'wouter-preact';
import { TopBar } from '@/components/TopBar';
import { FileEditor } from '@/components/FileEditor';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { useFetch } from '@/lib/useFetch';

interface AgentFile {
  filename: string;
  content: string;
  language?: string;
}
interface AgentFilesResponse {
  agentId: string;
  files: AgentFile[];
}

export function AgentFiles() {
  const [, params] = useRoute('/agents/:id/files');
  const agentId = params?.id ?? '';
  const { data, loading, error, refresh } = useFetch<AgentFilesResponse>(
    agentId ? `/api/agents/${agentId}/files` : null
  );
  const [activeFile, setActiveFile] = useState<string | null>(null);

  if (!agentId) return <Empty title="No agent specified" />;
  if (loading) return <div class="flex items-center justify-center h-full"><Spinner size={20} /></div>;
  if (error) return <Empty title="Failed to load files" description={error} />;

  const files = data?.files ?? [];
  const current = files.find((f) => f.filename === activeFile) ?? files[0];

  return (
    <div class="flex flex-col h-full">
      <TopBar title={`${agentId} · files`} subtitle={`${files.length} file${files.length === 1 ? '' : 's'}`} />
      <div class="flex flex-1 overflow-hidden">
        <aside class="w-56 border-r border-[var(--color-border)] overflow-y-auto">
          {files.map((f) => (
            <button
              key={f.filename}
              type="button"
              onClick={() => setActiveFile(f.filename)}
              class={[
                'w-full text-left px-3 py-2 text-[12px] font-mono border-b border-[var(--color-border)]',
                f.filename === current?.filename
                  ? 'bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
                  : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-elevated)]',
              ].join(' ')}
            >
              {f.filename}
            </button>
          ))}
        </aside>
        <div class="flex-1 min-w-0 flex flex-col">
          {current ? (
            <FileEditor
              key={current.filename}
              agentId={agentId}
              filename={current.filename}
              initialContent={current.content}
              language={current.language}
              onSaved={refresh}
            />
          ) : (
            <Empty title="No files" />
          )}
        </div>
      </div>
    </div>
  );
}
