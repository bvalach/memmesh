"""SharedMem MCP server — shared memory for multi-agent collaboration."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .indexer import index_all, index_source
from .memory import (
    build_metadata,
    build_structured_content,
    conflict_summary,
    make_excerpt,
    memory_fingerprint,
    memory_brief_payload,
    memory_pack_payload,
    matches_scope,
    normalize_budget,
    normalize_list,
    rank_results,
    structured_path,
)
from .store import MemoryStore, preferred_backend
from .watcher import start_watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("sharedmem")

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent
FALLBACK_DATA_DIR = PROJECT_ROOT / "data_runtime"


def _resolve_config_path() -> Path:
    env_path = os.environ.get("SHAREDMEM_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()

    for name in ("config.local.yaml", "config.yaml", "config.example.yaml"):
        candidate = PROJECT_ROOT / name
        if candidate.exists():
            return candidate

    return PROJECT_ROOT / "config.yaml"


class Runtime:
    """Lazy bootstrap so MCP initialize can complete before heavy startup."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._config = None
        self._store = None
        self._observer = None
        self._lock = threading.RLock()
        self._bootstrap_started = False
        self._bootstrap_thread = None
        self._degraded_reason: str | None = None

    @property
    def config(self):
        with self._lock:
            if self._config is None:
                self._config = load_config(_resolve_config_path())
            return self._config

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    def _probe_store_dir(self, data_dir: Path) -> tuple[bool, str | None]:
        """Check ChromaDB startup in a subprocess to avoid crashing the MCP server."""
        probe_code = (
            "import chromadb; "
            f"client = chromadb.PersistentClient(path={str(data_dir)!r}); "
            "client.get_or_create_collection("
            "name='sharedmem', metadata={'hnsw:space': 'cosine'})"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", probe_code],
                check=False,
                capture_output=True,
                env=os.environ.copy(),
                text=True,
                timeout=15,
            )
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

        if result.returncode == 0:
            return True, None
        if result.returncode < 0:
            return False, f"probe died with signal {-result.returncode}"

        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            detail = detail.splitlines()[-1]
        return False, detail or f"probe exited with code {result.returncode}"

    def _select_data_dir(self) -> Path:
        configured = self.config.resolved_data_dir
        configured.mkdir(parents=True, exist_ok=True)

        if preferred_backend() == "simple":
            self._degraded_reason = None
            return configured

        ok, reason = self._probe_store_dir(configured)
        if ok:
            self._degraded_reason = None
            return configured

        FALLBACK_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._degraded_reason = (
            f"Primary store {configured} is not usable in this runtime: {reason}. "
            f"Using fallback store at {FALLBACK_DATA_DIR}."
        )
        logger.error(self._degraded_reason)
        return FALLBACK_DATA_DIR

    @property
    def store(self) -> MemoryStore:
        with self._lock:
            if self._store is None:
                self._store = MemoryStore(self._select_data_dir())
            return self._store

    def ensure_bootstrap(self) -> None:
        with self._lock:
            if self._bootstrap_started:
                return
            self._bootstrap_started = True

        store = self.store

        try:
            if store.count == 0 and self.config.sources:
                logger.info("Store is empty — running initial index...")
                results = index_all(store, self.config)
                total = sum(results.values())
                logger.info("Initial index complete: %d chunks across %d sources", total, len(results))
        except Exception:
            logger.exception("Initial index failed — continuing without it")

        try:
            self._observer = start_watcher(store, self.config)
        except Exception:
            logger.exception("File watcher failed to start — continuing without it")

    def ensure_bootstrap_async(self) -> None:
        with self._lock:
            if self._bootstrap_thread is not None:
                return
            self._bootstrap_thread = threading.Thread(
                target=self.ensure_bootstrap,
                name="sharedmem-bootstrap",
                daemon=True,
            )
            self._bootstrap_thread.start()


runtime = Runtime(PROJECT_ROOT)

