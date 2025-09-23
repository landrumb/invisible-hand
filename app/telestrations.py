"""Helpers for telestrations gameplay configuration and utilities."""

from __future__ import annotations

from typing import Iterable, List, Optional, Set


def extract_seed_prompts(params: Optional[dict] = None) -> List[str]:
    """Return a cleaned, deduplicated list of seed prompts from configuration."""

    if not isinstance(params, dict):
        return []

    seeds = params.get("seed_prompts")
    if isinstance(seeds, str):
        items: Iterable[str] = [seeds]
    elif isinstance(seeds, (list, tuple, set, frozenset)):
        items = seeds  # type: ignore[assignment]
    else:
        return []

    cleaned_prompts: List[str] = []
    seen: Set[str] = set()
    for value in items:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        normalized = cleaned.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned_prompts.append(cleaned)
    return cleaned_prompts

