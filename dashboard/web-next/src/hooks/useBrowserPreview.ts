import { useEffect, useState } from 'react';
import { apiGet, apiGetBlob } from '@/lib/api';
import type { BrowserViewerStatus } from '@/types';

export function useBrowserPreview(poll = true) {
  const [status, setStatus] = useState<BrowserViewerStatus | null>(null);
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let currentFrame: string | null = null;

    const refresh = async () => {
      try {
        const next = await apiGet<BrowserViewerStatus>(
          '/api/browser-viewer/status',
          controller.signal,
        );
        setStatus(next);
        if (next.readiness.cdp_reachable) {
          const blob = await apiGetBlob('/api/browser-viewer/screenshot', controller.signal);
          const nextUrl = URL.createObjectURL(blob);
          if (currentFrame) URL.revokeObjectURL(currentFrame);
          currentFrame = nextUrl;
          setFrameUrl(nextUrl);
        }
        setError(null);
      } catch (reason) {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    };

    void refresh();
    const interval = poll ? window.setInterval(() => void refresh(), 8_000) : null;
    return () => {
      controller.abort();
      if (interval !== null) window.clearInterval(interval);
      if (currentFrame) URL.revokeObjectURL(currentFrame);
    };
  }, [poll]);

  return { status, frameUrl, error };
}
