"""Verify the extracted release EXE with no Python interpreter on its PATH."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    manifest = json.loads((ROOT/'release/manifest.json').read_text(encoding='utf-8'))
    archive = ROOT/'release'/manifest['asset']
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != manifest['sha256']:
        raise RuntimeError('ZIP checksum mismatch')
    checks = []
    with tempfile.TemporaryDirectory(prefix='fut19-portable-') as temporary:
        base = Path(temporary)
        extracted = base/'แพ็กเกจ FIFA19'
        with zipfile.ZipFile(archive) as package:
            if any(name.lower().endswith('.key') for name in package.namelist()):
                raise RuntimeError('Private key included in ZIP')
            package.extractall(extracted)
        executable = extracted/'FIFA19LocalServer/FIFA19LocalServer.exe'
        runtime = base/'เซฟทดสอบ'
        runtime.mkdir()
        environment = os.environ.copy()
        environment['PATH'] = os.environ.get('SYSTEMROOT', r'C:\Windows')+r'\System32'
        environment['FIFA19_LOCAL_RUNTIME'] = str(runtime)
        environment.pop('PYTHONPATH', None)
        environment.pop('PYTHONHOME', None)

        def request(path, method='GET', document=None):
            payload = None if document is None else json.dumps(document).encode()
            req = urllib.request.Request('http://127.0.0.1:9099'+path, data=payload, method=method,
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.load(response)

        def start(iteration):
            stop_file = runtime/f'worker-{iteration}.stop'
            process = subprocess.Popen([str(executable), '--server', '--stop-file', str(stop_file)],
                                       cwd=base, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic()+25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Portable worker failed; '+(runtime/'portable.log').read_text(encoding='utf-8')[-2500:])
                try:
                    health = request('/health')
                    if Path(health['database']).parent != runtime or health.get('processId') != process.pid:
                        raise RuntimeError('A different server occupies the default port')
                    return process, stop_file, health
                except OSError:
                    time.sleep(0.1)
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError('Portable worker did not become ready')

        for iteration in (1, 2):
            process, stop_file, health = start(iteration)
            try:
                if health['clientVerified'] or health['playerCount'] != 15462:
                    raise RuntimeError('Unexpected portable health data')
                if iteration == 1:
                    squad = request('/ut/game/fifa19/squad/active', 'PUT', {'id': 1, 'squadName': 'Portable Persisted'})
                    if squad['squadName'] != 'Portable Persisted':
                        raise RuntimeError('Portable save write failed')
                    with urllib.request.urlopen('https://127.0.0.1:42330/redirector', context=ssl._create_unverified_context(), timeout=3) as response:
                        if b'<port>10151</port>' not in response.read():
                            raise RuntimeError('TLS redirector failed')
                    with socket.create_connection(('127.0.0.1', 10151), timeout=3) as connection:
                        # FIRE2 Util.Ping request: component9, command2, message1.
                        connection.sendall(bytes.fromhex('00000000000000090002000001000000'))
                        reply = connection.recv(4096)
                        if len(reply) < 16 or reply[6:10] != bytes.fromhex('00090002'):
                            raise RuntimeError('Blaze Ping failed')
                    checks.extend(['Extracted EXE startup without Python on PATH', 'Unicode/space package and save paths',
                                   'FUT HTTP account/squad/save', 'TLS redirector handshake', 'Blaze FIRE2 Ping'])
                elif request('/ut/game/fifa19/squad/active')['squadName'] != 'Portable Persisted':
                    raise RuntimeError('Portable squad did not persist across processes')
                stop_file.touch()
                if process.wait(timeout=10) != 0:
                    raise RuntimeError('Worker did not shut down cleanly')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        checks.extend(['SQLite persistence across extracted EXE restart', 'Graceful EXE shutdown'])
        gui = subprocess.Popen([str(executable), '--check-launcher'], cwd=base, env=environment,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            if gui.wait(timeout=60) != 0:
                details = (runtime/'launcher-check.json').read_text(encoding='utf-8') if (runtime/'launcher-check.json').exists() else (runtime/'portable.log').read_text(encoding='utf-8')[-2500:]
                raise RuntimeError('Packaged GUI lifecycle failed: '+details)
        finally:
            if gui.poll() is None:
                gui.kill()
                gui.wait(timeout=5)
        gui_report = json.loads((runtime/'launcher-check.json').read_text(encoding='utf-8'))
        if gui_report['status'] != 'passed' or not gui_report['frozen']:
            raise RuntimeError('The packaged GUI was not verified')
        checks.extend(gui_report['checks'])
        if not (runtime/'tls/localhost.key').is_file():
            raise RuntimeError('Per-machine TLS generation failed')
        checks.append('TLS identity generated in local runtime; no private key shipped')
    report = {'status': 'passed', 'version': manifest['version'], 'sha256': digest,
              'clientVerified': False, 'platform': 'Windows x64', 'checks': checks}
    (ROOT/'release/verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
