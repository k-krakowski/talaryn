from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from talaryn.app import (
    ActivityRow,
    CACHE_AGE_PRESETS,
    CACHE_SIZE_PRESETS,
    PRESETS,
    RemoteConfigDialog,
    RemoteManagerDialog,
    MainWindow,
    SPONSOR_INITIAL_DELAY_SECONDS,
    SPONSOR_URL,
    _config_question_choices,
    _activity_icon,
    _duration_seconds,
    _escape_pango_markup,
    _normalize_text_search_result,
    _parsed_http_url,
    _speed_limit_bytes,
    _sponsor_reminder_due,
    _sponsor_reminder_due_at,
    _stepped_profile_value,
    _stepped_speed_limit,
    _webdav_credentials_are_transport_safe,
    create_application_about_dialog,
)
from talaryn.icons import icon_path


class AppHelperTests(unittest.TestCase):
    def test_sponsor_reminder_becomes_due_after_two_weeks(self) -> None:
        state = {"sponsor_first_used_at": 100.0}
        due_at = 100.0 + SPONSOR_INITIAL_DELAY_SECONDS

        self.assertEqual(_sponsor_reminder_due_at(state), due_at)
        self.assertFalse(_sponsor_reminder_due(state, due_at - 1))
        self.assertTrue(_sponsor_reminder_due(state, due_at))
        self.assertFalse(
            _sponsor_reminder_due(
                {**state, "sponsor_reminder_disabled": True},
                due_at,
            )
        )

    def test_explicit_sponsor_reminder_date_overrides_first_use(self) -> None:
        state = {
            "sponsor_first_used_at": 100.0,
            "sponsor_remind_after": 500.0,
        }

        self.assertEqual(_sponsor_reminder_due_at(state), 500.0)
        self.assertFalse(_sponsor_reminder_due(state, 499.0))
        self.assertTrue(_sponsor_reminder_due(state, 500.0))

    def test_profile_presets_explain_their_tradeoffs(self) -> None:
        for preset_id, preset in PRESETS.items():
            with self.subTest(preset_id=preset_id):
                self.assertTrue(preset.get("label_key"))
                self.assertTrue(preset.get("description_key"))
                self.assertIn(
                    preset.get("vfs_cache_mode"),
                    {"off", "minimal", "writes", "full"},
                )
                self.assertIsNotNone(
                    _duration_seconds(str(preset.get("dir_cache_time", "")))
                )

        self.assertEqual(
            PRESETS["ssh_development"]["dir_cache_time"],
            "5m",
        )
        self.assertEqual(PRESETS["large_files"]["dir_cache_time"], "1h")
        self.assertEqual(
            PRESETS["many_small_files"]["dir_cache_time"],
            "6h",
        )
        self.assertIn("sftp_no_hashcheck", PRESETS)

    def test_activity_row_signature_skips_irrelevant_refreshes(self) -> None:
        item = {
            "state": "uploading",
            "operation": "copy",
            "path": "folder/file.bin",
            "progress": 25,
            "queue_id": 1,
            "timestamp": 100,
        }

        signature = ActivityRow._item_update_signature(item)

        self.assertEqual(
            signature,
            ActivityRow._item_update_signature(
                {**item, "queue_id": 2, "timestamp": 101}
            ),
        )
        self.assertNotEqual(
            signature,
            ActivityRow._item_update_signature({**item, "progress": 26}),
        )

    def test_activity_thumbnail_does_not_stat_mount_on_main_thread(self) -> None:
        window = Mock()
        window.thumbnail_factory = None
        window._cached_thumbnail_for_uri.return_value = None
        image = Mock()
        file = Mock()
        file.get_uri.return_value = "file:///mnt/cloud/file.jpg"
        with (
            patch("talaryn.app.Gio.File.new_for_path", return_value=file),
            patch("talaryn.app.Path.stat") as path_stat,
        ):
            MainWindow.load_activity_thumbnail(
                window,
                image,
                {"local_path": "/mnt/cloud/file.jpg"},
            )

        path_stat.assert_not_called()

    def test_about_dialog_acknowledges_russian_translation_editor(self) -> None:
        dialog = Mock()
        with (
            patch(
                "talaryn.app.Adw.AboutDialog",
                return_value=dialog,
            ),
            patch(
                "talaryn.app.tr",
                side_effect=lambda key, **_kwargs: key,
            ),
            patch(
                "talaryn.app._attach_sponsor_card",
                return_value=True,
            ) as attach_sponsor_card,
        ):
            result = create_application_about_dialog("1.74.4")

        self.assertIs(result, dialog)
        dialog.add_acknowledgement_section.assert_called_once_with(
            "about_acknowledgements",
            ["about_russian_translation_credit"],
        )
        attach_sponsor_card.assert_called_once_with(dialog)
        dialog.add_link.assert_not_called()

    def test_about_dialog_keeps_sponsor_fallback_for_new_adwaita(self) -> None:
        dialog = Mock()
        with (
            patch(
                "talaryn.app.Adw.AboutDialog",
                return_value=dialog,
            ),
            patch(
                "talaryn.app.tr",
                side_effect=lambda key, **_kwargs: key,
            ),
            patch(
                "talaryn.app._attach_sponsor_card",
                return_value=False,
            ),
        ):
            create_application_about_dialog("1.74.4")

        dialog.add_link.assert_called_once_with(
            "about_support_creator",
            SPONSOR_URL,
        )

    def test_sponsor_heart_icon_is_packaged(self) -> None:
        self.assertIsNotNone(icon_path("support-heart.svg"))

    def test_first_run_schedules_sponsor_reminder_in_two_weeks(self) -> None:
        window = Mock()
        window._sponsor_reminder_source = 0
        window._sponsor_reminder_open = False
        window._sponsor_reminder_shown = False
        with (
            patch("talaryn.app.load_state", return_value={}),
            patch("talaryn.app.time.time", return_value=100.0),
            patch(
                "talaryn.app.update_sponsor_reminder_state"
            ) as update_state,
            patch(
                "talaryn.app.GLib.timeout_add_seconds",
                return_value=77,
            ) as timeout_add,
        ):
            MainWindow.schedule_sponsor_reminder(window)

        update_state.assert_called_once_with(
            first_used_at=100.0,
            remind_after=100.0 + SPONSOR_INITIAL_DELAY_SECONDS,
            disabled=False,
        )
        timeout_add.assert_called_once_with(
            SPONSOR_INITIAL_DELAY_SECONDS,
            window._show_sponsor_reminder,
        )
        self.assertEqual(window._sponsor_reminder_source, 77)

    def test_never_show_choice_permanently_disables_reminder(self) -> None:
        window = Mock()
        window._sponsor_reminder_open = True
        checkbox = Mock()
        checkbox.get_active.return_value = True
        with (
            patch(
                "talaryn.app.update_sponsor_reminder_state"
            ) as update_state,
            patch("talaryn.app._open_sponsor_page") as open_page,
        ):
            MainWindow._on_sponsor_reminder_response(
                window,
                Mock(),
                "close",
                checkbox,
            )

        update_state.assert_called_once_with(disabled=True)
        open_page.assert_not_called()
        self.assertFalse(window._sponsor_reminder_open)

    def test_never_show_checkbox_disables_remind_later(self) -> None:
        checkbox = Mock()
        checkbox.get_active.return_value = True
        dialog = Mock()

        MainWindow._on_sponsor_never_show_toggled(checkbox, dialog)

        dialog.set_response_enabled.assert_called_once_with("later", False)

    def test_reminder_sponsor_button_opens_link_and_closes_dialog(self) -> None:
        dialog = Mock()
        with (
            patch("talaryn.app.time.time", return_value=100.0),
            patch(
                "talaryn.app.update_sponsor_reminder_state"
            ) as update_state,
            patch("talaryn.app._open_sponsor_page") as open_page,
        ):
            MainWindow._on_sponsor_reminder_button_clicked(
                Mock(),
                Mock(),
                dialog,
            )

        update_state.assert_called_once()
        open_page.assert_called_once_with(None)
        dialog.close.assert_called_once_with()

    def test_close_confirmation_is_shown_for_syncing_activity(self) -> None:
        window = Mock()
        window.settings = {"confirm_close_during_sync": True}
        window._close_confirmation_open = False
        window._sync_activity_for_close.return_value = {
            "is_syncing": True,
            "total_active_count": 1,
            "queued_count": 2,
        }
        dialog = Mock()

        with patch(
            "talaryn.app.Adw.AlertDialog.new",
            return_value=dialog,
        ):
            shown = MainWindow.confirm_close_if_needed(window, Mock())

        self.assertTrue(shown)
        self.assertTrue(window._close_confirmation_open)
        dialog.present.assert_called_once_with(window)

    def test_close_confirmation_is_skipped_without_syncing_activity(self) -> None:
        window = Mock()
        window.settings = {"confirm_close_during_sync": True}
        window._close_confirmation_open = False
        window._sync_activity_for_close.return_value = {"is_syncing": False}

        shown = MainWindow.confirm_close_if_needed(window, Mock())

        self.assertFalse(shown)

    def test_text_search_accepts_match_and_handles_no_result(self) -> None:
        self.assertIsNone(_normalize_text_search_result(None))
        self.assertEqual(
            _normalize_text_search_result(("start", "end")),
            ("start", "end"),
        )

    def test_rclone_help_text_is_safe_for_pango_markup(self) -> None:
        help_text = (
            'Drive OK?\n\nFound drive "root" of type "personal"\n'
            "URL: https://onedrive.live.com?cid=abc&id=def"
        )

        escaped = _escape_pango_markup(help_text)

        self.assertIn("?cid=abc&amp;id=def", escaped)
        self.assertNotIn("?cid=abc&id=def", escaped)

    def test_webdav_credentials_require_https_or_loopback(self) -> None:
        self.assertTrue(
            _webdav_credentials_are_transport_safe(
                "https://cloud.example.test/remote.php/dav"
            )
        )
        self.assertTrue(
            _webdav_credentials_are_transport_safe("http://localhost:8080")
        )
        self.assertTrue(
            _webdav_credentials_are_transport_safe("http://[::1]:8080")
        )
        self.assertFalse(
            _webdav_credentials_are_transport_safe(
                "http://cloud.example.test/remote.php/dav"
            )
        )
        self.assertIsNone(_parsed_http_url("https://"))

    def test_speed_limit_buttons_follow_presets(self) -> None:
        self.assertEqual(_stepped_speed_limit("10M", -1), "5M")
        self.assertEqual(_stepped_speed_limit("10M", 1), "20M")
        self.assertEqual(_stepped_speed_limit("12M", -1), "10M")
        self.assertEqual(_stepped_speed_limit("12M", 1), "20M")

    def test_speed_limit_buttons_treat_empty_value_as_unlimited(self) -> None:
        self.assertEqual(_stepped_speed_limit("", 1), "")
        self.assertEqual(_stepped_speed_limit("", -1), "10G")
        self.assertEqual(_stepped_speed_limit("10G", 1), "")

    def test_speed_limit_buttons_preserve_invalid_manual_value(self) -> None:
        self.assertEqual(_stepped_speed_limit("invalid", 1), "invalid")

    def test_cache_size_buttons_follow_presets(self) -> None:
        self.assertEqual(
            _stepped_profile_value(
                "10G",
                -1,
                CACHE_SIZE_PRESETS,
                _speed_limit_bytes,
            ),
            "5G",
        )
        self.assertEqual(
            _stepped_profile_value(
                "12G",
                1,
                CACHE_SIZE_PRESETS,
                _speed_limit_bytes,
            ),
            "20G",
        )

    def test_cache_age_buttons_support_custom_values(self) -> None:
        self.assertEqual(_duration_seconds("1.5h"), 5400)
        self.assertEqual(
            _stepped_profile_value(
                "25h",
                1,
                CACHE_AGE_PRESETS,
                _duration_seconds,
            ),
            "48h",
        )
        self.assertEqual(
            _stepped_profile_value(
                "custom",
                1,
                CACHE_AGE_PRESETS,
                _duration_seconds,
            ),
            "custom",
        )

    def test_rclone_drive_choices_keep_exact_drive_ids(self) -> None:
        self.assertEqual(
            _config_question_choices(
                [
                    {
                        "Value": "b!exact-drive-id",
                        "Help": "OneDrive (business)",
                    }
                ]
            ),
            [("OneDrive (business)", "b!exact-drive-id")],
        )

    def test_first_stage_remote_providers_are_available(self) -> None:
        self.assertTrue(
            {
                "dropbox",
                "pcloud",
                "box",
                "webdav",
                "smb",
                "b2",
                "s3",
            }.issubset(RemoteConfigDialog.PROVIDER_IDS)
        )
        self.assertEqual(
            RemoteManagerDialog.EDITABLE_TYPES,
            set(RemoteConfigDialog.PROVIDER_IDS),
        )

    def test_new_icon_backends_are_available(self) -> None:
        self.assertTrue(
            {
                "protondrive",
                "mega",
                "iclouddrive",
                "seafile",
                "google cloud storage",
                "azureblob",
                "azurefiles",
            }.issubset(RemoteConfigDialog.PROVIDER_IDS)
        )

    def test_remote_provider_grid_covers_every_provider(self) -> None:
        tiles = [
            tile
            for _category, category_tiles
            in RemoteConfigDialog.PROVIDER_TILE_GROUPS
            for tile in category_tiles
        ]
        self.assertEqual(
            {tile[0] for tile in tiles},
            set(RemoteConfigDialog.PROVIDER_IDS),
        )
        self.assertFalse([tile for tile in tiles if not tile[2]])
        self.assertFalse(
            [
                tile[4]
                for tile in tiles
                if tile[0] == "webdav"
                and tile[4] not in RemoteConfigDialog.WEBDAV_VENDOR_IDS
            ]
        )
        self.assertFalse(
            [
                tile[4]
                for tile in tiles
                if tile[0] == "s3"
                and tile[4] not in RemoteConfigDialog.S3_PROVIDER_IDS
            ]
        )

    def test_provider_grid_uses_only_generic_category_artwork(self) -> None:
        groups = dict(RemoteConfigDialog.PROVIDER_TILE_GROUPS)
        cloud_icons = {
            tile[0]: tile[2]
            for tile in groups["remote_drive_type_cloud"]
        }
        self.assertEqual(cloud_icons["dropbox"], "remote-storage.svg")
        self.assertEqual(cloud_icons["seafile"], "remote-storage.svg")
        for provider in (
            "onedrive",
            "drive",
            "pcloud",
            "box",
            "protondrive",
            "mega",
            "iclouddrive",
        ):
            self.assertEqual(cloud_icons[provider], "remote-storage.svg")

        webdav_tiles = groups["remote_drive_type_webdav"]
        webdav_icons = {tile[4]: tile[2] for tile in webdav_tiles}
        self.assertEqual(webdav_icons["fastmail"], "webdav.svg")
        self.assertEqual(webdav_icons["nextcloud"], "webdav.svg")
        self.assertEqual(webdav_icons["owncloud"], "webdav.svg")
        self.assertEqual(webdav_icons["sharepoint"], "webdav.svg")
        self.assertEqual(webdav_icons["other"], "webdav.svg")

        object_tiles = groups["remote_drive_type_object"]
        self.assertTrue(
            all(tile[2] == "object-storage.svg" for tile in object_tiles)
        )

    def test_generic_protocol_tiles_are_last_in_their_groups(self) -> None:
        groups = dict(RemoteConfigDialog.PROVIDER_TILE_GROUPS)
        self.assertEqual(
            groups["remote_drive_type_webdav"][-1][4],
            "other",
        )
        self.assertEqual(
            groups["remote_drive_type_object"][-1][4],
            "Other",
        )

    def test_missing_generic_remote_artwork_uses_symbolic_last_resort(self) -> None:
        image = Mock()
        with (
            patch("talaryn.app.remote_icon_path", return_value=None),
            patch("talaryn.app.Gtk.Image", return_value=image) as image_type,
        ):
            result = _activity_icon("provider-without-licensed-art.svg", 48)

        self.assertIs(result, image)
        image_type.assert_called_once_with(icon_name="folder-remote-symbolic")
        image.set_pixel_size.assert_called_once_with(48)

    def test_generic_webdav_omits_tiled_services(self) -> None:
        self.assertFalse(
            set(RemoteConfigDialog.WEBDAV_GENERIC_VENDOR_IDS)
            & set(RemoteConfigDialog.WEBDAV_TILE_VENDOR_IDS)
        )
        self.assertEqual(
            len(RemoteConfigDialog.WEBDAV_GENERIC_VENDOR_IDS),
            len(RemoteConfigDialog.WEBDAV_GENERIC_VENDOR_LABEL_KEYS),
        )
        self.assertIn(
            "other",
            RemoteConfigDialog.WEBDAV_GENERIC_VENDOR_IDS,
        )

    def test_generic_s3_omits_tiled_services(self) -> None:
        self.assertTrue(
            set(RemoteConfigDialog.S3_TILE_PROVIDER_IDS).issubset(
                RemoteConfigDialog.S3_PROVIDER_IDS
            )
        )
        generic = set(RemoteConfigDialog.S3_PROVIDER_IDS) - set(
            RemoteConfigDialog.S3_TILE_PROVIDER_IDS
        )
        self.assertNotIn("AWS", generic)
        self.assertNotIn("Cloudflare", generic)
        self.assertNotIn("Wasabi", generic)
        self.assertIn("Other", generic)

    def test_cloud_oauth_providers_can_be_reauthorized(self) -> None:
        self.assertEqual(
            set(RemoteConfigDialog.OAUTH_PROVIDER_IDS),
            {
                "onedrive",
                "drive",
                "dropbox",
                "pcloud",
                "box",
                "google cloud storage",
            },
        )

    def test_s3_provider_ids_and_labels_stay_aligned(self) -> None:
        self.assertEqual(
            len(RemoteConfigDialog.S3_PROVIDER_IDS),
            len(RemoteConfigDialog.S3_PROVIDER_LABELS),
        )
        self.assertIn("Cloudflare", RemoteConfigDialog.S3_PROVIDER_IDS)
        self.assertIn("Minio", RemoteConfigDialog.S3_PROVIDER_IDS)
        self.assertEqual(RemoteConfigDialog.S3_PROVIDER_IDS[-1], "Other")


if __name__ == "__main__":
    unittest.main()
