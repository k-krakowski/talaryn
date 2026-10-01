from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def application_command(*arguments: str) -> list[str]:
    """Run this package without importing Python modules from the CWD."""
    package_root = Path(__file__).resolve().parent.parent
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0, {str(package_root)!r});"
        "from talaryn.main import main;"
        "main()"
    )
    return [sys.executable, "-I", "-c", bootstrap, *arguments]


def notify(title: str, body: str) -> None:
    if command_exists("notify-send"):
        try:
            subprocess.run(
                ["notify-send", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        print(f"{title}: {body}", file=sys.stderr)


def run_quiet(cmd: list[str], timeout: float = 15.0) -> int:
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 124


def run_capture(
    cmd: list[str],
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return 124, stdout, stderr or "command timed out"
    return proc.returncode, proc.stdout, proc.stderr


def safe_id(value: str) -> str:
    value = value.strip().rstrip(":")
    out = []
    for char in value:
        if char.isalnum() or char in "-_":
            out.append(char)
        elif char.isspace() or char in ":/\\.":
            out.append("-")
    result = "".join(out).strip("-")
    while "--" in result:
        result = result.replace("--", "-")
    return result or "profile"


def expand_path(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path)))


def shlex_format(template: str, values: dict[str, object]) -> list[str]:
    quoted = {key: shlex.quote(str(value)) for key, value in values.items()}
    formatted = template.format(**quoted)
    return shlex.split(formatted)


def read_tail(path: Path, max_chars: int = 12000) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_chars * 4), os.SEEK_SET)
            data = stream.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return str(exc)
    if len(data) > max_chars:
        return data[-max_chars:]
    return data
