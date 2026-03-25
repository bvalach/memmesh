# SharedMem

MCP server de memoria compartida entre agentes IA (Codex, Codex CLI).

## Quick Commands

```bash
cd /path/to/sharedmem
uv sync                              # Install dependencies
uv run python -m sharedmem.server    # Run MCP server (stdio)
```

## Architecture

- `src/sharedmem/server.py` — MCP server (FastMCP, 5 tools)
- `src/sharedmem/store.py` — ChromaDB persistent wrapper
- `src/sharedmem/indexer.py` — File scanner + indexer
- `src/sharedmem/watcher.py` — Watchdog file watcher
- `src/sharedmem/config.py` — Configuration loader
- `config.example.yaml` — Public example config
- `config.yaml` / `config.local.yaml` — Local private configs (gitignored)
- `data/` — ChromaDB persistent storage (gitignored)

## MCP Tools

| Tool | Purpose |
|------|---------|
| `remember` | Store a new memory manually |
| `recall` | Semantic search across all indexed sources |
| `list_sources` | Show indexed sources and stats |
| `reindex` | Re-scan and index a source |
| `forget` | Delete a specific memory |
