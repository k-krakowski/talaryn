from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import SETTINGS_FILE, data_dirs

MINIMAL_TRANSLATIONS = {
    "app_title": "Talaryn",
    "error": "Error",
    "close": "Close",
}

SUPPORTED_LANGUAGES = (
    "pl",
    "en",
    "ru",
)


def detect_system_language() -> str:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if not value:
            continue
        first = value.split(":")[0]
        code = first.split(".")[0].split("_")[0].lower()
        if code in SUPPORTED_LANGUAGES:
            return code
    return "en"


def configured_language() -> str:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return "system"
    if not isinstance(data, dict):
        return "system"
    value = str(data.get("language", "system"))
    return (
        value
        if value in ("system", *SUPPORTED_LANGUAGES)
        else "system"
    )


def detect_language(preference: str | None = None) -> str:
    selected = configured_language() if preference is None else preference
    if selected in SUPPORTED_LANGUAGES:
        return selected
    return detect_system_language()


def apply_toolkit_language(preference: str | None = None) -> None:
    """Make GTK/libadwaita use the language selected by the application."""
    selected = configured_language() if preference is None else preference
    if selected == "system" or selected not in SUPPORTED_LANGUAGES:
        return

    os.environ["LANGUAGE"] = selected


def _candidate_files(lang: str) -> list[Path]:
    files: list[Path] = []
    for base in data_dirs():
        files.append(base / "i18n" / f"{lang}.json")
        files.append(base / f"{lang}.json")
    return files


def _load_translation_file(lang: str) -> dict[str, str]:
    for path in _candidate_files(lang):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}

    return {}


def load_translation(language: str | None = None) -> dict[str, str]:
    default = {**MINIMAL_TRANSLATIONS, **_load_translation_file("en")}
    selected_language = detect_language(language)
    if selected_language == "en":
        return default

    selected = _load_translation_file(selected_language)
    return {**default, **selected}


TR = load_translation()


def tr(key: str, **kwargs: object) -> str:
    text = TR.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text
