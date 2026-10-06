"""Read-only FIFA19 executable fingerprint and embedded network endpoint report."""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import re
import struct
from pathlib import Path


NETWORK_WORDS = ('gosredirector', 'easports.com', 'fifa19.content', 'fifa-2019', '/ut/game/', 'blaze')
SESSION_WORDS = ('originsdk', 'origincheckonline', 'origingetdefaultuser', 'originrequestauthcode', 'eadesktop')


def _pe(data):
    def require(offset, length):
        if offset < 0 or offset + length > len(data):
            raise ValueError('Truncated Windows PE headers')
    require(0, 64)
    if data[:2] != b'MZ':
        raise ValueError('Not a Windows executable')
    pe = struct.unpack_from('<I', data, 60)[0]
    require(pe, 24)
    if data[pe:pe+4] != b'PE\0\0':
        raise ValueError('Invalid Windows PE signature')
    machine, sections = struct.unpack_from('<HH', data, pe+4)
    optional_size = struct.unpack_from('<H', data, pe+20)[0]
    require(pe+24, optional_size)
    if optional_size < 2 or struct.unpack_from('<H', data, pe+24)[0] not in (0x10b, 0x20b):
        raise ValueError('Unsupported Windows PE optional header')
    if not 1 <= sections <= 96:
        raise ValueError('Invalid Windows PE section count')
    require(pe+24+optional_size, sections*40)
    return {'machine': hex(machine), 'architecture': {0x8664: 'x64', 0x14c: 'x86', 0xaa64: 'arm64'}.get(machine, 'unknown'),
            'sections': sections}


def _inspect_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            digest.update(chunk)
        if source.tell() == 0:
            raise ValueError('Empty executable')
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
            pe = _pe(data)
            findings = []
            for pattern, encoding in ((rb'[ -~]{8,}', 'ascii'), (rb'(?:[ -~]\x00){8,}', 'utf-16-le')):
                for match in re.finditer(pattern, data):
                    if len(findings) >= 256:
                        break
                    value = match.group().decode(encoding)
                    lowered = value.lower()
                    kind = 'network' if any(word in lowered for word in NETWORK_WORDS) else (
                        'session-api' if any(word in lowered for word in SESSION_WORDS) else None)
                    if kind:
                        findings.append({'kind': kind, 'fileOffset': match.start(), 'encoding': encoding, 'text': value[:1000]})
    return {'file': str(path.resolve()), 'size': path.stat().st_size, 'sha256': digest.hexdigest(),
            'pe': pe, 'findings': findings,
            'note': 'String offsets are observations, not validated patch locations'}


def inspect(path: Path) -> dict:
    executable = path/'FIFA19.exe' if path.is_dir() else path
    if executable.name.lower() != 'fifa19.exe' or not executable.is_file():
        raise ValueError('Choose the FIFA19.exe file or the folder containing it')
    root = executable.parent
    main = _inspect_file(executable)
    companions = []
    # FIFA18 routes FUT HTTP via CardsDLL, not just strings in the main EXE.
    # Inspect direct companions only; do not traverse arbitrary game assets.
    for name in ('CardsDLL_Win64_retail.dll', 'OriginSDK.dll', 'OriginSDK_Win64.dll', 'version.dll'):
        candidate = root/name
        if not candidate.is_file():
            companions.append({'name': name, 'present': False})
            continue
        try:
            companions.append({'name': name, 'present': True, **_inspect_file(candidate)})
        except (OSError, ValueError) as exc:
            companions.append({'name': name, 'present': True, 'error': str(exc)})
    return {'schemaVersion': 2, 'file': main['file'], 'size': main['size'], 'sha256': main['sha256'],
            'pe': main['pe'], 'findings': main['findings'], 'companions': companions,
            'endpoints': sorted({row['text'] for row in main['findings'] if row['kind'] == 'network'}),
            'clientVerified': False, 'readOnly': True, 'supportedPatch': False,
            'next': 'Verify FIFA19 URL routing, TLS pins, session APIs and actual FUT schema before creating a reversible patch'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game', type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]/'reports/fifa19-client.json')
    args = parser.parse_args()
    try:
        report = inspect(args.game)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Read-only report saved: {args.output}')
        return 0
    except (OSError, ValueError) as exc:
        print(f'Could not inspect FIFA19: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
