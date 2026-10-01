# Releasing Talaryn

## Prepare the release

1. Set `__version__` and `__release_date__` in `src/talaryn/__init__.py` and add
   the matching version/date heading to `CHANGELOG.md`.
2. Check the project URL, maintainer contact, translations, and included artwork.
3. Run the automated checks:

   ```bash
   git diff --check
   PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
   python3 -m compileall -q src data/nautilus
   bash -n install.sh uninstall.sh packaging/build-deb.sh
   PUBLIC_RELEASE=1 ./packaging/build-deb.sh
   ```

4. Inspect the package control fields and payload. Ensure it contains no user
   configuration, tokens, private keys, databases, logs, or provider artwork.
5. Scan the source tree and the Git history that will actually be published with
   a secret scanner. A new initial commit must contain only reviewed source;
   do not copy an old `.git` directory into the public repository.

## Manual acceptance tests

Use a disposable Ubuntu 24.04 account or virtual machine and synthetic files.
Test both `install.sh` and `sudo apt install ./dist/talaryn_1.0.0_all.deb`:

- Install prerequisites, launch the GUI, and restart Nautilus.
- Create and edit a remote, mount a profile, write/read files, and verify the
  contents on the remote. Exercise the storage backends advertised for the release.
- Check active transfers, queued uploads, retries, disconnection, cache errors,
  bandwidth limits, and remounting after login.
- Try normal unmount, wait-and-unmount, connection deletion, shutdown, and
  uninstall while transfers are pending or monitoring is unavailable.
- Exercise every enabled Nautilus action, including cancelling Google document
  creation, refusing duplicate filenames, and confirming/cancelling export
  overwrites.
- Check English, Polish, and Russian labels and dialogs for clipped text.
- Confirm that comparison is disabled by default, then smoke-test it separately
  as an experimental feature.
- Unmount all profiles, uninstall, and verify that user data remains intact.

Record the results before marking a release stable. Passing unit tests and
changing the version number do not replace these checks.

## Publish

Commit the reviewed source, run CI on that commit, and build the release package
from a clean checkout or source archive. Confirm that `git status --short` is
empty apart from ignored build outputs. Create an annotated `v1.0.0` tag and
publish the package, source archive, release notes, and a SHA-256 checksum.

The repository is <https://github.com/k-krakowski/talaryn>.
