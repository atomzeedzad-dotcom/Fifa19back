"""Freeze the selected FIFA18 backend; apply only explicit bootstrap changes."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ['localfut18_server.py', 'fut18_catalog.py', 'fut18_sbc_archive.py', 'fut18_runtime.py', 'fut18_draft_slots.py']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    manifest = {'source': str(args.source.resolve()), 'files': [],
                'review': 'FIFA18 reference review needs_changes: duplicate slot-zero Draft choice. FIFA19 Draft endpoints disabled.',
                'clientFilesCopied': False}
    for name in FILES:
        original = (args.source/'tools'/name).read_bytes()
        content = original.decode('utf-8-sig')
        changes = []
        if name == 'localfut18_server.py':
            replacements = {
                "RUNTIME=LOCALAPP/'FIFA18LocalFUT'": "RUNTIME=Path(os.environ.get('FIFA19_LOCAL_RUNTIME',str(LOCALAPP/'FIFA19LocalFUT')))",
                "DB_PATH=RUNTIME/'fut18-local.sqlite3'": "DB_PATH=RUNTIME/'fut19-local.sqlite3'",
                "OFFLINE_ONLY=env_flag('FIFA18_LOCAL_OFFLINE',True)": 'OFFLINE_ONLY=True',
                "ENABLE_BACKGROUND_REFRESH=env_flag('FIFA18_LOCAL_BACKGROUND_REFRESH',False) and not OFFLINE_ONLY": 'ENABLE_BACKGROUND_REFRESH=False',
                "LOGFILE=LOGDIR/f'localfut18-{STAMP}.log'": "LOGFILE=LOGDIR/f'localfut19-{STAMP}.log'",
                "_db_init()\n_load_persistent_globals()": "from fut19_profile import install as _install_fifa19_profile\n_install_fifa19_profile(globals())\n_db_init()\n_load_persistent_globals()",
                "'resourceGameYear':2018": "'resourceGameYear':2019",
                "'year':2018": "'year':2019",
                "'year':'2018'": "'year':'2019'",
                "contexts=((0,0),(0,2018),(2,2018),(6,0))": "contexts=((0,0),(0,2019),(2,2019),(6,0))",
            }
            # Normalize CRLF before matching the import-time initialization.
            content = content.replace('\r\n', '\n')
            for before, after in replacements.items():
                count = content.count(before)
                if not count:
                    raise ValueError(f'Reference changed; missing bootstrap anchor {before!r}')
                content = content.replace(before, after)
                changes.append({'anchor': before, 'count': count})
        target = ROOT/'engine'/name
        target.write_text(content, encoding='utf-8')
        manifest['files'].append({'file': name, 'sourceSha256': hashlib.sha256(original).hexdigest(),
                                  'snapshotSha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                                  'bootstrapChanges': changes})
    for name in ['winter15-chain.pem', 'winter15.key']:
        shutil.copy2(args.source/'tls'/name, ROOT/'tls'/name)
    (ROOT/'docs/reference-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Frozen backend snapshot and development TLS certificate created; no game patches copied.')


if __name__ == '__main__':
    main()