# FastMCP server
mcp = FastMCP(
    "sharedmem",
    instructions=(
        "SharedMem is a shared memory system. Use 'memory_brief' first for "
        "budget-aware context, then 'memory_pack' or 'memory_entity' only when "
        "more detail is needed. Use 'turn_summary' after dense work, "
        "'promote_memory' to selectively persist reusable facts, and "
        "'decision_record'/'entity_update' for durable structured memories. "
        "Structured writes with a conflicting dedupe_key are flagged instead "
        "of silently overwritten. Legacy 'recall' and 'remember' are available "
        "for direct search and manual notes."
    ),
)


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------

STRUCTURED_SOURCE = "structured_memory"
SESSION_SOURCE = "session_compaction"
CONFLICT_KIND = "memory_conflict"


def _json(data: dict | list) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _find_dedupe_conflicts(kind: str, dedupe_key: str, fingerprint: str) -> tuple[list[dict], list[dict]]:
    """Return exact duplicates and same-key conflicts for a structured write."""
    if not dedupe_key:
        return [], []

    existing = runtime.store.find_by_metadata({"dedupe_key": dedupe_key}, limit=20)
    active = [
        item for item in existing
        if item["metadata"].get("kind") != CONFLICT_KIND
        and item["metadata"].get("status", "active") == "active"
    ]
    duplicates = [
        item for item in active
        if item["metadata"].get("kind") == kind
        and item["metadata"].get("fingerprint") == fingerprint
    ]
    conflicts = [
        item for item in active
        if item["metadata"].get("fingerprint") != fingerprint
    ]
    return duplicates, conflicts


def _store_conflict(
    *,
    kind: str,
    summary: str,
    dedupe_key: str,
    existing: list[dict],
    entity_refs: list[str] | None = None,
    scope_user: str = "user",
    scope_project: str = "",
    scope_agent: str = "",
    visibility: str = "shared",
    details: str = "",
) -> str:
    existing_ids = [item["id"] for item in existing]
    conflict_text = conflict_summary(kind, dedupe_key, existing_ids, summary)
    facts = [
        f"candidate_kind={kind}",
        f"dedupe_key={dedupe_key}",
        f"existing_ids={', '.join(existing_ids)}",
    ]
    content = build_structured_content(
        kind=CONFLICT_KIND,
        summary=conflict_text,
        entity_refs=entity_refs,
        facts=facts,
        details=details,
        scope_user=scope_user,
        scope_project=scope_project,
        scope_agent=scope_agent,
        visibility=visibility,
    )
    metadata = build_metadata(
        kind=CONFLICT_KIND,
        summary=conflict_text,
        entity_refs=entity_refs,
        facts=facts,
        scope_user=scope_user,
        scope_project=scope_project,
        scope_agent=scope_agent,
        visibility=visibility,
        confidence="medium",
        dedupe_key=f"conflict:{dedupe_key}:{time.time_ns()}",
        tokens_budget_hint="small",
        status="needs_review",
        conflicts_with=existing_ids,
    )
    return runtime.store.add(
        content=content,
        source=STRUCTURED_SOURCE,
        file_path=structured_path(CONFLICT_KIND, f"{conflict_text} {time.time_ns()}"),
        doc_type=CONFLICT_KIND,
        tags=["conflict", "structured", kind],
        extra_metadata=metadata,
    )


