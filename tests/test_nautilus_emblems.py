from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_nautilus_extension(module_name: str, filename: str):
    gi = types.ModuleType("gi")
    repository = types.ModuleType("gi.repository")

    class DummyGObject:
        pass

    class DummyInfoProvider:
        pass

    class DummyFileInfo:
        @staticmethod
        def lookup_for_uri(_uri):
            return None

    class DummyCancellable:
        def cancel(self):
            return None

    class DummyGioFile:
        @staticmethod
        def new_for_path(_path):
            return DummyGioFile()

        def read_async(self, *_args):
            return None

    class DummyMenuProvider:
        pass

    class DummyMenuItem:
        def __init__(self, **values):
            self.values = values
            self.callback = None
            self.callback_args = ()

        def connect(self, _signal, callback, *args):
            self.callback = callback
            self.callback_args = args

        def activate(self):
            if self.callback is not None:
                self.callback(self, *self.callback_args)

    gi.require_version = lambda *_args: None
    repository.GObject = types.SimpleNamespace(GObject=DummyGObject)
    repository.Nautilus = types.SimpleNamespace(
        InfoProvider=DummyInfoProvider,
        FileInfo=DummyFileInfo,
        MenuProvider=DummyMenuProvider,
        MenuItem=DummyMenuItem,
    )
    repository.Gio = types.SimpleNamespace(
        Cancellable=DummyCancellable,
        File=DummyGioFile,
    )
    repository.GLib = types.SimpleNamespace(
        PRIORITY_DEFAULT=0,
        SOURCE_CONTINUE=True,
        SOURCE_REMOVE=False,
        source_remove=lambda *_args: True,
        timeout_add_seconds=lambda *_args: 1,
    )
    gi.repository = repository

    path = PROJECT_ROOT / "data" / "nautilus" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "gi": gi,
            "gi.repository": repository,
        },
    ):
        spec.loader.exec_module(module)
    return module


SYNC_EMBLEMS = _load_nautilus_extension(
    "test_rclone_sync_emblems",
    "rclone_sync_emblems.py",
)
GOOGLE_EMBLEMS = _load_nautilus_extension(
    "test_google_link_emblems",
    "google_link_emblems.py",
)
SSH_TERMINAL = _load_nautilus_extension(
    "test_rclone_ssh_terminal",
    "rclone_ssh_terminal.py",
)
FILE_COMPARISON = _load_nautilus_extension(
    "test_talaryn_file_comparison",
    "talaryn_file_comparison.py",
)


class FakeLocation:
    def __init__(self, path: str) -> None:
        self._path = path

    def get_path(self) -> str:
        return self._path


class FakeFile:
    def __init__(
        self,
        path: str,
        name: str = "file.txt",
        *,
        directory: bool = False,
    ) -> None:
        self._location = FakeLocation(path)
        self._name = name
        self._directory = directory
        self.emblems: list[str] = []
        self.attributes: dict[str, str] = {}
        self.invalidations = 0

    def is_directory(self) -> bool:
        return self._directory

    def get_location(self) -> FakeLocation:
        return self._location

    def get_name(self) -> str:
        return self._name

    def get_uri(self) -> str:
        return f"file://{self._location.get_path()}"

    def add_emblem(self, emblem: str) -> None:
        self.emblems.append(emblem)

    def add_string_attribute(self, key: str, value: str) -> None:
        self.attributes[key] = value

    def invalidate_extension_info(self) -> None:
        self.invalidations += 1


