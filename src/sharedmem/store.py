"""ChromaDB persistent store for SharedMem."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import chromadb


class MemoryStore:
    """Wrapper around ChromaDB for memory storage and retrieval."""

    COLLECTION_NAME = "sharedmem"

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(data_dir))
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def count(self) -> int:
        return self._collection.count()

    @staticmethod
    def _doc_id(source: str, path: str, chunk: int = 0) -> str:
        """Deterministic ID from source + path + chunk index."""
        raw = f"{source}::{path}::{chunk}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def add(
        self,
        content: str,
        source: str,
        file_path: str = "",
        doc_type: str = "general",
        chunk_index: int = 0,
        tags: list[str] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Add or update a document in the store. Returns the doc ID."""
        doc_id = self._doc_id(source, file_path or content[:100], chunk_index)

        metadata: dict[str, Any] = {
            "source": source,
            "file_path": file_path,
            "type": doc_type,
            "chunk_index": chunk_index,
            "indexed_at": time.time(),
        }
        if tags:
            metadata["tags"] = ",".join(tags)
        if extra_metadata:
            metadata.update(extra_metadata)

        self._collection.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata],
        )
        return doc_id

    def query(
        self,
        text: str,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search. Returns list of {id, content, metadata, distance}."""
        kwargs: dict[str, Any] = {
            "query_texts": [text],
            "n_results": min(top_k, self.count) if self.count > 0 else 1,
        }
        if where:
            kwargs["where"] = where

        if self.count == 0:
            return []

        results = self._collection.query(**kwargs)

        hits = []
        for i in range(len(results["ids"][0])):
            hits.append({
                "id": results["ids"][0][i],
                "content": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "distance": results["distances"][0][i] if results.get("distances") else None,
            })
        return hits

    def delete(self, doc_id: str) -> bool:
        """Delete a document by ID."""
        try:
            self._collection.delete(ids=[doc_id])
            return True
        except Exception:
            return False

    def delete_by_source(self, source: str) -> int:
        """Delete all documents from a given source. Returns count deleted."""
        results = self._collection.get(where={"source": source})
        if not results["ids"]:
            return 0
        self._collection.delete(ids=results["ids"])
        return len(results["ids"])

    def delete_by_path(self, file_path: str) -> int:
        """Delete all chunks for a specific file path."""
        results = self._collection.get(where={"file_path": file_path})
        if not results["ids"]:
            return 0
        self._collection.delete(ids=results["ids"])
        return len(results["ids"])

    def get_sources_stats(self) -> dict[str, int]:
        """Get document count per source."""
        all_docs = self._collection.get(include=["metadatas"])
        stats: dict[str, int] = {}
        for meta in all_docs["metadatas"]:
            src = meta.get("source", "unknown")
            stats[src] = stats.get(src, 0) + 1
        return stats

    def list_by_source(self, source: str, limit: int = 50) -> list[dict[str, Any]]:
        """List documents from a source."""
        results = self._collection.get(
            where={"source": source},
            include=["documents", "metadatas"],
            limit=limit,
        )
        items = []
        for i in range(len(results["ids"])):
            items.append({
                "id": results["ids"][i],
                "content": results["documents"][i][:200] + "..." if len(results["documents"][i]) > 200 else results["documents"][i],
                "metadata": results["metadatas"][i],
            })
        return items