def _store_structured_memory(
    *,
    kind: str,
    summary: str,
    facts: list[str] | None = None,
    entity_refs: list[str] | None = None,
    details: str = "",
    source: str = STRUCTURED_SOURCE,
    tags: list[str] | None = None,
    scope_user: str = "user",
    scope_project: str = "",
    scope_agent: str = "",
    visibility: str = "shared",
    confidence: str = "medium",
    dedupe_key: str = "",
    tokens_budget_hint: str = "small",
    conflict_policy: str = "flag",
    promoted_from: str = "",
    supersedes: list[str] | None = None,
    conflicts_with: list[str] | None = None,
) -> dict:
    """Store structured memory with duplicate and conflict handling."""
    clean_facts = normalize_list(facts)
    clean_entities = normalize_list(entity_refs)
    fingerprint = memory_fingerprint(summary, clean_facts)
    duplicates, conflicts = _find_dedupe_conflicts(kind, dedupe_key, fingerprint)

    if duplicates and conflict_policy != "replace":
        return {
            "status": "duplicate",
            "kind": kind,
            "id": duplicates[0]["id"],
            "dedupe_key": dedupe_key,
            "message": "Equivalent structured memory already exists.",
        }

    if conflicts and conflict_policy == "flag":
        conflict_id = _store_conflict(
            kind=kind,
            summary=summary,
            dedupe_key=dedupe_key,
            existing=conflicts,
            entity_refs=clean_entities,
            scope_user=scope_user,
            scope_project=scope_project,
            scope_agent=scope_agent,
            visibility=visibility,
            details=details,
        )
        return {
            "status": "conflict_detected",
            "kind": kind,
            "conflict_id": conflict_id,
            "existing_ids": [item["id"] for item in conflicts],
            "dedupe_key": dedupe_key,
            "stored": False,
        }

    supersedes_ids = normalize_list(supersedes)
    if conflicts and conflict_policy == "replace":
        supersedes_ids = normalize_list(supersedes_ids + [item["id"] for item in conflicts])

    content = build_structured_content(
        kind=kind,
        summary=summary,
        entity_refs=clean_entities,
        facts=clean_facts,
        details=details,
        scope_user=scope_user,
        scope_project=scope_project,
        scope_agent=scope_agent,
        visibility=visibility,
    )
    metadata = build_metadata(
        kind=kind,
        summary=summary,
        entity_refs=clean_entities,
        facts=clean_facts,
        scope_user=scope_user,
        scope_project=scope_project,
        scope_agent=scope_agent,
        visibility=visibility,
        confidence=confidence,
        dedupe_key=dedupe_key,
        tokens_budget_hint=tokens_budget_hint,
        promoted_from=promoted_from,
        supersedes=supersedes_ids,
        conflicts_with=conflicts_with,
    )

    path_key = dedupe_key if dedupe_key and conflict_policy != "allow" else ""
    doc_id = runtime.store.add(
        content=content,
        source=source,
        file_path=structured_path(kind, summary, path_key),
        doc_type=kind,
        tags=tags or ["structured", kind],
        extra_metadata=metadata,
    )
    return {
        "status": "stored",
        "kind": kind,
        "id": doc_id,
        "dedupe_key": dedupe_key,
        "supersedes": supersedes_ids,
    }


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
    Prefer structured tools such as decision_record, entity_update, and
    turn_summary when the memory has durable operational meaning.
    """
    runtime.ensure_bootstrap()
    doc_id = runtime.store.add(
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

    This legacy search tool returns source excerpts. Use memory_brief first
    when the caller only needs compact context.
    """
    runtime.ensure_bootstrap()
    where = {}
    if source:
        where["source"] = source
    if doc_type:
        where["type"] = doc_type

    results = runtime.store.query(text=query, top_k=top_k, where=where or None)

    formatted = []
    for r in results:
        formatted.append({
            "id": r["id"],
            "content": r["content"],
            "source": r["metadata"].get("source", ""),
            "type": r["metadata"].get("type", ""),
            "file": r["metadata"].get("file_path", ""),
            "indexed_at": r["metadata"].get("indexed_at"),
            "modified_at": r["metadata"].get("modified_at"),
            "distance": round(r["distance"], 4) if r["distance"] is not None else None,
        })
    return json.dumps(formatted, ensure_ascii=False, indent=2)


