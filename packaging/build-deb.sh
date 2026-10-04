#!/usr/bin/env bash
set -euo pipefail
umask 022
export LC_ALL=C
export TZ=UTC

APP="talaryn"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_MAINTAINER="Kamil Krakowski <k.krakowski.dev@gmail.com>"
MAINTAINER="${DEB_MAINTAINER:-$DEFAULT_MAINTAINER}"
if [[ "$MAINTAINER" == *$'\n'* || "$MAINTAINER" == *$'\r'* ]]; then
  echo "DEB_MAINTAINER must be a single control-file line" >&2
  exit 1
fi
VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$ROOT/src/talaryn/__init__.py")"
if [[ -z "$VERSION" || "$VERSION" == *[!0-9A-Za-z.+:~_-]* ]]; then
  echo "Unable to read a valid version from src/talaryn/__init__.py" >&2
  exit 1
fi
if ! dpkg --validate-version "$VERSION" >/dev/null 2>&1; then
  echo "Version '$VERSION' is not a valid Debian package version" >&2
  exit 1
fi

PROJECT_RELEASE_DATE="$(sed -n 's/^__release_date__ = "\([^"]*\)"$/\1/p' "$ROOT/src/talaryn/__init__.py")"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(date -u --date="$PROJECT_RELEASE_DATE" +%s)}"
if [[ ! "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]]; then
  echo "SOURCE_DATE_EPOCH must be an integer Unix timestamp" >&2
  exit 1
fi
export SOURCE_DATE_EPOCH
RELEASE_DATE="${APPSTREAM_RELEASE_DATE:-$PROJECT_RELEASE_DATE}"
if [[ ! "$RELEASE_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "APPSTREAM_RELEASE_DATE must use YYYY-MM-DD format" >&2
  exit 1
fi
PUBLIC_RELEASE="${PUBLIC_RELEASE:-0}"
if [[ "$PUBLIC_RELEASE" != "0" && "$PUBLIC_RELEASE" != "1" ]]; then
  echo "PUBLIC_RELEASE must be 0 or 1" >&2
  exit 1
fi
if [[ "$PUBLIC_RELEASE" == "1" ]]; then
  release_errors=()
  maintainer_pattern='^[^<>]+ <[^[:space:]<>]+@[^[:space:]<>]+\.[^[:space:]<>]+>$'
  if [[ ! "$MAINTAINER" =~ $maintainer_pattern ]] \
    || [[ "$MAINTAINER" == *localhost* || "$MAINTAINER" == *example.org* ]]; then
    release_errors+=("set DEB_MAINTAINER to a real public contact")
  fi
  if grep -Fq 'Public upstream URL not assigned yet' \
    "$ROOT/packaging/data/copyright" \
    || ! grep -Fq '<url type="homepage">' \
      "$ROOT/data/metainfo/io.github.k_krakowski.Talaryn.metainfo.xml.in"; then
    release_errors+=("assign the public project URL and AppStream homepage")
  fi
  if ! grep -Fq "## $VERSION - $RELEASE_DATE" "$ROOT/CHANGELOG.md"; then
    release_errors+=("date the $VERSION changelog entry with $RELEASE_DATE")
  fi
  if (( ${#release_errors[@]} > 0 )); then
    echo "Public release checks failed:" >&2
    for error in "${release_errors[@]}"; do
      echo " - $error" >&2
    done
    exit 1
  fi
fi

BUILD_DIR="$ROOT/dist/debroot"
PKG_DIR="$BUILD_DIR/DEBIAN"
USR_SHARE="$BUILD_DIR/usr/share/$APP"
USR_BIN="$BUILD_DIR/usr/bin"
USR_APPS="$BUILD_DIR/usr/share/applications"
USR_SYSTEMD="$BUILD_DIR/usr/lib/systemd/user"
USR_NAUTILUS="$BUILD_DIR/usr/share/nautilus-python/extensions"
USR_EMBLEMS="$BUILD_DIR/usr/share/icons/hicolor/scalable/emblems"
USR_APP_ICONS="$BUILD_DIR/usr/share/icons/hicolor/scalable/apps"
USR_METAINFO="$BUILD_DIR/usr/share/metainfo"
DOC_DIR="$BUILD_DIR/usr/share/doc/$APP"

rm -rf "$BUILD_DIR"
mkdir -p "$PKG_DIR" "$USR_SHARE" "$USR_BIN" "$USR_APPS" "$USR_SYSTEMD" "$USR_NAUTILUS" "$USR_EMBLEMS" "$USR_APP_ICONS" "$USR_METAINFO" "$DOC_DIR"

cp -r "$ROOT/src/talaryn" "$USR_SHARE/talaryn"
cp -r "$ROOT/data/icons" "$USR_SHARE/icons"
# Keep the horizontal logo in the source README only.  The installed
# application and AppIndicator both use talaryn-app-icon.svg.
rm -f "$USR_SHARE/icons/talaryn.svg"
cp -r "$ROOT/i18n" "$USR_SHARE/i18n"
find "$USR_SHARE/talaryn" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$USR_SHARE/talaryn" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
cp "$ROOT/data/nautilus/google_link_emblems.py" "$USR_NAUTILUS/google_link_emblems.py"
cp "$ROOT/data/nautilus/rclone_sync_emblems.py" "$USR_NAUTILUS/rclone_sync_emblems.py"
cp "$ROOT/data/nautilus/google_new_docs.py" "$USR_NAUTILUS/google_new_docs.py"
cp "$ROOT/data/nautilus/rclone_ssh_terminal.py" "$USR_NAUTILUS/rclone_ssh_terminal.py"
cp "$ROOT/data/nautilus/talaryn_file_comparison.py" "$USR_NAUTILUS/talaryn_file_comparison.py"
cp "$ROOT/data/nautilus/talaryn_i18n.py" "$USR_NAUTILUS/talaryn_i18n.py"
cp "$ROOT/data/emblems/"*.svg "$USR_EMBLEMS/"
cp "$ROOT/data/icons/talaryn-app-icon.svg" "$USR_APP_ICONS/talaryn.svg"
sed -e "s|@VERSION@|$VERSION|g" \
  -e "s|@RELEASE_DATE@|$RELEASE_DATE|g" \
  "$ROOT/data/metainfo/io.github.k_krakowski.Talaryn.metainfo.xml.in" \
  > "$USR_METAINFO/io.github.k_krakowski.Talaryn.metainfo.xml"
cp "$ROOT/packaging/data/copyright" "$DOC_DIR/copyright"
gzip -n -9 -c "$ROOT/CHANGELOG.md" > "$DOC_DIR/changelog.gz"
gzip -n -9 -c "$ROOT/README.md" > "$DOC_DIR/README.md.gz"

cat > "$USR_BIN/$APP" <<'EOF_WRAPPER'
#!/bin/sh
export TALARYN_DATA_DIR="/usr/share/talaryn"
exec /usr/bin/python3 -I -c 'import os, sys; sys.path.insert(0, os.environ["TALARYN_DATA_DIR"]); from talaryn.main import main; main()' "$@"
EOF_WRAPPER
chmod 0755 "$USR_BIN/$APP"

sed "s|@EXEC@|/usr/bin/$APP|g" \
  "$ROOT/data/applications/io.github.k_krakowski.Talaryn.desktop.in" \
  > "$USR_APPS/io.github.k_krakowski.Talaryn.desktop"
sed "s|@EXEC@|/usr/bin/$APP|g" \
  "$ROOT/data/applications/io.github.k_krakowski.Talaryn.Comparison.desktop.in" \
  > "$USR_APPS/io.github.k_krakowski.Talaryn.Comparison.desktop"
sed "s|@EXEC@|/usr/bin/$APP|g" \
  "$ROOT/data/applications/io.github.k_krakowski.Talaryn.GoogleCreate.desktop.in" \
  > "$USR_APPS/io.github.k_krakowski.Talaryn.GoogleCreate.desktop"

sed "s|@EXEC@|/usr/bin/$APP|g" \
  "$ROOT/data/systemd/talaryn@.service.in" \
  > "$USR_SYSTEMD/talaryn@.service"
sed "s|@EXEC@|/usr/bin/$APP|g" \
  "$ROOT/data/systemd/talaryn-monitor.service.in" \
  > "$USR_SYSTEMD/talaryn-monitor.service"

INSTALLED_SIZE="$(du -sk "$BUILD_DIR/usr" | awk '{print $1}')"
cat > "$PKG_DIR/control" <<EOF_CONTROL
Package: talaryn
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_SIZE
Homepage: https://github.com/k-krakowski/talaryn
Conflicts: rclone-mount-gui
Replaces: rclone-mount-gui
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0 (>= 4.10), gir1.2-adw-1 (>= 1.5), gir1.2-gnomedesktop-4.0, gir1.2-gtk-3.0, python3-nautilus, gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1, rclone (>= 1.74.4), fuse3, xdg-utils, libglib2.0-bin, libnotify-bin, zenity, systemd
Description: Talaryn - Give your cloud wings
 Talaryn is a graphical Linux interface for mounting cloud and remote storage
 as local drives using rclone.
EOF_CONTROL

cat > "$PKG_DIR/postinst" <<'EOF_POSTINST'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
exit 0
EOF_POSTINST
chmod 0755 "$PKG_DIR/postinst"

cat > "$PKG_DIR/prerm" <<'EOF_PRERM'
#!/bin/sh
set -e
case "${1:-}" in
  remove|deconfigure)
    for command_line in /proc/[0-9]*/cmdline; do
      [ -r "$command_line" ] || continue
      arguments="$(tr '\000' '\n' < "$command_line" 2>/dev/null || true)"
      executable="$(printf '%s\n' "$arguments" | sed -n '1p')"
      verb="$(printf '%s\n' "$arguments" | sed -n '2p')"
      case "$executable" in
        rclone|*/rclone)
          if [ "$verb" = 'mount' ] \
            && printf '%s\n' "$arguments" | grep -q '/talaryn/'; then
            echo "Refusing to remove Talaryn while one of its mounts is active." >&2
            echo "Finish pending transfers and unmount every profile, then retry." >&2
            exit 1
          fi
          ;;
      esac
    done
    ;;
esac
exit 0
EOF_PRERM
chmod 0755 "$PKG_DIR/prerm"

cat > "$PKG_DIR/postrm" <<'EOF_POSTRM'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
exit 0
EOF_POSTRM
chmod 0755 "$PKG_DIR/postrm"

find "$BUILD_DIR" -type d -exec chmod 0755 {} +
find "$BUILD_DIR" -type f -exec chmod 0644 {} +
chmod 0755 \
  "$USR_BIN/$APP" \
  "$PKG_DIR/postinst" \
  "$PKG_DIR/prerm" \
  "$PKG_DIR/postrm"

(
  cd "$BUILD_DIR"
  find usr -type f -print0 | sort -z | xargs -0 md5sum
) > "$PKG_DIR/md5sums"
chmod 0644 "$PKG_DIR/md5sums"

find "$BUILD_DIR" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$BUILD_DIR" "$ROOT/dist/${APP}_${VERSION}_all.deb"
echo "Built: $ROOT/dist/${APP}_${VERSION}_all.deb"
