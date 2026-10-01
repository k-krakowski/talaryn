from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

from .i18n import tr
from .util import expand_path, notify


LINK_SUFFIX = ".link.html"
LINK_READ_LIMIT = 8192
EXPORT_TIMEOUT_SECONDS = 1800

GOOGLE_LINK_MARKERS = {
    "docs.google.com/document/": "document",
    "docs.google.com/spreadsheets/": "spreadsheet",
    "docs.google.com/presentation/": "presentation",
}

EXPORT_FORMATS = {
    "document": ("docx", "odt", "pdf", "txt"),
    "spreadsheet": ("xlsx", "ods", "pdf", "csv"),
    "presentation": ("pptx", "odp", "pdf"),
}


def google_link_kind(content: str) -> str | None:
    for marker, kind in GOOGLE_LINK_MARKERS.items():
        if marker in content:
            return kind
    return None


def _relative_google_link_path(
    profile: dict[str, Any],
    local_path: str | Path,
) -> Path:
    mount_dir = Path(expand_path(str(profile.get("mount_dir", ""))))
    path = Path(expand_path(str(local_path)))
    try:
        relative = path.relative_to(mount_dir)
    except ValueError as error:
        raise ValueError("path_outside_google_mount") from error
    if not relative.name.casefold().endswith(LINK_SUFFIX):
        raise ValueError("not_google_link")
    return relative


def _remote_path(profile: dict[str, Any], relative: Path) -> str:
    remote_spec = str(profile.get("remote_spec", "")).strip().rstrip("/")
    if not remote_spec:
        raise ValueError("missing_remote_spec")
    relative_text = relative.as_posix().lstrip("/")
    if remote_spec.endswith(":"):
        return f"{remote_spec}{relative_text}"
    return f"{remote_spec}/{relative_text}"


def google_link_remote_path(
    profile: dict[str, Any],
    local_path: str | Path,
) -> str:
    return _remote_path(
        profile,
        _relative_google_link_path(profile, local_path),
    )


def google_export_remote_path(
    profile: dict[str, Any],
    local_path: str | Path,
    extension: str,
) -> str:
    extension = extension.casefold().lstrip(".")
    relative = _relative_google_link_path(profile, local_path)
    base_name = relative.name[: -len(LINK_SUFFIX)]
    exported = relative.with_name(f"{base_name}.{extension}")
    return _remote_path(profile, exported)


def normalized_export_destination(
    destination: str | Path,
    extension: str,
) -> Path:
    extension = extension.casefold().lstrip(".")
    path = Path(expand_path(str(destination)))
    known_extensions = {
        item for formats in EXPORT_FORMATS.values() for item in formats
    }
    if path.suffix.casefold().lstrip(".") in known_extensions:
        return path.with_suffix(f".{extension}")
    if not path.name.casefold().endswith(f".{extension}"):
        return path.with_name(f"{path.name}.{extension}")
    return path


