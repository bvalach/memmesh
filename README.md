# SharedMem

Local MCP server that gives AI agents a shared semantic memory. It indexes local knowledge sources into a persistent store so MCP-compatible clients can search and reuse context across sessions.

## What it does

SharedMem indexes local knowledge sources — notes, agent memories, skills, project configs — into a single searchable store. Any MCP-compatible agent can then search across everything with natural language queries.

The MemMesh 2.0 surface adds budget-aware retrieval on top of that index. Agents
should ask for a compact `memory_brief` first, escalate to `memory_pack` only
when they need broader context, and use `memory_entity` when they already know
the repo, host, agent, workflow, or other persistent object they are working on.
After dense work, agents can store a compact `turn_summary` and selectively
promote only durable facts with `promote_memory`.

```
┌──────────────┐        ┌──────────────┐
│  MCP Client  │        │  MCP Client  │
│      A       │        │      B       │
└──────┬───────┘        └──────┬───────┘
       │  stdio MCP            │  stdio MCP
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
| `memory_brief(query, budget, scope_project, scope_agent, entity)` | Compact, budget-aware retrieval for default context use |
| `memory_pack(query, scope_project, scope_agent, entity, top_k)` | Wider context pack with summaries, facts, entities, citations, and excerpts |
| `memory_entity(entity_id, scope_project, scope_agent, top_k)` | Consolidated view of one known entity |
| `decision_record(summary, facts, entity_refs, ...)` | Store a durable decision as structured memory |
| `entity_update(entity_id, summary, facts, ...)` | Store a durable update about a persistent entity |
| `turn_summary(objective, actions, result, ...)` | Compact a dense interaction into operational memory |
| `promote_memory(memory_id, kind, summary, ...)` | Promote a retrieved memory into a durable structured memory |
| `recall(query, top_k, source, doc_type)` | Semantic search across all indexed sources |
| `remember(content, tags, source, doc_type)` | Store a new memory manually |
| `list_sources()` | Show indexed sources and document counts |
| `reindex(source_name?)` | Re-scan files from configured sources |
| `forget(memory_id)` | Delete a specific memory |

## Indexed sources

Configured in `config.yaml` or `config.local.yaml`. Example setup:

| Source | Path | Type | What |
|--------|------|------|------|
| `notes` | `/path/to/notes/` | notes | Markdown notes |
| `agent_memory` | `/path/to/agent-memory/` | agent_memory | Agent memory files |
| `skills` | `/path/to/skills/` | skill | Skill or workflow definitions |
| `repo_docs` | `/path/to/repos/` | project_config | Project instruction files |

## Setup

### Prerequisites

- Python 3.11+ (managed by uv)
- [uv](https://docs.astral.sh/uv/) package manager

### Install

```bash
cd /path/to/memmesh
uv sync
cp config.example.yaml config.yaml
```

First run downloads the embedding model (~80MB) and indexes all sources. Subsequent starts use the persisted ChromaDB store.

The server resolves configuration in this order:

1. `SHAREDMEM_CONFIG`
2. `config.local.yaml`
3. `config.yaml`
4. `config.example.yaml`

### Configure MCP Clients

Use this shape for MCP clients that read JSON configuration:

```json
{
  "mcpServers": {
    "sharedmem": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/memmesh", "python", "-m", "sharedmem.server"],
      "cwd": "/path/to/memmesh",
      "env": {}
    }
  }
}
```

For MCP clients that read TOML configuration:

```toml
[mcp_servers.sharedmem]
command = "uv"
args = ["run", "--directory", "/path/to/memmesh", "python", "-m", "sharedmem.server"]
cwd = "/path/to/memmesh"
startup_timeout_sec = 30
tool_timeout_sec = 60
enabled = true
env = {}
```

Some clients also support CLI registration. Use the equivalent command for
your client:

```bash
mcp-client add sharedmem -- uv run --directory /path/to/memmesh python -m sharedmem.server
```

For clients that use an array-style local command:

```json
{
  "mcp": {
    "sharedmem": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/path/to/memmesh", "python", "-m", "sharedmem.server"],
      "environment": {},
      "enabled": true,
      "timeout": 60000
    }
  }
}
```

Set `SHAREDMEM_BACKEND=simple` when you want the lightweight lexical backend
instead of ChromaDB, for example in constrained or sandboxed runtimes.

## Security and privacy

SharedMem is intended for local use with trusted MCP clients. It can return indexed file contents and absolute local file paths in tool results such as `recall()` and `list_sources()`.

Do not connect untrusted agents or remote clients to a SharedMem instance that indexes personal notes, credentials, or sensitive work documents.

Keep local runtime files out of version control. `config.yaml`,
`config.local.yaml`, `.env*`, `data/`, and `data_runtime/` are intentionally
gitignored because they may contain private paths, indexed text, or local state.

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

### Get a compact memory brief

```
memory_brief("what do we know about the agent gateway", entity="agent-gateway")
memory_brief("decisions about project-alpha", budget="medium", scope_project="project-alpha")
```

Use this as the default retrieval call. It returns short answer bullets and
citations instead of dragging full chunks into context.

### Escalate to a context pack

```
memory_pack("architecture and deployment notes", scope_project="project-alpha", top_k=6)
```

Use a pack only when the brief is not enough. It includes compact excerpts plus
structured metadata.

### Open an entity profile

```
memory_entity("agent-gateway")
memory_entity("project-alpha", top_k=8)
```

This is useful when the agent already knows the object it is working on and
does not need a broad thematic search.

### Store a structured decision

```
decision_record(
  "Use the private network for remote access to the agent gateway",
  facts=["The gateway should not bind to a public interface", "Remote access uses a private network"],
  entity_refs=["agent-gateway"],
  scope_project="project-alpha",
  dedupe_key="agent_gateway_remote_access_policy"
)
```

When `dedupe_key` matches an existing active memory and the content differs,
SharedMem returns `conflict_detected` and writes a `memory_conflict` record
instead of silently overwriting the existing decision. Use
`conflict_policy="replace"` only when the new decision intentionally supersedes
the old one.

### Store an entity update

```
entity_update(
  "project-alpha",
  "Project Alpha now uses the shared deployment workflow",
  facts=["Deployment steps are tracked as structured memory"],
  scope_project="project-alpha"
)
```

### Compact a dense turn

```
turn_summary(
  "Implement budget-aware memory retrieval",
  actions=["added memory_brief", "added memory_pack"],
  result="agents can request compact memory context",
  artifacts=["src/sharedmem/server.py", "src/sharedmem/memory.py"],
  decisions=["Agents should call memory_brief before memory_pack"],
  open_questions=["whether automatic preflight context is needed"],
  next_step="observe real agent usage",
  entity_refs=["sharedmem"],
  scope_project="sharedmem"
)
```

### Promote selectively

```
promote_memory(
  "c916a14cd2e2c6c4",
  "decision_record",
  summary="Agents should call memory_brief before memory_pack",
  facts=["memory_brief returns short bullets and citations"],
  entity_refs=["sharedmem"],
  scope_project="sharedmem",
  dedupe_key="default_retrieval_policy"
)
```

Allowed promotion kinds are `decision_record`, `entity_update`,
`preference_signal`, and `derived_artifact`.

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
├── server.py    # MCP server (FastMCP tools, stdio transport)
├── memory.py    # MemMesh 2.0 helpers for structured memory + budget retrieval
├── store.py     # ChromaDB/simple persistent wrapper (add/query/delete)
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
    path: /path/to/directory
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
