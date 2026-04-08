"""Wiki-link graph traversal for the memory vault.

Builds a bidirectional link graph from [[wiki-links]] in vault/memory/
markdown files. Used by the recall pipeline to find contextually related
notes that keyword/vector search might miss.

Phase 4 refactor: ALL dicts keyed by vault-relative path (not stem).
Stems become a reverse-lookup index only. This prevents collisions
when two files share a stem (e.g. daily/index.md vs docs/index.md).

Pattern: Adapted from vault.py extract_links(), build_backlinks(), cmd_graph() BFS.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_links(content: str) -> list[str]:
    """Extract [[wiki-links]] from markdown. Returns deduplicated raw bodies."""
    return list(set(_WIKI_LINK_RE.findall(content)))


def normalize_link(raw_link: str) -> str:
    """Normalize [[...]] body: strip alias (|), header (#), block ref (^)."""
    target = raw_link.split("|")[0].strip()
    target = target.split("#")[0].split("^")[0].strip()
    return target.lower()


@dataclass
class MemoryGraph:
    """Bidirectional link graph. ALL dicts keyed by vault-relative path."""

    forward_links: dict[str, list[str]] = field(default_factory=dict)
    backward_links: dict[str, list[str]] = field(default_factory=dict)
    path_to_stem: dict[str, str] = field(default_factory=dict)
    stem_to_paths: dict[str, list[str]] = field(default_factory=dict)
    link_counts: dict[str, int] = field(default_factory=dict)

    # Backward-compat alias for callers that used stem_to_path (singular)
    @property
    def stem_to_path(self) -> dict[str, str]:
        """Return first path for each stem (backward compat)."""
        return {s: ps[0] for s, ps in self.stem_to_paths.items() if ps}


def _stem(path: Path | str) -> str:
    """Normalize a path or link target to a lowercase stem."""
    return Path(path).stem.lower()


def _resolve_link(target: str, stem_to_paths: dict[str, list[str]]) -> str | None:
    """Resolve a normalized link target to a vault-relative path."""
    if target in stem_to_paths:
        paths = stem_to_paths[target]
        if len(paths) == 1:
            return paths[0]
        return min(paths, key=len)
    for _stem_key, paths in stem_to_paths.items():
        for path in paths:
            if path.lower().replace("\\", "/").removesuffix(".md") == target:
                return path
    return None


def build_memory_graph(memory_dir: Path) -> MemoryGraph:
    """Scan .md files, build PATH-BASED bidirectional link graph."""
    graph = MemoryGraph()

    if not memory_dir.exists():
        return graph

    md_files = list(memory_dir.rglob("*.md"))
    if not md_files:
        return graph

    # Pass 1: Build path<->stem mappings
    for md_file in md_files:
        try:
            rel_path = str(md_file.relative_to(memory_dir)).replace("\\", "/")
        except ValueError:
            continue
        stem = _stem(md_file)
        graph.path_to_stem[rel_path] = stem
        graph.stem_to_paths.setdefault(stem, []).append(rel_path)

    # Pass 2: Extract + resolve links, build forward/backward maps
    for md_file in md_files:
        try:
            rel_path = str(md_file.relative_to(memory_dir)).replace("\\", "/")
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue
        raw_links = _WIKI_LINK_RE.findall(content)
        resolved_targets: list[str] = []
        for raw in raw_links:
            target_stem = normalize_link(raw)
            resolved = _resolve_link(target_stem, graph.stem_to_paths)
            if resolved and resolved != rel_path:
                resolved_targets.append(resolved)
        graph.forward_links[rel_path] = list(set(resolved_targets))
        for target_path in graph.forward_links[rel_path]:
            graph.backward_links.setdefault(target_path, [])
            if rel_path not in graph.backward_links[target_path]:
                graph.backward_links[target_path].append(rel_path)

    # Pass 3: Compute link counts (keyed by rel_path)
    for rel_path in graph.path_to_stem:
        outgoing = len(graph.forward_links.get(rel_path, []))
        incoming = len(graph.backward_links.get(rel_path, []))
        graph.link_counts[rel_path] = outgoing + incoming

    return graph


def get_neighbors(
    graph: MemoryGraph,
    start_ids: list[str],
    max_hops: int = 1,
    max_per_start: int = 5,
) -> list[str]:
    """BFS from given IDs (rel_paths or stems), return neighbor rel_paths.

    Accepts both rel_paths and stems for backward compat with recall.py.
    """
    visited: set[str] = set()
    result_paths: list[str] = []

    for start_id in start_ids:
        # Resolve stem to rel_path if needed
        if start_id in graph.path_to_stem:
            start_path = start_id
        else:
            paths = graph.stem_to_paths.get(start_id.lower(), [])
            start_path = paths[0] if paths else None
        if not start_path:
            continue

        local_visited: set[str] = {start_path}
        local_found: list[str] = []
        queue: deque[tuple[str, int]] = deque([(start_path, 0)])

        while queue and len(local_found) < max_per_start:
            current, hop = queue.popleft()
            if hop > max_hops:
                break

            if hop > 0 and current not in visited:
                local_found.append(current)
                visited.add(current)

            if hop < max_hops:
                for linked in graph.forward_links.get(current, []):
                    if linked not in local_visited:
                        local_visited.add(linked)
                        queue.append((linked, hop + 1))
                for src in graph.backward_links.get(current, []):
                    if src not in local_visited:
                        local_visited.add(src)
                        queue.append((src, hop + 1))

        result_paths.extend(local_found)

    return result_paths


_MOC_PATTERNS = re.compile(r"^(moc[-_]|_dashboard|_index|_canvas)", re.I)


def is_moc(identifier: str, graph: MemoryGraph, link_threshold: int = 15) -> bool:
    """Detect Map of Content / index notes.

    Accepts rel_path or stem. MOCs are graph expansion anchors.
    """
    if identifier in graph.path_to_stem:
        stem = graph.path_to_stem[identifier]
        fwd_key = identifier
    else:
        stem = identifier.lower()
        fwd_key = (graph.stem_to_paths.get(stem, [None]) or [None])[0]

    if _MOC_PATTERNS.match(stem):
        return True
    if fwd_key:
        forward_count = len(graph.forward_links.get(fwd_key, []))
        return forward_count > link_threshold
    return False


def get_hub_scores(graph: MemoryGraph) -> dict[str, float]:
    """Return normalized hub scores (0-1) keyed by rel_path."""
    if not graph.link_counts:
        return {}

    max_count = max(graph.link_counts.values())
    if max_count == 0:
        return {}

    return {p: count / max_count for p, count in graph.link_counts.items()}


# === Phase 4 additions ===

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def parse_frontmatter(content: str) -> dict:
    """Extract simple frontmatter fields via regex. No YAML dependency."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}
    block = match.group(1)
    meta: dict = {}
    tags_match = re.search(r"tags:\s*\[([^\]]*)\]", block)
    if tags_match:
        meta["tags"] = [t.strip().strip("'\"") for t in tags_match.group(1).split(",") if t.strip()]
    date_match = re.search(r"date:\s*(\d{4}-\d{2}-\d{2})", block)
    if date_match:
        meta["date"] = date_match.group(1)
    summary_match = re.search(r'summary:\s*"([^"]*)"', block)
    if summary_match:
        meta["summary"] = summary_match.group(1)
    return meta


_IDENTITY_STEMS = {"soul", "user", "memory", "goals", "heartbeat", "self"}


def classify_node_type(stem: str, rel_path: str) -> str:
    """Classify a vault file into a node type for the graph UI."""
    rel_lower = rel_path.lower().replace("\\", "/")
    if rel_lower.startswith("daily/"):
        return "daily"
    if rel_lower.startswith("weekly/"):
        return "weekly"
    if rel_lower.startswith("drafts/"):
        return "draft"
    if rel_lower.startswith("docs/"):
        return "doc"
    if rel_lower.startswith("_"):
        return "operational"
    if stem in _IDENTITY_STEMS:
        return "identity"
    if stem.startswith("moc-") or stem.startswith("moc_"):
        return "moc"
    return "doc"


def shortest_path(graph: MemoryGraph, from_path: str, to_path: str) -> list[str]:
    """BFS shortest path. Accepts and returns vault-relative paths."""
    if from_path == to_path:
        return [from_path]
    if from_path not in graph.path_to_stem or to_path not in graph.path_to_stem:
        return []
    visited: set[str] = {from_path}
    queue: deque[tuple[str, list[str]]] = deque([(from_path, [from_path])])
    while queue:
        current, trail = queue.popleft()
        neighbors = set(graph.forward_links.get(current, []))
        neighbors.update(graph.backward_links.get(current, []))
        for neighbor in neighbors:
            if neighbor == to_path:
                return trail + [neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, trail + [neighbor]))
    return []


def compute_pagerank(
    graph: MemoryGraph, damping: float = 0.85, iterations: int = 100,
) -> dict[str, float]:
    """Power iteration PageRank. Returns rel_path -> score (normalized 0-1)."""
    node_ids = list(graph.path_to_stem.keys())
    n = len(node_ids)
    if n == 0:
        return {}
    rank = {p: 1.0 / n for p in node_ids}
    for _ in range(iterations):
        new_rank = {}
        for node in node_ids:
            incoming = graph.backward_links.get(node, [])
            total = (1.0 - damping) / n
            for src in incoming:
                out_degree = len(graph.forward_links.get(src, []))
                if out_degree > 0:
                    total += damping * rank.get(src, 0.0) / out_degree
            new_rank[node] = total
        rank = new_rank
    max_rank = max(rank.values()) if rank else 1.0
    if max_rank > 0:
        return {p: round(v / max_rank, 4) for p, v in rank.items()}
    return rank


def compute_betweenness(graph: MemoryGraph) -> dict[str, float]:
    """Brandes algorithm for betweenness centrality. Returns normalized 0-1."""
    node_ids = list(graph.path_to_stem.keys())
    betweenness = {p: 0.0 for p in node_ids}
    for source in node_ids:
        stack: list[str] = []
        predecessors: dict[str, list[str]] = {p: [] for p in node_ids}
        sigma = {p: 0.0 for p in node_ids}
        sigma[source] = 1.0
        dist = {p: -1 for p in node_ids}
        dist[source] = 0
        queue: deque[str] = deque([source])
        while queue:
            v = queue.popleft()
            stack.append(v)
            neighbors = set(graph.forward_links.get(v, []))
            neighbors.update(graph.backward_links.get(v, []))
            for w in neighbors:
                if w not in dist:
                    continue
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    predecessors[w].append(v)
        delta = {p: 0.0 for p in node_ids}
        while stack:
            w = stack.pop()
            for v in predecessors[w]:
                if sigma[w] > 0:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != source:
                betweenness[w] += delta[w]
    max_b = max(betweenness.values()) if betweenness else 1.0
    if max_b > 0:
        return {p: round(v / max_b, 4) for p, v in betweenness.items()}
    return betweenness
