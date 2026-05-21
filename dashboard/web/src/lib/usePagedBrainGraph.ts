import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { apiGet, ApiError } from './api';
import type { BrainActivity, BrainGraphData, BrainGraphEdge, BrainGraphNode } from '@/components/BrainGraph2D';

export const BRAIN_GRAPH_PAGE_SIZE = 300;

interface PagedBrainGraphState {
  data: BrainGraphData | null;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => void;
  refresh: () => void;
}

export function usePagedBrainGraph(baseParams: URLSearchParams, enabled = true, pollMs = 0): PagedBrainGraphState {
  const pathKey = baseParams.toString();
  const [data, setData] = useState<BrainGraphData | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const activePathKeyRef = useRef(pathKey);

  useEffect(() => {
    activePathKeyRef.current = pathKey;
    if (!enabled) {
      setData(null);
      setLoading(false);
      setLoadingMore(false);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadingMore(false);
    fetchBrainGraphPage(pathKey, 0)
      .then((page) => {
        if (cancelled) return;
        setData(normalizeBrainGraphPage(page));
        setError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : String(e));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [enabled, pathKey, tick]);

  useEffect(() => {
    if (!enabled || !pollMs) return;
    const id = setInterval(() => setTick((value) => value + 1), pollMs);
    return () => clearInterval(id);
  }, [enabled, pollMs]);

  const page = getMemoryPage(data);
  const hasMore = Boolean(page?.has_more);
  const nextOffset = (page?.offset ?? 0) + (page?.returned_chunks ?? 0);

  function loadMore() {
    if (!enabled || !data || !hasMore || loadingMore) return;
    setLoadingMore(true);
    const requestPathKey = pathKey;
    fetchBrainGraphPage(pathKey, nextOffset)
      .then((pageData) => {
        if (activePathKeyRef.current !== requestPathKey) return;
        setData((current) => mergeBrainGraphPages(current, pageData));
        setError(null);
      })
      .catch((e) => {
        if (activePathKeyRef.current !== requestPathKey) return;
        setError(e instanceof ApiError ? e.message : String(e));
      })
      .finally(() => {
        if (activePathKeyRef.current === requestPathKey) {
          setLoadingMore(false);
        }
      });
  }

  return useMemo(() => ({
    data,
    loading,
    loadingMore,
    error,
    hasMore,
    loadMore,
    refresh: () => setTick((value) => value + 1),
  }), [data, loading, loadingMore, error, hasMore, nextOffset]);
}

function fetchBrainGraphPage(pathKey: string, offset: number): Promise<BrainGraphData> {
  const params = new URLSearchParams(pathKey);
  params.set('limit', String(BRAIN_GRAPH_PAGE_SIZE));
  params.set('offset', String(offset));
  return apiGet<BrainGraphData>(`/api/brain/graph?${params.toString()}`);
}

function normalizeBrainGraphPage(page: BrainGraphData): BrainGraphData {
  return withLoadedStats(page, page);
}

function mergeBrainGraphPages(current: BrainGraphData | null, next: BrainGraphData): BrainGraphData {
  if (!current) return normalizeBrainGraphPage(next);
  const nodes = dedupeNodes([...(current.nodes ?? []), ...(next.nodes ?? [])]);
  const edges = dedupeEdges([...(current.edges ?? []), ...(next.edges ?? [])]);
  const activity = dedupeActivity([...(current.activity ?? []), ...(next.activity ?? [])]);
  const scopes = new Set<string>([
    ...((current as any).layers?.scopes ?? []),
    ...((next as any).layers?.scopes ?? []),
  ]);
  return withLoadedStats({
    ...next,
    nodes,
    edges,
    activity,
    layers: {
      ...((current as any).layers ?? {}),
      ...((next as any).layers ?? {}),
      scopes: [...scopes],
    },
  }, next);
}

function withLoadedStats(merged: BrainGraphData, latestPage: BrainGraphData): BrainGraphData {
  const latestStats = latestPage.stats ?? {};
  const previousStats = merged.stats ?? {};
  const latestMemory = latestStats.memory ?? {};
  const previousMemory = previousStats.memory ?? {};
  const latestPageInfo = latestMemory.page ?? previousMemory.page;
  const loadedChunks = latestPageInfo
    ? (latestPageInfo.offset ?? 0) + (latestPageInfo.returned_chunks ?? 0)
    : undefined;
  return {
    ...merged,
    stats: {
      ...previousStats,
      ...latestStats,
      total_nodes: merged.nodes?.length ?? 0,
      total_edges: merged.edges?.length ?? 0,
      returned_nodes: merged.nodes?.length ?? 0,
      returned_edges: merged.edges?.length ?? 0,
      activity_count: merged.activity?.length ?? latestStats.activity_count,
      memory: {
        ...previousMemory,
        ...latestMemory,
        page: latestPageInfo ? {
          ...latestPageInfo,
          loaded_chunks: loadedChunks,
        } : undefined,
      },
    },
  };
}

function getMemoryPage(data: BrainGraphData | null) {
  return data?.stats?.memory?.page ?? data?.stats?.page ?? null;
}

function dedupeNodes(items: BrainGraphNode[]): BrainGraphNode[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function dedupeEdges(items: BrainGraphEdge[]): BrainGraphEdge[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function dedupeActivity(items: BrainActivity[]): BrainActivity[] {
  const seen = new Set<string>();
  return items.filter((item, index) => {
    const key = String(item.id ?? item.eventId ?? item.event_id ?? `activity-${index}`);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