def _run(
    command: list[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
    )


def _link_probe_command(remote_path: str) -> list[str]:
    return [
        "rclone",
        "cat",
        remote_path,
        "--drive-export-formats=link.html",
        "--head",
        str(LINK_READ_LIMIT),
        "--log-level",
        "ERROR",
    ]


def _export_command(
    remote_path: str,
    destination: Path,
    extension: str,
) -> list[str]:
    return [
        "rclone",
        "copyto",
        remote_path,
        str(destination),
        f"--drive-export-formats={extension}",
        "--log-level",
        "ERROR",
    ]


def _start_progress(title: str, text: str):
    try:
        progress = subprocess.Popen(
            [
                "zenity",
                "--progress",
                "--pulsate",
                "--no-cancel",
                "--auto-close",
                f"--title={title}",
                f"--text={text}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    return progress


def _stop_progress(progress) -> None:
    if progress is None:
        return
    try:
        if progress.stdin:
            progress.stdin.write("100\n")
            progress.stdin.flush()
            progress.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    try:
        progress.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            progress.terminate()
        except OSError:
            pass


def _choose_format(kind: str) -> str | None:
    formats = EXPORT_FORMATS[kind]
    rows: list[str] = []
    for index, extension in enumerate(formats):
        rows.extend(("TRUE" if index == 0 else "FALSE", extension.upper()))
    try:
        result = _run(
            [
                "zenity",
                "--list",
                "--radiolist",
                f"--title={tr('google_export_title')}",
                f"--text={tr('google_export_warning')}",
                "--width=720",
                "--height=420",
                "--column=",
                f"--column={tr('google_export_format')}",
                "--print-column=2",
                *rows,
            ],
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    selected = result.stdout.strip().casefold()
    return selected if result.returncode == 0 and selected in formats else None


def _default_destination(local_path: str | Path, extension: str) -> Path:
    name = Path(str(local_path)).name[: -len(LINK_SUFFIX)]
    download_dir = Path.home() / "Downloads"
    parent = download_dir if download_dir.is_dir() else Path.home()
    return parent / f"{name}.{extension}"


def _choose_destination(
    local_path: str | Path,
    extension: str,
) -> Path | None:
    default = _default_destination(local_path, extension)
    try:
        result = _run(
            [
                "zenity",
                "--file-selection",
                "--save",
                f"--title={tr('google_export_destination_title')}",
                f"--filename={default}",
                f"--file-filter=*.{extension}",
            ],
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    destination = normalized_export_destination(result.stdout.strip(), extension)
    # Confirm the actual target, after adding or replacing its extension.
    # is_symlink also catches dangling links that exists() does not see.
    if destination.exists() or destination.is_symlink():
        try:
            confirmation = _run(
                [
                    "zenity", "--question", "--default-cancel", "--no-markup",
                    f"--title={tr('google_export_destination_title')}",
                    f"--text={tr('google_export_confirm_overwrite', path=str(destination))}",
                ],
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if confirmation.returncode != 0:
            return None
    return destination


def _error_detail(error: object) -> str:
    text = str(error or "").strip()
    return text[-500:] if text else tr("google_export_unknown_error")


def run_google_export(
    profile: dict[str, Any],
    local_path: str | Path,
) -> int:
    if str(profile.get("kind", "")) != "gdrive":
        notify(tr("google_export_failed_title"), tr("google_export_invalid_link"))
        return 1

    try:
        link_remote = google_link_remote_path(profile, local_path)
    except ValueError:
        notify(tr("google_export_failed_title"), tr("google_export_invalid_link"))
        return 1

    progress = _start_progress(
        tr("google_export_title"),
        tr("google_export_preparing"),
    )
    try:
        probe = _run(
            _link_probe_command(link_remote),
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _stop_progress(progress)
        notify(tr("google_export_failed_title"), _error_detail(error))
        return 1
    _stop_progress(progress)

    kind = google_link_kind(probe.stdout) if probe.returncode == 0 else None
    if kind is None:
        detail = probe.stderr if probe.returncode else tr("google_export_invalid_link")
        notify(tr("google_export_failed_title"), _error_detail(detail))
        return 1

    extension = _choose_format(kind)
    if extension is None:
        return 0
    destination = _choose_destination(local_path, extension)
    if destination is None:
        return 0

    try:
        remote_path = google_export_remote_path(
            profile,
            local_path,
            extension,
        )
    except ValueError:
        notify(tr("google_export_failed_title"), tr("google_export_invalid_link"))
        return 1

    progress = _start_progress(
        tr("google_export_title"),
        tr("google_export_progress", name=destination.name),
    )
    try:
        result = _run(
            _export_command(remote_path, destination, extension),
            timeout=EXPORT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _stop_progress(progress)
        notify(tr("google_export_failed_title"), _error_detail(error))
        return 1
    _stop_progress(progress)

    if result.returncode != 0:
        notify(tr("google_export_failed_title"), _error_detail(result.stderr))
        return 1

    notify(
        tr("google_export_complete_title"),
        tr("google_export_complete", path=str(destination)),
    )
    return 0
