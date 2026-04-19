# SharedMem / MemMesh

MCP server de memoria compartida entre agentes IA compatibles con MCP.

## Quick Commands

```bash
cd /path/to/memmesh
uv sync                              # Install dependencies
uv run python -m sharedmem.server    # Run MCP server (stdio)
```

## Architecture

- `src/sharedmem/server.py` — MCP server (FastMCP tools)
- `src/sharedmem/memory.py` — MemMesh 2.0 helpers for structured memory + budget retrieval
- `src/sharedmem/store.py` — ChromaDB/simple persistent wrapper
- `src/sharedmem/indexer.py` — File scanner + indexer
- `src/sharedmem/watcher.py` — Watchdog file watcher
- `src/sharedmem/config.py` — Configuration loader
- `config.example.yaml` — Public example config
- `config.yaml` / `config.local.yaml` — Local private configs (gitignored)
- `data/` — ChromaDB persistent storage (gitignored)

## MCP Tools

| Tool | Purpose |
|------|---------|
| `memory_brief` | Compact, budget-aware retrieval for default context use |
| `memory_pack` | Wider context pack when a brief is insufficient |
| `memory_entity` | Consolidated view of one known entity |
| `decision_record` | Store a durable decision as structured memory |
| `entity_update` | Store a durable update about a persistent entity |
| `turn_summary` | Compact dense work into operational session memory |
| `promote_memory` | Promote selected memories into durable structured records |
| `remember` | Store a new memory manually |
| `recall` | Semantic search across all indexed sources |
| `list_sources` | Show indexed sources and stats |
| `reindex` | Re-scan and index a source |
| `forget` | Delete a specific memory |

## MemMesh 2.0 Policy

- Use `memory_brief` before `memory_pack`.
- Use `turn_summary` after dense work or task closure.
- Promote only durable decisions, preferences, entity updates, and derived artifacts.
- Provide `dedupe_key` for stable policies; conflicting writes are flagged as `memory_conflict` by default.
- Keep examples generic and never commit private local paths, secrets, personal notes, or indexed data.
