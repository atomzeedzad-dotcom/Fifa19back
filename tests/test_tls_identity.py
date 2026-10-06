from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.tls_identity import ensure_identity


class TLSIdentityTests(unittest.TestCase):
    def test_per_machine_keys_are_distinct_and_reused(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            cert1, key1 = ensure_identity(Path(first))
            cert2, key2 = ensure_identity(Path(second))
            initial_cert, initial_key = cert1.read_bytes(), key1.read_bytes()
            self.assertNotEqual(initial_key, key2.read_bytes())
            self.assertNotEqual(initial_cert, cert2.read_bytes())
            self.assertEqual(ensure_identity(Path(first)), (cert1, key1))
            self.assertEqual(initial_key, key1.read_bytes())
            self.assertEqual(initial_cert, cert1.read_bytes())

    def test_broken_generated_pair_can_be_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = ensure_identity(Path(directory))
            key.write_text('interrupted write', encoding='utf-8')
            ensure_identity(Path(directory))
            self.assertIn(b'BEGIN PRIVATE KEY', key.read_bytes())


if __name__ == '__main__':
    unittest.main()
