from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from talaryn.util import application_command, read_tail


class UtilTests(unittest.TestCase):
    def test_application_command_uses_isolated_python(self) -> None:
        command = application_command("monitor")
        self.assertEqual(command[1:3], ["-I", "-c"])
        self.assertNotIn("-m", command)
        self.assertEqual(command[-1], "monitor")

    def test_read_tail_does_not_return_the_whole_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.log"
            path.write_text("a" * 100_000 + "END", encoding="utf-8")
            result = read_tail(path, max_chars=100)

        self.assertLessEqual(len(result), 100)
        self.assertTrue(result.endswith("END"))


if __name__ == "__main__":
    unittest.main()
