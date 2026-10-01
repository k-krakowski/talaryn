from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from talaryn.activity_filter import DEFAULT_HIDDEN_ACTIVITY_PATTERNS
from talaryn.activity_store import ActivityStore
from talaryn.app import MainWindow


class ActivityWindow:
    """Exercise the real activity pipeline without starting mounts or a monitor."""

    _activity_history_key = staticmethod(MainWindow._activity_history_key)
    _activity_hidden_patterns = MainWindow._activity_hidden_patterns
    _activity_history_filter_key = MainWindow._activity_history_filter_key
    _filtered_activity_events = MainWindow._filtered_activity_events
    _sync_activity_history = MainWindow._sync_activity_history
    _merge_activity_history = MainWindow._merge_activity_history
    _compose_activity_events = MainWindow._compose_activity_events
    _recent_activity_group_headers = MainWindow._recent_activity_group_headers
    _complete_activity_group_summaries = MainWindow._complete_activity_group_summaries
    _load_activity_group_children = MainWindow._load_activity_group_children
    _on_activity_visibility_changed = MainWindow._on_activity_visibility_changed
    _load_more_activity_page = MainWindow._load_more_activity_page

    def __init__(self, store):
        self.settings = {'activity_hidden_patterns': list(DEFAULT_HIDDEN_ACTIVITY_PATTERNS)}
        self.show_hidden_activity = Mock()
        self.show_hidden_activity.get_active.return_value = False
        self._activity_filter_key = Mock(return_value=('', '', ''))
        self._activity_store = store
        self._activity_history_events = []
        self._all_activity_events = []
        self._activity_visible_history_cache = None
        self._activity_history_initialized = False
        self._activity_history_total = 0
        self._activity_history_global_offset = 0
        self._activity_history_profile_totals = {}
        self._activity_group_summary_cache = {}
        self._activity_group_header_cache = {}
        self._activity_group_child_events = {}
        self._activity_group_child_load_pending = set()
        self._activity_history_summary_signature = None
        self._activity_history_recent_group_signatures = {}
        self._activity_profile_summary_cache = {}
        self._update_activity_pagination = Mock()
        self._refresh_sync_activity = Mock()
        self._capture_activity_scroll = Mock()
        self._render_filtered_activity = Mock(return_value=False)


