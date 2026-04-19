"""Storage backends for SharedMem.

Uses ChromaDB by default, with an optional pure-Python backend for constrained
local runtimes where ChromaDB cannot start reliably.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import chromadb


def preferred_backend() -> str:
    """Return the storage backend to use for the current runtime."""
    override = os.environ.get("SHAREDMEM_BACKEND", "").strip().lower()
    if override in {"chroma", "simple"}:
        return override

    if os.environ.get("CODEX_SANDBOX"):
        return "simple"
    return "chroma"


class _SimpleCollection:
    """Small persistent document store with lexical ranking."""

    STORE_FILE = "simple_store.json"

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / self.STORE_FILE
        self._docs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._docs = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._docs = {}

    def _save(self) -> None:
        self._path.write_text(
            json.dumps(self._docs, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9_]{2,}", text.lower()))

    @classmethod
    def _score(cls, query: str, content: str, metadata: dict[str, Any]) -> float:
        query_tokens = cls._tokens(query)
        if not query_tokens:
            return 0.0

        metadata_text = " ".join(str(v) for v in metadata.values() if isinstance(v, (str, int, float)))
        haystack = f"{content}\n{metadata_text}"
        haystack_lower = haystack.lower()
        doc_tokens = cls._tokens(haystack)
        overlap = len(query_tokens & doc_tokens)

        score = overlap / max(len(query_tokens), 1)
        if query.lower() in haystack_lower:
            score += 1.0

        for token in query_tokens:
            if token in haystack_lower:
                score += 0.05
        return score

    @staticmethod
    def _matches_where(metadata: dict[str, Any], where: dict[str, Any] | None) -> bool:
        if not where:
            return True
        return all(metadata.get(key) == value for key, value in where.items())

    def count(self) -> int:
        return len(self._docs)

    def upsert(self, doc_id: str, document: str, metadata: dict[str, Any]) -> None:
        self._docs[doc_id] = {
            "document": document,
            "metadata": metadata,
        }
        self._save()

    def query(
        self,
        text: str,
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        scored = []
        for doc_id, item in self._docs.items():
            metadata = item["metadata"]
            if not self._matches_where(metadata, where):
                continue
            score = self._score(text, item["document"], metadata)
            if score <= 0:
                continue
            scored.append({
                "id": doc_id,
                "content": item["document"],
                "metadata": metadata,
                "distance": round(max(0.0, 1.0 - min(score, 1.0)), 4),
                "score": score,
            })

        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:top_k]

    def delete(self, doc_id: str) -> bool:
        if doc_id not in self._docs:
            return False
        del self._docs[doc_id]
        self._save()
        return True

    def get(self, doc_id: str) -> dict[str, Any] | None:
        item = self._docs.get(doc_id)
        if not item:
            return None
        return {
            "id": doc_id,
            "content": item["document"],
            "metadata": item["metadata"],
        }

    def delete_where(self, predicate) -> int:
        to_delete = [doc_id for doc_id, item in self._docs.items() if predicate(item["metadata"])]
        for doc_id in to_delete:
            del self._docs[doc_id]
        if to_delete:
            self._save()
        return len(to_delete)

    def find_by_metadata(self, where: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        items = []
        for doc_id, item in self._docs.items():
            if not self._matches_where(item["metadata"], where):
                continue
            items.append({
                "id": doc_id,
                "content": item["document"],
                "metadata": item["metadata"],
            })
            if len(items) >= limit:
                break
        return items

    def get_sources_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for item in self._docs.values():
            src = item["metadata"].get("source", "unknown")
            stats[src] = stats.get(src, 0) + 1
        return stats

    def list_by_source(self, source: str, limit: int) -> list[dict[str, Any]]:
        items = []
        for doc_id, item in self._docs.items():
            if item["metadata"].get("source") != source:
                continue
            content = item["document"]
            items.append({
                "id": doc_id,
                "content": content[:200] + "..." if len(content) > 200 else content,
                "metadata": item["metadata"],
            })
            if len(items) >= limit:
                break
        return items


class MemoryStore:
    """Storage wrapper with automatic backend selection."""

    COLLECTION_NAME = "sharedmem"

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._backend = preferred_backend()
        self._simple = None
        self._client = None
        self._collection = None

        if self._backend == "simple":
            self._simple = _SimpleCollection(data_dir)
            return

        self._client = chromadb.PersistentClient(path=str(data_dir))
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def count(self) -> int:
        if self._simple is not None:
            return self._simple.count()
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

        if self._simple is not None:
            self._simple.upsert(doc_id, content, metadata)
            return doc_id

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
        """Search documents and return {id, content, metadata, distance} items."""
        if self.count == 0:
            return []

        if self._simple is not None:
            return self._simple.query(text=text, top_k=top_k, where=where)

        kwargs: dict[str, Any] = {
            "query_texts": [text],
            "n_results": min(top_k, self.count),
        }
        if where:
            kwargs["where"] = where

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
        if self._simple is not None:
            return self._simple.delete(doc_id)
        try:
            self._collection.delete(ids=[doc_id])
            return True
        except Exception:
            return False

    def get(self, doc_id: str) -> dict[str, Any] | None:
        """Return a document by ID, or None when missing."""
        if self._simple is not None:
            return self._simple.get(doc_id)

        results = self._collection.get(ids=[doc_id], include=["documents", "metadatas"])
        if not results["ids"]:
            return None
        return {
            "id": results["ids"][0],
            "content": results["documents"][0],
            "metadata": results["metadatas"][0],
        }

    def delete_by_source(self, source: str) -> int:
        """Delete all documents from a given source. Returns count deleted."""
        if self._simple is not None:
            return self._simple.delete_where(lambda meta: meta.get("source") == source)

        results = self._collection.get(where={"source": source})
        if not results["ids"]:
            return 0
        self._collection.delete(ids=results["ids"])
        return len(results["ids"])

    def delete_by_path(self, file_path: str) -> int:
        """Delete all chunks for a specific file path."""
        if self._simple is not None:
            return self._simple.delete_where(lambda meta: meta.get("file_path") == file_path)

        results = self._collection.get(where={"file_path": file_path})
        if not results["ids"]:
            return 0
        self._collection.delete(ids=results["ids"])
        return len(results["ids"])

    def find_by_metadata(self, where: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
        """Return documents whose metadata exactly matches all keys in where."""
        if self._simple is not None:
            return self._simple.find_by_metadata(where, limit)

        results = self._collection.get(
            where=where,
            include=["documents", "metadatas"],
            limit=limit,
        )
        items = []
        for i in range(len(results["ids"])):
            items.append({
                "id": results["ids"][i],
                "content": results["documents"][i],
                "metadata": results["metadatas"][i],
            })
        return items

    def get_sources_stats(self) -> dict[str, int]:
        """Get document count per source."""
        if self._simple is not None:
            return self._simple.get_sources_stats()

        all_docs = self._collection.get(include=["metadatas"])
        stats: dict[str, int] = {}
        for meta in all_docs["metadatas"]:
            src = meta.get("source", "unknown")
            stats[src] = stats.get(src, 0) + 1
        return stats

    def list_by_source(self, source: str, limit: int = 50) -> list[dict[str, Any]]:
        """List documents from a source."""
        if self._simple is not None:
            return self._simple.list_by_source(source, limit)

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
