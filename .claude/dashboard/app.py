"""The Homie Dashboard — read-only FastAPI backend.

Serves conversation history, daily logs, memory files, and system health
from existing data stores. Zero writes, zero risk to bot state.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import markdown  # type: ignore[import-untyped]
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Add scripts dir for config/shared imports
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_CHAT_DIR = Path(__file__).resolve().parent.parent / "chat"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_CHAT_DIR))

from config import (  # noqa: E402
    CHAT_DB_PATH,
    DAILY_DIR,
    HEARTBEAT_FILE,
    HEARTBEAT_STATE_FILE,
    MEMORY_FILE,
    REFLECTION_STATE_FILE,
    SOUL_FILE,
    USER_FILE,
)
from shared import is_pid_alive, load_state, read_pid  # noqa: E402

# JSONL transcripts live here
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def encode_project_path(path: str) -> str:
    """Encode a filesystem path the way Claude Code does for project directories.

    Verified against 14 real project directories on this machine.
    """
    result = path.replace(":\\", "--").replace(":/", "--")
    result = result.replace("\\", "-").replace("/", "-")
    result = result.replace(" ", "-").replace(".", "-")
    result = result.replace(":", "-")
    return result


# Our project's encoded directory name
_PROJECT_PATH = str(Path(__file__).resolve().parent.parent.parent)
_ENCODED_PROJECT = encode_project_path(_PROJECT_PATH)
TRANSCRIPTS_DIR = CLAUDE_PROJECTS_DIR / _ENCODED_PROJECT

# Allowed memory files (whitelist to prevent path traversal)
ALLOWED_MEMORY_FILES = {"SOUL.md", "USER.md", "MEMORY.md", "HEARTBEAT.md"}
MEMORY_FILE_PATHS = {
    "SOUL.md": SOUL_FILE,
    "USER.md": USER_FILE,
    "MEMORY.md": MEMORY_FILE,
    "HEARTBEAT.md": HEARTBEAT_FILE,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_transcript(agent_session_id: str) -> list[dict[str, str]]:
    """Parse a JSONL transcript file and extract displayable messages.

    Returns list of {role, content, timestamp} for user text + assistant text.
    Skips tool_use, tool_result, progress, queue-operation, and system messages.
    """
    jsonl_path = TRANSCRIPTS_DIR / f"{agent_session_id}.jsonl"
    if not jsonl_path.exists():
        return []

    messages: list[dict[str, str]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant"):
            continue

        timestamp = entry.get("timestamp", "")
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", entry_type)
        content = msg.get("content", "")

        # Extract text from content
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            text = "\n".join(text_parts)

        if text.strip():
            messages.append({"role": role, "content": text, "timestamp": timestamp})

    return messages


def _load_db_messages(session_id: str) -> list[dict[str, str]]:
    """Load persisted chat messages from chat.db when available."""

    if not _db_conn:
        return []

    try:
        rows = _db_conn.execute(
            """
            SELECT role, content, created_at
            FROM chat_messages
            WHERE session_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (session_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    return [
        {
            "role": row["role"],
            "content": row["content"],
            "timestamp": row["created_at"],
        }
        for row in rows
        if row["content"]
    ]


def _parse_message_timestamp(value: str) -> datetime | None:
    """Parse dashboard transcript timestamps for transitional source merging."""

    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _merge_messages(
    transcript_messages: list[dict[str, str]],
    db_messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Merge legacy JSONL and DB-backed transcript sources during migration."""

    combined = transcript_messages + db_messages
    ordered = sorted(
        enumerate(combined),
        key=lambda item: (
            _parse_message_timestamp(item[1].get("timestamp", "")) or datetime.min.replace(tzinfo=UTC),
            item[0],
        ),
    )

    deduped: list[dict[str, str]] = []
    for _, message in ordered:
        if (
            deduped
            and deduped[-1]["role"] == message["role"]
            and deduped[-1]["content"] == message["content"]
        ):
            continue
        deduped.append(message)
    return deduped


def _render_markdown(text: str) -> str:
    """Convert markdown text to HTML."""
    result: str = markdown.markdown(text, extensions=["fenced_code", "tables", "nl2br"])
    return result


def _date_pattern() -> re.Pattern[str]:
    """Compiled regex for YYYY-MM-DD date format."""
    return re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

_db_conn: sqlite3.Connection | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Open read-only SQLite connection on startup, close on shutdown."""
    global _db_conn
    db_path = CHAT_DB_PATH
    if db_path.exists():
        _db_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
    yield
    if _db_conn:
        _db_conn.close()
        _db_conn = None


app = FastAPI(title="The Homie Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    """System health: bot PID/alive, heartbeat state, reflection state."""
    # Bot status
    pid = read_pid()
    bot_alive = is_pid_alive(pid) if pid else False

    # Heartbeat state
    hb_state = load_state(HEARTBEAT_STATE_FILE)

    # Reflection state
    ref_state = load_state(REFLECTION_STATE_FILE)

    # Count crash entries in today's daily log
    from config import get_today_log_path

    crash_count = 0
    today_log = get_today_log_path()
    if today_log.exists():
        log_text = today_log.read_text(encoding="utf-8").lower()
        crash_count = log_text.count("crash")

    return {
        "bot": {
            "pid": pid,
            "alive": bot_alive,
        },
        "heartbeat": hb_state,
        "reflection": ref_state,
        "crashes_today": crash_count,
    }


@app.get("/api/diagnostics")
def api_diagnostics() -> dict[str, Any]:
    """Full system diagnostics — cognition, recall, runtime, sessions."""
    import dataclasses
    import sys as _sys

    _chat_dir = str(Path(__file__).parent.parent / "chat")
    if _chat_dir not in _sys.path:
        _sys.path.insert(0, _chat_dir)
    from diagnostics import collect_diagnostics

    report = collect_diagnostics()
    return dataclasses.asdict(report)


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, Any]]:
    """List all chat sessions, ordered by most recently updated."""
    if not _db_conn:
        return []

    rows = _db_conn.execute("SELECT * FROM chat_sessions ORDER BY updated_at DESC").fetchall()

    return [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "agent_session_id": row["agent_session_id"],
            "platform": row["platform"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": row["message_count"],
            "total_cost_usd": row["total_cost_usd"],
            "status": row["status"],
            "mode": row["mode"] if "mode" in row.keys() else "execute",
        }
        for row in rows
    ]


@app.get("/api/sessions/{session_id}")
def get_session(session_id: int) -> dict[str, Any]:
    """Get session metadata + conversation transcript from JSONL."""
    if not _db_conn:
        raise HTTPException(404, "No database available")

    row = _db_conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()

    if not row:
        raise HTTPException(404, "Session not found")

    agent_session_id = row["agent_session_id"]
    transcript_messages = _parse_transcript(agent_session_id)
    db_messages = _load_db_messages(row["session_id"])
    messages = _merge_messages(transcript_messages, db_messages) if db_messages else transcript_messages

    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "agent_session_id": agent_session_id,
        "platform": row["platform"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": row["message_count"],
        "total_cost_usd": row["total_cost_usd"],
        "status": row["status"],
        "mode": row["mode"] if "mode" in row.keys() else "execute",
        "messages": messages,
    }


@app.get("/api/logs")
def list_logs() -> list[dict[str, Any]]:
    """List daily log files, sorted by date descending."""
    if not DAILY_DIR.exists():
        return []

    date_re = _date_pattern()
    logs: list[dict[str, Any]] = []

    for f in DAILY_DIR.iterdir():
        if f.suffix == ".md" and date_re.match(f.stem):
            logs.append(
                {
                    "date": f.stem,
                    "filename": f.name,
                    "size_bytes": f.stat().st_size,
                }
            )

    logs.sort(key=lambda x: x["date"], reverse=True)
    return logs


@app.get("/api/logs/{date}")
def get_log(date: str) -> dict[str, Any]:
    """Read a specific daily log and render markdown to HTML."""
    if not _date_pattern().match(date):
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")

    log_path = DAILY_DIR / f"{date}.md"
    if not log_path.exists():
        raise HTTPException(404, f"No log found for {date}")

    content = log_path.read_text(encoding="utf-8")
    return {
        "date": date,
        "content": content,
        "html": _render_markdown(content),
    }


@app.get("/api/memory/{filename}")
def get_memory(filename: str) -> dict[str, Any]:
    """Read a memory file (SOUL.md, USER.md, MEMORY.md, HEARTBEAT.md)."""
    if filename not in ALLOWED_MEMORY_FILES:
        allowed = ", ".join(sorted(ALLOWED_MEMORY_FILES))
        raise HTTPException(400, f"File not allowed. Choose from: {allowed}")

    file_path = MEMORY_FILE_PATHS[filename]
    if not file_path.exists():
        raise HTTPException(404, f"{filename} not found")

    content = file_path.read_text(encoding="utf-8")
    mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=UTC).isoformat()

    return {
        "filename": filename,
        "content": content,
        "html": _render_markdown(content),
        "last_modified": mtime,
    }


# ---------------------------------------------------------------------------
# Memory Graph endpoints (Phase 4)
# ---------------------------------------------------------------------------

MEMORY_DIR = Path(__file__).resolve().parents[2] / "TheHomie" / "Memory"
CANVAS_DIR = MEMORY_DIR / "_canvas"
GRAPH_JSON = MEMORY_DIR / ".obsidian" / "graph.json"

_EXCLUDE_PREFIXES = ("_templates/", "_canvas/", ".obsidian/", ".nexus/", ".workspaces/")

try:
    from cognition.graph import (
        build_memory_graph,
        classify_node_type,
        compute_betweenness,
        compute_pagerank,
        get_hub_scores,
        get_neighbors,
        is_moc,
        parse_frontmatter,
        shortest_path,
    )
    from cognition.observability import RecallLogStore

    _GRAPH_AVAILABLE = True
except ImportError:
    _GRAPH_AVAILABLE = False


def _read_force_params() -> dict:
    if not GRAPH_JSON.exists():
        return {}
    try:
        data = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
        return {
            "centerStrength": data.get("centerStrength", 0.5),
            "repelStrength": data.get("repelStrength", 10),
            "linkDistance": data.get("linkDistance", 250),
        }
    except Exception:
        return {}


@app.get("/api/graph")
def get_graph(include_emergent: bool = False) -> dict:
    """Full vault graph: nodes + edges + stats + metrics."""
    if not _GRAPH_AVAILABLE:
        raise HTTPException(503, "Graph module not available")

    graph = build_memory_graph(MEMORY_DIR)
    hub_scores = get_hub_scores(graph)
    pr = compute_pagerank(graph)
    bc = compute_betweenness(graph)

    nodes = []
    for rel_path, stem in graph.path_to_stem.items():
        if any(rel_path.lower().startswith(p) for p in _EXCLUDE_PREFIXES):
            continue
        full_path = MEMORY_DIR / rel_path
        meta: dict = {}
        try:
            content = full_path.read_text(encoding="utf-8")
            meta = parse_frontmatter(content)
        except Exception:
            pass
        node_type = classify_node_type(stem, rel_path)
        nodes.append({
            "id": rel_path,
            "stem": stem,
            "label": Path(rel_path).stem,
            "path": rel_path,
            "type": node_type,
            "hubScore": round(hub_scores.get(rel_path, 0.0), 3),
            "linkCount": graph.link_counts.get(rel_path, 0),
            "isMoc": is_moc(rel_path, graph),
            "tags": meta.get("tags", []),
            "date": meta.get("date"),
            "summary": meta.get("summary"),
            "pageRank": pr.get(rel_path, 0),
            "betweenness": bc.get(rel_path, 0),
        })

    edges = []
    for source_path, target_paths in graph.forward_links.items():
        if any(source_path.lower().startswith(p) for p in _EXCLUDE_PREFIXES):
            continue
        for target_path in target_paths:
            if any(target_path.lower().startswith(p) for p in _EXCLUDE_PREFIXES):
                continue
            edges.append({
                "id": f"{source_path}->{target_path}",
                "source": source_path,
                "target": target_path,
                "type": "wikilink",
                "weight": 1.0,
            })

    if include_emergent:
        import asyncio

        try:
            from cognition.connections import find_emergent_connections

            emergent = asyncio.run(find_emergent_connections(MEMORY_DIR, max_results=10))
            for conn in emergent:
                edges.append({
                    "id": f"{conn.note_a}~{conn.note_b}",
                    "source": conn.note_a,
                    "target": conn.note_b,
                    "type": "emergent",
                    "weight": round(conn.similarity, 3),
                })
        except Exception:
            pass

    hub_ids = [n["id"] for n in nodes if n["isMoc"]]
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "totalFiles": len(nodes),
            "totalLinks": sum(1 for e in edges if e["type"] == "wikilink"),
            "totalEmergent": sum(1 for e in edges if e["type"] == "emergent"),
            "hubs": hub_ids,
            "forceParams": _read_force_params(),
        },
    }


@app.get("/api/graph/local")
def get_graph_local(path: str = "", hops: int = 1) -> dict:
    """Neighborhood subgraph: BFS from a vault-relative path."""
    if not _GRAPH_AVAILABLE:
        raise HTTPException(503, "Graph module not available")
    if not path:
        raise HTTPException(400, "path query parameter required")

    graph = build_memory_graph(MEMORY_DIR)
    neighbor_paths = get_neighbors(graph, [path], max_hops=min(hops, 3), max_per_start=20)
    all_rel_paths = set(neighbor_paths)
    all_rel_paths.add(path)

    full_graph = get_graph()
    node_ids = all_rel_paths
    return {
        "nodes": [n for n in full_graph["nodes"] if n["id"] in node_ids],
        "edges": [
            e for e in full_graph["edges"]
            if e["source"] in node_ids and e["target"] in node_ids
        ],
        "stats": full_graph["stats"],
    }


@app.get("/api/pathways")
def get_pathway(from_path: str = "", to_path: str = "") -> dict:
    """Find shortest path between two notes (by vault-relative path)."""
    if not _GRAPH_AVAILABLE:
        raise HTTPException(503, "Graph module not available")
    if not from_path or not to_path:
        raise HTTPException(400, "from and to query parameters required")

    graph = build_memory_graph(MEMORY_DIR)
    path_result = shortest_path(graph, from_path, to_path)
    if not path_result:
        return {"found": False, "path": [], "edges": [], "distance": 0, "connectionType": "none"}

    path_edges = []
    for i in range(len(path_result) - 1):
        path_edges.append({
            "id": f"{path_result[i]}->{path_result[i + 1]}",
            "source": path_result[i],
            "target": path_result[i + 1],
            "type": "wikilink",
            "weight": 1.0,
        })
    return {
        "found": True,
        "path": path_result,
        "edges": path_edges,
        "distance": len(path_result) - 1,
        "connectionType": "graph_traversal",
    }


@app.get("/api/recall-log")
def get_recall_log(limit: int = 20) -> dict:
    """Recent recall events from the ring buffer."""
    if not _GRAPH_AVAILABLE:
        return {"events": [], "totalEvents": 0}
    store = RecallLogStore()
    events = store.get_recent(min(limit, 50))
    return {"events": events, "totalEvents": len(events)}


@app.get("/api/canvas")
def list_canvases() -> dict:
    """List available .canvas files."""
    canvases = []
    if CANVAS_DIR.exists():
        for f in CANVAS_DIR.glob("*.canvas"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                canvases.append({
                    "name": f.stem,
                    "path": f"_canvas/{f.name}",
                    "nodeCount": len(data.get("nodes", [])),
                    "edgeCount": len(data.get("edges", [])),
                })
            except Exception:
                continue
    return {"canvases": canvases}


@app.get("/api/canvas/{name}")
def get_canvas(name: str) -> dict:
    """Return parsed JSON Canvas data (jsoncanvas.org spec)."""
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise HTTPException(400, "Invalid canvas name")
    canvas_path = CANVAS_DIR / f"{name}.canvas"
    if not canvas_path.exists():
        raise HTTPException(404, f"Canvas '{name}' not found")
    return json.loads(canvas_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Static frontend — MUST be last (after all /api/* routes)
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="frontend")
