"""Exercise the supported rclone RC schema using disposable local files."""
from __future__ import annotations

import http.client
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from talaryn.transfer_safety import probe_transfer_state


@unittest.skipUnless(shutil.which('rclone'), 'rclone is not installed')
class LocalTransferSafetyTests(unittest.TestCase):
    def test_local_vfs_pending_upload_is_busy(self):
        with tempfile.TemporaryDirectory(prefix='talaryn-safety-') as directory:
            root = Path(directory)
            (root / 'files').mkdir()
            sock = root / 'rc.sock'
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            process = subprocess.Popen([
                'rclone', 'serve', 'webdav', str(root / 'files'),
                '--addr', f'127.0.0.1:{port}', '--rc',
                '--rc-addr', f'unix://{sock}', '--rc-no-auth',
                '--vfs-cache-mode', 'full', '--vfs-write-back', '1h',
                '--cache-dir', str(root / 'cache'), '--config', '/dev/null',
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                with patch('talaryn.rc.rc_socket_for', return_value=sock):
                    while time.monotonic() < deadline:
                        if probe_transfer_state('test') == 'idle':
                            break
                        if process.poll() is not None:
                            self.fail(process.stderr.read())
                        time.sleep(.05)
                    self.assertEqual(probe_transfer_state('test'), 'idle')
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                    try:
                        connection.request('PUT', '/.goutputstream-test', body=b'pending content')
                        response = connection.getresponse()
                        response.read()
                        self.assertIn(response.status, (200, 201, 204))
                    finally:
                        connection.close()
                    self.assertEqual(probe_transfer_state('test'), 'busy')
                    self.assertFalse((root / 'files/.goutputstream-test').exists())
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
