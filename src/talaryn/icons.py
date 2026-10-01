from __future__ import annotations

import shutil
from pathlib import Path

from .paths import APP, DATA_HOME, data_dirs
from .util import safe_id


SUPPORTED_ICON_EXTENSIONS = {".svg", ".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_REMOTE_ICON_NAME = "remote-storage.svg"
WEBDAV_REMOTE_ICON_NAME = "webdav.svg"
OBJECT_STORAGE_REMOTE_ICON_NAME = "object-storage.svg"
HIDDEN_ICON_NAME_PREFIXES = ("gnome-logo-text",)
# The horizontal brand mark belongs to the project README, not to the profile
# icon chooser or any runtime application surface.
HIDDEN_ICON_NAMES = {"talaryn.svg"}
# Saved profiles may still refer to artwork from pre-release builds. Resolve
# these names to project-authored icons even if old installed assets remain.
LEGACY_PROVIDER_ICON_DEFAULTS = {
    "dropbox.svg": DEFAULT_REMOTE_ICON_NAME,
    "Glyph_128.svg": DEFAULT_REMOTE_ICON_NAME,
    "seafile.svg": DEFAULT_REMOTE_ICON_NAME,
    "fastmail.svg": WEBDAV_REMOTE_ICON_NAME,
    "FM-Icon-RGB.svg": WEBDAV_REMOTE_ICON_NAME,
    "nextcloud.svg": WEBDAV_REMOTE_ICON_NAME,
    "logo_nextcloud_blue.svg": WEBDAV_REMOTE_ICON_NAME,
    "logo_nextcloud_white.svg": WEBDAV_REMOTE_ICON_NAME,
}


def icon_path(icon_name: str | None) -> str | None:
    name = icon_name or DEFAULT_REMOTE_ICON_NAME
    name = LEGACY_PROVIDER_ICON_DEFAULTS.get(name, name)
    for base in data_dirs():
        candidates = [
            base / "custom-icons" / name,
            base / "icons" / name,
            base / "data" / "icons" / name,
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return None


def remote_icon_path(icon_name: str | None) -> str | None:
    """Resolve provider artwork, falling back to the generic remote drive."""
    requested = icon_path(icon_name)
    if requested:
        return requested
    return icon_path(DEFAULT_REMOTE_ICON_NAME)


def icon_storage_dir() -> Path:
    return DATA_HOME / APP / "custom-icons"


def available_icon_names() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    for base in data_dirs():
        for icon_dir in (
            base / "custom-icons",
            base / "icons",
            base / "data" / "icons",
        ):
            if not icon_dir.is_dir():
                continue

            for child in sorted(icon_dir.iterdir(), key=lambda path: path.name.lower()):
                if not child.is_file():
                    continue
                if child.suffix.lower() not in SUPPORTED_ICON_EXTENSIONS:
                    continue
                if child.name.casefold().startswith(
                    HIDDEN_ICON_NAME_PREFIXES
                ):
                    continue
                if child.name.casefold() in HIDDEN_ICON_NAMES:
                    continue
                if child.name in LEGACY_PROVIDER_ICON_DEFAULTS:
                    continue
                if child.name in seen:
                    continue

                seen.add(child.name)
                names.append(child.name)

    return names


def install_custom_icon(source_path: str) -> str:
    source = Path(source_path)
    suffix = source.suffix.lower()

    if not source.is_file() or suffix not in SUPPORTED_ICON_EXTENSIONS:
        raise ValueError("unsupported_icon_file")

    target_dir = icon_storage_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    stem = safe_id(source.stem)
    target_name = f"{stem}{suffix}"
    target = target_dir / target_name

    idx = 2
    while target.exists():
        target_name = f"{stem}-{idx}{suffix}"
        target = target_dir / target_name
        idx += 1

    shutil.copy2(source, target)
    return target_name
