"""Create native Google documents and refresh the affected mounted directory."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import time
from typing import Callable
import zipfile
from uuid import uuid4

from .activity_store import ActivityStore
from .icons import DEFAULT_REMOTE_ICON_NAME
from .i18n import tr
from .rc import rc_call
from .util import expand_path

TEMPLATE_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "rclone-google-new/templates"


@dataclass(frozen=True)
class CreationResult:
    created: bool
    detail: str = ""
    local_path: Path | None = None
    url: str | None = None
    refresh_warning: bool = False


def clean_title(title: str) -> str:
    title = title.replace("/", "-").strip()
    for suffix in (".link.html", ".docx", ".xlsx", ".pptx"):
        if title.lower().endswith(suffix):
            title = title[:-len(suffix)].strip()
            break
    if not title or title in (".", "..") or any(ord(c) < 32 for c in title):
        raise ValueError(tr("google_name_invalid"))
    return title


def relative_folder(profile: dict, folder_path: str | Path) -> Path:
    if profile.get("kind") != "gdrive" or not profile.get("mount_dir"):
        raise ValueError(tr("google_create_invalid_folder"))
    folder = Path(expand_path(str(folder_path)))
    mount = Path(expand_path(str(profile["mount_dir"])))
    try:
        return folder.relative_to(mount)
    except ValueError as error:
        raise ValueError(tr("google_create_invalid_folder")) from error


def remote_folder_for(profile: dict, relative: Path) -> str:
    remote = str(profile.get("remote_spec", "")).strip().rstrip("/")
    if not remote or ":" not in remote:
        raise ValueError(tr("google_create_invalid_folder"))
    if relative == Path("."):
        return remote
    return remote + ("" if remote.endswith(":") else "/") + relative.as_posix()


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, check=False, timeout=timeout)


def _export_formats(profile: dict, ext: str) -> list[str]:
    # Google profiles normally expose links; custom mounts may export documents.
    formats = ["link.html", ext]
    try:
        args = shlex.split(str(profile.get("custom_command", "")))
        for index, arg in enumerate(args):
            if arg.startswith("--drive-export-formats="):
                formats = arg.split("=", 1)[1].split(",")
            elif arg == "--drive-export-formats" and index + 1 < len(args):
                formats = args[index + 1].split(",")
    except ValueError:
        pass
    return [value.strip() for value in formats if value.strip()]


def _local_names(profile: dict, title: str, ext: str) -> set[str]:
    return {f"{title}.{value}" for value in _export_formats(profile, ext)}


def _copied_new_file(log: str) -> bool:
    # --ignore-existing returns success even when a concurrent creation skips upload.
    for line in log.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and str(entry.get("msg", "")).startswith("Copied (new)"):
            return True
    return False


def _save_creation_event(profile_id: str, profile: dict, folder: Path,
                         title: str, kind: str, timestamp: float,
                         local_path: Path | None) -> None:
    relative = relative_folder(profile, folder)
    if local_path is None:
        formats = _export_formats(profile, KINDS[kind]["ext"])
        extension = formats[0] if formats else "link.html"
        local_path = folder / f"{title}.{extension}"
    path = relative / local_path.name
    icon = str(profile.get("icon", DEFAULT_REMOTE_ICON_NAME))
    ActivityStore().save_events([{
        "event_id": f"{profile_id}:google-create:{uuid4().hex}",
        "profile_id": profile_id,
        "profile_label": str(profile.get("label", profile_id)),
        "profile_icon": icon,
        "mount_dir": expand_path(str(profile["mount_dir"])),
        "path": path.as_posix(),
        "local_path": str(local_path),
        "reveal_path": str(local_path),
        "name": local_path.name,
        "timestamp": timestamp,
        "state": "completed",
        "operation": "create",
        "direction": "remote",
        "source": "Talaryn",
        "source_icon": "talaryn",
        "destination": str(local_path),
        "destination_icon": icon,
        "google_kind": kind,
        "size": 0,
        "size_known": False,
        "tries": 0,
        "error": "",
    }])


def refresh_created_file(profile_id: str, profile: dict, folder: Path,
                         title: str, ext: str) -> Path | None:
    relative = relative_folder(profile, folder)
    directory = "" if relative == Path(".") else relative.as_posix()
    names = _local_names(profile, title, ext)
    for attempt in range(3):
        response = rc_call(profile_id, "vfs/refresh", {"dir": directory}, timeout=5)
        # HTTP success can still contain a per-directory error.
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(result, dict) and result.get(directory) == "OK":
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if entry.name in names:
                            return folder / entry.name
            except OSError:
                pass
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    return None


def create_google_file(profile_id: str, profile: dict, folder_path: str | Path,
                       kind: str, title: str,
                       progress: Callable[[str], None] = lambda _text: None,
                       visible: Callable[[Path], None] = lambda _path: None) -> CreationResult:
    try:
        title = clean_title(title)
        cfg = KINDS[kind]
        folder = Path(expand_path(str(folder_path)))
        remote_folder = remote_folder_for(profile, relative_folder(profile, folder))
        ext = cfg["ext"]
        dest = remote_folder + ("" if remote_folder.endswith(":") else "/") + f"{title}.{ext}"
        ensure_templates()
        template = TEMPLATE_DIR / cfg["template"]
        progress(tr("google_name_checking"))
        existing = _run(["rclone", "lsjson", "--stat", dest,
                         f"--drive-export-formats={ext}"], 30)
        if existing.returncode == 0:
            return CreationResult(False, tr("google_file_exists", title=title))
        if existing.returncode not in (3, 4):
            return CreationResult(False, existing.stderr[-500:] or tr("rclone_unknown_error"))
        progress(tr("google_file_creating_name", title=title))
        uploaded = _run(["rclone", "copyto", "--ignore-existing", str(template), dest,
                         f"--drive-import-formats={ext}", f"--drive-export-formats={ext}",
                         "--use-json-log", "--log-level=INFO", "--log-file=", "--stats=0"], 120)
        if uploaded.returncode != 0:
            return CreationResult(False, uploaded.stderr[-500:] or tr("rclone_unknown_error"))
        if not _copied_new_file(uploaded.stderr):
            return CreationResult(False, tr("google_creation_not_confirmed"))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
        return CreationResult(False, str(error))

    # After upload, an unavailable RC socket or browser must never invite another upload.
    created_at = time.time()
    progress(tr("google_folder_refreshing"))
    try:
        local_path = refresh_created_file(profile_id, profile, folder, title, ext)
    except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
        local_path = None
    history_warning = ""
    try:
        _save_creation_event(profile_id, profile, folder, title, kind, created_at, local_path)
    except (OSError, RuntimeError, sqlite3.Error) as error:
        history_warning = tr("google_history_save_failed", error=str(error))
    if local_path is not None:
        visible(local_path)
    progress(tr("google_file_open_preparing"))
    file_id = None
    for attempt in range(3):
        try:
            stat = _run(["rclone", "lsjson", "--stat", dest,
                         f"--drive-export-formats={ext}"], 10)
            if stat.returncode == 0:
                file_id = json.loads(stat.stdout).get("ID")
                if file_id:
                    break
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            pass
        if attempt < 2:
            time.sleep(0.5)
    url = cfg["url_template"].format(file_id=file_id) if file_id else None
    warnings = [history_warning] if history_warning else []
    if local_path is None:
        warnings.append(tr("google_refresh_failed"))
    if url is None:
        warnings.append(tr("google_file_id_missing"))
    return CreationResult(True, "\n".join(warnings), local_path, url, local_path is None)

KINDS = {
    "doc": {
        "label": "google_new_doc",
        "default_name": "google_default_doc",
        "template": "blank.docx",
        "ext": "docx",
        "url_template": "https://docs.google.com/document/d/{file_id}/edit",
    },
    "sheet": {
        "label": "google_new_sheet",
        "default_name": "google_default_sheet",
        "template": "blank.xlsx",
        "ext": "xlsx",
        "url_template": "https://docs.google.com/spreadsheets/d/{file_id}/edit",
    },
    "slides": {
        "label": "google_new_slides",
        "default_name": "google_default_slides",
        "template": "blank.pptx",
        "ext": "pptx",
        "url_template": "https://docs.google.com/presentation/d/{file_id}/edit",
    },
}


def _write_zip(path: Path, files: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def ensure_templates() -> None:
    docx = TEMPLATE_DIR / "blank.docx"
    if not docx.exists():
        _write_zip(
            docx,
            {
                "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
                "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
                "word/document.xml": '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/><w:sectPr/></w:body></w:document>',
            },
        )

    xlsx = TEMPLATE_DIR / "blank.xlsx"
    if not xlsx.exists():
        _write_zip(
            xlsx,
            {
                "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
                "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
                "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
                "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
                "xl/worksheets/sheet1.xml": '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>',
            },
        )

    pptx = TEMPLATE_DIR / "blank.pptx"
    if not pptx.exists():
        _write_zip(
            pptx,
            {
                "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>',
                "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
                "ppt/_rels/presentation.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>',
                "ppt/presentation.xml": '<?xml version="1.0" encoding="UTF-8"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
                "ppt/slides/slide1.xml": '<?xml version="1.0" encoding="UTF-8"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree/></p:cSld></p:sld>',
            },
        )
