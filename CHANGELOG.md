# Changelog

## Unreleased

- Replace the separate "New Google" naming and progress dialogs with one
  asynchronous GTK window using Talaryn branding and desktop identity.
- Place the creation window near the pointer on X11 and use the originating
  Nautilus window as its native parent on X11 and Wayland when available.
- Refresh the affected rclone VFS directory and Nautilus view after creating
  a Google document. A failed refresh can be retried without uploading again.

## 1.0.0 - 2026-09-28

First public release of Talaryn, a Linux desktop application for mounting
cloud and remote storage with rclone.

### Features

- GTK 4 and libadwaita interface with English, Polish, and Russian translations.
- Connection wizard and manager for cloud storage, WebDAV, SMB, object storage,
  SFTP, and FTP/FTPS, with support for existing rclone remotes.
- Mount profiles, cache presets, remote directory browsing, and systemd user
  services, with optional mounting after login and automatic remounting.
- Panel menu for mounting, unmounting, opening drives, and viewing logs.
- Transfer monitoring, persistent activity history, filtering, speed graphs,
  bandwidth limits, cache status, and completion/error notifications.
- Safe unmount checks and an option to wait for pending transfers to finish.
- Nautilus synchronization emblems, Google document creation and export,
  remote SSH terminal actions, and remote-path copying.
- Per-user source installer and a Debian package for Ubuntu 24.04.

### Experimental

- File and folder comparison with content hashes and optional byte-by-byte
  verification. This beta feature is disabled by default and can be enabled
  in Settings → Experimental features.

### Security

- Private Unix sockets for local rclone control and credential exchange.
- Private configuration storage with locked, atomic updates.
- Destructive mount operations are blocked when transfer safety cannot be
  confirmed; forced unmounting requires an explicit user action.
- Google document creation respects cancellation and refuses to overwrite an
  existing file.
