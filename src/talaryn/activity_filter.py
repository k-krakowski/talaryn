"""Presentation-only activity filters; never used to control transfers."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any


DEFAULT_HIDDEN_ACTIVITY_PATTERNS = (
    "*.sglock",
    ".~lock.*#",
    ".DS_Store",
    "*.swp",
    "*.swo",
    "*.swn",
)


def normalize_activity_patterns(value: object) -> list[str]:
    """Accept one file/folder name glob per line, preserving literal # characters."""
    if isinstance(value, str):
        entries = value.splitlines()
    elif isinstance(value, (list, tuple)):
        entries = value
    else:
        entries = DEFAULT_HIDDEN_ACTIVITY_PATTERNS
    return list(dict.fromkeys(
        entry.strip() for entry in entries
        if isinstance(entry, str) and entry.strip()
    ))


def activity_is_visible(
    item: dict[str, Any], patterns: tuple[str, ...] | list[str],
) -> bool:
    """Hide matching names and their descendants, but always expose errors."""
    if not patterns or item.get("state") == "error" or item.get("error"):
        return True
    components = str(item.get("path", "")).split("/")
    return not any(
        fnmatchcase(component, pattern)
        for component in components if component
        for pattern in patterns
    )
