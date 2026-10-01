from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from talaryn.activity_filter import (
    DEFAULT_HIDDEN_ACTIVITY_PATTERNS,
    activity_is_visible,
    normalize_activity_patterns,
)
from talaryn.activity_grouping import activity_group_key
from talaryn.activity_store import ActivityStore
from talaryn.config import load_settings, save_settings


class ActivityFilterTests(unittest.TestCase):
    def test_defaults_match_basenames_in_any_folder(self):
        for name in ('sequence.dna.sglock', '.~lock.report.odt#', '.DS_Store',
                     '.notes.swp', '.notes.swo', '.notes.swn'):
            with self.subTest(name=name):
                self.assertFalse(activity_is_visible(
                    {'path': f'folder with spaces/nested/{name}'},
                    DEFAULT_HIDDEN_ACTIVITY_PATTERNS,
                ))
        for name in ('sequence.dna', 'report.odt', 'Cargo.lock', 'data.tmp',
                     'data.bak', 'notes~', '.gitignore', 'x.SGLOCK'):
            self.assertTrue(activity_is_visible(
                {'path': name}, DEFAULT_HIDDEN_ACTIVITY_PATTERNS,
            ))

    def test_errors_and_retries_with_errors_remain_visible(self):
        for fields in ({'state': 'error'}, {'state': 'retrying', 'error': 'Failed'}):
            self.assertTrue(activity_is_visible(
                {'path': 'x.sglock', **fields}, DEFAULT_HIDDEN_ACTIVITY_PATTERNS,
            ))
        self.assertTrue(activity_is_visible({'path': 'x.sglock'}, ()))

    def test_matching_folders_hide_all_descendants(self):
        for path in (
            '.sglock', '.sglock/', 'work/.sglock/',
            'work/.sglock/owner', 'work/.sglock/nested/session',
            'work/sequence.dna.sglock/owner',
        ):
            with self.subTest(path=path):
                self.assertFalse(activity_is_visible(
                    {'path': path}, DEFAULT_HIDDEN_ACTIVITY_PATTERNS,
                ))
                self.assertTrue(activity_is_visible({'path': path}, ()))
        for path in ('work/sequence.dna', 'work/.sglock-backup/owner',
                     'work/.sglock.txt', 'work/project.sglock2/owner'):
            self.assertTrue(activity_is_visible(
                {'path': path}, DEFAULT_HIDDEN_ACTIVITY_PATTERNS,
            ))
        for fields in ({'state': 'error'}, {'state': 'retrying', 'error': 'Failed'}):
            self.assertTrue(activity_is_visible(
                {'path': 'work/.sglock/nested/owner', **fields},
                DEFAULT_HIDDEN_ACTIVITY_PATTERNS,
            ))

    def test_patterns_preserve_hash_and_remove_duplicates(self):
        self.assertEqual(normalize_activity_patterns(
            '  .~lock.*# \n\n*.sglock\n*.sglock\n#literal\n'
        ), ['.~lock.*#', '*.sglock', '#literal'])
        self.assertEqual(normalize_activity_patterns(''), [])
        self.assertEqual(normalize_activity_patterns([]), [])

    def test_existing_settings_get_defaults_and_empty_list_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps({'language': 'pl'}))
            with (
                patch('talaryn.config.SETTINGS_FILE', path),
                patch('talaryn.config.ensure_runtime_dirs'),
                patch('talaryn.config.set_tray_autostart_enabled'),
            ):
                settings = load_settings()
                self.assertIn('.~lock.*#', settings['activity_hidden_patterns'])
                settings['activity_hidden_patterns'] = []
                save_settings(settings)
                self.assertEqual(load_settings()['activity_hidden_patterns'], [])
                self.assertEqual(load_settings()['language'], 'pl')
                settings['activity_hidden_patterns'] = ['*.sglock', '.~lock.*#']
                save_settings(settings)
                self.assertEqual(load_settings()['activity_hidden_patterns'],
                                 ['*.sglock', '.~lock.*#'])

    def test_history_filters_before_limits_and_preserves_complete_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / 'activity.db')
            now = int(time.time() // 300) * 300 + 1

            def event(name, path, stamp, **fields):
                return {'event_id': name, 'profile_id': 'p', 'path': path,
                        'timestamp': stamp, 'operation': 'copy',
                        'state': 'completed', 'size': 10, **fields}

            document = event('document', 'work/sequence.dna', now, size=100)
            other = event('other', 'work/report.odt', now + 1, size=200)
            error = event('error', 'errors/x.sglock', now + 200,
                          state='error', error='Upload failed')
            hidden = [event(f'h{i}', f'work/{i}.sglock', now + i + 2)
                      for i in range(150)]
            hidden += [event('lock', 'only-locks/.~lock.report.odt#', now + 250)]
            store.save_events([document, other, error, *hidden])
            patterns = DEFAULT_HIDDEN_ACTIVITY_PATTERNS
            self.assertEqual(store.activity_count(hidden_patterns=patterns), 3)
            self.assertEqual([e['event_id'] for e in store.recent(
                2, hidden_patterns=patterns)], ['error', 'other'])
            self.assertEqual([e['event_id'] for e in store.recent(
                2, offset=2, hidden_patterns=patterns)], ['document'])
            self.assertEqual(store.recent(10, profile_id='other',
                                          hidden_patterns=patterns), [])
            self.assertEqual(store.recent(10, operation='delete',
                                          hidden_patterns=patterns), [])
            key = activity_group_key(document)
            self.assertEqual([e['event_id'] for e in store.group_events(
                key, 2, hidden_patterns=patterns)], ['other', 'document'])
            headers = store.recent_group_headers(2, hidden_patterns=patterns)
            self.assertEqual(len(headers), 2)
            self.assertEqual({h['representative']['event_id'] for h in headers},
                             {'error', 'other'})
            summary = store.group_summaries([key], hidden_patterns=patterns)[key]
            self.assertEqual(summary['item_count'], 2)
            self.assertEqual(summary['total_size'], 300)
            self.assertEqual(summary['transferred_size'], 300)
            error_headers = store.recent_group_headers(
                10, state='error', hidden_patterns=patterns,
            )
            self.assertEqual(error_headers[0]['error_count'], 1)
            # Revealing entries never requires a rescan or rewriting the database.
            self.assertEqual(store.activity_count(), 154)
            self.assertEqual(len(store.recent_group_headers(10)), 3)
            self.assertEqual(store.summary()['day']['completed'], 153)
            self.assertEqual(store.summary()['day']['errors'], 1)


if __name__ == '__main__':
    unittest.main()
