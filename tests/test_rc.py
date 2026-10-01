from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
import socketserver
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from talaryn.rc import (
    installed_rclone_version,
    queue_set_expiry,
    rc_call,
    version_is_supported,
)


class RcloneRcTests(unittest.TestCase):
    def test_rc_call_over_unix_socket(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = json.dumps({"path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "rc.sock"
            try:
                server = socketserver.UnixStreamServer(str(socket_path), Handler)
            except PermissionError:
                self.skipTest("Unix sockets are disabled by the test sandbox")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch(
                    "talaryn.rc.rc_socket_for",
                    return_value=socket_path,
                ):
                    result = rc_call("profile", "core/stats")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertEqual(result, {"path": "/core/stats"})

    def test_minimum_version(self) -> None:
        self.assertFalse(version_is_supported((1, 74, 3)))
        self.assertTrue(version_is_supported((1, 74, 4)))
        self.assertTrue(version_is_supported((1, 75, 0)))

    @patch("talaryn.rc.subprocess.run")
    def test_installed_version_parser(self, run: Mock) -> None:
        run.return_value = Mock(
            returncode=0,
            stdout="rclone v1.74.4\n- os/type: linux\n",
        )
        self.assertEqual(installed_rclone_version(), (1, 74, 4))

    @patch("talaryn.rc.rc_call", return_value={})
    def test_queue_priority_uses_documented_large_negative_expiry(
        self,
        call: Mock,
    ) -> None:
        self.assertTrue(queue_set_expiry("profile", 42, -1_000_000_000))
        call.assert_called_once_with(
            "profile",
            "vfs/queue-set-expiry",
            {"id": 42, "expiry": -1_000_000_000},
        )


if __name__ == "__main__":
    unittest.main()
