"""SharedMem MCP server — shared memory for multi-agent collaboration."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .indexer import index_all, index_source
from .store import MemoryStore
from .watcher import start_watcher

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logger = logging.getLogger("sharedmem")

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _resolve_config_path() -> Path:
    env_path = os.environ.get("SHAREDMEM_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()

    for name in ("config.local.yaml", "config.yaml", "config.example.yaml"):
        candidate = PROJECT_ROOT / name
        if candidate.exists():
            return candidate

    return PROJECT_ROOT / "config.yaml"


config = load_config(_resolve_config_path())
store = MemoryStore(config.resolved_data_dir)

# FastMCP server
mcp = FastMCP(
    "sharedmem",
    instructions=(
        "SharedMem is a shared memory system. Use 'recall' to search for "
        "information across all indexed sources (Obsidian vaults, agent memories, "
        "skills, project configs). Use 'remember' to store new memories. "
        "Use 'reindex' after adding new sources."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def remember(
    content: str,
    tags: list[str] | None = None,
    source: str = "manual",
    doc_type: str = "note",
) -> str:
    """Store a new memory manually.

    Use this to save important information that should be shared across agents.
    Examples: decisions, user preferences, project context, learnings.

    Args:
        content: The memory content to store.
        tags: Optional tags for categorization (e.g. ["project:myproduction", "decision"]).
        source: Origin identifier (default "manual").
        doc_type: Type of memory (e.g. "note", "decision", "preference", "context").
    """
    doc_id = store.add(
        content=content,
        source=source,
        doc_type=doc_type,
        tags=tags,
    )
    return json.dumps({"status": "stored", "id": doc_id, "chars": len(content)})


@mcp.tool()
def recall(
    query: str,
    top_k: int = 5,
    source: str | None = None,
    doc_type: str | None = None,
) -> str:
    """Search memories semantically across all indexed sources.

    This is the primary tool for finding information. It searches across:
    - Markdown knowledge bases and vaults
    - Agent memories
    - Skill definitions
    - Project configuration files
    - Manual memories

    Args:
        query: Natural language search query.
        top_k: Number of results to return (default 5).
        source: Filter by source name (e.g. "obsidian_personal", "claude_memory").
        doc_type: Filter by document type (e.g. "vault", "skill", "agent_memory").
    """
    where = {}
    if source:
        where["source"] = source
    if doc_type:
        where["type"] = doc_type

    results = store.query(text=query, top_k=top_k, where=where or None)

    formatted = []
    for r in results:
        formatted.append({
            "id": r["id"],
            "content": r["content"],
            "source": r["metadata"].get("source", ""),
            "type": r["metadata"].get("type", ""),
            "file": r["metadata"].get("file_path", ""),
            "distance": round(r["distance"], 4) if r["distance"] is not None else None,
        })
    return json.dumps(formatted, ensure_ascii=False, indent=2)


@mcp.tool()
def list_sources() -> str:
    """Show all indexed sources and their document counts.

    Returns configured sources, how many documents each has indexed,
    and the total memory count.
    """
    stats = store.get_sources_stats()
    configured = {s.name: str(s.resolved_path) for s in config.sources}

    output = {
        "total_documents": store.count,
        "sources": {
            name: {
                "path": configured.get(name, "n/a"),
                "documents": count,
            }
            for name, count in sorted(stats.items())
        },
        "configured_but_empty": [
            name for name in configured if name not in stats
        ],
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


@mcp.tool()
def reindex(source_name: str | None = None) -> str:
    """Re-scan and index files from configured sources.

    Call this after adding new sources to config.yaml or when you want
    to refresh the index with the latest file contents.

    Args:
        source_name: Specific source to reindex. If omitted, reindexes ALL sources.
    """
    if source_name:
        src = next((s for s in config.sources if s.name == source_name), None)
        if not src:
            available = [s.name for s in config.sources]
            return json.dumps({"error": f"Source '{source_name}' not found", "available": available})
        store.delete_by_source(source_name)
        count = index_source(store, src, config)
        return json.dumps({"status": "reindexed", "source": source_name, "chunks": count})

    results = index_all(store, config)
    return json.dumps({"status": "reindexed_all", "chunks_per_source": results, "total": sum(results.values())})


@mcp.tool()
def forget(memory_id: str) -> str:
    """Delete a specific memory by its ID.

    Use recall first to find the memory ID you want to delete.

    Args:
        memory_id: The ID of the memory to delete (from recall results).
    """
    ok = store.delete(memory_id)
    return json.dumps({"status": "deleted" if ok else "not_found", "id": memory_id})


# ---------------------------------------------------------------------------
# Startup: initial index if store is empty
# ---------------------------------------------------------------------------

def _maybe_initial_index() -> None:
    if store.count == 0 and config.sources:
        logger.info("Store is empty — running initial index...")
        results = index_all(store, config)
        total = sum(results.values())
        logger.info("Initial index complete: %d chunks across %d sources", total, len(results))


_maybe_initial_index()

# Start file watcher
_observer = start_watcher(store, config)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server (stdio transport)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
