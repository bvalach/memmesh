"""Structured memory helpers for budget-aware retrieval."""

from __future__ import annotations

import re
import time
from typing import Any


KIND_PRIORITY = {
    "decision_record": 4,
    "preference_signal": 4,
    "entity_update": 3,
    "derived_artifact": 3,
    "turn_summary": 2,
    "memory_conflict": 1,
}

BUDGETS = {
    "small": {"items": 3, "chars": 220},
    "medium": {"items": 5, "chars": 420},
    "large": {"items": 8, "chars": 700},
}


def clean_text(text: str) -> str:
    """Collapse noisy whitespace without changing the substance."""
    return re.sub(r"\s+", " ", text).strip()


def normalize_budget(budget: str) -> str:
    value = (budget or "small").strip().lower()
    aliases = {
        "short": "small",
        "brief": "small",
        "compact": "small",
        "normal": "medium",
        "full": "large",
        "expanded": "large",
    }
    return aliases.get(value, value if value in BUDGETS else "small")


def normalize_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    cleaned = [clean_text(str(v)) for v in values if clean_text(str(v))]
    return list(dict.fromkeys(cleaned))


def normalized_key(text: str) -> str:
    """Normalize text for cheap deterministic equality checks."""
    text = clean_text(text).lower()
    text = re.sub(r"[^a-z0-9_]+", " ", text)
    return clean_text(text)


def memory_fingerprint(summary: str, facts: list[str] | None = None) -> str:
    """Return a stable fingerprint for a compact structured memory."""
    parts = [normalized_key(summary)]
    parts.extend(normalized_key(fact) for fact in normalize_list(facts))
    return "|".join(part for part in parts if part)


def csv(values: list[str] | None) -> str:
    return ", ".join(normalize_list(values))


def make_excerpt(content: str, max_chars: int) -> str:
    """Return a compact, readable excerpt from a memory body."""
    text = content.strip()
    text = re.sub(r"^---\s.*?\n---\s*", "", text, flags=re.DOTALL)

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        lines.append(line)
        if len(" ".join(lines)) >= max_chars:
            break

    excerpt = clean_text(" ".join(lines) if lines else text)
    if len(excerpt) <= max_chars:
        return excerpt
    return excerpt[: max_chars - 1].rstrip() + "..."


def build_structured_content(
    *,
    kind: str,
    summary: str,
    entity_refs: list[str] | None = None,
    facts: list[str] | None = None,
    details: str = "",
    scope_user: str = "user",
    scope_project: str = "",
    scope_agent: str = "",
    visibility: str = "shared",
    extra_lines: list[str] | None = None,
) -> str:
    """Build a searchable text representation for a structured memory."""
    lines = [
        f"Kind: {kind}",
        f"Visibility: {visibility or 'shared'}",
    ]
    scope_bits = []
    if scope_user:
        scope_bits.append(f"user={scope_user}")
    if scope_project:
        scope_bits.append(f"project={scope_project}")
    if scope_agent:
        scope_bits.append(f"agent={scope_agent}")
    if scope_bits:
        lines.append(f"Scope: {', '.join(scope_bits)}")

    entities = normalize_list(entity_refs)
    if entities:
        lines.append(f"Entities: {', '.join(entities)}")

    lines.append(f"Summary: {clean_text(summary)}")

    fact_lines = normalize_list(facts)
    if fact_lines:
        lines.append("Facts:")
        lines.extend(f"- {fact}" for fact in fact_lines)

    if details.strip():
        lines.append("Details:")
        lines.append(details.strip())

    if extra_lines:
        lines.extend(extra_lines)

    return "\n".join(lines)


def build_metadata(
    *,
    kind: str,
    summary: str,
    entity_refs: list[str] | None = None,
    facts: list[str] | None = None,
    scope_user: str = "user",
    scope_project: str = "",
    scope_agent: str = "",
    visibility: str = "shared",
    confidence: str = "medium",
    dedupe_key: str = "",
    tokens_budget_hint: str = "small",
    status: str = "active",
    promoted_from: str = "",
    supersedes: list[str] | None = None,
    conflicts_with: list[str] | None = None,
) -> dict[str, Any]:
    """Build Chroma-compatible metadata for structured memories."""
    metadata: dict[str, Any] = {
        "kind": kind,
        "summary": clean_text(summary)[:1000],
        "visibility": clean_text(visibility or "shared"),
        "confidence": clean_text(confidence or "medium"),
        "tokens_budget_hint": normalize_budget(tokens_budget_hint),
        "status": clean_text(status or "active"),
        "fingerprint": memory_fingerprint(summary, facts),
        "created_at": time.time(),
    }
    if scope_user:
        metadata["scope_user"] = clean_text(scope_user)
    if scope_project:
        metadata["scope_project"] = clean_text(scope_project)
    if scope_agent:
        metadata["scope_agent"] = clean_text(scope_agent)
    if dedupe_key:
        metadata["dedupe_key"] = clean_text(dedupe_key)
    if promoted_from:
        metadata["promoted_from"] = clean_text(promoted_from)

    entities = csv(entity_refs)
    if entities:
        metadata["entities"] = entities

    superseded = csv(supersedes)
    if superseded:
        metadata["supersedes"] = superseded

    conflicts = csv(conflicts_with)
    if conflicts:
        metadata["conflicts_with"] = conflicts

    fact_text = " | ".join(normalize_list(facts))
    if fact_text:
        metadata["facts"] = fact_text[:1500]

    return metadata


