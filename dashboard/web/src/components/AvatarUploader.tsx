import { useState, useRef } from 'preact/hooks';
import { Upload, Trash2 } from 'lucide-preact';
import { apiPutForm, apiDelete } from '@/lib/api';
import { pushToast } from '@/lib/toasts';

interface AvatarUploaderProps {
  agentId: string;
  currentUrl?: string;
  onChange: () => void;
}

// Magic-byte hint check — server is canonical (Python validates), this is
// just UX to fail fast on obviously-wrong files. WS3's avatar route does
// the real anti-spoof validation.
async function fileLooksLikeImage(file: File): Promise<boolean> {
  const buf = await file.slice(0, 12).arrayBuffer();
  const bytes = new Uint8Array(buf);
  // PNG: 89 50 4E 47
  if (bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) return true;
  // JPEG: FF D8 FF
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return true;
  // GIF: 47 49 46 38
  if (bytes[0] === 0x47 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x38) return true;
  // WebP: 52 49 46 46 .. 57 45 42 50
  if (bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46 &&
      bytes[8] === 0x57 && bytes[9] === 0x45 && bytes[10] === 0x42 && bytes[11] === 0x50) return true;
  return false;
}

export function AvatarUploader({ agentId, currentUrl, onChange }: AvatarUploaderProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  async function handleFile(file: File) {
    if (file.size > 5 * 1024 * 1024) {
      pushToast({ tone: 'error', title: 'File too large', description: 'Max 5 MB' });
      return;
    }
    const looks = await fileLooksLikeImage(file);
    if (!looks) {
      pushToast({ tone: 'error', title: 'Not an image', description: 'PNG, JPEG, GIF, or WebP only.' });
      return;
    }
    setBusy(true);
    try {
      const form = new FormData();
      form.append('image', file);
      await apiPutForm(`/api/agents/${agentId}/avatar`, form);
      pushToast({ tone: 'success', title: 'Avatar updated' });
      onChange();
    } catch (err: any) {
      pushToast({ tone: 'error', title: 'Upload failed', description: err?.message || String(err) });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await apiDelete(`/api/agents/${agentId}/avatar`);
      pushToast({ tone: 'success', title: 'Avatar removed' });
      onChange();
    } catch (err: any) {
      pushToast({ tone: 'error', title: 'Remove failed', description: err?.message || String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="flex items-center gap-3">
      <div class="w-16 h-16 rounded-full bg-[var(--color-elevated)] flex items-center justify-center overflow-hidden">
        {currentUrl ? (
          <img src={currentUrl} alt={`${agentId} avatar`} class="w-full h-full object-cover" />
        ) : (
          <span class="text-[14px] text-[var(--color-text-faint)]">{agentId.slice(0, 2).toUpperCase()}</span>
        )}
      </div>
      <div class="flex flex-col gap-2">
        <input
          ref={inputRef}
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp"
          class="hidden"
          onChange={(e) => {
            const f = (e.target as HTMLInputElement).files?.[0];
            if (f) handleFile(f);
          }}
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
          class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-[12px] bg-[var(--color-elevated)] text-[var(--color-text)] border border-[var(--color-border)] hover:border-[var(--color-border-strong)] transition-colors disabled:opacity-40"
        >
          <Upload size={12} /> Upload
        </button>
        {currentUrl && (
          <button
            type="button"
            disabled={busy}
            onClick={remove}
            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-status-failed)] transition-colors disabled:opacity-40"
          >
            <Trash2 size={12} /> Remove
          </button>
        )}
      </div>
    </div>
  );
}
