import { useEffect, useRef, useState } from 'preact/hooks';
import loader from '@monaco-editor/loader';
import { apiPatch } from '@/lib/api';
import { pushToast } from '@/lib/toasts';

interface FileEditorProps {
  agentId: string;
  filename: string;
  initialContent: string;
  language?: string;
  onSaved?: () => void;
}

/**
 * Monaco-based file editor for agent persona/config files.
 *
 * IMPORTANT — Q5 single-yaml-surface lock:
 * Monaco syntax-highlights YAML via its built-in TextMate grammar — that
 * is NOT a YAML parser. We DO NOT validate YAML client-side. The PATCH
 * posts raw text and the Python framework (which owns the canonical
 * personas/services.py YAML reader) is the single validator.
 *
 * If you're tempted to add `import yaml from 'js-yaml'` here for
 * "better error messages" — DON'T. The anti-patterns test will fail the
 * build, and you'll have created a second YAML parser that drifts from
 * Python's. Fast feedback comes from a quick PATCH round-trip, not
 * client-side validation.
 */
export function FileEditor({ agentId, filename, initialContent, language, onSaved }: FileEditorProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<any>(null);
  const [content, setContent] = useState(initialContent);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    let mounted = true;
    let editor: any;
    loader.init().then((monaco) => {
      if (!mounted || !containerRef.current) return;
      const lang = language || (filename.endsWith('.yaml') || filename.endsWith('.yml') ? 'yaml'
        : filename.endsWith('.md') ? 'markdown'
        : filename.endsWith('.json') ? 'json'
        : 'plaintext');
      editor = monaco.editor.create(containerRef.current, {
        value: initialContent,
        language: lang,
        theme: 'vs-dark',
        minimap: { enabled: false },
        fontSize: 12,
        wordWrap: 'on',
        scrollBeyondLastLine: false,
      });
      editor.onDidChangeModelContent(() => {
        const v = editor.getValue();
        setContent(v);
        setDirty(v !== initialContent);
      });
      editorRef.current = editor;
    }).catch(() => {
      pushToast({ tone: 'error', title: 'Editor failed to load', description: 'Monaco initialization error.' });
    });
    return () => {
      mounted = false;
      if (editor) editor.dispose();
    };
  }, [agentId, filename]);

  async function save() {
    setSaving(true);
    try {
      // PATCH posts raw text. Python validates (Q5 lock).
      await apiPatch(`/api/agents/${agentId}/files/${encodeURIComponent(filename)}`, { content });
      pushToast({ tone: 'success', title: `${filename} saved` });
      setDirty(false);
      onSaved?.();
    } catch (err: any) {
      pushToast({
        tone: 'error',
        title: `${filename} save failed`,
        description: err?.message || String(err),
        durationMs: 8000,
      });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div class="flex flex-col h-full">
      <div class="flex items-center justify-between px-3 py-2 border-b border-[var(--color-border)] bg-[var(--color-elevated)]">
        <div class="text-[12px] font-mono text-[var(--color-text)]">
          {filename}
          {dirty && <span class="ml-2 text-[var(--color-status-warn)]">·</span>}
        </div>
        <button
          type="button"
          onClick={save}
          disabled={!dirty || saving}
          class="px-3 py-1 rounded text-[11px] bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
      </div>
      <div ref={containerRef} class="flex-1 min-h-0" />
    </div>
  );
}
