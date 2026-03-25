# SharedMem

Local MCP server that gives your AI agents a shared semantic memory. It indexes local knowledge sources into a persistent vector store so MCP-compatible clients can search and reuse context across sessions.

## What it does

SharedMem indexes your local knowledge sources — Obsidian vaults, agent memories, skills, project configs — into a single ChromaDB vector store. Any MCP-compatible agent can then search across everything with natural language queries.

```
┌──────────────┐        ┌──────────────┐
│  Claude Code │        │  Codex CLI   │
│  (Anthropic) │        │  (OpenAI)    │
└──────┬───────┘        └──────┬───────┘
       │  MCP stdio            │  MCP stdio
       └──────────┬────────────┘
                  ▼
   ┌────────────────────────────┐
   │   SharedMem MCP Server     │
   │                            │
   │   ChromaDB (persistent)    │
   │   all-MiniLM-L6-v2 local  │
   │   Watchdog auto-reindex    │
   └────────────────────────────┘
```

## Tools

| Tool | Description |
|------|-------------|
| `recall(query, top_k, source, doc_type)` | Semantic search across all indexed sources |
| `remember(content, tags, source, doc_type)` | Store a new memory manually |
| `list_sources()` | Show indexed sources and document counts |
| `reindex(source_name?)` | Re-scan files from configured sources |
| `forget(memory_id)` | Delete a specific memory |

## Indexed sources

Configured in `config.yaml` or `config.local.yaml`. Example setup:

| Source | Path | Type | What |
|--------|------|------|------|
| `claude_memory` | `~/.claude/projects/` | agent_memory | Claude Code project memories |
| `claude_agent_memory` | `~/.claude/agent-memory/` | agent_memory | Claude Code agent memories |
| `claude_skills` | `~/.claude/skills/` | skill | Claude Code skill definitions |
| `notes` | `~/Documents/notes/` | vault | Markdown notes or an Obsidian vault |
| `repo_docs` | `~/GitHub/` | project_config | `CLAUDE.md` and `AGENTS.md` files |

## Setup

### Prerequisites

- Python 3.11+ (managed by uv)
- [uv](https://docs.astral.sh/uv/) package manager

### Install

```bash
cd /path/to/sharedmem
uv sync
cp config.example.yaml config.yaml
```

First run downloads the embedding model (~80MB) and indexes all sources. Subsequent starts use the persisted ChromaDB store.

The server resolves configuration in this order:

1. `SHAREDMEM_CONFIG`
2. `config.local.yaml`
3. `config.yaml`
4. `config.example.yaml`

### Configure Claude Code

File: `~/.claude/.mcp.json`

```json
{
  "mcpServers": {
    "sharedmem": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/sharedmem", "python", "-m", "sharedmem"],
      "env": {}
    }
  }
}
```

### Configure Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.sharedmem]
command = "uv"
args = ["run", "python", "-m", "sharedmem"]
cwd = "/path/to/sharedmem"
startup_timeout_sec = 30
tool_timeout_sec = 60
enabled = true
```

Or via CLI:

```bash
codex mcp add sharedmem -- uv run --directory /path/to/sharedmem python -m sharedmem
```

## Security and privacy

SharedMem is intended for local use with trusted MCP clients. It can return indexed file contents and absolute local file paths in tool results such as `recall()` and `list_sources()`.

Do not connect untrusted agents or remote clients to a SharedMem instance that indexes personal notes, credentials, or sensitive work documents.

## Usage

Once configured, the tools are available in any agent session automatically.

### Search for information

```
recall("how does the CP-SAT solver work")
recall("meeting notes about quarterly planning", source="notes")
recall("project setup instructions", doc_type="project_config")
```

### Store a shared memory

```
remember("Decision: keep weekly planning notes in the team vault", tags=["decision", "knowledge-base"])
```

### Check what's indexed

```
list_sources()
```

### Refresh the index

```
reindex()                    # all sources
reindex("notes")             # specific source
```

## Architecture

```
src/sharedmem/
├── server.py    # MCP server (FastMCP, 5 tools, stdio transport)
├── store.py     # ChromaDB persistent wrapper (add/query/delete)
├── indexer.py   # File scanner, markdown chunking, batch indexer
├── watcher.py   # Watchdog file watcher with debouncing
├── config.py    # YAML configuration loader
└── __main__.py  # Entry point
```

- **Embeddings**: `all-MiniLM-L6-v2` via ONNX Runtime (local, no API calls)
- **Storage**: ChromaDB persistent mode in `data/` (single SQLite file + HNSW index)
- **Chunking**: Markdown files split by headers (`##`/`###`) when >2000 chars
- **Auto-reindex**: Watchdog monitors all source directories; changes are debounced (2s default) and re-indexed automatically
- **Frontmatter**: Extracted and stored as searchable metadata

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit it to add or modify sources:

```yaml
sources:
  my_new_source:
    path: ~/path/to/directory
    patterns:
      - "**/*.md"
      - "**/*.yaml"
    exclude:
      - ".git/"
      - "node_modules/"
    type: my_type
```

After editing, call `reindex()` from any agent or restart the server.

## Resource footprint

- ~200MB RAM (ChromaDB + embedding model)
- CPU negligible except during reindexing
- Disk: `data/` directory grows with indexed content (~50MB for 9K docs)
- Single Python process, no Docker, no external services
