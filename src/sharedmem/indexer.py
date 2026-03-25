"""File scanner and indexer for SharedMem."""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path

from .config import Settings, SourceConfig
from .store import MemoryStore

logger = logging.getLogger("sharedmem.indexer")


def _should_exclude(path: Path, excludes: list[str]) -> bool:
    """Check if a path matches any exclude pattern."""
    path_str = str(path)
    for pattern in excludes:
        if pattern in path_str:
            return True
        if fnmatch.fnmatch(path.name, pattern):
            return True
    return False


def _chunk_by_headers(content: str, max_chars: int) -> list[str]:
    """Split markdown content by ## headers if too long."""
    if len(content) <= max_chars:
        return [content]

    # Split by markdown headers (## or ###)
    sections = re.split(r"(?=^#{1,3}\s)", content, flags=re.MULTILINE)
    chunks = []
    current = ""

    for section in sections:
        if len(current) + len(section) > max_chars and current:
            chunks.append(current.strip())
            current = section
        else:
            current += section

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else [content[:max_chars]]


def _extract_frontmatter_context(content: str) -> str:
    """Extract frontmatter metadata as searchable context."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return ""
    return match.group(1)


def scan_source(source: SourceConfig) -> list[Path]:
    """Find all matching files in a source directory."""
    root = source.resolved_path
    if not root.exists():
        logger.warning("Source path does not exist: %s", root)
        return []

    files = []
    for pattern in source.patterns:
        for path in root.glob(pattern):
            if path.is_file() and not _should_exclude(path, source.exclude):
                files.append(path)

    return sorted(set(files))


def index_file(
    store: MemoryStore,
    file_path: Path,
    source_name: str,
    source_type: str,
    max_chars: int,
) -> int:
    """Index a single file into the store. Returns number of chunks indexed."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.error("Failed to read %s: %s", file_path, e)
        return 0

    if not content.strip():
        return 0

    # Extract frontmatter for extra context
    frontmatter = _extract_frontmatter_context(content)

    # Remove old chunks for this file
    store.delete_by_path(str(file_path))

    # Chunk if needed
    chunks = _chunk_by_headers(content, max_chars)

    extra = {}
    if frontmatter:
        extra["frontmatter"] = frontmatter[:500]
    extra["file_name"] = file_path.name
    extra["modified_at"] = file_path.stat().st_mtime

    for i, chunk in enumerate(chunks):
        store.add(
            content=chunk,
            source=source_name,
            file_path=str(file_path),
            doc_type=source_type,
            chunk_index=i,
            extra_metadata=extra,
        )

    return len(chunks)


def index_source(store: MemoryStore, source: SourceConfig, settings: Settings) -> int:
    """Index all files from a source. Returns total chunks indexed."""
    files = scan_source(source)
    if not files:
        logger.info("No files found for source '%s' at %s", source.name, source.resolved_path)
        return 0

    total = 0
    for f in files:
        n = index_file(store, f, source.name, source.type, settings.chunk_max_chars)
        total += n

    logger.info("Indexed %d chunks from %d files in '%s'", total, len(files), source.name)
    return total


def index_all(store: MemoryStore, settings: Settings) -> dict[str, int]:
    """Index all configured sources. Returns chunks per source."""
    results = {}
    for source in settings.sources:
        results[source.name] = index_source(store, source, settings)
    return results
