"""File system watcher for auto-reindexing on changes."""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Settings, SourceConfig
from .indexer import _is_safe_source_file, index_file
from .store import MemoryStore

logger = logging.getLogger("sharedmem.watcher")


class _DebouncedHandler(FileSystemEventHandler):
    """Handles file changes with debouncing to avoid rapid re-indexing."""

    def __init__(
        self,
        store: MemoryStore,
        source: SourceConfig,
        settings: Settings,
    ) -> None:
        self._store = store
        self._source = source
        self._settings = settings
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _is_relevant(self, path: str) -> bool:
        p = Path(path)
        for excl in self._source.exclude:
            if excl in path:
                return False

        try:
            relative_path = str(p.relative_to(self._source.resolved_path))
        except ValueError:
            relative_path = str(p)

        for pattern in self._source.patterns:
            if fnmatch.fnmatch(relative_path, pattern):
                return True
            if fnmatch.fnmatch(p.name, pattern):
                return True

        return False

    def _schedule_flush(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self._settings.debounce_seconds, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths = dict(self._pending)
            self._pending.clear()

        for file_path in paths:
            p = Path(file_path)
            if _is_safe_source_file(p, self._source.resolved_path):
                n = index_file(
                    self._store, p,
                    self._source.name,
                    self._source.type,
                    self._settings.chunk_max_chars,
                )
                logger.info("Re-indexed %s (%d chunks)", p.name, n)
            else:
                # File was deleted or is not a safe source file
                self._store.delete_by_path(file_path)
                logger.info("Removed deleted file from index: %s", p.name)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._is_relevant(str(event.src_path)):
            with self._lock:
                self._pending[str(event.src_path)] = time.time()
            self._schedule_flush()

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._is_relevant(str(event.src_path)):
            with self._lock:
                self._pending[str(event.src_path)] = time.time()
            self._schedule_flush()


def start_watcher(store: MemoryStore, settings: Settings) -> Observer | None:
    """Start watching all configured source directories. Returns the Observer."""
    if not settings.watch or not settings.sources:
        return None
    if os.environ.get("CODEX_SANDBOX"):
        logger.info("Watcher disabled in sandboxed runtime")
        return None

    observer = Observer()
    watched = 0

    for source in settings.sources:
        root = source.resolved_path
        if not root.exists() or not root.is_dir():
            logger.warning("Skipping watcher for %s (path missing or not a dir)", source.name)
            continue

        handler = _DebouncedHandler(store, source, settings)
        observer.schedule(handler, str(root), recursive=True)
        watched += 1
        logger.info("Watching %s → %s", source.name, root)

    if watched == 0:
        return None

    observer.daemon = True
    observer.start()
    logger.info("File watcher started for %d sources", watched)
    return observer
