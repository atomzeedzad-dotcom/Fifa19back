"""FIFA19 local backend adapter. Actual FIFA19 client integration is unverified."""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_paths import app_root, runtime_root

ROOT = app_root()
VERSION = '0.2.1-backend-preview'
DEFAULT_CONFIG = ROOT/'config/server.json'


def load_config(path=DEFAULT_CONFIG) -> dict:
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if config['host'] != '127.0.0.1':
        raise ValueError('This local backend must bind to 127.0.0.1')
    names = ('redirector', 'blaze', 'easw', 'fut', 'fut_secondary')
    if set(config['ports']) != set(names):
        raise ValueError(f'Configure exactly these ports: {names}')
    ports = list(config['ports'].values())
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports) or len(set(ports)) != len(ports):
        raise ValueError('Ports must be unique integers between 1024 and 65535')
    # Verification cannot be enabled merely by editing a config switch.
    config['client_verified'] = False
    return config


def preflight(config: dict) -> None:
    for label, port in config['ports'].items():
        with socket.socket() as probe:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((config['host'], port))
            except OSError as exc:
                raise RuntimeError(f'{label} port {port} is already in use. Stop its server or edit config/server.json.') from exc


def import_engine():
    sys.path.insert(0, str(ROOT/'engine'))
    sys.path.insert(0, str(ROOT/'server'))
    return importlib.import_module('localfut18_server')


def adapt_engine(engine, config: dict):
    ports = config['ports']
    engine.TOKEN = 'LOCALFUT19_SESSION_1000000001'
    original_preauth = getattr(engine, '_fifa19_original_preauth', engine.preauth)
    engine._fifa19_original_preauth = original_preauth
    engine.preauth = lambda: original_preauth().replace(engine.sv('fifa-2018-pc'), engine.sv(config['instance']))
    engine.entitlements = lambda: engine.fl_group('NLST', [])
    original_config = getattr(engine, '_fifa19_original_coreconfig', engine.core_config)
    engine._fifa19_original_coreconfig = original_config

    def core_config():
        pairs = [(key, value.replace(':8099', ':'+str(ports['fut'])).replace(':42232', ':'+str(ports['easw'])))
                 for key, value in original_config()]
        pairs.extend([('FUT_GAME_YEAR', '2019'), ('FUT_GAME_NAME', 'fifa19')])
        return pairs

    engine.core_config = core_config
    # The inherited store contains FIFA18 events and rare guarantees that this
    # base-only FIFA19 archive cannot substantiate. Keep its list contract,
    # but publish no products and reject purchases in the preview.
    engine.STORE_PACKS = []
    original_item = getattr(engine, '_fifa19_original_definition_item', engine._definition_item)
    engine._fifa19_original_definition_item = original_item
    def definition_item(*args, **kwargs):
        item = original_item(*args, **kwargs)
        if item.get('itemType') == 'player':
            item.update(rareflag=0, rareFlag=0, resourceGameYear=2019)
        return item
    engine._definition_item = definition_item
    # Block helper crawlers too: they otherwise have their own urlopen calls.
    catalog = importlib.import_module('fut18_catalog')
    def offline(*args, **kwargs):
        raise RuntimeError('FIFA19 preview uses only its bundled offline data')
    catalog._ua_request = offline
    archive = importlib.import_module('fut18_sbc_archive')
    archive.refresh_archive = offline

    def health():
        return {'product': 'FIFA19LocalFUT', 'version': VERSION, 'status': 'ready',
                'processId': os.getpid(),
                'clientVerified': False, 'ports': ports,
                'database': str(engine.DB_PATH), 'playerCount': len(engine._load_player_defs(False)),
                'unsupported': ['client-routing', 'draft', 'world-cup', 'division-rivals', 'sbc-archive', 'special-cards', 'matches', 'squad-battles', 'pack-purchases'],
                'note': 'Backend/API preview; FIFA19 gameplay has not been verified'}

    class Fut19Handler(engine.FutHandler):
        def body(self):
            if not hasattr(self, '_cached_body'):
                self._cached_body = super().body()
            return self._cached_body

        def go(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path).lower().rstrip('/')
            if path == '/health':
                self.send(200, json.dumps(health()).encode())
                return
            if '/fifa18' in path:
                self.send(404, b'{"error":"Use /ut/game/fifa19"}')
                return
            query = urllib.parse.parse_qs(parsed.query)
            body = self.body()
            try:
                document = json.loads(body) if body else {}
            except (ValueError, UnicodeDecodeError):
                self.send(400, b'{"error":"Invalid JSON request body"}')
                return
            def unsupported_values(value):
                if isinstance(value, dict):
                    for key, entry in value.items():
                        label = key.lower()
                        if label == 'skumode' and str(entry).upper() == 'WC':
                            return True
                        if label in ('id', 'squadid'):
                            try:
                                if int(float(str(entry))) in (900001, 900002):
                                    return True
                            except (ValueError, OverflowError):
                                pass
                        if label in ('mode', 'draftmode', 'gamemode') and ('draft' in str(entry).lower() or 'world_cup' in str(entry).lower()):
                            return True
                        if unsupported_values(entry):
                            return True
                elif isinstance(value, list):
                    return any(unsupported_values(entry) for entry in value)
                return False
            query_document = {key: values[0] for key, values in query.items() if values}
            disabled_paths = ('/draft', '/worldcup', '/world-cup', '/rivals', '/sbc', '/sbs', '/choices/',
                              '/choose/', '/grant/award', '/squad/900001', '/squad/900002', '/match', '/purchase/',
                              '/squadbattle', '/sqbt', '/featuredsquad', 'squad-battle', 'squad/battle')
            numeric_squad = re.search(r'/squad/(\d+)(?:/|$)', path)
            draft_squad_route = bool(numeric_squad and int(numeric_squad.group(1)) in (900001, 900002))
            pack_purchase = self.command in ('POST', 'PUT') and path.endswith(('/purchased/items', '/purchased'))
            if (any(token in path for token in disabled_paths) or path.endswith(('/choices', '/choose', '/purchase'))
                    or draft_squad_route or pack_purchase or unsupported_values(query_document) or unsupported_values(document)):
                self.send(501, b'{"error":"Feature requires FIFA19 client verification","clientVerified":false}')
                return
            original = self.path
            self.path = self.path.replace('/fifa19', '/fifa18').replace('/FIFA19', '/fifa18')
            try:
                super().go()
            except Exception:
                engine.log.exception('FIFA19 request failed: %s %s', self.command, original)
                self.send(500, b'{"error":"Backend request failed; see local log"}')
            finally:
                self.path = original

        def send(self, status, payload, ctype='application/json; charset=utf-8', headers=None):
            if ctype.startswith('application/json'):
                document = json.loads(payload)
                if '/settings' in self.path.lower() and isinstance(document, dict):
                    for entry in document.get('configs', []):
                        if any(word in str(entry.get('type', '')).lower() for word in ('draft', 'squadbuildingchallenge', 'rivals', 'storeenabled', 'packstoreenabled', 'squadbattle')):
                            entry['value'] = '0'
                def rewrite(value):
                    if isinstance(value, dict):
                        return {k: (2019 if k in ('resourceGameYear', 'year') and v == 2018 else rewrite(v)) for k, v in value.items()}
                    if isinstance(value, list):
                        return [rewrite(x) for x in value]
                    if isinstance(value, str):
                        return value.replace('LOCALFUT18', 'LOCALFUT19').replace('/fifa18', '/fifa19').replace('127.0.0.1:8099', f'127.0.0.1:{ports["fut"]}')
                    return value
                payload = json.dumps(rewrite(document), separators=(',', ':')).encode()
            headers = {k: v.replace('LOCALFUT18', 'LOCALFUT19') for k, v in (headers or {}).items()}
            super().send(status, payload, ctype, headers)

        do_GET = go
        do_POST = go
        do_PUT = go
        do_DELETE = go
        do_OPTIONS = go

    class Redirect19Handler(engine.QuietHTTP):
        def go(self):
            self.body()
            engine.log.info('FIFA19 redirector request %s %s', self.command, self.path)
            payload = ('<?xml version="1.0" encoding="UTF-8"?><serverinstanceinfo>'
                       '<address member="0"><valu><hostname>127.0.0.1</hostname><ip>2130706433</ip>'
                       f'<port>{ports["blaze"]}</port></valu></address><secure>0</secure></serverinstanceinfo>').encode()
            self.send(200, payload, 'application/xml')
        do_GET = go
        do_POST = go

    return Fut19Handler, Redirect19Handler


