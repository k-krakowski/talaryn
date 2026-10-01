"""Load Talaryn translations without importing GTK into Nautilus workers."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path

SUPPORTED_LANGUAGES = ("en", "pl", "ru")


def _language() -> str:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    try:
        settings = json.loads((config / "talaryn/settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    selected = settings.get("language", "system") if isinstance(settings, dict) else "system"
    if selected in SUPPORTED_LANGUAGES:
        return selected
    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(variable, "")
        if value:
            code = value.split(":")[0].split(".")[0].split("_")[0].lower()
            return code if code in SUPPORTED_LANGUAGES else "en"
    return "en"


def _roots() -> tuple[Path, ...]:
    roots = []
    configured = os.environ.get("TALARYN_DATA_DIR", "").strip()
    if configured:
        roots.append(Path(configured))
    source = Path(__file__).resolve().parents[2]
    if (source / "src/talaryn").is_dir():
        roots.append(source)
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    roots.extend((data / "talaryn", Path("/usr/local/share/talaryn"), Path("/usr/share/talaryn")))
    return tuple(roots)


@lru_cache(maxsize=12)
def _catalog(language: str, roots: tuple[Path, ...]) -> dict[str, str]:
    for root in roots:
        try:
            data = json.loads((root / "i18n" / f"{language}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str)}
    return {}


def tr(key: str, **values: object) -> str:
    roots = _roots()
    text = _catalog(_language(), roots).get(key, _catalog("en", roots).get(key, key))
    return text.format(**values) if values else text
