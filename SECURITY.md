# Security policy

## Supported versions

The latest 1.x release receives security fixes on a best-effort basis.
Upgrade to the latest patch release before reporting a vulnerability.

## Reporting a vulnerability

Report vulnerabilities privately using [GitHub private vulnerability reporting](https://github.com/k-krakowski/talaryn/security/advisories/new). Do not publish exploit details or credentials in an issue.

A useful report contains:

- the affected version and installation method;
- Linux distribution, desktop environment, rclone version, and Nautilus
  version when relevant;
- a minimal reproduction using disposable remotes and synthetic data;
- expected and observed impact;
- suggested remediation, if known.

Never attach an unredacted `rclone.conf`, OAuth token, private key, activity
database, or application/rclone log. Rotate any credential accidentally shared
in a report.

## Security model and trust boundaries

- Rclone owns authentication and stores backend credentials in its own
  configuration. Treat `rclone.conf` as a secret and protect it with restrictive
  filesystem permissions.
- The connection wizard sends credentials in an HTTP JSON body to a short-lived
  local `rclone rcd` process through a private Unix socket. Secrets are not
  placed in process arguments. Rclone still receives and persists them in its
  own configuration according to the selected backend.
- Profile and settings JSON files are trusted local configuration. Do not copy
  profiles from untrusted sources.
- A custom mount command and the configurable terminal command can execute local
  programs with the user's privileges. They are intentionally powerful and are
  not a sandbox boundary.
- The SFTP Nautilus helper reads only the selected remote and filters rclone's
  output through an allowlist (`type`, `host`, `user`, `port`, `key_file`, and
  `ssh`) before it reaches the extension process. Standard password, token,
  and embedded-key fields are discarded by the filtering process.
- Mount processes run as systemd user services, not root services.
- Rclone RC uses profile-specific Unix sockets inside a private runtime
  directory. The application does not intentionally expose an unauthenticated
  TCP RC listener.
- Provider endpoints, TLS verification, token refresh, and remote-side
  encryption are implemented by rclone and the selected backend.
- The application does not implement an automatic self-update mechanism. Obtain
  packages only from a release location announced by the project and verify the
  published checksum or provenance attestation.

## Release security checks

Before publishing a release:

1. Run the complete CI workflow and manual mount, transfer, shutdown, upgrade,
   and uninstall tests on a disposable account.
2. Scan the full reachable Git history with a secret scanner such as gitleaks.
3. Inspect the package contents and confirm that no user configuration, logs,
   databases, caches, or credentials are included.
4. Generate and publish a SHA-256 checksum for every artifact.
5. Confirm that the source archive and installation packages contain only
   project-authored artwork, with no provider logos or private permission records.
