from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gi.repository import Gio

from talaryn.comparison import ComparisonRow, FileDetails, FolderComparison
from talaryn.comparison_dialog import (
    ComparisonApplication,
    ComparisonWindow,
    _comparison_tree,
    _comparison_targets,
    _folder_content_analysis,
    _folder_comparison_groups,
    _folder_group_tree,
    _profile_for_path,
)
from talaryn.paths import COMPARISON_APP_ID


class ComparisonApplicationTests(unittest.TestCase):
    def test_comparison_tree_groups_files_by_directory(self) -> None:
        rows = [
            ComparisonRow(
                "documents/archive/old.txt",
                FileDetails("/first/documents/archive/old.txt", size=3),
                FileDetails("/second/documents/archive/old.txt", size=3),
            ),
            ComparisonRow(
                "documents/current.txt",
                FileDetails("/first/documents/current.txt", size=5),
                None,
            ),
            ComparisonRow(
                "root.txt",
                FileDetails("/first/root.txt", size=7),
                None,
            ),
        ]

        tree = _comparison_tree(rows)

        self.assertEqual(
            [node.name for node in tree.ordered_children()],
            ["documents", "root.txt"],
        )
        documents = tree.children["documents"]
        self.assertEqual(
            [node.name for node in documents.ordered_children()],
            ["archive", "current.txt"],
        )
        self.assertEqual(
            documents.children["archive"].children["old.txt"].row,
            rows[0],
        )

    def test_comparison_tree_aggregates_both_folder_sides(self) -> None:
        rows = [
            ComparisonRow(
                "nested/both.bin",
                FileDetails("/first/nested/both.bin", size=4),
                FileDetails("/second/nested/both.bin", size=6),
            ),
            ComparisonRow(
                "nested/first-only.bin",
                FileDetails("/first/nested/first-only.bin", size=8),
                None,
            ),
        ]

        nested = _comparison_tree(rows).children["nested"]

        self.assertEqual(nested.reference_count, 2)
        self.assertEqual(nested.selected_count, 1)
        self.assertEqual(nested.reference_size, 12)
        self.assertEqual(nested.selected_size, 6)

    def test_three_folder_pairs_are_rendered_as_one_group(self) -> None:
        paths = ("/first", "/second", "/third")
        details = {
            path: FileDetails(f"{path}/nested/file.bin", size=4)
            for path in paths
        }
        for item in details.values():
            item.add_hash("sha256", "abcd", "local")

        def comparison(first: str, second: str) -> FolderComparison:
            return FolderComparison(
                first,
                second,
                [
                    ComparisonRow(
                        "nested/file.bin",
                        details[first],
                        details[second],
                        matched_hash_algorithm="sha256",
                    )
                ],
                4,
                4,
            )

        groups = _folder_comparison_groups(
            (
                comparison(paths[0], paths[1]),
                comparison(paths[0], paths[2]),
                comparison(paths[1], paths[2]),
            ),
            paths,
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].paths, paths)
        self.assertEqual(groups[0].matching_count, 1)
        self.assertTrue(groups[0].identical)
        tree = _folder_group_tree(groups[0])
        nested = tree.children["nested"]
        self.assertEqual(nested.counts, {path: 1 for path in paths})
        self.assertEqual(
            set(nested.children["file.bin"].files),
            set(paths),
        )

    def test_folder_content_matching_ignores_name_and_relative_path(
        self,
    ) -> None:
        first = FileDetails("/first/docs/report.txt", size=4)
        second = FileDetails("/second/archive/copy.bin", size=4)
        first.add_hash("sha256", "abcd", "local")
        second.add_hash("sha256", "abcd", "local")
        comparison = FolderComparison(
            "/first",
            "/second",
            [
                ComparisonRow("docs/report.txt", first, None),
                ComparisonRow("archive/copy.bin", None, second),
            ],
            4,
            4,
        )
        group = _folder_comparison_groups((comparison,))[0]

        analysis = _folder_content_analysis(group)

        self.assertEqual(len(analysis.matches), 1)
        self.assertEqual(
            set(analysis.matches[0].files),
            {"/first", "/second"},
        )
        self.assertEqual(analysis.matches[0].algorithms, ("sha256",))
        self.assertTrue(analysis.same_content)
        self.assertFalse(analysis.conflicts)
        self.assertFalse(analysis.unique)

    def test_folder_content_analysis_separates_conflicts_and_unique_files(
        self,
    ) -> None:
        first = FileDetails("/first/shared.txt", size=4)
        second = FileDetails("/second/shared.txt", size=5)
        unique = FileDetails("/first/only.txt", size=2)
        comparison = FolderComparison(
            "/first",
            "/second",
            [
                ComparisonRow("shared.txt", first, second),
                ComparisonRow("only.txt", unique, None),
            ],
            6,
            5,
        )
        group = _folder_comparison_groups((comparison,))[0]

        analysis = _folder_content_analysis(group)

        self.assertEqual(set(analysis.conflicts), {"shared.txt"})
        self.assertEqual(set(analysis.unique), {"only.txt"})
        self.assertFalse(analysis.same_content)

    def test_remote_path_uses_the_most_specific_mount_profile(self) -> None:
        profiles = {
            "root": {
                "mount_dir": "/cloud",
                "icon": "remote-storage.svg",
            },
            "nested": {
                "mount_dir": "/cloud/work",
                "icon": "sftp.svg",
            },
        }

        self.assertIs(
            _profile_for_path("/cloud/work/project/file", profiles),
            profiles["nested"],
        )
        self.assertIsNone(_profile_for_path("/home/file", profiles))

    def test_unique_file_uses_an_existing_provider_hash(self) -> None:
        details = FileDetails("/tmp/unique.bin")
        details.add_hash("dropbox", "abc123", "remote:dropbox")

        self.assertEqual(
            ComparisonWindow._display_hash(details),
            ("dropbox", "abc123"),
        )

    def test_missing_requested_hash_falls_back_to_available_hash(self) -> None:
        details = FileDetails("/tmp/unique.bin")
        details.add_hash("md5", "abc123", "remote:drive")

        self.assertEqual(
            ComparisonWindow._display_hash(details, "sha256"),
            ("md5", "abc123"),
        )

    def test_unique_file_without_hash_is_marked_for_on_demand_calculation(
        self,
    ) -> None:
        self.assertEqual(
            ComparisonWindow._display_hash(FileDetails("/tmp/unique.bin")),
            ("", ""),
        )

    def test_tag_text_is_limited_to_eight_characters(self) -> None:
        self.assertEqual(ComparisonWindow._short_tag_text("SHA-256"), "SHA-256")
        self.assertEqual(
            ComparisonWindow._short_tag_text("DATA MODYFIKACJI"),
            "DATA MO…",
        )
        self.assertEqual(len(ComparisonWindow._short_tag_text("IDENTYCZNE")), 8)

    def test_comparison_reuses_standard_thumbnail_cache(self) -> None:
        uri = "file:///tmp/example.pdf"
        filename = f"{hashlib.md5(uri.encode('utf-8')).hexdigest()}.png"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thumbnail = root / "thumbnails" / "normal" / filename
            thumbnail.parent.mkdir(parents=True)
            thumbnail.touch()
            with patch(
                "talaryn.comparison_dialog.CACHE_DIR",
                root / "talaryn",
            ):
                self.assertEqual(
                    ComparisonWindow._cached_thumbnail_for_uri(uri),
                    str(thumbnail),
                )

    def test_full_metadata_value_is_copied_to_clipboard(self) -> None:
        with patch(
            "talaryn.comparison_dialog.Gdk.Display.get_default"
        ) as get_display:
            clipboard = get_display.return_value.get_clipboard.return_value
            ComparisonWindow._copy_text(None, "/full/path/to/file")

        clipboard.set_text.assert_called_once_with("/full/path/to/file")

    def test_paths_are_merged_into_one_deduplicated_pool(self) -> None:
        self.assertEqual(
            _comparison_targets(
                ["/tmp/shared", "/tmp/selected", "/tmp/selected"],
                ["/tmp/shared"],
            ),
            ["/tmp/shared", "/tmp/selected"],
        )

    def test_folder_files_are_hidden_from_the_default_flat_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "folder"
            folder.mkdir()
            nested = folder / "nested.txt"
            standalone = Path(directory) / "standalone.txt"
            nested.touch()
            standalone.touch()
            nested_details = FileDetails(
                str(nested),
                source_path=str(folder),
            )
            standalone_details = FileDetails(
                str(standalone),
                source_path=str(standalone),
            )
            nested_row = ComparisonRow("nested.txt", nested_details, None)
            standalone_row = ComparisonRow(
                "standalone.txt",
                standalone_details,
                None,
            )

            self.assertTrue(
                ComparisonWindow._row_is_inside_folders(
                    nested_row,
                    (folder,),
                )
            )
            self.assertFalse(
                ComparisonWindow._row_is_inside_folders(
                    standalone_row,
                    (folder,),
                )
            )
            self.assertEqual(
                ComparisonWindow._details_belonging_to_source(
                    str(folder),
                    (nested_row, standalone_row),
                ),
                [nested_details],
            )

    def test_identical_folders_are_partitioned_into_duplicates(self) -> None:
        first = FileDetails("/tmp/first/file.txt", size=4)
        second = FileDetails("/tmp/second/file.txt", size=4)
        first.add_hash("sha256", "abcd", "local")
        second.add_hash("sha256", "abcd", "local")
        matching_row = ComparisonRow(
            "file.txt",
            first,
            second,
            matched_hash_algorithm="sha256",
        )
        identical = FolderComparison(
            "/tmp/first",
            "/tmp/second",
            [matching_row],
            4,
            4,
        )
        different = FolderComparison(
            "/tmp/first",
            "/tmp/third",
            [ComparisonRow("file.txt", first, None)],
            4,
            0,
        )

        duplicates, remaining = (
            ComparisonWindow._partition_folder_comparisons(
                (different, identical)
            )
        )

        self.assertEqual(duplicates, [identical])
        self.assertEqual(remaining, [different])

    def test_byte_verification_is_reflected_in_folder_tags(self) -> None:
        first = FileDetails("/tmp/first/a", size=4)
        second = FileDetails("/tmp/second/a", size=4)
        for details in (first, second):
            details.add_hash("sha256", "abcd", "local")
        compared = ComparisonRow(
            "a",
            first,
            second,
            matched_hash_algorithm="sha256",
            byte_match=True,
        )
        folder_row = ComparisonRow(
            "a",
            first,
            second,
            matched_hash_algorithm="sha256",
        )
        folder = FolderComparison(
            "/tmp/first",
            "/tmp/second",
            [folder_row],
            4,
            4,
        )

        ComparisonWindow._sync_folder_byte_results((compared,), (folder,))

        self.assertTrue(folder_row.byte_match)
        self.assertTrue(folder.matching_attributes()["byte"])

    def test_duplicate_files_with_the_same_tags_share_one_row(self) -> None:
        files = [
            FileDetails(f"/tmp/{name}.bin", size=4)
            for name in ("first", "second", "third")
        ]
        for details in files:
            details.add_hash("sha256", "abcd", "local")
        rows = [
            ComparisonRow(
                "first.bin",
                files[0],
                details,
                matched_hash_algorithm="sha256",
            )
            for details in files[1:]
        ]

        groups = ComparisonWindow._group_matching_rows(rows)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][1], files)

    def test_application_is_unique_and_reuses_its_window(self) -> None:
        application = ComparisonApplication()
        self.assertEqual(
            application.get_application_id(),
            COMPARISON_APP_ID,
        )
        self.assertTrue(
            application.get_flags()
            & Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        )
        self.assertFalse(
            application.get_flags()
            & Gio.ApplicationFlags.NON_UNIQUE
        )

        with patch(
            "talaryn.comparison_dialog.ComparisonWindow"
        ) as window_factory:
            window = window_factory.return_value
            window.get_application.return_value = application

            application._present_comparison(["/tmp/first"])
            application._present_comparison(["/tmp/second"])

        window_factory.assert_called_once_with(
            application,
            ["/tmp/first"],
        )
        window.set_selected_paths.assert_called_once_with(
            ["/tmp/second"]
        )
        self.assertEqual(window.present.call_count, 2)


if __name__ == "__main__":
    unittest.main()
