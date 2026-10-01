from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NautilusPackagingTests(unittest.TestCase):
    def test_source_and_debian_install_all_shipped_extensions(self):
        expected = {path.name for path in (ROOT / 'data/nautilus').glob('*.py')}
        for script, destination in (('install.sh', 'NAUTILUS_DIR'),
                                    ('packaging/build-deb.sh', 'USR_NAUTILUS')):
            with self.subTest(script=script):
                text = (ROOT / script).read_text()
                installed = set(re.findall(r'"\$' + destination + r'/([^"/]+\.py)"', text))
                self.assertTrue(expected <= installed, expected - installed)

    def test_uninstall_removes_every_shipped_extension(self):
        expected = {path.name for path in (ROOT / 'data/nautilus').glob('*.py')}
        removed = set(re.findall(r'rm -f "\$NAUTILUS_DIR/([^"/]+\.py)"',
                                 (ROOT / 'uninstall.sh').read_text()))
        self.assertTrue(expected <= removed, expected - removed)