class RunningServer:
    def __init__(self, config, cert=None, key=None):
        preflight(config)
        self.engine = import_engine()
        self.config = config
        self.servers = []
        self.threads = []
        fut_handler, redirect_handler = adapt_engine(self.engine, config)
        if bool(cert) != bool(key):
            raise ValueError('Supply both --cert and --key, or neither')
        if cert is None:
            from server.tls_identity import ensure_identity
            cert, key = ensure_identity(self.engine.RUNTIME)
        context = self.engine.tls_context(cert, key)
        ports = config['ports']
        constructors = [
            ('redirector', lambda address: self.engine.LoggingTLSServer(address, redirect_handler, context)),
            ('blaze', lambda address: self.engine.ReuseTCP(address, self.engine.BlazeHandler)),
            ('easw', lambda address: ThreadingHTTPServer(address, self.engine.EASWHandler)),
            ('fut', lambda address: ThreadingHTTPServer(address, fut_handler)),
            ('fut_secondary', lambda address: ThreadingHTTPServer(address, fut_handler)),
        ]
        try:
            for name, make in constructors:
                server = make((config['host'], ports[name]))
                server.label = 'FIFA19-'+name
                server.daemon_threads = True
                self.servers.append(server)
        except Exception:
            for server in self.servers:
                server.server_close()
            raise

    def start(self):
        for server in self.servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.engine.log.warning('FIFA19 backend preview ready: http://127.0.0.1:%s/health ; client unverified', self.config['ports']['fut'])

    def close(self):
        for server, thread in zip(self.servers, self.threads):
            server.shutdown()
            thread.join(timeout=5)
        for server in self.servers:
            server.server_close()
        self.threads.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--runtime', type=Path, help='Use an isolated save/log directory')
    parser.add_argument('--cert', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--launcher-control', action='store_true', help='Stop gracefully when launcher sends stop on stdin')
    parser.add_argument('--stop-file', type=Path, help='Stop when this private launcher control file exists')
    args = parser.parse_args()
    if args.runtime:
        os.environ['FIFA19_LOCAL_RUNTIME'] = str(args.runtime.resolve())
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if args.launcher_control and sys.stdin is not None:
        def watch_launcher():
            for line in sys.stdin:
                if line.strip().lower() == 'stop':
                    stop.set()
                    return
            stop.set()
        threading.Thread(target=watch_launcher, daemon=True).start()
    try:
        running = RunningServer(load_config(args.config), args.cert, args.key)
        running.start()
        try:
            while not stop.wait(0.2):
                if args.stop_file and args.stop_file.is_file():
                    args.stop_file.unlink(missing_ok=True)
                    stop.set()
        finally:
            running.close()
    except Exception as exc:
        print(f'FIFA19 local server could not start: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
