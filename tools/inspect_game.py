"""Read-only FIFA19 executable fingerprint and embedded network endpoint report."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def inspect(path: Path) -> dict:
    executable = path/'FIFA19.exe' if path.is_dir() else path
    if executable.name.lower() != 'fifa19.exe' or not executable.is_file():
        raise ValueError('Choose the FIFA19.exe file or the folder containing it')
    data = executable.read_bytes()
    if data[:2] != b'MZ':
        raise ValueError('Not a Windows executable')
    endpoints = set()
    for run in re.findall(rb'[ -~]{8,}', data):
        text = run.decode('ascii')
        if any(word in text.lower() for word in ('gosredirector', 'easports.com', 'fifa19.content', 'fifa-2019', '/ut/game/', 'blaze')):
            endpoints.add(text[:1000])
    for run in re.findall(rb'(?:[ -~]\x00){8,}', data):
        text = run.decode('utf-16-le')
        if any(word in text.lower() for word in ('gosredirector', 'easports.com', 'fifa-2019', '/ut/game/')):
            endpoints.add(text[:1000])
    return {'file': str(executable.resolve()), 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'endpoints': sorted(endpoints),
            'clientVerified': False, 'readOnly': True,
            'next': 'Compare actual redirector, FIRE/TDF and FUT schema; do not reuse FIFA18 binary offsets'}


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