@mcp.tool()
def memory_brief(
    query: str,
    budget: str = "small",
    scope_project: str | None = None,
    scope_agent: str | None = None,
    entity: str | None = None,
    source: str | None = None,
    doc_type: str | None = None,
) -> str:
    """Return the smallest useful memory context for a task.

    This is the default budget-aware retrieval tool. It searches broadly, ranks
    durable structured memories above raw chunks, and returns short answer
    bullets plus citations instead of large source excerpts.
    """
    runtime.ensure_bootstrap()
    where = {}
    if source:
        where["source"] = source
    if doc_type:
        where["type"] = doc_type

    budget_name = normalize_budget(budget)
    raw_top_k = {"small": 8, "medium": 12, "large": 20}[budget_name]
    search_query = f"{query} {entity or ''}".strip()
    results = runtime.store.query(text=search_query, top_k=raw_top_k, where=where or None)
    filtered = [
        r for r in results
        if matches_scope(r, scope_project=scope_project, scope_agent=scope_agent, entity=entity)
    ]
    payload = memory_brief_payload(filtered, query=query, budget=budget_name)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def memory_pack(
    query: str,
    scope_project: str | None = None,
    scope_agent: str | None = None,
    entity: str | None = None,
    top_k: int = 8,
    source: str | None = None,
    doc_type: str | None = None,
) -> str:
    """Return a wider context pack when a brief is not enough.

    The pack includes ranked memory items with summaries, facts, entities,
    citations, and compact excerpts. Use this sparingly; agents should usually
    call memory_brief first.
    """
    runtime.ensure_bootstrap()
    where = {}
    if source:
        where["source"] = source
    if doc_type:
        where["type"] = doc_type

    requested = max(1, min(top_k, 20))
    search_query = f"{query} {entity or ''}".strip()
    results = runtime.store.query(text=search_query, top_k=max(requested * 2, 10), where=where or None)
    filtered = [
        r for r in results
        if matches_scope(r, scope_project=scope_project, scope_agent=scope_agent, entity=entity)
    ]
    payload = memory_pack_payload(filtered, query=query, max_items=requested)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def memory_entity(
    entity_id: str,
    scope_project: str | None = None,
    scope_agent: str | None = None,
    top_k: int = 10,
) -> str:
    """Return a compact consolidated view of one known entity."""
    runtime.ensure_bootstrap()
    requested = max(1, min(top_k, 20))
    results = runtime.store.query(text=entity_id, top_k=max(requested * 3, 15))
    filtered = [
        r for r in results
        if matches_scope(r, scope_project=scope_project, scope_agent=scope_agent, entity=entity_id)
    ]
    ranked = rank_results(filtered)[:requested]

    decisions = []
    updates = []
    references = []
    for result in ranked:
        meta = result["metadata"]
        item = {
            "id": result["id"],
            "summary": meta.get("summary", ""),
            "facts": meta.get("facts", ""),
            "source": meta.get("source", ""),
            "type": meta.get("type") or meta.get("kind", ""),
            "file": meta.get("file_path", ""),
            "distance": round(result["distance"], 4) if isinstance(result.get("distance"), (int, float)) else None,
        }
        kind = meta.get("kind") or meta.get("type")
        if kind == "decision_record":
            decisions.append(item)
        elif kind == "entity_update":
            updates.append(item)
        else:
            references.append(item)

    payload = {
        "entity": entity_id,
        "scope_project": scope_project,
        "scope_agent": scope_agent,
        "decisions": decisions,
        "updates": updates,
        "references": references,
        "count": len(ranked),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def decision_record(
    summary: str,
    facts: list[str] | None = None,
    entity_refs: list[str] | None = None,
    details: str = "",
    scope_user: str = "user",
    scope_project: str = "",
    scope_agent: str = "",
    visibility: str = "shared",
    confidence: str = "high",
    dedupe_key: str = "",
    conflict_policy: str = "flag",
    supersedes: list[str] | None = None,
    conflicts_with: list[str] | None = None,
) -> str:
    """Store a durable decision as structured memory.

    Use this for conventions, discarded paths, architecture choices, security
    constraints, scheduling rules, and other decisions future agents should not
    rediscover from raw chat history.
    """
    runtime.ensure_bootstrap()
    result = _store_structured_memory(
        kind="decision_record",
        summary=summary,
        entity_refs=entity_refs,
        facts=facts,
        details=details,
        scope_user=scope_user,
        scope_project=scope_project,
        scope_agent=scope_agent,
        visibility=visibility,
        confidence=confidence,
        dedupe_key=dedupe_key,
        tokens_budget_hint="small",
        conflict_policy=conflict_policy,
        supersedes=supersedes,
        conflicts_with=conflicts_with,
        tags=["decision", "structured"],
    )
    return _json(result)


@mcp.tool()
def entity_update(
    entity_id: str,
    summary: str,
    facts: list[str] | None = None,
    details: str = "",
    scope_user: str = "user",
    scope_project: str = "",
    scope_agent: str = "",
    visibility: str = "shared",
    confidence: str = "medium",
    dedupe_key: str = "",
    conflict_policy: str = "flag",
    supersedes: list[str] | None = None,
    conflicts_with: list[str] | None = None,
) -> str:
    """Store a durable update about a persistent entity."""
    runtime.ensure_bootstrap()
    result = _store_structured_memory(
        kind="entity_update",
        summary=summary,
        entity_refs=[entity_id],
        facts=facts,
        details=details,
        scope_user=scope_user,
        scope_project=scope_project,
        scope_agent=scope_agent,
        visibility=visibility,
        confidence=confidence,
        dedupe_key=dedupe_key,
        tokens_budget_hint="small",
        conflict_policy=conflict_policy,
        supersedes=supersedes,
        conflicts_with=conflicts_with,
        tags=["entity", "structured", entity_id],
    )
    return _json(result)


@mcp.tool()
def turn_summary(
    objective: str,
    actions: list[str] | None = None,
    result: str = "",
    artifacts: list[str] | None = None,
    decisions: list[str] | None = None,
    open_questions: list[str] | None = None,
    next_step: str = "",
    entity_refs: list[str] | None = None,
    scope_user: str = "user",
    scope_project: str = "",
    scope_agent: str = "",
    visibility: str = "shared",
    session_id: str = "",
    promote_decisions: bool = False,
) -> str:
    """Store a compact operational summary after dense work."""
    runtime.ensure_bootstrap()
    facts = []
    facts.extend(f"action: {item}" for item in normalize_list(actions))
    if result:
        facts.append(f"result: {result}")
    facts.extend(f"artifact: {item}" for item in normalize_list(artifacts))
    facts.extend(f"decision: {item}" for item in normalize_list(decisions))
    facts.extend(f"open_question: {item}" for item in normalize_list(open_questions))
    if next_step:
        facts.append(f"next_step: {next_step}")

    details = "\n".join([
        f"Objective: {objective}",
        *(f"Action: {item}" for item in normalize_list(actions)),
        f"Result: {result}" if result else "",
        *(f"Artifact: {item}" for item in normalize_list(artifacts)),
        *(f"Decision: {item}" for item in normalize_list(decisions)),
        *(f"Open question: {item}" for item in normalize_list(open_questions)),
        f"Next step: {next_step}" if next_step else "",
    ]).strip()

    result_payload = _store_structured_memory(
        kind="turn_summary",
        summary=objective,
        facts=facts,
        entity_refs=entity_refs,
        details=details,
        source=SESSION_SOURCE,
        tags=["turn_summary", "session_compaction"],
        scope_user=scope_user,
        scope_project=scope_project,
        scope_agent=scope_agent,
        visibility=visibility,
        confidence="medium",
        dedupe_key=session_id,
        tokens_budget_hint="small",
        conflict_policy="replace" if session_id else "allow",
    )

    promoted = []
    if promote_decisions and result_payload.get("status") == "stored":
        for decision in normalize_list(decisions):
            promoted.append(_store_structured_memory(
                kind="decision_record",
                summary=decision,
                facts=[f"Promoted from turn_summary {result_payload['id']}"],
                entity_refs=entity_refs,
                details=f"Promoted from session compaction: {objective}",
                scope_user=scope_user,
                scope_project=scope_project,
                scope_agent=scope_agent,
                visibility=visibility,
                confidence="medium",
                dedupe_key="",
                tokens_budget_hint="small",
                conflict_policy="allow",
                promoted_from=result_payload["id"],
                tags=["decision", "structured", "promoted"],
            ))

    result_payload["promoted"] = promoted
    return _json(result_payload)


@mcp.tool()
def promote_memory(
    memory_id: str,
    kind: str,
    summary: str = "",
    facts: list[str] | None = None,
    entity_refs: list[str] | None = None,
    reason: str = "",
    scope_user: str = "user",
    scope_project: str = "",
    scope_agent: str = "",
    visibility: str = "shared",
    confidence: str = "medium",
    dedupe_key: str = "",
    conflict_policy: str = "flag",
    supersedes: list[str] | None = None,
    conflicts_with: list[str] | None = None,
) -> str:
    """Promote a retrieved memory into a durable structured memory."""
    runtime.ensure_bootstrap()
    allowed = {"decision_record", "entity_update", "preference_signal", "derived_artifact"}
    if kind not in allowed:
        return _json({"error": f"Unsupported promotion kind '{kind}'", "allowed": sorted(allowed)})

    source_item = runtime.store.get(memory_id)
    if not source_item:
        return _json({"status": "not_found", "id": memory_id})

    source_meta = source_item["metadata"]
    promoted_summary = summary or source_meta.get("summary") or make_excerpt(source_item["content"], 260)
    promoted_facts = normalize_list(facts)
    if not promoted_facts and source_meta.get("facts"):
        promoted_facts = [str(source_meta["facts"])]

    promoted_entities = normalize_list(entity_refs)
    if not promoted_entities and source_meta.get("entities"):
        promoted_entities = [item.strip() for item in str(source_meta["entities"]).split(",") if item.strip()]

    details = "\n".join([
        f"Promotion reason: {reason}" if reason else "Promotion reason: not specified",
        f"Source memory: {memory_id}",
        "Source excerpt:",
        make_excerpt(source_item["content"], 900),
    ])

    result = _store_structured_memory(
        kind=kind,
        summary=promoted_summary,
        facts=promoted_facts,
        entity_refs=promoted_entities,
        details=details,
        source=STRUCTURED_SOURCE,
        tags=["structured", "promoted", kind],
        scope_user=scope_user,
        scope_project=scope_project or source_meta.get("scope_project", ""),
        scope_agent=scope_agent or source_meta.get("scope_agent", ""),
        visibility=visibility,
        confidence=confidence,
        dedupe_key=dedupe_key,
        tokens_budget_hint="small" if kind != "derived_artifact" else "medium",
        conflict_policy=conflict_policy,
        promoted_from=memory_id,
        supersedes=supersedes,
        conflicts_with=conflicts_with,
    )
    return _json(result)


@mcp.tool()
def list_sources() -> str:
    """Show all indexed sources and their document counts."""
    runtime.ensure_bootstrap()
    stats = runtime.store.get_sources_stats()
    configured = {s.name: str(s.resolved_path) for s in runtime.config.sources}

    output = {
        "total_documents": runtime.store.count,
        "backend": runtime.store.backend,
        "degraded_reason": runtime.degraded_reason,
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
    """Re-scan and index files from configured sources."""
    runtime.ensure_bootstrap()
    if source_name:
        src = next((s for s in runtime.config.sources if s.name == source_name), None)
        if not src:
            available = [s.name for s in runtime.config.sources]
            return json.dumps({"error": f"Source '{source_name}' not found", "available": available})
        runtime.store.delete_by_source(source_name)
        count = index_source(runtime.store, src, runtime.config)
        return json.dumps({"status": "reindexed", "source": source_name, "chunks": count})

    results = index_all(runtime.store, runtime.config)
    return json.dumps({"status": "reindexed_all", "chunks_per_source": results, "total": sum(results.values())})


@mcp.tool()
def forget(memory_id: str) -> str:
    """Delete a specific memory by its ID."""
    runtime.ensure_bootstrap()
    ok = runtime.store.delete(memory_id)
    return json.dumps({"status": "deleted" if ok else "not_found", "id": memory_id})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server (stdio transport)."""
    runtime.ensure_bootstrap_async()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
