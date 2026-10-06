"""Publish verified portable assets using Git's existing GitHub credential.

Only small public result metadata is printed. Never print credential output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'atomzeedzad-dotcom/Fifa19back'


def credential() -> str:
    environment = os.environ.copy()
    environment['GIT_TERMINAL_PROMPT'] = '0'
    environment['GCM_INTERACTIVE'] = 'never'
    result = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                            text=True, capture_output=True, env=environment, timeout=15, check=False)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    token = fields.get('password')
    if not token:
        raise RuntimeError('No saved GitHub credential available; sign in using Git first')
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True, help='Verified commit already pushed to GitHub')
    args = parser.parse_args()
    manifest = json.loads((ROOT/'release/manifest.json').read_text(encoding='utf-8'))
    zip_path = ROOT/'release'/manifest['asset']
    if hashlib.sha256(zip_path.read_bytes()).hexdigest() != manifest['sha256']:
        raise RuntimeError('Portable archive does not match the manifest')
    verification = json.loads((ROOT/'release/verification.json').read_text(encoding='utf-8'))
    if verification.get('status') != 'passed' or verification.get('sha256') != manifest['sha256']:
        raise RuntimeError('Packaged executable has not passed verification for this ZIP')
    token = credential()
    def request(path, method='GET', data=None, content_type='application/json'):
        url = path if path.startswith('https://') else 'https://api.github.com'+path
        if urllib.parse.urlsplit(url).hostname not in ('api.github.com', 'uploads.github.com'):
            raise ValueError('Refusing to send GitHub credentials to another host')
        body = json.dumps(data).encode() if isinstance(data, dict) else data
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json',
                                              'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': content_type,
                                              'User-Agent': 'FIFA19LocalServerRelease'})
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not print request headers or the credential provider's output.
            raise RuntimeError(f'GitHub returned HTTP {exc.code} for {method} {urllib.parse.urlsplit(url).path}') from None
    tag = 'v'+manifest['version']
    body = (f'Windows x64 portable launcher {tag}. Download the ZIP below, Extract All, and open FIFA19LocalServer.exe. '
            'Python and all required runtime libraries are included; no command line is needed.\n\n'
            '**This is a local backend preview, not a playable FIFA19 Local FUT release.** '
            'FIFA19 game files are not included. Client routing and FUT19 gameplay are not implemented/verified. '
            'Draft, matches, Squad Battles and pack purchases remain disabled.\n\n'
            'Includes a graphical Start/Stop/Check launcher, a read-only game inspection option, '
            '15,462 historical base player definitions and independent local saves. '
            'Development TLS keys are generated separately on each machine; no private key is shipped.\n\n'
            'ดาวน์โหลด ZIP แล้วแตกไฟล์ทั้งหมด จากนั้นดับเบิลคลิก FIFA19LocalServer.exe '
            'ไม่ต้องติดตั้ง Python และไม่ต้องพิมพ์คำสั่ง รุ่นนี้ยังเข้าเล่น FUT19 ในเกมจริงไม่ได้\n\n'
            'Verification: 16 source regression tests, extracted executable startup with no Python on PATH, '
            'TLS/Blaze/FUT checks, persistent squad across executable restart, and graceful stop. '
            'See SHA256SUMS.txt for the archive checksum.')
    releases = request(f'/repos/{REPOSITORY}/releases')
    existing = next((release for release in releases if release['tag_name'] == tag), None)
    if existing:
        release = existing
    else:
        release = request(f'/repos/{REPOSITORY}/releases', 'POST', {
            'tag_name': tag, 'target_commitish': args.commit, 'name': f'FIFA19 Local Server {tag} — Windows Portable Preview',
            'body': body, 'draft': True, 'prerelease': True})
    # Upload to a draft first. Publish only after all three public assets exist.
    for path in (zip_path, ROOT/'release/SHA256SUMS.txt', ROOT/'release/verification.json'):
        if any(asset['name'] == path.name for asset in release.get('assets', [])):
            raise RuntimeError(f'Existing release asset {path.name}; inspect it instead of replacing automatically')
        upload = release['upload_url'].split('{', 1)[0]+'?'+urllib.parse.urlencode({'name': path.name})
        request(upload, 'POST', path.read_bytes(), 'application/zip' if path.suffix == '.zip' else 'application/octet-stream')
    release = request(f'/repos/{REPOSITORY}/releases/{release["id"]}', 'PATCH', {'draft': False, 'prerelease': True, 'body': body})
    print(json.dumps({'url': release['html_url'], 'tag': tag, 'draft': release['draft'], 'prerelease': release['prerelease'],
                      'assets': [{'name': x['name'], 'url': x['browser_download_url']} for x in release['assets']]}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
