import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force-3d';
import { Activity, FileText, GitBranch, Minus, Plus, RotateCw, Search, X } from 'lucide-preact';
import { formatRelativeTime } from '@/lib/format';
import { renderMarkdown } from '@/lib/markdown';

export interface BrainGraphNode {
  id: string;
  label: string;
  kind: 'note' | 'chunk' | 'entity' | 'session' | 'decision' | string;
  scope_type: 'global' | 'persona' | 'agent' | 'team' | 'room' | string;
  scope_id: string;
  visibility?: string;
  source_path?: string;
  section_title?: string;
  text?: string;
  tags?: string[];
  created_at?: number;
  preview_source?: string;
  preview_chunk_count?: number;
}

export interface BrainGraphEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
  resolved?: boolean;
  mention_count?: number;
  source_field?: string;
}

export interface BrainActivity {
  id?: string | number;
  event_id?: string | number;
  eventId?: string | number;
  persona_id?: string;
  personaId?: string;
  agent_id?: string;
  agentId?: string;
  chat_id?: string;
  chatId?: string;
  session_id?: string;
  sessionId?: string;
  type?: string;
  event_type?: string;
  action?: string;
  role?: string;
  timestamp?: number | string;
  created_at?: number | string;
  createdAt?: number | string;
  details?: string;
  excerpt?: string;
  summary?: string;
  artifacts?: string | null;
  provider?: string | null;
  model?: string | null;
}

export interface BrainGraphData {
  nodes: BrainGraphNode[];
  edges: BrainGraphEdge[];
  activity?: BrainActivity[];
  layers?: {
    memory?: boolean;
    activity?: boolean;
    scopes?: string[];
  };
  stats?: {
    total_nodes?: number;
    total_edges?: number;
    returned_nodes?: number;
    returned_edges?: number;
    total_chunks?: number;
    activity_count?: number;
    page?: BrainGraphPage;
    vault_graph?: VaultGraphStats;
    scopes?: Array<{ scope_type: string; scope_id: string; count: number }>;
    memory?: {
      total_chunks?: number;
      matching_chunks?: number;
      returned_chunks?: number;
      page?: BrainGraphPage;
      vault_graph?: VaultGraphStats;
    };
    activity?: {
      total_events?: number;
    };
  };
}

export interface BrainGraphPage {
  limit?: number;
  offset?: number;
  returned_chunks?: number;
  matching_chunks?: number;
  loaded_chunks?: number;
  has_more?: boolean;
}

export interface VaultGraphStats {
  vault_notes?: number;
  vault_wikilink_edges?: number;
  vault_resolved_wikilink_edges?: number;
  vault_unresolved_wikilink_edges?: number;
  vault_wikilink_mentions?: number;
  vault_body_wikilink_edges?: number;
  vault_related_edges?: number;
  vault_property_wikilink_edges?: number;
}

interface Props {
  data: BrainGraphData;
  mode?: 'hive' | 'memory';
  agentFilter?: string;
  agentColors?: Record<string, string>;
  showActivity?: boolean;
  allowActivityToggle?: boolean;
  onShowActivityChange?: (value: boolean) => void;
  blurOn?: boolean;
}

interface Pt { x: number; y: number }

interface GraphLayout {
  positions: Map<string, Pt>;
  degreeByNodeId: Map<string, number>;
  width: number;
  height: number;
}

interface DragPhysicsState {
  id: string;
  pointerId: number;
  moved: boolean;
  activeIds: Set<string>;
  lastPositions: Map<string, Pt>;
}

interface NodePreview {
  text: string;
  derived: boolean;
  count: number;
  sourceLabel: string;
}

type Selection =
  | { type: 'node'; id: string }
  | { type: 'activity'; id: string };

const DEFAULT_VIEW_W = 1120;
const DEFAULT_VIEW_H = 680;
const MIN_WORLD_W = 1120;
const MIN_WORLD_H = 680;
const OBSIDIAN_BG = '#1f1f1f';
const OBSIDIAN_EDGE = '#5f6368';
const OBSIDIAN_NODE = '#b8b8b8';
const OBSIDIAN_NODE_HUB = '#d4d4d4';
const OBSIDIAN_SELECTED = '#ffffff';
const OBSIDIAN_ACCENT = '#8fbaff';

