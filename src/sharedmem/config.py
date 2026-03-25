"""Configuration loader for SharedMem."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SourceConfig:
    name: str
    path: str
    patterns: list[str] = field(default_factory=lambda: ["**/*.md"])
    exclude: list[str] = field(default_factory=list)
    type: str = "general"

    @property
    def resolved_path(self) -> Path:
        return Path(self.path).expanduser().resolve()


@dataclass
class Settings:
    base_dir: Path = field(default_factory=lambda: Path.cwd())
    data_dir: str = "data"
    watch: bool = True
    debounce_seconds: float = 2.0
    chunk_max_chars: int = 2000
    sources: list[SourceConfig] = field(default_factory=list)

    @property
    def resolved_data_dir(self) -> Path:
        data_path = Path(self.data_dir).expanduser()
        if data_path.is_absolute():
            return data_path.resolve()
        return (self.base_dir / data_path).resolve()


def load_config(config_path: Path | None = None) -> Settings:
    """Load configuration from config.yaml."""
    if config_path is None:
        config_path = Path(__file__).parent.parent.parent / "config.yaml"

    if not config_path.exists():
        return Settings()

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    sources = []
    for name, src in raw.get("sources", {}).items():
        sources.append(SourceConfig(
            name=name,
            path=src["path"],
            patterns=src.get("patterns", ["**/*.md"]),
            exclude=src.get("exclude", []),
            type=src.get("type", "general"),
        ))

    return Settings(
        base_dir=config_path.parent.resolve(),
        data_dir=raw.get("data_dir", "data"),
        watch=raw.get("watch", True),
        debounce_seconds=raw.get("debounce_seconds", 2.0),
        chunk_max_chars=raw.get("chunk_max_chars", 2000),
        sources=sources,
    )