class ActivityVisibilityTests(unittest.TestCase):
    def test_live_events_errors_and_show_hidden(self):
        window = ActivityWindow(Mock())
        events = [
            {'event_id': 'doc', 'path': 'work/sequence.dna', 'state': 'uploading'},
            {'event_id': 'lock', 'path': 'work/sequence.sglock', 'state': 'queued'},
            {'event_id': 'folder', 'path': 'work/.sglock/', 'state': 'queued'},
            {'event_id': 'child', 'path': 'work/.sglock/session', 'state': 'uploading'},
            {'event_id': 'error', 'path': 'work/.~lock.report.odt#', 'state': 'error'},
        ]
        window._all_activity_events = events
        self.assertEqual([e['event_id'] for e in window._filtered_activity_events()],
                         ['doc', 'error'])
        window.show_hidden_activity.get_active.return_value = True
        self.assertEqual(window._filtered_activity_events(), events)
        window.show_hidden_activity.get_active.return_value = False
        window.settings['activity_hidden_patterns'] = []
        self.assertEqual(window._filtered_activity_events(), events)

    def test_history_toggle_reloads_pages_headers_and_children(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / 'activity.db')
            now = int(time.time() // 300) * 300 + 1
            document = {'event_id': 'doc', 'profile_id': 'p',
                        'path': 'work/sequence.dna', 'timestamp': now,
                        'state': 'completed', 'operation': 'copy', 'size': 100}
            events = [document] + [
                {**document, 'event_id': f'lock{i}', 'path': f'work/{i}.sglock',
                 'timestamp': now + i + 1, 'size': 1}
                for i in range(150)
            ]
            store.save_events(events)
            snapshot = {'history_total': store.activity_count(),
                        'recent': store.recent(100), 'active': [], 'queued': []}
            window = ActivityWindow(store)
            window._sync_activity_history(snapshot)
            window._compose_activity_events(snapshot)
            self.assertEqual(window._activity_history_total, 1)
            self.assertEqual(window._activity_history_global_offset, 1)
            self.assertEqual(window._filtered_activity_events(), [document])
            headers = window._recent_activity_group_headers()
            self.assertEqual(headers[0]['item_count'], 1)
            key = headers[0]['group_key']
            filtered_key = window._activity_history_filter_key()
            window._load_activity_group_children(filtered_key, key)
            self.assertEqual(window._activity_group_child_events[(filtered_key, key)],
                             [document])
            window.show_hidden_activity.get_active.return_value = True
            window._on_activity_visibility_changed()
            window._sync_activity_history(snapshot)
            window._compose_activity_events(snapshot)
            self.assertEqual(window._activity_history_total, 151)
            self.assertEqual(window._recent_activity_group_headers()[0]['item_count'], 151)
            self.assertNotEqual(filtered_key, window._activity_history_filter_key())
            window.show_hidden_activity.get_active.return_value = False
            window._on_activity_visibility_changed()
            window._sync_activity_history(snapshot)
            window._compose_activity_events(snapshot)
            self.assertEqual(window._filtered_activity_events(), [document])
            self.assertEqual(store.activity_count(), 151)

    def test_lock_folder_groups_disappear_but_errors_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / 'activity.db')
            now = int(time.time() // 300) * 300 + 1
            paths = ('work/sequence.dna', 'work/.sglock/',
                     'work/.sglock/owner', 'work/.sglock/nested/session',
                     'work/sequence.dna.sglock/owner', 'work/.sglock/failed')
            events = [
                {'event_id': str(i), 'profile_id': 'p', 'path': path,
                 'timestamp': now + i, 'operation': 'copy', 'size': 10,
                 'state': 'error' if i == 5 else 'completed',
                 'error': 'Upload failed' if i == 5 else ''}
                for i, path in enumerate(paths)
            ]
            store.save_events(events)
            snapshot = {'history_total': store.activity_count(),
                        'recent': store.recent(100), 'active': [], 'queued': []}
            window = ActivityWindow(store)
            window._sync_activity_history(snapshot)
            window._compose_activity_events(snapshot)
            self.assertEqual(window._filtered_activity_events(), [events[5], events[0]])
            self.assertEqual(window._activity_history_total, 2)
            headers = window._recent_activity_group_headers()
            self.assertEqual(len(headers), 2)
            self.assertEqual(sum(h['item_count'] for h in headers), 2)
            self.assertEqual(sum(h['error_count'] for h in headers), 1)
            for header in headers:
                filter_key = window._activity_history_filter_key()
                key = header['group_key']
                window._load_activity_group_children(filter_key, key)
                children = window._activity_group_child_events[(filter_key, key)]
                self.assertEqual(children, [header['representative']])
            window.show_hidden_activity.get_active.return_value = True
            window._on_activity_visibility_changed()
            window._sync_activity_history(snapshot)
            window._compose_activity_events(snapshot)
            self.assertEqual(len(window._filtered_activity_events()), 6)
            self.assertEqual(sum(h['item_count'] for h in
                                 window._recent_activity_group_headers()), 6)
            self.assertEqual(store.activity_count(), 6)

    def test_pending_page_from_previous_rules_is_discarded(self):
        window = ActivityWindow(Mock())
        window._activity_page_patterns = ()
        window._activity_page_load_pending = True
        window._activity_page_profile_id = 'p'
        self.assertFalse(window._load_more_activity_page())
        self.assertFalse(window._activity_page_load_pending)
        window._activity_store.recent.assert_not_called()


if __name__ == '__main__':
    unittest.main()