export function BrainGraph2D({
  data,
  mode = 'hive',
  agentFilter = 'all',
  agentColors = {},
  showActivity = true,
  allowActivityToggle = false,
  onShowActivityChange,
  blurOn = false,
}: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const dragRef = useRef<{ x: number; y: number; pan: Pt; viewW: number; viewH: number } | null>(null);
  const nodeDragRef = useRef<DragPhysicsState | null>(null);
  const nodePositionOverridesRef = useRef<Record<string, Pt>>({});
  const livePhysicsPositionsRef = useRef<Record<string, Pt>>({});
  const settleFrameRef = useRef<number | null>(null);
  const suppressNodeClickRef = useRef<string | null>(null);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [query, setQuery] = useState('');
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState<Pt>({ x: 0, y: 0 });
  const [nodePositionOverrides, setNodePositionOverrides] = useState<Record<string, Pt>>({});
  const [livePhysicsPositions, setLivePhysicsPositions] = useState<Record<string, Pt>>({});
  const [nodeListLimit, setNodeListLimit] = useState(80);
  const [activityListLimit, setActivityListLimit] = useState(60);
  const [edgeKindFilter, setEdgeKindFilter] = useState('all');
  const [focusedNodeId, setFocusedNodeId] = useState<string | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null);

  const nodes = data.nodes ?? [];
  const edges = data.edges ?? [];
  const layout = useMemo(() => layoutNodes(nodes, edges), [nodes, edges]);
  const positions = useMemo(() => {
    const next = new Map(layout.positions);
    for (const [id, pt] of Object.entries(nodePositionOverrides)) {
      if (next.has(id)) next.set(id, pt);
    }
    for (const [id, pt] of Object.entries(livePhysicsPositions)) {
      if (next.has(id)) next.set(id, pt);
    }
    return next;
  }, [layout.positions, nodePositionOverrides, livePhysicsPositions]);
  const degreeByNodeId = layout.degreeByNodeId;
  const previewByNodeId = useMemo(() => buildNodePreviews(nodes, edges), [nodes, edges]);
  const normalizedActivity = useMemo(() => normalizeActivities(data.activity ?? []), [data.activity]);
  const nodeById = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const edgeKinds = useMemo(() => uniqueEdgeKinds(edges), [edges]);
  const hasUnresolvedEdges = useMemo(() => edges.some((edge) => isUnresolvedEdge(edge, nodeById)), [edges, nodeById]);
  const edgeFilterItems = useMemo(
    () => ['all', ...edgeKinds, ...(hasUnresolvedEdges ? ['unresolved'] : [])],
    [edgeKinds, hasUnresolvedEdges],
  );
  const kindFilteredEdges = useMemo(() => edges
    .filter((edge) => positions.has(edge.source) && positions.has(edge.target))
    .filter((edge) => edgeMatchesFilter(edge, edgeKindFilter, nodeById)), [edges, positions, edgeKindFilter, nodeById]);
  const queryText = query.trim().toLowerCase();
  const selectedNodeId = selected?.type === 'node' ? selected.id : null;
  const activeNodeId = draggingNodeId ?? hoveredNodeId ?? selectedNodeId;
  const neighborhoodIds = useMemo(() => {
    if (!focusedNodeId) return null;
    const ids = new Set<string>([focusedNodeId]);
    for (const edge of kindFilteredEdges) {
      if (edge.source === focusedNodeId) ids.add(edge.target);
      if (edge.target === focusedNodeId) ids.add(edge.source);
    }
    return ids;
  }, [kindFilteredEdges, focusedNodeId]);

  useEffect(() => {
    setNodeListLimit(80);
    setActivityListLimit(60);
  }, [queryText, agentFilter, focusedNodeId, edgeKindFilter]);

  useEffect(() => {
    if (!edgeFilterItems.includes(edgeKindFilter)) {
      setEdgeKindFilter('all');
    }
  }, [edgeFilterItems, edgeKindFilter]);

  useEffect(() => {
    cancelSettleFrame();
    nodePositionOverridesRef.current = {};
    livePhysicsPositionsRef.current = {};
    setNodePositionOverrides({});
    setLivePhysicsPositions({});
  }, [nodes.length, edges.length]);

  useEffect(() => () => cancelSettleFrame(), []);

  const visibleNodes = useMemo(() => {
    const queried = queryText ? nodes.filter((node) => nodeMatchesQuery(node, queryText)) : nodes;
    if (!neighborhoodIds) return queried;
    return queried.filter((node) => neighborhoodIds.has(node.id));
  }, [nodes, queryText, neighborhoodIds]);
  const visibleNodeIds = useMemo(() => new Set(visibleNodes.map((node) => node.id)), [visibleNodes]);
  const visibleEdges = useMemo(() => kindFilteredEdges
    .filter((edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)), [kindFilteredEdges, visibleNodeIds]);
  const visibleActivity = useMemo(() => normalizedActivity
    .filter((item) => agentFilter === 'all' || item.agent_id === agentFilter)
    .filter((item) => !queryText || item.summary.toLowerCase().includes(queryText) || item.action.toLowerCase().includes(queryText)), [normalizedActivity, agentFilter, queryText]);
  const activityPositions = useMemo(
    () => placeActivity(visibleActivity, nodes, positions),
    [visibleActivity, nodes, positions],
  );

  const selectedNode = selected?.type === 'node'
    ? nodes.find((node) => node.id === selected.id) ?? null
    : null;
  const selectedActivity = selected?.type === 'activity'
    ? visibleActivity.find((item) => item.id === selected.id) ?? null
    : null;
  const activeNeighborIds = useMemo(() => {
    if (!activeNodeId) return new Set<string>();
    const ids = new Set<string>([activeNodeId]);
    for (const edge of kindFilteredEdges) {
      if (edge.source === activeNodeId) ids.add(edge.target);
      if (edge.target === activeNodeId) ids.add(edge.source);
    }
    return ids;
  }, [kindFilteredEdges, activeNodeId]);
  const fallbackNode = visibleNodes[0] ?? nodes[0] ?? null;
  const panelNode = selectedNode ?? (!selectedActivity ? fallbackNode : null);
  const panelRelationships = useMemo(
    () => panelNode ? nodeRelationships(panelNode.id, edges, nodeById) : [],
    [panelNode?.id, edges, nodeById],
  );
  const filteredPanelRelationships = useMemo(() => {
    if (edgeKindFilter === 'all') return panelRelationships;
    return panelRelationships.filter((relationship) => relationshipMatchesFilter(relationship, edgeKindFilter));
  }, [panelRelationships, edgeKindFilter]);
  const graphPage = data.stats?.memory?.page ?? data.stats?.page;
  const vaultGraphStats = data.stats?.memory?.vault_graph ?? data.stats?.vault_graph;
  const matchingChunks = graphPage?.matching_chunks ?? data.stats?.memory?.matching_chunks ?? data.stats?.total_chunks;
  const loadedChunks = graphPage?.loaded_chunks ?? graphPage?.returned_chunks;
  const nodeRows = visibleNodes.slice(0, nodeListLimit);
  const activityRows = visibleActivity.slice(0, activityListLimit);

  const view = useMemo(() => {
    const clampedZoom = clamp(zoom, 0.65, 2.4);
    const w = Math.min(layout.width, DEFAULT_VIEW_W / clampedZoom);
    const h = Math.min(layout.height, DEFAULT_VIEW_H / clampedZoom);
    const baseX = Math.max(0, (layout.width - w) / 2);
    const baseY = Math.max(0, (layout.height - h) / 2);
    return {
      x: clamp(baseX + pan.x, 0, Math.max(0, layout.width - w)),
      y: clamp(baseY + pan.y, 0, Math.max(0, layout.height - h)),
      w,
      h,
    };
  }, [zoom, pan, layout.width, layout.height]);

  useEffect(() => {
    if (selected?.type === 'activity') return;
    if (selected?.type === 'node' && nodeById.has(selected.id)) return;
    if (fallbackNode) {
      setSelected({ type: 'node', id: fallbackNode.id });
    }
  }, [selected?.type, selected?.id, nodeById, fallbackNode?.id]);

  function setZoomClamped(next: number) {
    setZoom(clamp(next, 0.65, 2.4));
  }

  function resetViewport() {
    cancelSettleFrame();
    setZoom(1);
    setPan({ x: 0, y: 0 });
    setFocusedNodeId(null);
    setDraggingNodeId(null);
    nodePositionOverridesRef.current = {};
    livePhysicsPositionsRef.current = {};
    setNodePositionOverrides({});
    setLivePhysicsPositions({});
  }

  function centerOnNode(id: string) {
    const pt = positions.get(id);
    if (!pt) return;
    const clampedZoom = clamp(zoom, 0.65, 2.4);
    const w = Math.min(layout.width, DEFAULT_VIEW_W / clampedZoom);
    const h = Math.min(layout.height, DEFAULT_VIEW_H / clampedZoom);
    const baseX = Math.max(0, (layout.width - w) / 2);
    const baseY = Math.max(0, (layout.height - h) / 2);
    setPan({
      x: clamp(pt.x - w / 2 - baseX, -baseX, Math.max(0, layout.width - w) - baseX),
      y: clamp(pt.y - h / 2 - baseY, -baseY, Math.max(0, layout.height - h) - baseY),
    });
  }

  function selectNode(id: string, center = true) {
    setSelected({ type: 'node', id });
    if (center) centerOnNode(id);
  }

  function selectActivity(id: string) {
    setSelected({ type: 'activity', id });
  }

  function eventToGraphPoint(event: any): Pt {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const rect = svg.getBoundingClientRect();
    return {
      x: view.x + ((event.clientX - rect.left) / Math.max(rect.width, 1)) * view.w,
      y: view.y + ((event.clientY - rect.top) / Math.max(rect.height, 1)) * view.h,
    };
  }

  function handlePointerDown(event: any) {
    const svg = svgRef.current;
    if (!svg) return;
    if (nodeDragRef.current) return;
    dragRef.current = {
      x: event.clientX,
      y: event.clientY,
      pan,
      viewW: view.w,
      viewH: view.h,
    };
    svg.setPointerCapture?.(event.pointerId);
  }

  function handleNodePointerDown(event: any, node: BrainGraphNode) {
    const svg = svgRef.current;
    if (!svg) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    cancelSettleFrame();
    const activeIds = buildDragNeighborhood(node.id, visibleEdges);
    const seededPositions = seedPhysicsPositions(activeIds, positions, layout.positions, nodePositionOverridesRef.current);
    nodeDragRef.current = {
      id: node.id,
      pointerId: event.pointerId,
      moved: false,
      activeIds,
      lastPositions: seededPositions,
    };
    commitLivePhysicsPositions(seededPositions);
    setDraggingNodeId(node.id);
    setSelected({ type: 'node', id: node.id });
    svg.setPointerCapture?.(event.pointerId);
  }

  function handlePointerMove(event: any) {
    const svg = svgRef.current;
    const nodeDrag = nodeDragRef.current;
    if (svg && nodeDrag) {
      const pt = eventToGraphPoint(event);
      const radius = nodeRadius(nodeById.get(nodeDrag.id) ?? { id: nodeDrag.id, label: nodeDrag.id, kind: 'chunk', scope_type: 'global', scope_id: 'main' }, degreeByNodeId.get(nodeDrag.id) ?? 0);
      const clampedPoint = {
        x: clamp(pt.x, radius + 12, layout.width - radius - 12),
        y: clamp(pt.y, radius + 12, layout.height - radius - 12),
      };
      nodeDrag.moved = true;
      const nextOverrides = {
        ...nodePositionOverridesRef.current,
        [nodeDrag.id]: clampedPoint,
      };
      nodePositionOverridesRef.current = nextOverrides;
      setNodePositionOverrides(nextOverrides);
      const nextLivePositions = relaxPhysicsNeighborhood({
        activeIds: nodeDrag.activeIds,
        currentPositions: nodeDrag.lastPositions,
        basePositions: layout.positions,
        fixedPositions: nextOverrides,
        edges: visibleEdges,
        nodesById: nodeById,
        degreeByNodeId,
        width: layout.width,
        height: layout.height,
        iterations: 2,
      });
      nodeDrag.lastPositions = nextLivePositions;
      commitLivePhysicsPositions(nextLivePositions);
      return;
    }
    const drag = dragRef.current;
    if (!svg || !drag) return;
    const rect = svg.getBoundingClientRect();
    const dx = ((event.clientX - drag.x) / Math.max(rect.width, 1)) * drag.viewW;
    const dy = ((event.clientY - drag.y) / Math.max(rect.height, 1)) * drag.viewH;
    setPan({
      x: drag.pan.x - dx,
      y: drag.pan.y - dy,
    });
  }

  function handlePointerUp(event: any) {
    const nodeDrag = nodeDragRef.current;
    if (nodeDrag) {
      if (typeof event.pointerId === 'number' && event.pointerId !== nodeDrag.pointerId) {
        return;
      }
      if (nodeDrag.moved) {
        suppressNodeClickRef.current = nodeDrag.id;
        releaseNodeOverride(nodeDrag.id);
        startPhysicsSettle(nodeDrag.activeIds, nodeDrag.lastPositions);
      }
      nodeDragRef.current = null;
      setDraggingNodeId(null);
      releasePointerCaptureSafe(event.pointerId);
      return;
    }
    dragRef.current = null;
    releasePointerCaptureSafe(event.pointerId);
  }

  function handlePointerLeave(event: any) {
    if (nodeDragRef.current) return;
    handlePointerUp(event);
  }

  useEffect(() => {
    const releaseActiveDrag = (event: PointerEvent | MouseEvent) => {
      if (!nodeDragRef.current && !dragRef.current) return;
      handlePointerUp(event);
    };
    window.addEventListener('pointerup', releaseActiveDrag);
    window.addEventListener('pointercancel', releaseActiveDrag);
    window.addEventListener('mouseup', releaseActiveDrag);
    return () => {
      window.removeEventListener('pointerup', releaseActiveDrag);
      window.removeEventListener('pointercancel', releaseActiveDrag);
      window.removeEventListener('mouseup', releaseActiveDrag);
    };
  }, [layout.positions, layout.width, layout.height, visibleEdges, nodeById, degreeByNodeId]);

  function handleWheel(event: any) {
    event.preventDefault?.();
    if (event.deltaY === 0) return;
    setZoomClamped(zoom + (event.deltaY < 0 ? 0.12 : -0.12));
  }

  function commitLivePhysicsPositions(nextPositions: Map<string, Pt>) {
    const nextObject = Object.fromEntries(nextPositions.entries());
    livePhysicsPositionsRef.current = nextObject;
    setLivePhysicsPositions(nextObject);
  }

  function releaseNodeOverride(id: string) {
    if (!nodePositionOverridesRef.current[id]) return;
    const { [id]: _released, ...remaining } = nodePositionOverridesRef.current;
    nodePositionOverridesRef.current = remaining;
    setNodePositionOverrides(remaining);
  }

  function releasePointerCaptureSafe(pointerId: unknown) {
    if (typeof pointerId !== 'number') return;
    svgRef.current?.releasePointerCapture?.(pointerId);
  }

  function cancelSettleFrame() {
    if (settleFrameRef.current === null) return;
    cancelAnimationFrameSafe(settleFrameRef.current);
    settleFrameRef.current = null;
  }

  function startPhysicsSettle(activeIds: Set<string>, seedPositions: Map<string, Pt>) {
    cancelSettleFrame();
    let frame = 0;
    let currentPositions = seedPositions;
    const step = () => {
      frame += 1;
      currentPositions = relaxPhysicsNeighborhood({
        activeIds,
        currentPositions,
        basePositions: layout.positions,
        fixedPositions: nodePositionOverridesRef.current,
        edges: visibleEdges,
        nodesById: nodeById,
        degreeByNodeId,
        width: layout.width,
        height: layout.height,
        iterations: 1,
      });
      commitLivePhysicsPositions(currentPositions);
      if (frame < 34) {
        settleFrameRef.current = requestAnimationFrameSafe(step);
      } else {
        settleFrameRef.current = null;
      }
    };
    settleFrameRef.current = requestAnimationFrameSafe(step);
  }

  const graphLabel = mode === 'memory' ? 'Homie memory graph' : 'Homie brain graph';

  return (
    <div class="flex-1 h-full min-h-0 grid grid-cols-1 grid-rows-[minmax(320px,1fr)_minmax(300px,0.82fr)] md:grid-rows-none md:grid-cols-[minmax(390px,1fr)_300px] xl:grid-cols-[minmax(0,1fr)_390px]">
      <div class="min-h-0 overflow-hidden bg-[var(--color-bg)]">
        <div class="h-full min-h-[320px] p-3">
          <div class="h-full border border-[#333] bg-[#1f1f1f] rounded-md overflow-hidden">
            <svg
              ref={svgRef}
              viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
              role="img"
              aria-label={graphLabel}
              class={`w-full h-full block touch-none select-none ${draggingNodeId ? 'cursor-grabbing' : 'cursor-grab'}`}
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              onPointerLeave={handlePointerLeave}
              onPointerCancel={handlePointerUp}
              onLostPointerCapture={handlePointerUp}
              onWheel={handleWheel}
            >
              <rect width={layout.width} height={layout.height} fill={OBSIDIAN_BG} />
              <g>
                {visibleEdges.map((edge) => {
                  const source = positions.get(edge.source)!;
                  const target = positions.get(edge.target)!;
                  const selectedEdge = Boolean(activeNodeId && (edge.source === activeNodeId || edge.target === activeNodeId));
                  const draggingEdge = Boolean(draggingNodeId && (edge.source === draggingNodeId || edge.target === draggingNodeId));
                  const isSource = edge.kind === 'source';
                  const unresolvedEdge = isUnresolvedEdge(edge, nodeById);
                  return (
                    <line
                      key={edge.id}
                      x1={source.x}
                      y1={source.y}
                      x2={target.x}
                      y2={target.y}
                      stroke={edgeStroke(edge, {
                        dragging: draggingEdge,
                        selected: selectedEdge,
                        unresolved: unresolvedEdge,
                      })}
                      stroke-width={draggingEdge ? 2.15 : selectedEdge ? 1.8 : isSource ? 0.72 : 0.95}
                      stroke-dasharray={unresolvedEdge ? '4 5' : undefined}
                      stroke-linecap="round"
                      opacity={activeNodeId ? (selectedEdge ? 0.9 : 0.1) : (unresolvedEdge ? 0.42 : isSource ? 0.18 : 0.28)}
                    />
                  );
                })}
              </g>
              <g>
                {visibleNodes.map((node) => {
                  const pt = positions.get(node.id);
                  if (!pt) return null;
                  const selectedNode = selectedNodeId === node.id;
                  const draggingNode = draggingNodeId === node.id;
                  const degree = degreeByNodeId.get(node.id) ?? 0;
                  const relatedToSelection = activeNeighborIds.has(node.id);
                  const dimmed = Boolean(activeNodeId && !relatedToSelection);
                  const radius = nodeRadius(node, degree);
                  const displayRadius = draggingNode ? radius + 1.4 : radius;
                  const showLabel = selectedNode || draggingNode;
                  return (
                    <g
                      key={node.id}
                      role="button"
                      tabindex={0}
                      aria-label={`Open memory node ${node.label}`}
                      data-graph-node-id={node.id}
                      data-selected={selectedNode ? 'true' : 'false'}
                      onPointerDown={(event) => handleNodePointerDown(event, node)}
                      onPointerEnter={() => setHoveredNodeId(node.id)}
                      onPointerLeave={() => setHoveredNodeId((current) => current === node.id ? null : current)}
                      onClick={(event) => {
                        event.stopPropagation();
                        if (suppressNodeClickRef.current === node.id) {
                          suppressNodeClickRef.current = null;
                          return;
                        }
                        selectNode(node.id, false);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') selectNode(node.id, false);
                      }}
                      class="cursor-pointer"
                    >
                      <circle
                        cx={pt.x}
                        cy={pt.y}
                        r={Math.max(radius + 8, 11)}
                        fill="transparent"
                        stroke="transparent"
                        pointer-events="all"
                      />
                      {(selectedNode || draggingNode) && (
                        <>
                          <circle
                            cx={pt.x}
                            cy={pt.y}
                            r={radius + (draggingNode ? 14 : 11)}
                            fill={draggingNode ? OBSIDIAN_ACCENT : OBSIDIAN_SELECTED}
                            opacity={draggingNode ? '0.18' : '0.12'}
                          />
                          <circle
                            cx={pt.x}
                            cy={pt.y}
                            r={radius + (draggingNode ? 7.2 : 5.8)}
                            fill="none"
                            stroke={draggingNode ? OBSIDIAN_ACCENT : OBSIDIAN_SELECTED}
                            stroke-width={draggingNode ? 2.8 : 2.2}
                            opacity="0.95"
                          />
                        </>
                      )}
                      <circle
                        cx={pt.x}
                        cy={pt.y}
                        r={displayRadius}
                        fill={draggingNode ? OBSIDIAN_SELECTED : nodeFill(node, degree, selectedNode, relatedToSelection)}
                        opacity={dimmed ? '0.28' : node.kind === 'chunk' ? '0.82' : '0.98'}
                        stroke={draggingNode ? OBSIDIAN_ACCENT : selectedNode ? OBSIDIAN_SELECTED : relatedToSelection ? OBSIDIAN_ACCENT : 'rgba(255,255,255,0.16)'}
                        stroke-width={draggingNode ? 3.1 : selectedNode ? 2.8 : relatedToSelection ? 1.7 : 0.6}
                      />
                      {showLabel && (
                        <text
                          x={pt.x + radius + 5}
                          y={pt.y + 4}
                          fill={selectedNode ? OBSIDIAN_SELECTED : '#bbbbbb'}
                          font-size={selectedNode ? 11.5 : 9}
                          pointer-events="none"
                        >
                          {truncateLabel(node.label, selectedNode ? 34 : 20)}
                        </text>
                      )}
                      <title>{node.label}</title>
                    </g>
                  );
                })}
              </g>
              {showActivity && (
                <g>
                  {visibleActivity.map((item, index) => {
                    const pt = activityPositions.get(item.id);
                    if (!pt) return null;
                    const color = agentColors[item.agent_id] || paletteColor(item.agent_id);
                    const selectedItem = selected?.type === 'activity' && selected.id === item.id;
                    return (
                      <g
                        key={item.id}
                        role="button"
                        tabindex={0}
                        aria-label={`Open activity event ${item.summary}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          selectActivity(item.id);
                        }}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') selectActivity(item.id);
                        }}
                        class="cursor-pointer"
                      >
                        <circle
                          class="brain-activity-dot"
                          cx={pt.x}
                          cy={pt.y}
                          r={selectedItem ? 7 : 5}
                          fill={color}
                          opacity="0.95"
                          stroke={selectedItem ? '#ffffff' : 'var(--color-card)'}
                          stroke-width={selectedItem ? 2.4 : 1.4}
                        />
                        <circle
                          cx={pt.x}
                          cy={pt.y}
                          r={12 + (index % 4)}
                          fill={color}
                          opacity="0.14"
                          class="brain-dot-pulse"
                        />
                        <title>{item.summary}</title>
                      </g>
                    );
                  })}
                </g>
              )}
            </svg>
          </div>
        </div>
      </div>

      <aside class="min-h-0 border-t md:border-t-0 md:border-l border-[var(--color-border)] bg-[var(--color-bg)] grid grid-rows-[auto_minmax(185px,0.52fr)_minmax(160px,0.48fr)]">
        <div class="px-3 py-2 border-b border-[var(--color-border)] space-y-2">
          <div class="grid grid-cols-3 gap-2">
            <Stat label="Nodes" value={String(data.stats?.total_nodes ?? nodes.length)} />
            <Stat label="Links" value={String(data.stats?.total_edges ?? edges.length)} />
            <Stat
              label={mode === 'hive' ? 'Activity' : 'Chunks'}
              value={String(mode === 'hive' ? (data.stats?.activity_count ?? visibleActivity.length) : (data.stats?.total_chunks ?? data.stats?.memory?.total_chunks ?? 0))}
            />
          </div>
          <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1 text-[10.5px] text-[var(--color-text-muted)] leading-snug">
            <span class="sr-only">
              Rendering {visibleNodes.length}/{nodes.length} loaded nodes and {visibleEdges.length}/{edges.length} loaded links.
            </span>
            <div class="truncate">
              {visibleNodes.length}/{nodes.length} nodes · {visibleEdges.length}/{edges.length} links
            </div>
            {typeof matchingChunks === 'number' && (
              <>
                <span class="sr-only">
                  Loaded {loadedChunks ?? nodes.length}/{matchingChunks} matching memory chunks{graphPage?.has_more ? '; more pages available.' : '.'}
                </span>
                <div class="truncate">
                  {loadedChunks ?? nodes.length}/{matchingChunks} chunks loaded{graphPage?.has_more ? ' · more pages available' : ''}
                </div>
              </>
            )}
            {vaultGraphStats && typeof vaultGraphStats.vault_notes === 'number' && (
              <>
                <div class="truncate">
                  {vaultGraphStats.vault_notes} vault notes · {vaultGraphStats.vault_resolved_wikilink_edges ?? vaultGraphStats.vault_wikilink_edges ?? 0} resolved links
                  {vaultGraphStats.vault_unresolved_wikilink_edges ? ` · ${vaultGraphStats.vault_unresolved_wikilink_edges} broken` : ''}
                </div>
                {(vaultGraphStats.vault_related_edges || vaultGraphStats.vault_body_wikilink_edges || vaultGraphStats.vault_property_wikilink_edges) && (
                  <div class="truncate">
                    {vaultGraphStats.vault_related_edges ?? 0} related · {vaultGraphStats.vault_body_wikilink_edges ?? 0} body · {vaultGraphStats.vault_property_wikilink_edges ?? 0} property
                  </div>
                )}
              </>
            )}
            {focusedNodeId && (
              <div>
                Neighborhood focus is active.
              </div>
            )}
          </div>
          <div class="flex items-center gap-2">
            <div class="relative flex-1 min-w-0">
              <Search size={12} class="absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-faint)]" />
              <input
                value={query}
                onInput={(event) => setQuery((event.target as HTMLInputElement).value)}
                placeholder="Search graph..."
                class="w-full pl-7 pr-2.5 py-1 rounded bg-[var(--color-elevated)] border border-[var(--color-border)] focus:border-[var(--color-accent)] focus:outline-none text-[12px] text-[var(--color-text)]"
              />
            </div>
            {allowActivityToggle && (
              <button
                type="button"
                title="Activity overlay"
                aria-pressed={showActivity}
                onClick={() => onShowActivityChange?.(!showActivity)}
                class={[
                  'inline-flex items-center justify-center w-7 h-7 rounded border transition-colors shrink-0',
                  showActivity
                    ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
                    : 'border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
                ].join(' ')}
              >
                <Activity size={14} />
              </button>
            )}
          </div>
          <div class="flex items-center gap-1.5">
            <IconButton title="Zoom out" onClick={() => setZoomClamped(zoom - 0.15)}><Minus size={13} /></IconButton>
            <input
              type="range"
              class="brain-slider flex-1"
              min={0.65}
              max={2.4}
              step={0.05}
              value={zoom}
              aria-label="Graph zoom"
              onInput={(event) => setZoomClamped(parseFloat((event.target as HTMLInputElement).value))}
            />
            <IconButton title="Zoom in" onClick={() => setZoomClamped(zoom + 0.15)}><Plus size={13} /></IconButton>
            <IconButton title="Reset view and dragged nodes" onClick={resetViewport}><RotateCw size={13} /></IconButton>
          </div>
          <div class="flex items-center gap-1.5 min-w-0">
            <div class="flex items-center gap-1 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] shrink-0">
              <GitBranch size={11} />
              Edge
            </div>
            <div class="flex flex-wrap gap-1">
              {edgeFilterItems.map((kind) => (
                <button
                  key={kind}
                  type="button"
                  aria-pressed={edgeKindFilter === kind}
                  aria-label={`Show ${edgeKindLabel(kind)} relationships`}
                  onClick={() => setEdgeKindFilter(kind)}
                  class={[
                    'px-1.5 py-0.5 rounded border text-[10.5px] transition-colors',
                    edgeKindFilter === kind
                      ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
                      : 'border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
                  ].join(' ')}
                >
                  {edgeKindLabel(kind)}
                </button>
              ))}
            </div>
          </div>
          {focusedNodeId && (
            <button
              type="button"
              onClick={() => setFocusedNodeId(null)}
              class="w-full inline-flex items-center justify-center gap-1.5 px-2.5 py-1.5 rounded border border-[var(--color-border)] bg-[var(--color-elevated)] text-[11.5px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
            >
              <X size={12} />
              Clear neighborhood focus
            </button>
          )}
        </div>

        <div class="min-h-0 overflow-y-auto border-b border-[var(--color-border)] bg-[var(--color-card)]/30">
          {selectedActivity && <ActivityDetail entry={selectedActivity} color={agentColors[selectedActivity.agent_id] || paletteColor(selectedActivity.agent_id)} blurOn={blurOn} />}
          {panelNode && !selectedActivity && (
            <MemoryNodeDetail
              node={panelNode}
              preview={previewByNodeId.get(panelNode.id)}
              degree={degreeByNodeId.get(panelNode.id) ?? 0}
              relationships={filteredPanelRelationships}
              totalRelationshipCount={panelRelationships.length}
              edgeKindFilter={edgeKindFilter}
              focused={focusedNodeId === panelNode.id}
              onFocusNeighborhood={() => {
                setFocusedNodeId(panelNode.id);
                centerOnNode(panelNode.id);
              }}
              onClearFocus={() => setFocusedNodeId(null)}
              onSelectNode={selectNode}
            />
          )}
        </div>

        <div class="min-h-0 overflow-y-auto">
          <div class="px-4 py-3 border-t border-[var(--color-border)]">
            <div class="flex items-center justify-between gap-2 mb-2">
              <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Nodes</div>
              <div class="text-[10.5px] text-[var(--color-text-faint)] tabular-nums">{nodeRows.length}/{visibleNodes.length}</div>
            </div>
            <div class="space-y-1.5">
              {nodeRows.map((node) => (
                <button
                  key={node.id}
                  type="button"
                  aria-label={`Open memory node ${node.label}`}
                  aria-pressed={selectedNodeId === node.id}
                  onClick={() => selectNode(node.id)}
                  class={[
                    'w-full text-left px-2.5 py-2 rounded border transition-colors',
                    selectedNodeId === node.id
                      ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] shadow-[inset_3px_0_0_var(--color-accent)]'
                      : 'border-[var(--color-border)] bg-[var(--color-elevated)] hover:border-[var(--color-text-faint)]',
                  ].join(' ')}
                >
                  <div class="flex items-center gap-2">
                    <span class="w-2 h-2 rounded-full shrink-0" style={{ background: scopeColor(node.scope_type) }} />
                    <span class="text-[12px] text-[var(--color-text)] truncate">{node.label}</span>
                    {selectedNodeId === node.id && (
                      <span class="ml-auto text-[9.5px] uppercase tracking-wider text-[var(--color-accent)]">Selected</span>
                    )}
                  </div>
                  <div class="text-[10.5px] text-[var(--color-text-faint)] mt-0.5 truncate">
                    {node.scope_type}/{node.scope_id} - {node.kind}
                  </div>
                  {previewByNodeId.get(node.id)?.text && (node.kind === 'note' || previewByNodeId.get(node.id)?.derived) && (
                    <div class="text-[10.5px] text-[var(--color-text-muted)] mt-1 truncate">
                      Preview: {previewSnippet(previewByNodeId.get(node.id)?.text)}
                    </div>
                  )}
                </button>
              ))}
              {visibleNodes.length > nodeRows.length && (
                <button
                  type="button"
                  onClick={() => setNodeListLimit((value) => value + 80)}
                  class="w-full px-2.5 py-2 rounded border border-[var(--color-border)] bg-[var(--color-card)] text-[11px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
                >
                  Show more nodes
                </button>
              )}
            </div>
          </div>
          {showActivity && visibleActivity.length > 0 && (
            <div class="px-4 py-3 border-t border-[var(--color-border)]">
              <div class="flex items-center justify-between gap-2 mb-2">
                <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Activity</div>
                <div class="text-[10.5px] text-[var(--color-text-faint)] tabular-nums">{activityRows.length}/{visibleActivity.length}</div>
              </div>
              <div class="space-y-1.5">
                {activityRows.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    aria-label={`Open activity event ${item.summary}`}
                    onClick={() => selectActivity(item.id)}
                    class={[
                      'w-full text-left px-2.5 py-2 rounded border transition-colors',
                      selected?.type === 'activity' && selected.id === item.id
                        ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)]'
                        : 'border-[var(--color-border)] bg-[var(--color-elevated)] hover:border-[var(--color-text-faint)]',
                    ].join(' ')}
                  >
                    <div class="flex items-center gap-2">
                      <span class="w-2 h-2 rounded-full shrink-0" style={{ background: agentColors[item.agent_id] || paletteColor(item.agent_id) }} />
                      <span class="text-[12px] text-[var(--color-text)] truncate">{item.summary}</span>
                    </div>
                    <div class="text-[10.5px] text-[var(--color-text-faint)] mt-0.5 truncate">
                      @{item.agent_id} - {item.action}
                    </div>
                  </button>
                ))}
                {visibleActivity.length > activityRows.length && (
                  <button
                    type="button"
                    onClick={() => setActivityListLimit((value) => value + 60)}
                    class="w-full px-2.5 py-2 rounded border border-[var(--color-border)] bg-[var(--color-card)] text-[11px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
                  >
                    Show more activity
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

function IconButton({ title, onClick, children }: { title: string; onClick: () => void; children: any }) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      class="inline-flex items-center justify-center w-7 h-7 rounded border border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
    >
      {children}
    </button>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-2 py-1.5">
      <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{label}</div>
      <div class="text-[15px] font-semibold text-[var(--color-text)] tabular-nums">{value}</div>
    </div>
  );
}

interface NodeRelationship {
  id: string;
  kind: string;
  direction: 'from' | 'to';
  neighbor: BrainGraphNode | null;
  neighborId: string;
  resolved?: boolean;
  mentionCount?: number;
  sourceField?: string;
}

function MemoryNodeDetail({
  node,
  preview,
  degree,
  relationships,
  totalRelationshipCount,
  edgeKindFilter,
  focused,
  onFocusNeighborhood,
  onClearFocus,
  onSelectNode,
}: {
  node: BrainGraphNode;
  preview?: NodePreview;
  degree: number;
  relationships: NodeRelationship[];
  totalRelationshipCount: number;
  edgeKindFilter: string;
  focused: boolean;
  onFocusNeighborhood: () => void;
  onClearFocus: () => void;
  onSelectNode: (id: string) => void;
}) {
  const tags = Array.isArray(node.tags) ? node.tags : [];
  const bodyText = preview?.text?.trim() || String(node.text || '').trim();
  const html = renderMarkdown(bodyText);
  return (
    <div class="px-4 py-4">
      <div class="rounded-md border border-[var(--color-accent)] bg-[var(--color-accent-soft)]/45 px-3 py-2 mb-3">
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full" style={{ background: scopeColor(node.scope_type) }} />
          <div class="min-w-0 flex-1">
            <div class="text-[10px] uppercase tracking-wider text-[var(--color-accent)]">Selected memory node</div>
            <div class="text-[13px] font-semibold text-[var(--color-text)] truncate">{node.label}</div>
          </div>
          <div class="text-[10.5px] text-[var(--color-text-muted)] tabular-nums">{degree} links</div>
        </div>
      </div>
      <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-2">
        {node.scope_type}/{node.scope_id} - {node.kind}
      </div>
      <div class="flex flex-wrap gap-1.5 mb-3">
        <button
          type="button"
          onClick={focused ? onClearFocus : onFocusNeighborhood}
          class={[
            'inline-flex items-center gap-1.5 px-2 py-1 rounded border text-[11px] transition-colors',
            focused
              ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
              : 'border-[var(--color-border)] bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
          ].join(' ')}
        >
          <GitBranch size={12} />
          {focused ? 'Neighborhood focused' : 'Focus neighborhood'}
        </button>
      </div>
      {node.source_path && (
        <div class="text-[11px] text-[var(--color-text-faint)] mb-2 break-all">{node.source_path}</div>
      )}
      {node.section_title && (
        <div class="text-[12px] text-[var(--color-text-muted)] mb-2">{node.section_title}</div>
      )}
      {tags.length > 0 && (
        <div class="flex flex-wrap gap-1 mb-3">
          {tags.map((tag) => (
            <span key={tag} class="px-1.5 py-0.5 rounded bg-[var(--color-elevated)] text-[10px] text-[var(--color-text-muted)]">
              #{tag}
            </span>
          ))}
        </div>
      )}
      {bodyText ? (
        <div
          class="max-h-[190px] overflow-y-auto rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-[12.5px] text-[var(--color-text)] leading-relaxed prose-sm"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : (
        <div class="rounded border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[12px] text-[var(--color-text-muted)]">
          No loaded text payload for this node.
        </div>
      )}
      {preview?.derived && (
        <div class="mt-2 inline-flex items-center gap-1.5 text-[10.5px] text-[var(--color-text-faint)]">
          <FileText size={11} />
          {preview.sourceLabel} preview from {preview.count} loaded chunk {preview.count === 1 ? 'neighbor' : 'neighbors'}.
        </div>
      )}
      <div class="mt-4 pt-3 border-t border-[var(--color-border)]">
        <div class="flex items-center justify-between gap-2 mb-2">
          <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Relationships</div>
          <div class="text-[10.5px] text-[var(--color-text-faint)] tabular-nums">
            {relationships.length}/{totalRelationshipCount}
          </div>
        </div>
        {edgeKindFilter !== 'all' && (
          <div class="text-[11px] text-[var(--color-text-faint)] mb-2">
            Filtered to {edgeKindLabel(edgeKindFilter)} relationships.
          </div>
        )}
        {relationships.length > 0 ? (
          <div class="space-y-1.5">
            {relationships.map((relationship) => (
              <button
                key={relationship.id}
                type="button"
                disabled={!relationship.neighbor}
                onClick={() => relationship.neighbor && onSelectNode(relationship.neighbor.id)}
                class={[
                  'w-full text-left rounded border px-2.5 py-2 disabled:hover:border-[var(--color-border)] disabled:cursor-default transition-colors',
                  relationship.neighbor?.id === node.id
                    ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)]'
                    : 'border-[var(--color-border)] bg-[var(--color-elevated)] hover:border-[var(--color-text-faint)]',
                ].join(' ')}
              >
                <div class="flex items-center gap-2">
                  <span class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{edgeKindLabel(relationship.kind)}</span>
                  <span class="text-[11px] text-[var(--color-text-muted)]">{relationship.direction === 'from' ? 'to' : 'from'}</span>
                  <span class="text-[12px] text-[var(--color-text)] truncate">{relationship.neighbor?.label ?? relationship.neighborId}</span>
                  {relationship.resolved === false && (
                    <span class="ml-auto text-[9.5px] uppercase tracking-wider text-[#ff9b9b]">Broken</span>
                  )}
                </div>
                {relationship.neighbor && (
                  <div class="text-[10.5px] text-[var(--color-text-faint)] mt-0.5 truncate">
                    {relationship.neighbor.scope_type}/{relationship.neighbor.scope_id} - {relationship.neighbor.kind}
                    {relationship.sourceField && relationship.sourceField !== relationship.kind ? ` - ${relationship.sourceField}` : ''}
                    {relationship.mentionCount && relationship.mentionCount > 1 ? ` - ${relationship.mentionCount} mentions` : ''}
                  </div>
                )}
              </button>
            ))}
          </div>
        ) : (
          <div class="text-[12px] text-[var(--color-text-muted)]">No loaded relationships for this node.</div>
        )}
      </div>
    </div>
  );
}

function ActivityDetail({ entry, color, blurOn }: { entry: NormalizedActivity; color: string; blurOn: boolean }) {
  const [revealed, setRevealed] = useState(false);
  return (
    <div class="px-4 py-4">
      <div class="flex items-center gap-2 mb-2">
        <span class="w-2.5 h-2.5 rounded-full" style={{ background: color }} />
        <div class="text-[13px] font-semibold text-[var(--color-text)] truncate">@{entry.agent_id}</div>
      </div>
      <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">
        {entry.action} - {formatRelativeTime(entry.created_at)}
      </div>
      <div
        class={'text-[12.5px] text-[var(--color-text)] leading-relaxed ' + (blurOn && !revealed ? 'privacy-blur' : (blurOn && revealed ? 'privacy-blur revealed' : ''))}
        onClick={() => blurOn && setRevealed((value) => !value)}
      >
        {entry.summary}
      </div>
      {entry.artifacts && (
        <div class="mt-3">
          <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Artifacts</div>
          <div class="font-mono text-[11px] text-[var(--color-text-muted)] whitespace-pre-wrap break-words">{entry.artifacts}</div>
        </div>
      )}
      <div class="mt-3">
        <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Chat</div>
        <div class="font-mono text-[11px] text-[var(--color-text-muted)] truncate">{entry.chat_id}</div>
      </div>
    </div>
  );
}

interface AuditForceNode extends SimulationNodeDatum {
  id: string;
  node: BrainGraphNode;
  degree: number;
  radius: number;
  anchorX: number;
  anchorY: number;
}

interface AuditForceLink extends SimulationLinkDatum<AuditForceNode> {
  edge: BrainGraphEdge;
}

function layoutNodes(nodes: BrainGraphNode[], edges: BrainGraphEdge[]): GraphLayout {
  const positions = new Map<string, Pt>();
  const degreeByNodeId = buildDegreeMap(nodes, edges);
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const nodeCount = Math.max(nodes.length, 1);
  const spread = Math.sqrt(nodeCount);
  const width = clamp(spread * 66, MIN_WORLD_W, 2800);
  const height = clamp(spread * 50, MIN_WORLD_H, 1900);
  const centerX = width / 2;
  const centerY = height / 2;
  const anchorRadius = Math.min(width, height) * 0.31;

  const forceNodes: AuditForceNode[] = nodes.map((node, index) => {
    const degree = degreeByNodeId.get(node.id) ?? 0;
    const radius = nodeRadius(node, degree);
    const groupSeed = `${node.scope_type}:${node.scope_id}:${folderKey(node.source_path || node.label || node.id)}`;
    const groupAngle = unitHash(groupSeed) * Math.PI * 2;
    const groupDistance = anchorRadius * (0.18 + unitHash(`${groupSeed}:distance`) * 0.82);
    const anchorX = centerX + Math.cos(groupAngle) * groupDistance;
    const anchorY = centerY + Math.sin(groupAngle) * groupDistance * 0.72;
    const localAngle = unitHash(`${node.id}:angle`) * Math.PI * 2;
    const localDistance = 20 + unitHash(`${node.id}:distance`) * Math.min(170, 34 + spread * 6);
    return {
      id: node.id,
      node,
      degree,
      radius,
      anchorX,
      anchorY,
      x: clamp(anchorX + Math.cos(localAngle) * localDistance + (index % 5 - 2) * 3, 28, width - 28),
      y: clamp(anchorY + Math.sin(localAngle) * localDistance + (index % 7 - 3) * 3, 28, height - 28),
    };
  });

  const forceLinks: AuditForceLink[] = edges
    .filter((edge) => nodeById.has(edge.source) && nodeById.has(edge.target))
    .map((edge) => ({ source: edge.source, target: edge.target, edge }));

  const simulation = forceSimulation<AuditForceNode>(forceNodes, 2)
    .alpha(0.95)
    .alphaDecay(0.035)
    .velocityDecay(0.42)
    .force('link', forceLink<AuditForceNode, AuditForceLink>(forceLinks)
      .id((node) => node.id)
      .distance((link) => {
        if (link.edge.kind === 'source') return 44;
        if (link.edge.kind === 'related') return 104;
        if (link.edge.kind === 'wikilink') return 120;
        if (link.edge.kind === 'property') return 132;
        if (link.edge.kind === 'scope') return 90;
        return 104;
      })
      .strength((link) => {
        if (link.edge.kind === 'source') return 0.74;
        if (link.edge.kind === 'related') return 0.34;
        if (link.edge.kind === 'wikilink') return 0.24;
        if (link.edge.kind === 'property') return 0.16;
        return 0.18;
      }))
    .force('charge', forceManyBody<AuditForceNode>()
      .strength((node) => {
        if (node.node.kind === 'note') return -170 - Math.min(110, node.degree * 5);
        if (node.node.kind === 'session' || node.node.kind === 'decision') return -130;
        if (node.node.kind === 'entity') return -90;
        return -38;
      })
      .distanceMin(8)
      .distanceMax(290))
    .force('collide', forceCollide<AuditForceNode>((node) => node.radius + (node.node.kind === 'chunk' ? 3.5 : 5.5))
      .strength(0.84)
      .iterations(2))
    .force('x', forceX<AuditForceNode>((node) => node.anchorX).strength(0.026))
    .force('y', forceY<AuditForceNode>((node) => node.anchorY).strength(0.026))
    .stop();

  const ticks = nodeCount > 900 ? 170 : nodeCount > 500 ? 195 : 220;
  simulation.tick(ticks);

  for (const node of forceNodes) {
    positions.set(node.id, {
      x: clamp(Number(node.x ?? node.anchorX), node.radius + 12, width - node.radius - 12),
      y: clamp(Number(node.y ?? node.anchorY), node.radius + 12, height - node.radius - 12),
    });
  }

  return { positions, degreeByNodeId, width, height };
}

function buildDragNeighborhood(rootId: string, edges: BrainGraphEdge[], maxNodes = 260): Set<string> {
  const adjacency = new Map<string, string[]>();
  for (const edge of edges) {
    if (!adjacency.has(edge.source)) adjacency.set(edge.source, []);
    if (!adjacency.has(edge.target)) adjacency.set(edge.target, []);
    adjacency.get(edge.source)!.push(edge.target);
    adjacency.get(edge.target)!.push(edge.source);
  }
  const visited = new Set<string>([rootId]);
  let frontier = [rootId];
  for (let depth = 0; depth < 1 && frontier.length > 0 && visited.size < maxNodes; depth += 1) {
    const nextFrontier: string[] = [];
    for (const id of frontier) {
      for (const neighbor of adjacency.get(id) ?? []) {
        if (visited.has(neighbor)) continue;
        visited.add(neighbor);
        nextFrontier.push(neighbor);
        if (visited.size >= maxNodes) break;
      }
      if (visited.size >= maxNodes) break;
    }
    frontier = nextFrontier;
  }
  return visited;
}

function seedPhysicsPositions(
  activeIds: Set<string>,
  currentPositions: Map<string, Pt>,
  basePositions: Map<string, Pt>,
  fixedPositions: Record<string, Pt>,
): Map<string, Pt> {
  const next = new Map<string, Pt>();
  for (const id of activeIds) {
    const pt = fixedPositions[id] ?? currentPositions.get(id) ?? basePositions.get(id);
    if (pt) next.set(id, { x: pt.x, y: pt.y });
  }
  return next;
}

function relaxPhysicsNeighborhood({
  activeIds,
  currentPositions,
  basePositions,
  fixedPositions,
  edges,
  nodesById,
  degreeByNodeId,
  width,
  height,
  iterations,
}: {
  activeIds: Set<string>;
  currentPositions: Map<string, Pt>;
  basePositions: Map<string, Pt>;
  fixedPositions: Record<string, Pt>;
  edges: BrainGraphEdge[];
  nodesById: Map<string, BrainGraphNode>;
  degreeByNodeId: Map<string, number>;
  width: number;
  height: number;
  iterations: number;
}): Map<string, Pt> {
  let next = seedPhysicsPositions(activeIds, currentPositions, basePositions, fixedPositions);
  const activeEdges = edges.filter((edge) => activeIds.has(edge.source) && activeIds.has(edge.target));

  for (let iteration = 0; iteration < iterations; iteration += 1) {
    const deltas = new Map<string, Pt>();
    const addDelta = (id: string, x: number, y: number) => {
      const current = deltas.get(id) ?? { x: 0, y: 0 };
      deltas.set(id, { x: current.x + x, y: current.y + y });
    };

    for (const edge of activeEdges) {
      const source = next.get(edge.source) ?? basePositions.get(edge.source);
      const target = next.get(edge.target) ?? basePositions.get(edge.target);
      if (!source || !target) continue;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const desired = edgePhysicsDistance(edge);
      const strength = edgePhysicsStrength(edge);
      const force = clamp((distance - desired) * strength, -22, 22);
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      if (!fixedPositions[edge.source]) addDelta(edge.source, fx * 0.42, fy * 0.42);
      if (!fixedPositions[edge.target]) addDelta(edge.target, -fx * 0.42, -fy * 0.42);
    }

    const relaxed = new Map<string, Pt>();
    for (const id of activeIds) {
      const fixed = fixedPositions[id];
      const base = basePositions.get(id);
      const current = next.get(id) ?? fixed ?? base;
      if (!current) continue;
      if (fixed) {
        relaxed.set(id, boundPhysicsPoint(id, fixed, width, height, nodesById, degreeByNodeId));
        continue;
      }
      const delta = deltas.get(id) ?? { x: 0, y: 0 };
      const anchorX = base ? (base.x - current.x) * 0.045 : 0;
      const anchorY = base ? (base.y - current.y) * 0.045 : 0;
      const step = limitVector({
        x: delta.x + anchorX,
        y: delta.y + anchorY,
      }, 10.5);
      relaxed.set(id, boundPhysicsPoint(id, {
        x: current.x + step.x,
        y: current.y + step.y,
      }, width, height, nodesById, degreeByNodeId));
    }
    next = relaxed;
  }

  return next;
}

function limitVector(vector: Pt, maxLength: number): Pt {
  const length = Math.hypot(vector.x, vector.y);
  if (length <= maxLength || length === 0) return vector;
  const scale = maxLength / length;
  return { x: vector.x * scale, y: vector.y * scale };
}

function edgePhysicsDistance(edge: BrainGraphEdge): number {
  if (edge.kind === 'source') return 44;
  if (edge.kind === 'related') return 104;
  if (edge.kind === 'wikilink') return 120;
  if (edge.kind === 'property') return 132;
  if (edge.kind === 'scope') return 90;
  return 104;
}

function edgePhysicsStrength(edge: BrainGraphEdge): number {
  if (edge.kind === 'source') return 0.055;
  if (edge.kind === 'related') return 0.04;
  if (edge.kind === 'wikilink') return 0.032;
  if (edge.kind === 'property') return 0.026;
  if (edge.kind === 'scope') return 0.04;
  return 0.034;
}

function boundPhysicsPoint(
  id: string,
  pt: Pt,
  width: number,
  height: number,
  nodesById: Map<string, BrainGraphNode>,
  degreeByNodeId: Map<string, number>,
): Pt {
  const node = nodesById.get(id) ?? fallbackPhysicsNode(id);
  const radius = nodeRadius(node, degreeByNodeId.get(id) ?? 0);
  return {
    x: clamp(pt.x, radius + 12, width - radius - 12),
    y: clamp(pt.y, radius + 12, height - radius - 12),
  };
}

function fallbackPhysicsNode(id: string): BrainGraphNode {
  return { id, label: id, kind: 'chunk', scope_type: 'global', scope_id: 'main' };
}

function requestAnimationFrameSafe(callback: FrameRequestCallback): number {
  if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
    return window.requestAnimationFrame(callback);
  }
  return setTimeout(() => callback(Date.now()), 16) as unknown as number;
}

function cancelAnimationFrameSafe(id: number) {
  if (typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
    window.cancelAnimationFrame(id);
    return;
  }
  clearTimeout(id as unknown as ReturnType<typeof setTimeout>);
}

function folderKey(value: string): string {
  const path = String(value || '').replace(/\\/g, '/');
  const parts = path.split('/').filter(Boolean);
  return parts.length > 1 ? parts[0] : 'root';
}

function compareAuditNodes(a: BrainGraphNode, b: BrainGraphNode): number {
  const degreeish = Number(b.preview_chunk_count ?? 0) - Number(a.preview_chunk_count ?? 0);
  if (degreeish !== 0) return degreeish;
  return String(a.source_path || a.label || a.id).localeCompare(String(b.source_path || b.label || b.id));
}

function buildDegreeMap(nodes: BrainGraphNode[], edges: BrainGraphEdge[]): Map<string, number> {
  const ids = new Set(nodes.map((node) => node.id));
  const degree = new Map<string, number>();
  for (const edge of edges) {
    if (ids.has(edge.source)) degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    if (ids.has(edge.target)) degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }
  return degree;
}

function nodeRadius(node: BrainGraphNode, degree = 0): number {
  const degreeBoost = Math.min(10, Math.sqrt(Math.max(0, degree)) * 1.9);
  if (node.kind === 'note') return 5.4 + degreeBoost;
  if (node.kind === 'session' || node.kind === 'decision') return 6.4 + Math.min(8, degreeBoost);
  if (node.kind === 'entity') return 4.8 + Math.min(6, degreeBoost * 0.55);
  return 2.8 + Math.min(4.2, degreeBoost * 0.42);
}

function nodeFill(node: BrainGraphNode, degree: number, selected: boolean, relatedToSelection: boolean): string {
  if (selected) return OBSIDIAN_SELECTED;
  if (relatedToSelection) return '#d7e6ff';
  if (isUnresolvedNode(node)) return '#ffb0b0';
  if (node.kind === 'note' && degree >= 10) return OBSIDIAN_NODE_HUB;
  if (node.kind === 'session' || node.kind === 'decision') return '#c6c6c6';
  if (node.kind === 'chunk') return '#9f9f9f';
  return OBSIDIAN_NODE;
}

function edgeStroke(edge: BrainGraphEdge, state: { dragging: boolean; selected: boolean; unresolved: boolean }): string {
  if (state.unresolved) return state.selected || state.dragging ? '#ffb0b0' : '#d87373';
  if (state.dragging) return OBSIDIAN_ACCENT;
  if (state.selected) return '#d8d8d8';
  if (edge.kind === 'related') return '#9db9ff';
  if (edge.kind === 'property') return '#8b8f97';
  return OBSIDIAN_EDGE;
}

function isUnresolvedNode(node?: BrainGraphNode | null): boolean {
  return Boolean(node?.tags?.includes('unresolved-wikilink'));
}

function isUnresolvedEdge(edge: BrainGraphEdge, nodes: Map<string, BrainGraphNode>): boolean {
  return edge.resolved === false || isUnresolvedNode(nodes.get(edge.target));
}

function edgeMatchesFilter(edge: BrainGraphEdge, filter: string, nodes: Map<string, BrainGraphNode>): boolean {
  if (filter === 'all') return true;
  if (filter === 'unresolved') return isUnresolvedEdge(edge, nodes);
  return edge.kind === filter;
}

function relationshipMatchesFilter(relationship: NodeRelationship, filter: string): boolean {
  if (filter === 'all') return true;
  if (filter === 'unresolved') return relationship.resolved === false || isUnresolvedNode(relationship.neighbor);
  return relationship.kind === filter;
}

function scopeColor(scope: string): string {
  switch (scope) {
    case 'persona': return '#a78bfa';
    case 'agent': return '#f59e0b';
    case 'team': return '#22c55e';
    case 'room': return '#38bdf8';
    default: return '#f472b6';
  }
}

interface NormalizedActivity {
  id: string;
  agent_id: string;
  chat_id: string;
  action: string;
  summary: string;
  artifacts: string | null;
  created_at: number;
}

function normalizeActivities(activity: BrainActivity[]): NormalizedActivity[] {
  return activity.map((event, index) => {
    const agentId = normalizeAgentId(event.personaId ?? event.persona_id ?? event.agentId ?? event.agent_id);
    const action = normalizeAction(event);
    return {
      id: String(event.id ?? event.eventId ?? event.event_id ?? `activity-${index}`),
      agent_id: agentId,
      chat_id: String(event.chatId ?? event.chat_id ?? event.sessionId ?? event.session_id ?? `agent:${agentId}`),
      action,
      summary: String(event.details ?? event.excerpt ?? event.summary ?? action),
      artifacts: normalizeArtifacts(event),
      created_at: normalizeTimestamp(event.timestamp ?? event.createdAt ?? event.created_at),
    };
  });
}

function placeActivity(activity: NormalizedActivity[], nodes: BrainGraphNode[], positions: Map<string, Pt>): Map<string, Pt> {
  const out = new Map<string, Pt>();
  const scopeAnchors = new Map<string, Pt>();
  for (const node of nodes) {
    const pt = positions.get(node.id);
    if (!pt) continue;
    const browserScope = node.scope_id === 'default' ? 'main' : node.scope_id;
    if (!scopeAnchors.has(browserScope)) {
      scopeAnchors.set(browserScope, pt);
    }
    if (node.scope_type === 'global' && !scopeAnchors.has('main')) {
      scopeAnchors.set('main', pt);
    }
  }

  activity.forEach((item, index) => {
    const anchor = scopeAnchors.get(item.agent_id) ?? scopeAnchors.get('main') ?? { x: 480, y: 270 };
    const angle = hash(`${item.id}:${item.agent_id}`) * 0.00011 + index * 1.618;
    const radius = 34 + (index % 6) * 8;
    out.set(item.id, {
      x: clamp(anchor.x + Math.cos(angle) * radius, 24, 936),
      y: clamp(anchor.y + Math.sin(angle) * radius, 24, 536),
    });
  });
  return out;
}

function buildNodePreviews(nodes: BrainGraphNode[], edges: BrainGraphEdge[]): Map<string, NodePreview> {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const previews = new Map<string, NodePreview>();
  for (const node of nodes) {
    const text = String(node.text || '').trim();
    if (text) {
      previews.set(node.id, {
        text,
        derived: Boolean(node.preview_source),
        count: Number(node.preview_chunk_count ?? 1),
        sourceLabel: node.preview_source ? 'Loaded chunks' : 'Node body',
      });
    }
  }

  const chunkNeighborsByNote = new Map<string, BrainGraphNode[]>();
  for (const edge of edges) {
    if (edge.kind !== 'source') continue;
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (source?.kind === 'chunk' && target?.kind === 'note' && String(source.text || '').trim()) {
      const list = chunkNeighborsByNote.get(target.id) ?? [];
      list.push(source);
      chunkNeighborsByNote.set(target.id, list);
    } else if (target?.kind === 'chunk' && source?.kind === 'note' && String(target.text || '').trim()) {
      const list = chunkNeighborsByNote.get(source.id) ?? [];
      list.push(target);
      chunkNeighborsByNote.set(source.id, list);
    }
  }

  for (const [noteId, chunks] of chunkNeighborsByNote.entries()) {
    if (previews.has(noteId)) continue;
    const text = chunks
      .sort((a, b) => Number(b.created_at ?? 0) - Number(a.created_at ?? 0))
      .slice(0, 3)
      .map((chunk) => {
        const body = String(chunk.text || '').trim();
        return chunk.section_title ? `## ${chunk.section_title}\n\n${body}` : body;
      })
      .filter(Boolean)
      .join('\n\n---\n\n');
    if (text) {
      previews.set(noteId, {
        text,
        derived: true,
        count: chunks.length,
        sourceLabel: 'Loaded chunk neighbors',
      });
    }
  }

  return previews;
}

function previewSnippet(value: unknown, max = 96): string {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (text.length <= max) return text;
  return text.slice(0, max - 3).trimEnd() + '...';
}

function truncateLabel(value: unknown, max = 28): string {
  const text = String(value || '').trim();
  if (text.length <= max) return text;
  return text.slice(0, max - 3).trimEnd() + '...';
}

function nodeMatchesQuery(node: BrainGraphNode, query: string): boolean {
  return [
    node.label,
    node.kind,
    node.scope_type,
    node.scope_id,
    node.source_path,
    node.section_title,
    node.text,
    ...(Array.isArray(node.tags) ? node.tags : []),
  ].some((value) => String(value ?? '').toLowerCase().includes(query));
}

function nodeRelationships(nodeId: string, edges: BrainGraphEdge[], nodes: Map<string, BrainGraphNode>): NodeRelationship[] {
  return edges
    .filter((edge) => edge.source === nodeId || edge.target === nodeId)
    .map((edge) => {
      const direction = edge.source === nodeId ? 'from' : 'to';
      const neighborId = direction === 'from' ? edge.target : edge.source;
      return {
        id: edge.id,
        kind: edge.kind,
        direction,
        neighborId,
        neighbor: nodes.get(neighborId) ?? null,
        resolved: edge.resolved,
        mentionCount: edge.mention_count,
        sourceField: edge.source_field,
      };
    });
}

function uniqueEdgeKinds(edges: BrainGraphEdge[]): string[] {
  return [...new Set(edges.map((edge) => edge.kind).filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function edgeKindLabel(kind: string): string {
  if (kind === 'all') return 'All';
  if (kind === 'related') return 'Related';
  if (kind === 'wikilink') return 'Wiki';
  if (kind === 'property') return 'Property';
  if (kind === 'unresolved') return 'Broken';
  return kind.replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function normalizeAgentId(value: unknown): string {
  const text = String(value || 'main').trim();
  return text === 'default' ? 'main' : text || 'main';
}

function normalizeAction(event: BrainActivity): string {
  const base = String(event.action ?? event.type ?? event.event_type ?? 'chat_message');
  if (base === 'chat_message' && event.role) {
    return `${base}:${event.role}`;
  }
  return base;
}

function normalizeArtifacts(event: BrainActivity): string | null {
  if (event.artifacts) return String(event.artifacts);
  const bits = [event.provider, event.model].filter(Boolean).map(String);
  return bits.length ? bits.join(' / ') : null;
}

function normalizeTimestamp(value: number | string | undefined): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 10_000_000_000 ? value / 1000 : value;
  }
  if (typeof value === 'string' && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric > 10_000_000_000 ? numeric / 1000 : numeric;
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed / 1000;
  }
  return Date.now() / 1000;
}

function hash(input: string): number {
  let value = 0;
  for (let i = 0; i < input.length; i++) {
    value = (value * 31 + input.charCodeAt(i)) >>> 0;
  }
  return value;
}

function unitHash(input: string): number {
  return hash(input) / 0xffffffff;
}

function paletteColor(id: string): string {
  const palette = ['#5eb6ff', '#10b981', '#f59e0b', '#a78bfa', '#f87171', '#2dd4bf', '#e879f9', '#84cc16'];
  return palette[hash(id) % palette.length];
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
