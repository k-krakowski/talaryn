#!/usr/bin/env bash
set -euo pipefail
umask 022

APP="talaryn"
LEGACY_APP="rclone-mount-gui"
APP_ID="io.github.k_krakowski.Talaryn"
LEGACY_APP_ID="io.github.rclonemountgui.RcloneMountGui"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$ROOT/src/talaryn/__init__.py")"
if [[ -z "$VERSION" || "$VERSION" == *[!0-9A-Za-z.+_-]* ]]; then
  echo "Unable to read a valid application version." >&2
  exit 1
fi
PROJECT_RELEASE_DATE="$(sed -n 's/^__release_date__ = "\([^"]*\)"$/\1/p' "$ROOT/src/talaryn/__init__.py")"
RELEASE_DATE="${APPSTREAM_RELEASE_DATE:-$PROJECT_RELEASE_DATE}"
if [[ ! "$RELEASE_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "APPSTREAM_RELEASE_DATE must use YYYY-MM-DD format." >&2
  exit 1
fi
BIN_DIR="$HOME/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
LEGACY_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$LEGACY_APP"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SYSTEMD_DIR="$HOME/.config/systemd/user"
NAUTILUS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus-python/extensions"
EMBLEM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/emblems"
APP_ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
METAINFO_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/metainfo"
EXEC_PATH="$BIN_DIR/$APP"
CUSTOM_ICON_DIR="$DATA_DIR/custom-icons"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing dependency: $1" >&2
    exit 1
  fi
}

require_cmd python3
require_cmd rclone
require_cmd systemctl
PYTHON_BIN="$(command -v python3)"

if command -v gapplication >/dev/null 2>&1; then
  gapplication action "$LEGACY_APP_ID" quit >/dev/null 2>&1 || true
fi

LEGACY_RUNTIME_DIR="${XDG_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/$LEGACY_APP/runtime}/$LEGACY_APP"
LEGACY_FALLBACK_RUNTIME_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/$LEGACY_APP/runtime/$LEGACY_APP"
"$PYTHON_BIN" -I - "$LEGACY_RUNTIME_DIR" "$LEGACY_FALLBACK_RUNTIME_DIR" <<'PY'
import fcntl
import os
from pathlib import Path
import sys

for directory in map(Path, sys.argv[1:]):
    path = directory / "tray.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        continue
    except OSError:
        raise SystemExit(
            "Close the previous rclone-mount-gui panel icon before upgrading."
        )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except BlockingIOError:
        raise SystemExit(
            "Close the previous rclone-mount-gui panel icon before upgrading."
        )
    finally:
        os.close(descriptor)
PY

mapfile -t LEGACY_MOUNT_UNITS < <(
  systemctl --user list-units \
    --type=service \
    --state=active,activating \
    --no-legend \
    --plain \
    "$LEGACY_APP@*.service" 2>/dev/null \
    | awk '{print $1}'
)
if (( ${#LEGACY_MOUNT_UNITS[@]} > 0 )); then
  echo "Unmount all profiles from the previous installation before upgrading:" >&2
  printf '  %s\n' "${LEGACY_MOUNT_UNITS[@]}" >&2
  exit 1
fi
systemctl --user disable --now "$LEGACY_APP-monitor.service" >/dev/null 2>&1 || true

"$PYTHON_BIN" -I - <<'PY'
import re
import subprocess

minimum = (1, 74, 4)
process = subprocess.run(
    ["rclone", "version"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    check=False,
)
first = process.stdout.splitlines()[0] if process.stdout else ""
match = re.search(r"rclone v(\d+)\.(\d+)\.(\d+)", first)
version = tuple(int(value) for value in match.groups()) if match else None
if version is None or version < minimum:
    found = ".".join(map(str, version)) if version else "unknown"
    raise SystemExit(
        f"Talaryn requires rclone >= 1.74.4; found {found}."
    )
PY

"$PYTHON_BIN" -I - <<'PY'
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    gi.require_version('GnomeDesktop', '4.0')
    from gi.repository import Gtk
    from gi.repository import Adw
    from gi.repository import GnomeDesktop
except Exception as exc:
    raise SystemExit(
        'Missing GTK/PyGObject dependency. On Ubuntu install:\n'
        '  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 '
        'gir1.2-adw-1 gir1.2-gnomedesktop-4.0\n'
        f'Original error: {exc}'
    )
PY

"$PYTHON_BIN" -I - <<'PY' || true
import gi

try:
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
except Exception as exc:
    print(
        'Warning: missing GTK3/PyGObject dependency for the panel icon.\n'
        'On Ubuntu install: sudo apt install gir1.2-gtk-3.0\n'
        f'Original error: {exc}'
    )
    raise SystemExit(0)

for namespace in ('AyatanaAppIndicator3', 'AppIndicator3'):
    try:
        gi.require_version(namespace, '0.1')
        if namespace == 'AyatanaAppIndicator3':
            from gi.repository import AyatanaAppIndicator3
        else:
            from gi.repository import AppIndicator3
        raise SystemExit(0)
    except Exception:
        pass

print(
    'Warning: missing AppIndicator typelib for the GNOME panel icon.\n'
    'On Ubuntu install: sudo apt install gir1.2-ayatanaappindicator3-0.1\n'
    'You may also need the GNOME Shell AppIndicator/KStatusNotifier extension enabled.'
)
PY

mkdir -p "$BIN_DIR" "$DATA_DIR" "$CUSTOM_ICON_DIR" "$APP_DIR" "$SYSTEMD_DIR" "$NAUTILUS_DIR" "$EMBLEM_DIR" "$APP_ICON_DIR" "$METAINFO_DIR"
chmod 0755 "$DATA_DIR"
chmod 0700 "$CUSTOM_ICON_DIR"

# Preserve custom icons imported by the pre-Talaryn installation. Managed
# provider icons are installed from this checkout and do not need migration.
if [[ -d "$LEGACY_DATA_DIR/custom-icons" ]]; then
  while IFS= read -r -d '' legacy_icon; do
    icon_name="${legacy_icon##*/}"
    if [[ ! -e "$CUSTOM_ICON_DIR/$icon_name" ]]; then
      cp -p "$legacy_icon" "$CUSTOM_ICON_DIR/$icon_name"
    fi
  done < <(find "$LEGACY_DATA_DIR/custom-icons" -maxdepth 1 -type f -print0)
fi

# Older releases stored user-imported icons beside managed assets. Preserve
# files that are not supplied by this checkout before replacing that folder.
if [[ -d "$DATA_DIR/icons" ]]; then
  while IFS= read -r -d '' existing_icon; do
    icon_name="${existing_icon##*/}"
    if [[ ! -e "$ROOT/data/icons/$icon_name" && ! -e "$CUSTOM_ICON_DIR/$icon_name" ]]; then
      mv "$existing_icon" "$CUSTOM_ICON_DIR/$icon_name"
    fi
  done < <(find "$DATA_DIR/icons" -maxdepth 1 -type f -print0)
fi

rm -rf "$DATA_DIR/talaryn" "$DATA_DIR/icons" "$DATA_DIR/i18n" "$DATA_DIR/nautilus" "$DATA_DIR/emblems" "$DATA_DIR/licenses"
cp -r "$ROOT/src/talaryn" "$DATA_DIR/talaryn"
cp -r "$ROOT/data/icons" "$DATA_DIR/icons"
# The horizontal logo is a README-only source asset.  Runtime surfaces use the
# square application icon instead.
rm -f "$DATA_DIR/icons/talaryn.svg"
cp -r "$ROOT/data/nautilus" "$DATA_DIR/nautilus"
cp -r "$ROOT/data/emblems" "$DATA_DIR/emblems"
cp -r "$ROOT/i18n" "$DATA_DIR/i18n"
cp "$ROOT/LICENSE" "$DATA_DIR/LICENSE"
rm -f "$DATA_DIR/THIRD_PARTY_NOTICES.md"
find "$DATA_DIR/talaryn" "$DATA_DIR/nautilus" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$DATA_DIR/talaryn" "$DATA_DIR/nautilus" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
cp "$ROOT/data/nautilus/google_link_emblems.py" "$NAUTILUS_DIR/google_link_emblems.py"
cp "$ROOT/data/nautilus/rclone_sync_emblems.py" "$NAUTILUS_DIR/rclone_sync_emblems.py"
cp "$ROOT/data/nautilus/google_new_docs.py" "$NAUTILUS_DIR/google_new_docs.py"
cp "$ROOT/data/nautilus/rclone_ssh_terminal.py" "$NAUTILUS_DIR/rclone_ssh_terminal.py"
cp "$ROOT/data/nautilus/talaryn_file_comparison.py" "$NAUTILUS_DIR/talaryn_file_comparison.py"
cp "$ROOT/data/nautilus/talaryn_i18n.py" "$NAUTILUS_DIR/talaryn_i18n.py"
rm -f "$NAUTILUS_DIR/rclone_cloud_browser.py"
rm -f "$EMBLEM_DIR/emblem-google-doc.svg"
rm -f "$EMBLEM_DIR/emblem-google-sheet.svg"
rm -f "$EMBLEM_DIR/emblem-google-slide.svg"
cp "$ROOT/data/emblems/"*.svg "$EMBLEM_DIR/"
cp "$ROOT/data/icons/talaryn-app-icon.svg" "$APP_ICON_DIR/talaryn.svg"
sed -e "s|@VERSION@|$VERSION|g" \
  -e "s|@RELEASE_DATE@|$RELEASE_DATE|g" \
  "$ROOT/data/metainfo/io.github.k_krakowski.Talaryn.metainfo.xml.in" \
  > "$METAINFO_DIR/io.github.k_krakowski.Talaryn.metainfo.xml"

cat > "$EXEC_PATH" <<EOF_WRAPPER
#!/bin/sh
export TALARYN_DATA_DIR="\${XDG_DATA_HOME:-\$HOME/.local/share}/$APP"
exec "$PYTHON_BIN" -I -c 'import os, sys; sys.path.insert(0, os.environ["TALARYN_DATA_DIR"]); from talaryn.main import main; main()' "\$@"
EOF_WRAPPER
chmod +x "$EXEC_PATH"

sed "s|@EXEC@|$EXEC_PATH|g" \
  "$ROOT/data/systemd/talaryn@.service.in" \
  > "$SYSTEMD_DIR/talaryn@.service"
sed "s|@EXEC@|$EXEC_PATH|g" \
  "$ROOT/data/systemd/talaryn-monitor.service.in" \
  > "$SYSTEMD_DIR/talaryn-monitor.service"

sed "s|@EXEC@|$EXEC_PATH|g" \
  "$ROOT/data/applications/io.github.k_krakowski.Talaryn.desktop.in" \
  > "$APP_DIR/io.github.k_krakowski.Talaryn.desktop"
sed "s|@EXEC@|$EXEC_PATH|g" \
  "$ROOT/data/applications/io.github.k_krakowski.Talaryn.Comparison.desktop.in" \
  > "$APP_DIR/io.github.k_krakowski.Talaryn.Comparison.desktop"

# Remove obsolete per-user installation files only after the new launcher and
# services are in place. User configuration and cache are migrated at startup.
rm -f "$BIN_DIR/$LEGACY_APP"
rm -f "$APP_DIR/$LEGACY_APP_ID.desktop"
rm -f "$SYSTEMD_DIR/$LEGACY_APP@.service"
rm -f "$SYSTEMD_DIR/$LEGACY_APP-monitor.service"
rm -f "$APP_ICON_DIR/$LEGACY_APP-icon.svg"
rm -f "$METAINFO_DIR/$LEGACY_APP_ID.metainfo.xml"
rm -rf "$LEGACY_DATA_DIR"

systemctl --user daemon-reload || true
systemctl --user enable --now talaryn-monitor.service || true

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" || true
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" || true
fi

echo "Installed Talaryn for current user."
echo "Run: talaryn gui"
echo "Nautilus extensions installed. Restart Nautilus to load them: nautilus -q"
