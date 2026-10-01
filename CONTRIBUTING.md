# Contributing

Talaryn is currently prepared and tested on Ubuntu 24.04 with Python
3.10 or newer, GTK 4.10 or newer, libadwaita 1.5 or newer, and rclone 1.74.4 or
newer.

## Development setup

Clone the repository using the clone URL displayed by its hosting service, then
enter the `talaryn` directory. Install the Ubuntu 24.04 packages listed
in the Requirements section of `README.md`.

Run the application directly from the checkout with:

```bash
PYTHONPATH=src python3 -m talaryn gui
```

Use disposable rclone remotes and non-sensitive files for development and
integration testing. Never commit `rclone.conf`, OAuth credentials, private
keys, logs, activity databases, or real user profiles.

## Checks required for every change

```bash
git diff --check
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src data/nautilus
bash -n install.sh uninstall.sh packaging/build-deb.sh
```

Changes to packaging should also pass:

```bash
./packaging/build-deb.sh
dpkg-deb --info dist/talaryn_*_all.deb
dpkg-deb --contents dist/talaryn_*_all.deb
```

Test GUI or Nautilus changes manually on Ubuntu 24.04. Mount and unmount a
disposable remote, exercise active and queued transfers, restart Nautilus, and
verify that closing or shutting down during synchronization follows the selected
safety settings.

## Translations

Translations and reviews by native speakers are welcome. You can contribute a
new language, complete an existing catalogue, or improve terminology and
clarity. If you are not comfortable changing code, open an issue naming the
language you would like to work on; the maintainer can prepare the catalogue for
translation.

English is the reference catalogue in `i18n/en.json`. To add a language:

1. Copy `i18n/en.json` to `i18n/<code>.json`, using a lowercase ISO 639-1
   language code when one exists, and translate the values without changing the
   keys.
2. Add the code to `SUPPORTED_LANGUAGES` in `src/talaryn/i18n.py` and
   `data/nautilus/talaryn_i18n.py`.
3. Add its display-name key to `LANGUAGE_LABEL_KEYS` in `src/talaryn/app.py`,
   then add that key to every language catalogue. Write language names in their
   native form, for example `Deutsch` or `Français`.
4. Run the checks below and test the interface in the new language. Pay
   particular attention to clipped labels, dialogs, Nautilus actions, and
   plural or grammatical forms involving numbers.

Every language file must:

- contain exactly the same keys as English;
- preserve format fields such as `{profile}`, `{path}`, and `{count}`;
- remain valid UTF-8 JSON;
- use consistent rclone, mount, remote, and profile terminology;
- receive human review for destructive actions, credential prompts, errors,
  and synchronization warnings.

Run the translation checks after changing any catalogue:

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p 'test_*i18n.py' -v
```

Nautilus extensions also use the JSON catalogues; restart Nautilus after
changing their translations. Desktop metadata uses localized desktop-entry
fields and should be updated when a translation covers those launchers.

## Versioning and changelog

`src/talaryn/__init__.py` is the single source of the application
version and release date (`__version__` and `__release_date__`). Packaging reads
these values dynamically. Update both for a release and add the matching entry
to `CHANGELOG.md`. See `RELEASING.md` for the release workflow.

## Artwork and third-party material

Do not add a logo, icon, screenshot, template, or other third-party asset unless
the contribution includes:

- its authoritative source URL and retrieval date;
- copyright holder and exact redistribution licence;
- all required licence and attribution text;
- modification history;
- confirmation that current trademark or brand rules permit the proposed use.

The current release uses only project-authored generic artwork. Keep provider
logos and private permission correspondence outside the repository and release
artifacts. If third-party artwork is introduced in a future release, add its
public licensing documentation and update `packaging/data/copyright` together.
An asset with unclear permission should be replaced by generic project-authored
artwork.

## Change scope

Keep changes focused and preserve unrelated work in the tree. Add regression
tests for bug fixes and parsing, persistence, command construction, or activity
tracking changes. Avoid silent data migration or deletion; document migrations
and provide a recovery path.

## Contact

For project questions, open an issue or write to [k.krakowski.dev@gmail.com](mailto:k.krakowski.dev@gmail.com).
Use `SECURITY.md` for private vulnerability reports.
