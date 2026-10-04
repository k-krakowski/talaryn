#!/usr/bin/env bash
set -euo pipefail
umask 022

APP="talaryn"
BIN_DIR="$HOME/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SYSTEMD_DIR="$HOME/.config/systemd/user"
NAUTILUS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus-python/extensions"
EMBLEM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/emblems"
APP_ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
METAINFO_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/metainfo"
AUTOSTART_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/io.github.k_krakowski.Talaryn.tray.desktop"
FORCE=false

for argument in "$@"; do
  case "$argument" in
    --force)
      FORCE=true
      ;;
    -h|--help)
      echo "Usage: $0 [--force]"
      echo "  --force  uninstall even when transfer safety cannot be confirmed"
      exit 0
      ;;
    *)
      echo "Unknown option: $argument" >&2
      exit 2
      ;;
  esac
done

if ! ACTIVE_UNIT_LIST="$(systemctl --user list-units \
    --type=service \
    --state=active,activating,deactivating \
    --no-legend \
    --plain \
    'talaryn@*.service' 2>/dev/null)"; then
  if [[ "$FORCE" != true ]]; then
    echo "Refusing to uninstall: unable to check active Talaryn mounts." >&2
    exit 1
  fi
  ACTIVE_UNIT_LIST=""
fi
mapfile -t ACTIVE_UNITS < <(printf '%s\n' "$ACTIVE_UNIT_LIST" | awk 'NF {print $1}')

if [[ "$FORCE" != true && ${#ACTIVE_UNITS[@]} -gt 0 ]]; then
  echo "Refusing to uninstall while Talaryn mounts are active." >&2
  echo "Finish synchronization and unmount every profile, then retry." >&2
  echo "Use --force only if you accept interrupting pending writes." >&2
  exit 1
fi

systemctl --user disable --now talaryn-monitor.service >/dev/null 2>&1 || true

for unit in "${ACTIVE_UNITS[@]}"; do
  if ! systemctl --user stop "$unit"; then
    systemctl --user enable --now talaryn-monitor.service >/dev/null 2>&1 || true
    echo "Unable to stop $unit; installation files were left intact." >&2
    exit 1
  fi
done

rm -f "$BIN_DIR/$APP"
rm -rf \
  "$DATA_DIR/talaryn" \
  "$DATA_DIR/icons" \
  "$DATA_DIR/i18n" \
  "$DATA_DIR/nautilus" \
  "$DATA_DIR/emblems" \
  "$DATA_DIR/licenses"
rm -f "$DATA_DIR/THIRD_PARTY_NOTICES.md" "$DATA_DIR/LICENSE"
rmdir "$DATA_DIR" >/dev/null 2>&1 || true
rm -f "$APP_DIR/io.github.k_krakowski.Talaryn.desktop"
rm -f "$APP_DIR/io.github.k_krakowski.Talaryn.Comparison.desktop"
rm -f "$APP_DIR/io.github.k_krakowski.Talaryn.GoogleCreate.desktop"
rm -f "$AUTOSTART_FILE"
rm -f "$SYSTEMD_DIR/talaryn@.service"
rm -f "$SYSTEMD_DIR/talaryn-monitor.service"
rm -f "$NAUTILUS_DIR/google_link_emblems.py"
rm -f "$NAUTILUS_DIR/rclone_sync_emblems.py"
rm -f "$NAUTILUS_DIR/google_new_docs.py"
rm -f "$NAUTILUS_DIR/rclone_ssh_terminal.py"
rm -f "$NAUTILUS_DIR/talaryn_file_comparison.py"
rm -f "$NAUTILUS_DIR/talaryn_i18n.py"
rm -f "$EMBLEM_DIR/emblem-google-doc.svg"
rm -f "$EMBLEM_DIR/emblem-google-sheet.svg"
rm -f "$EMBLEM_DIR/emblem-google-slide.svg"
rm -f "$EMBLEM_DIR/emblem-talaryn-document.svg"
rm -f "$EMBLEM_DIR/emblem-talaryn-spreadsheet.svg"
rm -f "$EMBLEM_DIR/emblem-talaryn-presentation.svg"
rm -f "$EMBLEM_DIR/emblem-rclone-synced.svg"
rm -f "$EMBLEM_DIR/emblem-rclone-syncing.svg"
rm -f "$APP_ICON_DIR/talaryn.svg"
rm -f "$METAINFO_DIR/io.github.k_krakowski.Talaryn.metainfo.xml"

systemctl --user daemon-reload || true

echo "Uninstalled Talaryn. User profiles and custom icons were preserved."
