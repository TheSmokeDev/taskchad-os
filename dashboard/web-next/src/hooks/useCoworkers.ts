import { useCallback, useEffect, useState } from 'react';
import { apiGet } from '@/lib/api';
import type { Coworker } from '@/types';

export function useCoworkers() {
  const [coworkers, setCoworkers] = useState<Coworker[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const response = await apiGet<{ agents: Coworker[] }>('/api/agents', signal);
      setCoworkers(Array.isArray(response.agents) ? response.agents : []);
      setError(null);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return;
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  return { coworkers, loading, error, refresh };
}
