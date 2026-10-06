from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from tools.inspect_game import inspect


def sample_pe(strings=b''):
    # Minimal static fixture, never an installed game or executable test launch.
    data = bytearray(512)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<HH', data, 132, 0x8664, 1)
    struct.pack_into('<H', data, 148, 240)
    struct.pack_into('<H', data, 152, 0x20b)
    return bytes(data)+strings


class InspectorTests(unittest.TestCase):
    def test_exe_and_dll_evidence_and_read_only(self):
        with tempfile.TemporaryDirectory(prefix='fut19 เกม ') as temporary:
            root = Path(temporary)
            exe = root/'FIFA19.exe'
            cards = root/'CardsDLL_Win64_retail.dll'
            exe.write_bytes(sample_pe(b'https://example.gosredirector.ea.com/\0OriginRequestAuthCodeSync\0'))
            cards.write_bytes(sample_pe('http://easw.easports.com:8099/'.encode('utf-16-le')+b'\0\0'))
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            report = inspect(root)
            self.assertEqual(report['sha256'], hashlib.sha256(before['FIFA19.exe']).hexdigest())
            self.assertEqual(report['pe']['architecture'], 'x64')
            self.assertTrue(any(row['kind'] == 'session-api' for row in report['findings']))
            companion = report['companions'][0]
            self.assertTrue(companion['present'])
            self.assertTrue(any('easports.com' in row['text'] and row['encoding'] == 'utf-16-le' for row in companion['findings']))
            self.assertFalse(report['clientVerified'])
            self.assertFalse(report['supportedPatch'])
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_invalid_main_executable_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe = Path(temporary)/'FIFA19.exe'
            for data in (b'', b'MZ'+b'\0'*62, sample_pe()[:170]):
                exe.write_bytes(data)
                with self.subTest(size=len(data)), self.assertRaises(ValueError):
                    inspect(exe)

    def test_malformed_companion_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'FIFA19.exe').write_bytes(sample_pe())
            (root/'version.dll').write_bytes(b'not a PE file')
            report = inspect(root)
            companion = next(row for row in report['companions'] if row['name'] == 'version.dll')
            self.assertTrue(companion['present'])
            self.assertIn('error', companion)


if __name__ == '__main__':
    unittest.main()