def structured_path(kind: str, summary: str, dedupe_key: str = "") -> str:
    key = clean_text(dedupe_key) if dedupe_key else clean_text(summary)[:120]
    key = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", key).strip("-").lower()
    return f"memory://{kind}/{key or 'untitled'}"


def conflict_summary(kind: str, dedupe_key: str, existing_ids: list[str], new_summary: str) -> str:
    target = f"dedupe_key={dedupe_key}" if dedupe_key else "related memory"
    return (
        f"Potential {kind} conflict for {target}: new memory was not stored "
        f"because it differs from existing memory ids {', '.join(existing_ids)}. "
        f"Candidate summary: {clean_text(new_summary)}"
    )


def matches_scope(
    result: dict[str, Any],
    *,
    scope_project: str | None = None,
    scope_agent: str | None = None,
    entity: str | None = None,
) -> bool:
    """Post-filter a result when structured scope metadata is present."""
    meta = result.get("metadata", {})
    content = result.get("content", "")

    if scope_project and meta.get("scope_project") not in (None, "", scope_project):
        return False
    if scope_agent and meta.get("scope_agent") not in (None, "", scope_agent):
        return False
    if entity:
        needle = entity.lower()
        entities = str(meta.get("entities", "")).lower()
        if needle not in entities and needle not in content.lower():
            return False

    return True


def rank_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer high-value structured memories while keeping retrieval relevance."""
    now = time.time()

    def score(result: dict[str, Any]) -> float:
        meta = result.get("metadata", {})
        distance = result.get("distance")
        relevance = 1.0 - float(distance) if isinstance(distance, (int, float)) else 0.5
        kind_bonus = KIND_PRIORITY.get(meta.get("kind") or meta.get("type"), 1) * 0.05
        created = meta.get("created_at") or meta.get("indexed_at") or 0
        try:
            age_days = max(0.0, (now - float(created)) / 86400)
        except (TypeError, ValueError):
            age_days = 365.0
        freshness = max(0.0, 0.05 - min(age_days, 365.0) / 7300.0)
        return relevance + kind_bonus + freshness

    return sorted(results, key=score, reverse=True)


def memory_brief_payload(
    results: list[dict[str, Any]],
    *,
    query: str,
    budget: str,
) -> dict[str, Any]:
    budget_name = normalize_budget(budget)
    limits = BUDGETS[budget_name]
    ranked = rank_results(results)
    selected = ranked[: limits["items"]]

    answer = []
    citations = []
    for result in selected:
        meta = result.get("metadata", {})
        summary = meta.get("summary") or make_excerpt(result.get("content", ""), limits["chars"])
        answer.append(make_excerpt(str(summary), limits["chars"]))
        citations.append({
            "id": result.get("id"),
            "source": meta.get("source", ""),
            "type": meta.get("type") or meta.get("kind", ""),
            "file": meta.get("file_path", ""),
            "distance": round(result["distance"], 4) if isinstance(result.get("distance"), (int, float)) else None,
        })

    confidence = "none"
    if selected:
        first_distance = selected[0].get("distance")
        if isinstance(first_distance, (int, float)):
            confidence = "high" if first_distance <= 0.35 else "medium" if first_distance <= 0.65 else "low"
        else:
            confidence = "medium"

    return {
        "query": query,
        "budget": budget_name,
        "answer": answer,
        "citations": citations,
        "confidence": confidence,
        "omitted_results": max(0, len(ranked) - len(selected)),
    }


def memory_pack_payload(
    results: list[dict[str, Any]],
    *,
    query: str,
    max_items: int = 8,
) -> dict[str, Any]:
    ranked = rank_results(results)[:max_items]
    items = []
    for result in ranked:
        meta = result.get("metadata", {})
        items.append({
            "id": result.get("id"),
            "kind": meta.get("kind") or meta.get("type", ""),
            "summary": meta.get("summary") or make_excerpt(result.get("content", ""), 260),
            "facts": meta.get("facts", ""),
            "entities": meta.get("entities", ""),
            "source": meta.get("source", ""),
            "file": meta.get("file_path", ""),
            "indexed_at": meta.get("indexed_at"),
            "modified_at": meta.get("modified_at"),
            "distance": round(result["distance"], 4) if isinstance(result.get("distance"), (int, float)) else None,
            "excerpt": make_excerpt(result.get("content", ""), 900),
        })

    return {
        "query": query,
        "items": items,
        "count": len(items),
    }
