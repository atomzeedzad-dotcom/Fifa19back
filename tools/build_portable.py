"""Build a complete Windows x64 portable ZIP for non-technical users."""
from __future__ import annotations

import hashlib
import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import VERSION


def main() -> int:
    if sys.platform != 'win32':
        raise RuntimeError('Build this Windows package on Windows')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repack', action='store_true', help='Repackage an already built executable')
    args = parser.parse_args()
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
               '--name', 'FIFA19LocalServer', '--distpath', str(ROOT/'dist'), '--workpath', str(ROOT/'build'),
               '--specpath', str(ROOT/'build'), '--noupx', '--paths', str(ROOT/'engine'),
               '--paths', str(ROOT/'server')]
    for module in ('localfut18_server', 'fut19_profile', 'fut18_catalog', 'fut18_sbc_archive',
                   'fut18_runtime', 'fut18_draft_slots'):
        command += ['--hidden-import', module]
    command += [str(ROOT/'portable_entry.py')]
    if not args.repack:
        subprocess.run(command, cwd=ROOT, check=True)
    folder = ROOT/'dist/FIFA19LocalServer'
    if not (folder/'FIFA19LocalServer.exe').is_file():
        raise RuntimeError('Build the executable before repackaging')
    for source, destination in [('config/server.json', 'config/server.json'),
                                ('data/fifa19-player-definitions.json', 'data/fifa19-player-definitions.json'),
                                ('data/LICENSE', 'data/LICENSE'), ('docs/START_HERE.txt', 'START_HERE.txt')]:
        target = folder/destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/source, target)
    notices = folder/'licenses'
    notices.mkdir(exist_ok=True)
    shutil.copy2(Path(sys.base_prefix)/'LICENSE.txt', notices/'Python-LICENSE.txt')
    for package_name in ('cryptography', 'cffi', 'pyinstaller'):
        distribution = importlib.metadata.distribution(package_name)
        for entry in distribution.files or []:
            if 'licenses/' in str(entry).replace('\\', '/'):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    destination = notices/package_name/source.name
                    destination.parent.mkdir(exist_ok=True)
                    shutil.copy2(source, destination)
    if list(folder.rglob('*.key')):
        raise RuntimeError('Private key found in portable distribution')
    release = ROOT/'release'
    release.mkdir(exist_ok=True)
    archive = release/f'FIFA19LocalServer-v{VERSION}-Windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for file in sorted(folder.rglob('*')):
            if file.is_file():
                package.write(file, str(Path('FIFA19LocalServer')/file.relative_to(folder)))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (release/'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    (release/'manifest.json').write_text(json.dumps({'version': VERSION, 'asset': archive.name,
                                                    'sha256': digest, 'bytes': archive.stat().st_size,
                                                    'includesPython': True, 'clientVerified': False,
                                                    'privateKeysIncluded': False}, indent=2), encoding='utf-8')
    print(f'Portable ZIP: {archive} ({archive.stat().st_size:,} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