class RcloneSyncEmblemsTests(unittest.TestCase):
    def setUp(self) -> None:
        SYNC_EMBLEMS._profiles_cache = {}
        SYNC_EMBLEMS._profiles_stamp = None
        SYNC_EMBLEMS._profiles_checked_at = 0.0
        SYNC_EMBLEMS._profile_roots_cache = ()
        SYNC_EMBLEMS._profile_roots_stamp = None
        SYNC_EMBLEMS._snapshot_cache = {}
        SYNC_EMBLEMS._snapshot_stamp = None
        SYNC_EMBLEMS._snapshot_syncing_paths = None
        SYNC_EMBLEMS._snapshot_checked_at = 0.0

    def test_profile_matching_never_probes_the_fuse_mount(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config" / "talaryn"
            mount_dir = root / "cloud" / "drive"
            config_dir.mkdir(parents=True)
            (config_dir / "profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "drive": {
                                "mount_dir": str(mount_dir),
                                "kind": "onedrive",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}),
                patch.object(
                    SYNC_EMBLEMS.os.path,
                    "ismount",
                    side_effect=AssertionError("FUSE mount was probed"),
                ),
            ):
                first = SYNC_EMBLEMS._profile_for_path(mount_dir / "one.txt")
                second = SYNC_EMBLEMS._profile_for_path(mount_dir / "two.txt")
                third = SYNC_EMBLEMS._profile_for_path(
                    mount_dir / "three.txt"
                )

            self.assertEqual(first[0], "drive")
            self.assertEqual(second[0], "drive")
            self.assertEqual(third[0], "drive")

    def test_syncing_paths_are_indexed_and_normalized(self) -> None:
        snapshot = {
            "syncing_paths": {
                "drive": ["/folder/file.txt/", "second.txt"],
                "invalid": "not-a-list",
            }
        }

        index = SYNC_EMBLEMS._build_syncing_path_index(snapshot)

        self.assertIsInstance(index["drive"], frozenset)
        self.assertEqual(
            index["drive"],
            frozenset(("folder/file.txt", "second.txt")),
        )
        self.assertIsNone(index["invalid"])

        SYNC_EMBLEMS._snapshot_syncing_paths = index
        with patch.object(
            SYNC_EMBLEMS,
            "_load_snapshot",
            return_value={
                "profiles": {"drive": {"connected": True}},
            },
        ):
            state = SYNC_EMBLEMS._snapshot_sync_state(
                "drive",
                Path("folder/file.txt"),
            )

        self.assertTrue(state)

    def test_cached_snapshot_still_expires_without_a_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_dir = root / "cache" / "talaryn"
            cache_dir.mkdir(parents=True)
            (cache_dir / "nautilus-status.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "updated_at": 100.0,
                        "profiles": {"drive": {"connected": True}},
                        "syncing_paths": {"drive": ["file.txt"]},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}),
                patch.object(SYNC_EMBLEMS.time, "time", side_effect=(100.0, 113.0)),
            ):
                self.assertTrue(SYNC_EMBLEMS._load_snapshot())
                self.assertEqual(SYNC_EMBLEMS._load_snapshot(), {})

    def test_provider_keeps_syncing_and_synced_emblem_semantics(self) -> None:
        provider = SYNC_EMBLEMS.RcloneSyncEmblemsProvider()
        profile = {"kind": "onedrive"}
        found = ("drive", profile, Path("folder/file.txt"))

        syncing_file = FakeFile("/cloud/drive/folder/file.txt")
        with (
            patch.object(SYNC_EMBLEMS, "_profile_for_path", return_value=found),
            patch.object(SYNC_EMBLEMS, "_snapshot_sync_state", return_value=True),
        ):
            provider.update_file_info(syncing_file)

        synced_file = FakeFile("/cloud/drive/folder/file.txt")
        with (
            patch.object(SYNC_EMBLEMS, "_profile_for_path", return_value=found),
            patch.object(SYNC_EMBLEMS, "_snapshot_sync_state", return_value=False),
        ):
            provider.update_file_info(synced_file)

        self.assertEqual(syncing_file.emblems, ["rclone-syncing"])
        self.assertEqual(
            syncing_file.attributes["rclone-sync-status"],
            "Synchronizing",
        )
        self.assertEqual(synced_file.emblems, ["rclone-synced"])
        self.assertEqual(
            synced_file.attributes["rclone-sync-status"],
            "Synchronized",
        )

    def test_one_status_timer_serves_many_files(self) -> None:
        found = ("drive", {"kind": "onedrive"}, Path("file.txt"))
        with (
            patch.object(
                SYNC_EMBLEMS.GLib,
                "timeout_add_seconds",
                return_value=1,
            ) as timeout,
            patch.object(SYNC_EMBLEMS, "_profile_for_path", return_value=found),
            patch.object(SYNC_EMBLEMS, "_snapshot_sync_state", return_value=False),
        ):
            provider = SYNC_EMBLEMS.RcloneSyncEmblemsProvider()
            for number in range(1000):
                provider.update_file_info(
                    FakeFile(f"/cloud/drive/file-{number}.txt")
                )

        self.assertEqual(timeout.call_count, 1)
        self.assertEqual(len(provider._tracked_files), 1000)

    def test_status_poll_refreshes_synced_syncing_synced_transition(self) -> None:
        provider = SYNC_EMBLEMS.RcloneSyncEmblemsProvider()
        profile = {"kind": "onedrive"}
        found = ("drive", profile, Path("folder/file.txt"))
        file = FakeFile("/cloud/drive/folder/file.txt")

        with (
            patch.object(SYNC_EMBLEMS, "_profile_for_path", return_value=found),
            patch.object(SYNC_EMBLEMS, "_snapshot_sync_state", return_value=False),
        ):
            provider.update_file_info(file)

        snapshot = {"profiles": {"drive": {"connected": True}}}
        provider._seen_profiles_stamp = SYNC_EMBLEMS._profiles_stamp
        SYNC_EMBLEMS._snapshot_syncing_paths = {
            "drive": frozenset(("folder/file.txt",)),
        }
        with (
            patch.object(SYNC_EMBLEMS, "_load_profiles", return_value={}),
            patch.object(SYNC_EMBLEMS, "_load_snapshot", return_value=snapshot),
            patch.object(
                SYNC_EMBLEMS.Nautilus.FileInfo,
                "lookup_for_uri",
                return_value=file,
            ),
        ):
            self.assertTrue(provider._poll_status())

        self.assertEqual(file.invalidations, 1)

        with (
            patch.object(SYNC_EMBLEMS, "_profile_for_path", return_value=found),
            patch.object(SYNC_EMBLEMS, "_snapshot_sync_state", return_value=True),
        ):
            provider.update_file_info(file)

        SYNC_EMBLEMS._snapshot_syncing_paths = {"drive": frozenset()}
        with (
            patch.object(SYNC_EMBLEMS, "_load_profiles", return_value={}),
            patch.object(SYNC_EMBLEMS, "_load_snapshot", return_value=snapshot),
            patch.object(
                SYNC_EMBLEMS.Nautilus.FileInfo,
                "lookup_for_uri",
                return_value=file,
            ),
        ):
            provider._poll_status()
            provider._poll_status()

        self.assertEqual(file.invalidations, 2)

    def test_changed_status_drops_files_no_longer_known_by_nautilus(self) -> None:
        provider = SYNC_EMBLEMS.RcloneSyncEmblemsProvider()
        provider._tracked_files["file:///gone"] = {
            "profile_id": "drive",
            "relative_path": Path("gone.txt"),
            "state": False,
        }
        provider._seen_profiles_stamp = SYNC_EMBLEMS._profiles_stamp
        SYNC_EMBLEMS._snapshot_syncing_paths = {
            "drive": frozenset(("gone.txt",)),
        }
        with (
            patch.object(SYNC_EMBLEMS, "_load_profiles", return_value={}),
            patch.object(
                SYNC_EMBLEMS,
                "_load_snapshot",
                return_value={
                    "profiles": {"drive": {"connected": True}},
                },
            ),
            patch.object(
                SYNC_EMBLEMS.Nautilus.FileInfo,
                "lookup_for_uri",
                return_value=None,
            ),
        ):
            provider._poll_status()

        self.assertNotIn("file:///gone", provider._tracked_files)

    def test_google_link_still_gets_no_rclone_sync_emblem(self) -> None:
        provider = SYNC_EMBLEMS.RcloneSyncEmblemsProvider()
        profile = {"kind": "gdrive"}
        found = ("drive", profile, Path("document.link.html"))
        file = FakeFile(
            "/cloud/drive/document.link.html",
            name="document.link.html",
        )

        with (
            patch.object(SYNC_EMBLEMS, "_profile_for_path", return_value=found),
            patch.object(SYNC_EMBLEMS, "_snapshot_sync_state") as sync_state,
        ):
            provider.update_file_info(file)

        self.assertEqual(file.emblems, [])
        self.assertEqual(provider._tracked_files, {})
        sync_state.assert_not_called()


