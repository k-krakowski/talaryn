from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talaryn.comparison import (
    ComparisonRow,
    FileDetails,
    FolderComparison,
    calculate_hashes,
    compare_comparison_rows,
    compare_matching_pairs_byte_by_byte,
    compare_pool_rows,
    create_comparison_rows,
    create_pool_rows,
    hash_algorithm_label,
    is_directory_comparison,
    load_marked_paths,
    mark_paths,
    match_comparison_rows,
    modification_times_match,
    unmark_paths,
)


class ComparisonTests(unittest.TestCase):
    def test_marked_paths_are_private_persistent_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "file-comparison.json"
            with (
                patch("talaryn.comparison.COMPARISON_SELECTION_FILE", state),
                patch("talaryn.comparison.ensure_runtime_dirs"),
            ):
                mark_paths(("/tmp/a", "/tmp/a", "/tmp/b"))
                self.assertEqual(load_marked_paths(), ["/tmp/a", "/tmp/b"])
                unmark_paths(("/tmp/a",))
                self.assertEqual(load_marked_paths(), ["/tmp/b"])
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_removing_folder_keeps_file_added_as_an_independent_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "file-comparison.json"
            folder = Path(directory) / "folder"
            child = folder / "child.txt"
            with (
                patch("talaryn.comparison.COMPARISON_SELECTION_FILE", state),
                patch("talaryn.comparison.ensure_runtime_dirs"),
            ):
                mark_paths((folder, child))
                unmark_paths((folder,))
                self.assertEqual(load_marked_paths(), [str(child)])

    def test_all_files_are_matched_by_content_not_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marked = root / "marked"
            selected = root / "selected"
            marked.mkdir()
            selected.mkdir()
            (marked / "renamed-a.bin").write_text("same A", encoding="utf-8")
            (marked / "only-marked.bin").write_text("marked", encoding="utf-8")
            (selected / "renamed-b.bin").write_text("same A", encoding="utf-8")
            (selected / "only-selected.bin").write_text("selected", encoding="utf-8")
            os.utime(selected / "renamed-b.bin", (1_700_000_000, 1_700_000_000))
            rows = create_comparison_rows(
                (marked / "renamed-a.bin", marked / "only-marked.bin"),
                (selected / "renamed-b.bin", selected / "only-selected.bin"),
            )
            calculate_hashes(rows)
            matched = match_comparison_rows(rows)
            self.assertEqual(len(matched), 3)
            self.assertEqual(matched[0].status(), "same_content")
            self.assertEqual(matched[0].reference.name, "renamed-a.bin")
            self.assertEqual(matched[0].selected.name, "renamed-b.bin")
            self.assertEqual(matched[1].status(), "missing_selected")
            self.assertEqual(matched[2].status(), "missing_reference")

    def test_sha256_is_calculated_from_local_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.txt"
            selected = root / "selected.txt"
            reference.write_text("same contents", encoding="utf-8")
            selected.write_text("same contents", encoding="utf-8")
            os.utime(reference, (1_700_000_000, 1_700_000_000))
            os.utime(selected, (1_700_000_000, 1_700_000_000))
            source_rows = create_comparison_rows((reference,), (selected,))
            calculate_hashes(source_rows)
            rows = match_comparison_rows(source_rows)
            self.assertEqual(rows[0].status(), "same_content")
            self.assertEqual(len(rows[0].reference.digest), 64)
            self.assertEqual(rows[0].matched_hash_algorithm, "sha256")

    def test_matching_pair_can_be_verified_byte_by_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.bin"
            selected = root / "selected.bin"
            reference.write_bytes(b"same contents")
            selected.write_bytes(b"same contents")
            source_rows = create_comparison_rows((reference,), (selected,))
            calculate_hashes(source_rows)
            rows = match_comparison_rows(source_rows)

            self.assertEqual(compare_matching_pairs_byte_by_byte(rows), 1)
            self.assertEqual(rows[0].status(), "byte_identical")

            selected.write_bytes(b"same contenTs")
            self.assertEqual(compare_matching_pairs_byte_by_byte(rows), 1)
            self.assertEqual(rows[0].status(), "byte_different")

    def test_all_matching_attributes_produce_identical_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first" / "same.bin"
            second = root / "second" / "same.bin"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            timestamp = 1_700_000_000
            os.utime(first, (timestamp, timestamp))
            os.utime(second, (timestamp, timestamp))
            source_rows = create_comparison_rows((first,), (second,))
            calculate_hashes(source_rows)
            row = match_comparison_rows(source_rows)[0]

            self.assertEqual(row.status(), "identical")
            self.assertEqual(
                row.matching_attributes(),
                {
                    "hash": True,
                    "size": True,
                    "modified": True,
                    "name": True,
                },
            )

    def test_provider_hash_avoids_reading_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory) / "cloud"
            first = mount / "first" / "same.bin"
            second = mount / "second" / "same.bin"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            timestamp = 1_700_000_000
            os.utime(first, (timestamp, timestamp))
            os.utime(second, (timestamp, timestamp))
            source_rows = create_comparison_rows((first,), (second,))
            profiles = {
                "cloud": {
                    "label": "OneDrive",
                    "mount_dir": str(mount),
                    "remote_spec": "onedrive:root",
                }
            }

            def fake_rc(_profile_id, method, params=None, **_kwargs):
                if method == "vfs/stats":
                    return {
                        "diskCache": {
                            "uploadsInProgress": 0,
                            "uploadsQueued": 0,
                        }
                    }
                if method == "operations/fsinfo":
                    return {
                        "Features": {"SlowHash": False},
                        "Hashes": ["QuickXorHash"],
                        "Precision": 1_000_000_000,
                    }
                if method == "operations/stat":
                    self.assertEqual(params["fs"], "onedrive:root")
                    return {
                        "item": {
                            "Size": 4,
                            "Hashes": {"QuickXorHash": "abcdef"},
                        }
                    }
                self.fail(f"Unexpected RC method: {method}")

            with patch.object(
                Path,
                "open",
                side_effect=AssertionError("content should not be read"),
            ):
                rows = compare_comparison_rows(
                    source_rows,
                    profiles=profiles,
                    rc_caller=fake_rc,
                )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].matched_hash_algorithm, "quickxor")
            self.assertEqual(rows[0].status(), "identical")
            self.assertEqual(
                rows[0].reference.hash_sources["quickxor"],
                "remote:OneDrive",
            )

    def test_slow_provider_hash_uses_local_sha256_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory) / "sftp"
            first = mount / "first.bin"
            second = mount / "second.bin"
            mount.mkdir()
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            source_rows = create_comparison_rows((first,), (second,))
            profiles = {
                "sftp": {
                    "mount_dir": str(mount),
                    "remote_spec": "sftp:root",
                }
            }

            def fake_rc(_profile_id, method, _params=None, **_kwargs):
                if method == "vfs/stats":
                    return {"diskCache": {}}
                if method == "operations/fsinfo":
                    return {
                        "Features": {"SlowHash": True},
                        "Hashes": ["MD5", "SHA-1"],
                    }
                self.fail("Slow provider hashes must not be requested")

            rows = compare_comparison_rows(
                source_rows,
                profiles=profiles,
                rc_caller=fake_rc,
            )

            self.assertEqual(rows[0].matched_hash_algorithm, "sha256")
            self.assertEqual(rows[0].reference.hash_sources["sha256"], "local")

    def test_pending_upload_does_not_use_stale_provider_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory) / "cloud"
            mount.mkdir()
            first = mount / "first.bin"
            second = mount / "second.bin"
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            source_rows = create_comparison_rows((first,), (second,))
            profiles = {
                "cloud": {
                    "mount_dir": str(mount),
                    "remote_spec": "cloud:",
                }
            }

            def fake_rc(_profile_id, method, _params=None, **_kwargs):
                self.assertEqual(method, "vfs/stats")
                return {
                    "diskCache": {
                        "uploadsInProgress": 1,
                        "uploadsQueued": 0,
                    }
                }

            rows = compare_comparison_rows(
                source_rows,
                profiles=profiles,
                rc_caller=fake_rc,
            )

            self.assertEqual(rows[0].matched_hash_algorithm, "sha256")
            self.assertEqual(rows[0].reference.hash_sources["sha256"], "local")

    def test_different_provider_algorithms_fall_back_to_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drive = root / "drive"
            onedrive = root / "onedrive"
            drive.mkdir()
            onedrive.mkdir()
            first = drive / "same.bin"
            second = onedrive / "same.bin"
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            source_rows = create_comparison_rows((first,), (second,))
            profiles = {
                "drive": {
                    "label": "Drive",
                    "mount_dir": str(drive),
                    "remote_spec": "drive:",
                },
                "onedrive": {
                    "label": "OneDrive",
                    "mount_dir": str(onedrive),
                    "remote_spec": "onedrive:",
                },
            }

            def fake_rc(profile_id, method, _params=None, **_kwargs):
                if method == "vfs/stats":
                    return {"diskCache": {}}
                if method == "operations/fsinfo":
                    algorithm = "SHA-256" if profile_id == "drive" else "QuickXorHash"
                    return {
                        "Features": {"SlowHash": False},
                        "Hashes": [algorithm],
                        "Precision": 1,
                    }
                if method == "operations/stat":
                    if profile_id == "drive":
                        digest = calculate_sha256(first)
                        hashes = {"SHA-256": digest}
                    else:
                        hashes = {"QuickXorHash": "abcdef"}
                    return {"item": {"Size": 4, "Hashes": hashes}}
                self.fail(f"Unexpected RC method: {method}")

            def calculate_sha256(path: Path) -> str:
                import hashlib

                return hashlib.sha256(path.read_bytes()).hexdigest()

            rows = compare_comparison_rows(
                source_rows,
                profiles=profiles,
                rc_caller=fake_rc,
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].matched_hash_algorithm, "sha256")
            self.assertEqual(
                rows[0].reference.hash_sources["sha256"],
                "remote:Drive",
            )
            self.assertEqual(rows[0].selected.hash_sources["sha256"], "local")

    def test_modification_time_uses_backend_precision(self) -> None:
        reference = FileDetails(
            "/tmp/a",
            modified=datetime.fromtimestamp(1_700_000_000.1),
            modified_precision_ns=1_000_000_000,
        )
        selected = FileDetails(
            "/tmp/b",
            modified=datetime.fromtimestamp(1_700_000_000.8),
        )
        self.assertTrue(modification_times_match(reference, selected))
        self.assertEqual(hash_algorithm_label("QuickXorHash"), "QUICKXOR")
        self.assertEqual(hash_algorithm_label("DropboxHash"), "DROPBOX")

    def test_directories_are_compared_by_relative_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference"
            selected = root / "selected"
            (reference / "nested").mkdir(parents=True)
            (selected / "nested").mkdir(parents=True)
            (reference / "nested" / "same.txt").write_text(
                "same",
                encoding="utf-8",
            )
            (selected / "nested" / "same.txt").write_text(
                "same",
                encoding="utf-8",
            )
            (reference / "changed.bin").write_bytes(b"AAAA")
            (selected / "changed.bin").write_bytes(b"BBBB")
            (reference / "only-reference.txt").write_text(
                "reference",
                encoding="utf-8",
            )
            (selected / "only-selected.txt").write_text(
                "selected",
                encoding="utf-8",
            )
            timestamp = 1_700_000_000
            for path in (
                reference / "nested" / "same.txt",
                selected / "nested" / "same.txt",
                reference / "changed.bin",
                selected / "changed.bin",
            ):
                os.utime(path, (timestamp, timestamp))

            self.assertTrue(is_directory_comparison((reference,), (selected,)))
            source_rows = create_comparison_rows((reference,), (selected,))
            rows = compare_comparison_rows(
                source_rows,
                profiles={},
                match_relative_paths=True,
            )
            paired = [
                row for row in rows
                if row.reference is not None and row.selected is not None
            ]

            self.assertEqual([row.key for row in paired], [
                "changed.bin",
                "nested/same.txt",
            ])
            self.assertEqual(paired[0].status(), "different")
            self.assertEqual(paired[1].status(), "identical")
            self.assertEqual(
                [
                    row.reference.relative_path
                    for row in rows
                    if row.reference is not None and row.selected is None
                ],
                ["only-reference.txt"],
            )
            self.assertEqual(
                [
                    row.selected.relative_path
                    for row in rows
                    if row.reference is None and row.selected is not None
                ],
                ["only-selected.txt"],
            )

    def test_file_selection_is_not_directory_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            file = root / "file.txt"
            folder.mkdir()
            file.touch()
            self.assertFalse(is_directory_comparison((folder,), (file,)))

    def test_pool_compares_folder_files_with_individual_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()
            nested = folder / "nested.txt"
            separate = root / "separate.txt"
            nested.write_text("same", encoding="utf-8")
            separate.write_text("same", encoding="utf-8")

            source_rows = create_pool_rows((folder, nested, separate))
            self.assertEqual(len(source_rows), 2)
            result = compare_pool_rows(
                source_rows,
                (folder, nested, separate),
                profiles={},
            )

            matching = [
                row for row in result.rows
                if row.reference is not None and row.selected is not None
            ]
            self.assertEqual(result.file_count, 2)
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].matched_hash_algorithm, "sha256")

    def test_pool_builds_collapsible_identical_folder_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            (first / "nested").mkdir(parents=True)
            (second / "nested").mkdir(parents=True)
            (first / "nested" / "same.txt").write_text(
                "same",
                encoding="utf-8",
            )
            (second / "nested" / "same.txt").write_text(
                "same",
                encoding="utf-8",
            )

            result = compare_pool_rows(
                create_pool_rows((first, second)),
                (first, second),
                profiles={},
            )

            self.assertEqual(len(result.folders), 1)
            self.assertTrue(result.folders[0].identical)
            self.assertEqual(result.folders[0].matching_count, 1)
            self.assertEqual(result.folders[0].rows[0].key, "nested/same.txt")

    def test_folder_summary_exposes_the_same_tags_as_file_pairs(self) -> None:
        modified = datetime.fromtimestamp(1_700_000_000)
        first = FileDetails("/tmp/first/a", size=4, modified=modified)
        second = FileDetails("/tmp/second/a", size=4, modified=modified)
        first.add_hash("sha256", "abcd", "local")
        second.add_hash("sha256", "abcd", "local")
        row = ComparisonRow(
            "a",
            first,
            second,
            matched_hash_algorithm="sha256",
            byte_match=True,
        )
        folder = FolderComparison(
            "/tmp/first",
            "/tmp/second",
            [row],
            4,
            4,
        )

        self.assertEqual(
            folder.matching_attributes(),
            {
                "hash": True,
                "size": True,
                "modified": True,
                "byte": True,
            },
        )
        self.assertEqual(folder.matched_hash_algorithms(), ("sha256",))


if __name__ == "__main__":
    unittest.main()
