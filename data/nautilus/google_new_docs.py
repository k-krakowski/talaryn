#!/usr/bin/env python3
#
# Copyright (c) 2026 Kamil Krakowski
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# Isolated workers also load the helper from this installed extension directory.
_extension_dir = str(Path(__file__).resolve().parent)
if _extension_dir not in sys.path:
    sys.path.insert(0, _extension_dir)
from talaryn_i18n import tr


WORKER_MODE = len(sys.argv) > 1 and sys.argv[1] == "--worker"

if not WORKER_MODE:
    import gi

    gi.require_version("Nautilus", "4.0")
    from gi.repository import GObject, Nautilus


TEMPLATE_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "rclone-google-new" / "templates"
OPEN_AFTER_CREATE = True

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


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "talaryn"


def _absolute_path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(value))))


def _load_profiles() -> dict[str, dict]:
    path = _config_dir() / "profiles.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
    return {str(k): v for k, v in profiles.items() if isinstance(v, dict)}


def _load_settings() -> dict:
    try:
        data = json.loads((_config_dir() / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _setting_enabled(key: str, default: bool = True) -> bool:
    return bool(_load_settings().get(key, default))


def _notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["notify-send", title, body],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _start_progress(title: str, body: str):
    try:
        progress = subprocess.Popen(
            [
                "zenity",
                "--progress",
                "--pulsate",
                "--no-cancel",
                "--auto-close",
                f"--title={title}",
                f"--text={body}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if progress.stdin:
            progress.stdin.write(f"# {body}\n")
            progress.stdin.flush()
        return progress
    except FileNotFoundError:
        _notify(title, body)
        return None


def _stop_progress(progress) -> None:
    if progress is None:
        return

    try:
        if progress.stdin:
            progress.stdin.write("100\n")
            progress.stdin.flush()
            progress.stdin.close()
    except Exception:
        pass

    try:
        progress.wait(timeout=1)
    except Exception:
        try:
            progress.terminate()
        except Exception:
            pass


def _write_zip(path: Path, files: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _ensure_templates() -> None:
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


def _get_google_file_id(remote_path: str, ext: str):
    for _ in range(5):
        try:
            result = subprocess.run(
                [
                    "rclone",
                    "lsjson",
                    "--stat",
                    remote_path,
                    f"--drive-export-formats={ext}",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
            )

            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                file_id = data.get("ID")
                if file_id:
                    return file_id
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

        time.sleep(0.5)

    return None


def _open_google_file(remote_path: str, kind: str, ext: str):
    cfg = KINDS[kind]
    file_id = _get_google_file_id(remote_path, ext)
    if not file_id:
        _notify(
            tr("google_file_open_failed"),
            tr("google_file_id_missing"),
        )
        return

    try:
        subprocess.Popen(
            ["xdg-open", cfg["url_template"].format(file_id=file_id)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        _notify(tr("google_file_open_failed"), tr("xdg_open_missing"))


def _run_worker(payload: dict) -> int:
    title = str(payload["title"])
    dest = str(payload["dest"])
    template = Path(str(payload["template"]))
    remote_folder = str(payload["remote_folder"])
    kind = str(payload["kind"])
    ext = str(payload["ext"])

    # Check before upload so an existing document is not reported as newly created.
    # --ignore-existing also protects a file created after this check.
    try:
        existing = subprocess.run(
            ["rclone", "lsjson", "--stat", dest, f"--drive-export-formats={ext}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _notify(tr("google_file_create_failed"), str(exc))
        return 1
    if existing.returncode == 0:
        _notify(tr("google_file_create_failed"), tr("google_file_exists", title=title))
        return 1
    # Rclone exit codes 3 and 4 mean directory/file not found. Other errors
    # (including authentication and network failures) must not start an upload.
    if existing.returncode not in (3, 4):
        _notify(tr("google_file_create_failed"),
                existing.stderr[-400:] or tr("rclone_unknown_error"))
        return existing.returncode or 1

    progress = _start_progress(
        tr("google_file_creating"),
        tr("google_file_creating_name", title=title),
    )

    try:
        result = subprocess.run(
            [
                "rclone",
                "copyto",
                "--ignore-existing",
                str(template),
                dest,
                f"--drive-import-formats={ext}",
                f"--drive-export-formats={ext}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _stop_progress(progress)
        _notify(tr("google_file_create_failed"), str(exc))
        return 1

    _stop_progress(progress)

    if result.returncode == 0:
        if OPEN_AFTER_CREATE:
            _open_google_file(dest, kind, ext)
        _notify(tr("google_file_created"), tr("google_file_created_location", title=title, folder=remote_folder))
        return 0

    _notify(
        tr("google_file_create_failed"),
        result.stderr[-400:] if result.stderr else tr("rclone_unknown_error"),
    )
    return result.returncode or 1


if WORKER_MODE:
    try:
        raise SystemExit(_run_worker(json.loads(sys.argv[2])))
    except Exception as exc:
        _notify(tr("google_file_create_failed"), str(exc))
        raise SystemExit(1)


class GoogleNewDocsProvider(GObject.GObject, Nautilus.MenuProvider):
    def get_background_items(self, current_folder):
        if not _setting_enabled("context_google_new_docs"):
            return []

        folder_path = self._folder_to_path(current_folder)
        match = self._profile_for_path(folder_path) if folder_path else None
        if not match:
            return []

        _ensure_templates()
        profile_id, profile, mount_dir = match
        submenu = Nautilus.Menu()

        for key, cfg in KINDS.items():
            item = Nautilus.MenuItem(
                name=f"GoogleNewDocsProvider::{profile_id}::{key}",
                label=tr(cfg["label"]),
                tip=tr("google_new_help"),
            )
            item.connect("activate", self._create_google_file, folder_path, profile, mount_dir, key)
            submenu.append_item(item)

        top = Nautilus.MenuItem(
            name=f"GoogleNewDocsProvider::{profile_id}::top",
            label=tr("context_google_new_docs"),
            tip=tr("google_new_help"),
        )
        top.set_submenu(submenu)
        return [top]

    def _folder_to_path(self, folder):
        location = folder.get_location()
        if location is None:
            return None

        path = location.get_path()
        if path is None:
            return None

        return _absolute_path(path)

    def _profile_for_path(self, folder_path: Path):
        for profile_id, profile in _load_profiles().items():
            if str(profile.get("kind", "")) != "gdrive":
                continue

            mount_dir = str(profile.get("mount_dir", "")).strip()
            if not mount_dir:
                continue

            mount_path = _absolute_path(mount_dir)
            try:
                folder_path.relative_to(mount_path)
            except ValueError:
                continue
            return profile_id, profile, mount_path

        return None

    def _remote_path_for_folder(self, folder_path: Path, profile: dict, mount_dir: Path) -> str:
        remote_spec = str(profile.get("remote_spec", "")).rstrip("/")
        rel = folder_path.relative_to(mount_dir).as_posix()
        if rel == ".":
            return remote_spec
        if remote_spec.endswith(":"):
            return f"{remote_spec}{rel}"
        return f"{remote_spec}/{rel}"

    def _ask_name(self, default_name: str) -> str | None:
        try:
            result = subprocess.run(
                [
                    "zenity",
                    "--entry",
                    f"--title={tr('google_new_file')}",
                    f"--text={tr('file_name_prompt')}",
                    f"--entry-text={default_name}",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            if result.returncode != 0:
                return None
            if result.returncode == 0:
                name = result.stdout.strip()
                if name:
                    return self._clean_title(name)
        except FileNotFoundError:
            pass

        return f"{default_name} {time.strftime('%Y-%m-%d %H-%M-%S')}"

    def _clean_title(self, title: str) -> str:
        title = title.replace("/", "-").strip()
        for ext in ("docx", "xlsx", "pptx"):
            suffix = f".{ext}"
            if title.lower().endswith(suffix):
                title = title[: -len(suffix)].strip()
        return title or f"{tr('google_new_file')} {time.strftime('%Y-%m-%d %H-%M-%S')}"

    def _create_google_file(self, menu_item, folder_path: Path, profile: dict, mount_dir: Path, kind: str):
        cfg = KINDS[kind]
        template = TEMPLATE_DIR / cfg["template"]
        if not template.exists():
            _notify(tr("template_missing"), tr("file_not_found", path=template))
            return

        title = self._ask_name(tr(cfg["default_name"]))
        if title is None:
            return
        ext = cfg["ext"]
        remote_folder = self._remote_path_for_folder(folder_path, profile, mount_dir)
        dest = f"{remote_folder}{title}.{ext}" if remote_folder.endswith(":") else f"{remote_folder}/{title}.{ext}"

        payload = {
            "folder_path": str(folder_path),
            "title": title,
            "dest": dest,
            "template": str(template),
            "remote_folder": remote_folder,
            "kind": kind,
            "ext": ext,
        }

        try:
            subprocess.Popen(
                [sys.executable, "-I", __file__, "--worker", json.dumps(payload)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except Exception as exc:
            _notify(tr("google_file_create_failed"), str(exc))