class GoogleLinkEmblemsTests(unittest.TestCase):
    def setUp(self) -> None:
        GOOGLE_EMBLEMS._google_profiles_cache = ()
        GOOGLE_EMBLEMS._google_mounts_cache = ()
        GOOGLE_EMBLEMS._profiles_stamp = None

    def test_link_types_use_project_authored_emblems(self) -> None:
        names = [marker["emblem"] for marker in GOOGLE_EMBLEMS.MARKERS]
        self.assertEqual(
            names,
            [
                "talaryn-document",
                "talaryn-spreadsheet",
                "talaryn-presentation",
            ],
        )
        emblem_dir = PROJECT_ROOT / "data" / "emblems"
        for name in names:
            with self.subTest(name=name):
                self.assertTrue((emblem_dir / f"emblem-{name}.svg").is_file())
        for legacy_name in ("google-doc", "google-sheet", "google-slide"):
            with self.subTest(legacy_name=legacy_name):
                self.assertFalse(
                    (emblem_dir / f"emblem-{legacy_name}.svg").exists()
                )

    def test_google_mounts_are_cached_until_profiles_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config" / "talaryn"
            config_dir.mkdir(parents=True)
            profiles_file = config_dir / "profiles.json"
            first_mount = root / "cloud" / "google"
            second_mount = root / "cloud" / "google-work-longer-name"
            profiles_file.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "google": {
                                "kind": "gdrive",
                                "mount_dir": str(first_mount),
                            },
                            "other": {
                                "kind": "onedrive",
                                "mount_dir": str(root / "cloud" / "other"),
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(root / "config")},
            ):
                first = GOOGLE_EMBLEMS._load_google_mounts()
                with patch.object(
                    GOOGLE_EMBLEMS.Path,
                    "read_text",
                    side_effect=AssertionError("profiles.json was read twice"),
                ):
                    cached = GOOGLE_EMBLEMS._load_google_mounts()

                profiles_file.write_text(
                    json.dumps(
                        {
                            "profiles": {
                                "google-work": {
                                    "kind": "gdrive",
                                    "mount_dir": str(second_mount),
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                changed = GOOGLE_EMBLEMS._load_google_mounts()

            self.assertEqual(first, (first_mount.resolve(),))
            self.assertIs(cached, first)
            self.assertEqual(changed, (second_mount.resolve(),))

    def test_link_contents_are_not_read_synchronously(self) -> None:
        provider = GOOGLE_EMBLEMS.GoogleLinkEmblemsProvider()
        file = FakeFile(
            "/cloud/google/document.link.html",
            name="document.link.html",
        )
        with (
            patch.object(
                provider,
                "_file_to_path",
                return_value=Path("/cloud/google/document.link.html"),
            ),
            patch.object(provider, "_is_inside_google_mount", return_value=True),
            patch.object(provider, "_queue_read") as queue_read,
            patch.object(
                Path,
                "open",
                side_effect=AssertionError("FUSE file opened in Nautilus callback"),
            ),
        ):
            provider.update_file_info(file)

        queue_read.assert_called_once_with(
            "file:///cloud/google/document.link.html",
            Path("/cloud/google/document.link.html"),
        )

    def test_cached_google_marker_is_applied_without_file_io(self) -> None:
        provider = GOOGLE_EMBLEMS.GoogleLinkEmblemsProvider()
        file = FakeFile(
            "/cloud/google/document.link.html",
            name="document.link.html",
        )
        provider._content_cache[file.get_uri()] = (
            GOOGLE_EMBLEMS.time.monotonic() + 60,
            GOOGLE_EMBLEMS.MARKERS[0],
        )
        with (
            patch.object(
                provider,
                "_file_to_path",
                return_value=Path("/cloud/google/document.link.html"),
            ),
            patch.object(provider, "_is_inside_google_mount", return_value=True),
            patch.object(provider, "_queue_read") as queue_read,
        ):
            provider.update_file_info(file)

        self.assertEqual(file.emblems, ["talaryn-document"])
        queue_read.assert_not_called()

    def test_google_link_has_an_export_context_action(self) -> None:
        provider = GOOGLE_EMBLEMS.GoogleLinkEmblemsProvider()
        file = FakeFile(
            "/cloud/google/document.link.html",
            name="document.link.html",
        )
        match = (
            "google-profile",
            {"kind": "gdrive"},
            Path("/cloud/google"),
        )
        with patch.object(
            GOOGLE_EMBLEMS,
            "_profile_for_path",
            return_value=match,
        ):
            items = provider.get_file_items([file])

        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0].values["label"],
            GOOGLE_EMBLEMS._tr("google_export_action"),
        )
        self.assertIn("link", items[0].values["tip"].casefold())

        with (
            patch.object(
                GOOGLE_EMBLEMS,
                "_talaryn_command",
                return_value=["/usr/bin/talaryn"],
            ),
            patch.object(GOOGLE_EMBLEMS.subprocess, "Popen") as process,
        ):
            items[0].activate()

        self.assertEqual(
            process.call_args.args[0],
            [
                "/usr/bin/talaryn",
                "google-export",
                "google-profile",
                "/cloud/google/document.link.html",
            ],
        )

    def test_export_context_action_is_limited_to_google_link_files(self) -> None:
        provider = GOOGLE_EMBLEMS.GoogleLinkEmblemsProvider()
        regular_file = FakeFile(
            "/cloud/google/document.pdf",
            name="document.pdf",
        )
        outside_link = FakeFile(
            "/home/user/document.link.html",
            name="document.link.html",
        )
        self.assertEqual(provider.get_file_items([regular_file]), [])
        with patch.object(
            GOOGLE_EMBLEMS,
            "_profile_for_path",
            return_value=None,
        ):
            self.assertEqual(provider.get_file_items([outside_link]), [])

    def test_async_google_reader_requests_only_the_header(self) -> None:
        provider = GOOGLE_EMBLEMS.GoogleLinkEmblemsProvider()
        stream = Mock()
        source = Mock()
        source.read_finish.return_value = stream
        cancellable = object()
        provider._pending["file:///document"] = {
            "cancellable": cancellable,
            "timeout_id": 1,
            "stream": None,
        }

        provider._stream_opened(source, object(), "file:///document")

        stream.read_bytes_async.assert_called_once()
        arguments = stream.read_bytes_async.call_args.args
        self.assertEqual(arguments[0], GOOGLE_EMBLEMS.READ_BYTES)
        self.assertIs(arguments[2], cancellable)


class RcloneSshTerminalTests(unittest.TestCase):
    def test_sftp_config_filter_works_with_rclone_1_74(self) -> None:
        if shutil.which("rclone") is None:
            self.skipTest("rclone is not installed")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "rclone.conf"
            config.write_text(
                "[server]\n"
                "type = sftp\n"
                "host = server.example\n"
                "user = alice\n"
                "port = 2222\n"
                "pass = obscured-secret\n",
                encoding="utf-8",
            )
            with patch.dict(
                SSH_TERMINAL.os.environ,
                {"RCLONE_CONFIG": str(config)},
                clear=False,
            ):
                parsed = SSH_TERMINAL._rclone_config("server")

        self.assertEqual(parsed["host"], "server.example")
        self.assertEqual(parsed["user"], "alice")
        self.assertNotIn("pass", parsed)

    def test_sftp_config_parser_keeps_only_connection_fields(self) -> None:
        parsed = SSH_TERMINAL._parse_sftp_connection(
            "type = sftp\n"
            "host = server.example\n"
            "user = alice\n"
            "port = 2222\n"
            "key_file = ~/.ssh/id_ed25519\n"
            "pass = obscured-secret\n"
            "key_pem = private-key-material\n"
            "token = access-token\n"
        )

        self.assertEqual(
            parsed,
            {
                "type": "sftp",
                "host": "server.example",
                "user": "alice",
                "port": "2222",
                "key_file": "~/.ssh/id_ed25519",
            },
        )

    def test_sftp_config_command_removes_debug_environment(self) -> None:
        with patch.dict(
            SSH_TERMINAL.os.environ,
            {
                "RCLONE_CONFIG": "/tmp/rclone.conf",
                "RCLONE_DUMP": "headers,bodies",
                "RCLONE_LOG_FILE": "/tmp/unsafe.log",
            },
            clear=True,
        ):
            environment = SSH_TERMINAL._safe_rclone_environment()

        self.assertEqual(
            environment,
            {"RCLONE_CONFIG": "/tmp/rclone.conf"},
        )



class FileComparisonMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        locale = patch.dict(FILE_COMPARISON.tr.__globals__, {"_language": lambda: "en"})
        locale.start()
        self.addCleanup(locale.stop)
        settings = patch.object(FILE_COMPARISON, "_settings",
                                return_value={"context_file_comparison": True})
        settings.start()
        self.addCleanup(settings.stop)

    def test_comparison_is_disabled_without_an_explicit_preference(self) -> None:
        provider = FILE_COMPARISON.TalarynFileComparisonProvider()
        with patch.object(FILE_COMPARISON, "_settings", return_value={}):
            self.assertEqual(provider.get_file_items([FakeFile("/tmp/file")]), [])

    def test_disabling_comparison_invalidates_existing_menu_actions(self) -> None:
        provider = FILE_COMPARISON.TalarynFileComparisonProvider()
        with patch.object(FILE_COMPARISON, "_load_marked_paths", return_value=[]):
            actions = provider.get_file_items([FakeFile("/tmp/file")])
        with patch.object(FILE_COMPARISON, "_settings", return_value={}), \
             patch.object(FILE_COMPARISON, "_mark_paths") as mark, \
             patch.object(FILE_COMPARISON.subprocess, "Popen") as launch:
            actions[0].activate()
        mark.assert_not_called()
        launch.assert_not_called()

    def test_compare_adds_to_pool_and_remove_follows_selection_state(self) -> None:
        provider = FILE_COMPARISON.TalarynFileComparisonProvider()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "file-comparison.json"
            with patch.object(
                FILE_COMPARISON,
                "_selection_file",
                return_value=state,
            ):
                first = FakeFile("/tmp/reference.txt")
                initial = provider.get_file_items([first])
                self.assertEqual(
                    [item.values["label"] for item in initial],
                    ["Compare"],
                )
                with (
                    patch.object(
                        FILE_COMPARISON,
                        "_talaryn_command",
                        return_value=["talaryn"],
                    ),
                    patch.object(FILE_COMPARISON.subprocess, "Popen"),
                ):
                    initial[0].activate()
                self.assertEqual(
                    FILE_COMPARISON._load_marked_paths(),
                    ["/tmp/reference.txt"],
                )
                self.assertEqual(first.invalidations, 1)

                current = FakeFile("/tmp/selected.txt")
                actions = provider.get_file_items([current])
                self.assertEqual(
                    [item.values["label"] for item in actions],
                    ["Compare"],
                )
                with (
                    patch.object(
                        FILE_COMPARISON,
                        "_talaryn_command",
                        return_value=["talaryn"],
                    ),
                    patch.object(FILE_COMPARISON.subprocess, "Popen") as process,
                ):
                    actions[0].activate()
                self.assertEqual(
                    process.call_args.args[0],
                    ["talaryn", "compare", "/tmp/selected.txt"],
                )

                remove = provider.get_file_items([first])
                self.assertEqual(
                    [item.values["label"] for item in remove],
                    ["Compare", "Remove from comparison"],
                )
                remove[1].activate()
                self.assertEqual(
                    FILE_COMPARISON._load_marked_paths(),
                    ["/tmp/selected.txt"],
                )
                self.assertEqual(first.invalidations, 2)
                refreshed = provider.get_file_items([first])
                self.assertEqual(
                    [item.values["label"] for item in refreshed],
                    ["Compare"],
                )

    def test_folder_can_be_marked_and_compared(self) -> None:
        provider = FILE_COMPARISON.TalarynFileComparisonProvider()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "file-comparison.json"
            reference = FakeFile(
                "/tmp/reference-folder",
                directory=True,
            )
            selected = FakeFile(
                "/tmp/selected-folder",
                directory=True,
            )
            with patch.object(
                FILE_COMPARISON,
                "_selection_file",
                return_value=state,
            ):
                mark_action = provider.get_file_items([reference])
                self.assertEqual(
                    [item.values["label"] for item in mark_action],
                    ["Compare"],
                )
                with (
                    patch.object(
                        FILE_COMPARISON,
                        "_talaryn_command",
                        return_value=["talaryn"],
                    ),
                    patch.object(FILE_COMPARISON.subprocess, "Popen"),
                ):
                    mark_action[0].activate()
                actions = provider.get_file_items([selected])
                self.assertEqual(
                    [item.values["label"] for item in actions],
                    ["Compare"],
                )

    def test_menu_can_be_disabled_in_settings(self) -> None:
        provider = FILE_COMPARISON.TalarynFileComparisonProvider()
        with patch.object(
            FILE_COMPARISON,
            "_settings",
            return_value={"context_file_comparison": False},
        ):
            self.assertEqual(provider.get_file_items([FakeFile("/tmp/file")]), [])

if __name__ == "__main__":
    unittest.main()
