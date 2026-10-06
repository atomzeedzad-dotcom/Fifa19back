from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
sys.path.insert(0, str(ROOT/'tools'))
import localfut19
import import_fut19


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class ServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix='fut19-test-')
        cls.previous_runtime = os.environ.get('FIFA19_LOCAL_RUNTIME')
        os.environ['FIFA19_LOCAL_RUNTIME'] = cls.sandbox.name
        cls.config = localfut19.load_config()
        cls.config['ports'] = {name: free_port() for name in cls.config['ports']}
        cls.running = localfut19.RunningServer(cls.config)
        cls.running.start()
        cls.engine = cls.running.engine

    @classmethod
    def tearDownClass(cls):
        cls.running.close()
        # Release log files before removing the isolated Windows directory.
        for handler in list(cls.engine.log.root.handlers):
            handler.close()
            cls.engine.log.root.removeHandler(handler)
        if cls.previous_runtime is None:
            os.environ.pop('FIFA19_LOCAL_RUNTIME', None)
        else:
            os.environ['FIFA19_LOCAL_RUNTIME'] = cls.previous_runtime
        cls.sandbox.cleanup()

    def request(self, path, method='GET', document=None, secondary=False):
        port = self.config['ports']['fut_secondary' if secondary else 'fut']
        body = json.dumps(document).encode() if document is not None else None
        request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=body, method=method,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response), dict(response.headers)

    def blaze_request(self, component, command, payload=b''):
        packet = self.engine.frame(component, command, 73, 0, payload)
        with socket.create_connection(('127.0.0.1', self.config['ports']['blaze']), timeout=3) as connection:
            connection.sendall(packet[:7])
            connection.sendall(packet[7:])
            buffer = bytearray()
            result = None
            while result is None:
                buffer.extend(connection.recv(65536))
                result = self.engine.parse(buffer)
            self.assertEqual((result.component, result.command, result.msg, result.typ), (component, command, 73, 1))
            return result.payload

    def test_health_and_all_services(self):
        document, _ = self.request('/health')
        self.assertEqual(document['product'], 'FIFA19LocalFUT')
        self.assertFalse(document['clientVerified'])
        self.assertGreater(document['playerCount'], 15000)
        self.assertEqual(Path(document['database']).parent, Path(self.sandbox.name))
        secondary, _ = self.request('/health', secondary=True)
        self.assertEqual(secondary, document)
        with urllib.request.urlopen(f'http://127.0.0.1:{self.config["ports"]["easw"]}/routing', timeout=3) as response:
            self.assertEqual(response.status, 200)

    def test_fifa18_style_service_chain_diagnostics(self):
        from server.diagnostics import diagnose
        report = diagnose(self.config, Path(self.sandbox.name), os.getpid())
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual({row['service'] for row in report['checks']}, set(self.config['ports']))
        self.assertFalse(report['clientVerified'])

    def test_diagnostics_reject_wrong_worker(self):
        from server.diagnostics import diagnose
        report = diagnose(self.config, Path(self.sandbox.name), -1)
        failed = {row['service'] for row in report['checks'] if not row['passed']}
        self.assertEqual(failed, {'fut', 'fut_secondary'})
        self.assertEqual(report['status'], 'failed')

    def test_diagnostics_do_not_skip_certificate_validation(self):
        from server.diagnostics import diagnose
        from server.tls_identity import ensure_identity
        with tempfile.TemporaryDirectory() as wrong_runtime:
            ensure_identity(Path(wrong_runtime))
            report = diagnose(self.config, Path(wrong_runtime), os.getpid())
        failed = {row['service'] for row in report['checks'] if not row['passed']}
        self.assertEqual(failed, {'redirector'})
        self.assertEqual(report['status'], 'failed')

    def test_tls_redirector_returns_configured_blaze_port(self):
        context = ssl._create_unverified_context()
        # This proves TLS framing only; certificate trust against a real
        # FIFA19 executable remains unverified.
        with urllib.request.urlopen(f'https://127.0.0.1:{self.config["ports"]["redirector"]}/redirector',
                                    context=context, timeout=3) as response:
            root = ET.fromstring(response.read())
        self.assertEqual(int(root.findtext('address/valu/port')), self.config['ports']['blaze'])
        self.assertEqual(root.findtext('address/valu/hostname'), '127.0.0.1')

    def test_blaze_fragmented_ping_preauth_and_config(self):
        payload = self.blaze_request(9, 2)
        self.assertGreater(self.engine.get_int(payload, 'STIM'), 0)
        payload = self.blaze_request(9, 7)
        self.assertEqual(self.engine.get_str(payload, 'INST'), 'fifa-2019-pc')
        payload = self.blaze_request(9, 1, self.engine.fs('CFID', 'OSDK_CORE'))
        self.assertIn(f'127.0.0.1:{self.config["ports"]["fut"]}'.encode(), payload)
        self.assertNotIn(b':8099', payload)

    def test_auth_and_account_use_fifa19_session(self):
        auth, headers = self.request('/ut/auth', 'POST', {})
        self.assertEqual(auth['sid'], 'LOCALFUT19-SID')
        self.assertEqual(auth['ipPort'], f'127.0.0.1:{self.config["ports"]["fut"]}')
        self.assertEqual(headers['X-UT-SID'], 'LOCALFUT19-SID')
        account, _ = self.request('/ut/game/fifa19/user/accountinfo')
        self.assertTrue(account['hasClub'])

    def test_starter_squad_uses_fifa19_roster_and_slots(self):
        squad, _ = self.request('/ut/game/fifa19/squad/active')
        self.assertEqual(len(squad['players']), 23)
        ids = []
        for slot in squad['players']:
            card = slot['itemData']
            definition = self.engine._PLAYER_DEF_MAP[card['assetId']]
            self.assertEqual(card['resourceGameYear'], 2019)
            self.assertEqual(card['rating'], definition['rating'])
            self.assertLess(card['rating'], 65)
            ids.append(card['assetId'])
        self.assertEqual(len(set(ids)), 23)
        self.assertEqual(squad['players'][0]['itemData']['position'], 'GK')

    def test_squad_update_persists_after_services_restart(self):
        saved, _ = self.request('/ut/game/fifa19/squad/active', 'PUT', {'id': 1, 'squadName': 'FUT19 Persisted', 'formation': 'f433'})
        self.assertEqual(saved['squadName'], 'FUT19 Persisted')
        self.running.close()
        self.__class__.running = localfut19.RunningServer(self.config)
        self.running.start()
        squad, _ = self.request('/ut/game/fifa19/squad/active')
        self.assertEqual(squad['squadName'], 'FUT19 Persisted')
        self.assertEqual(squad['formation'], 'f433')
        # Reconstruct database reads instead of relying on the response cache.
        with self.engine._db_connect() as connection:
            row = connection.execute('SELECT data FROM squads WHERE id=1').fetchone()
            self.assertEqual(json.loads(row[0])['squadName'], 'FUT19 Persisted')

    def test_massinfo_and_store(self):
        info, _ = self.request('/ut/game/fifa19/usermassinfo')
        self.assertEqual(len(info['squad']['players']), 23)
        store, _ = self.request('/pow/store/game/fifa19/catalog/0/item/list')
        self.assertEqual(store['items'], [])
        self.assertTrue(store['endOfList'])

    def test_unsupported_features_and_old_routes_are_explicit(self):
        for path, status in [('/ut/game/fifa18/user/accountinfo', 404),
                             ('/ut/game/fifa19/draft', 501),
                             ('/ut/game/fifa19/sbc/sets', 501),
                             ('/ut/game/fifa19/worldcup', 501),
                             ('/ut/game/fifa19/rivals', 501),
                             ('/ut/game/fifa19/sbs/sets', 501),
                             ('/ut/game/fifa19/choices/formation', 501),
                             ('/ut/game/fifa19/choose/player', 501),
                             ('/ut/game/fifa19/squad/900001', 501),
                             ('/ut/game/fifa19/squad/900002', 501),
                             ('/ut/game/fifa19/squad/0900001', 501),
                             ('/ut/game/fifa19/squad/000900002', 501),
                             ('/ut/game/fifa19/grant/award', 501),
                             ('/ut/game/fifa19/club?skuMode=%57%43', 501),
                             ('/ut/game/fifa19/match', 501),
                             ('/ut/game/fifa19/purchase/pack', 501),
                             ('/ut/game/fifa19/squadbattle', 501),
                             ('/ut/game/fifa19/featuredsquad', 501),
                             ('/ut/game/fifa19/squad-battle', 501),
                             ('/ut/game/fifa19/squad/battle', 501),
                             ('/ut/game/fifa19/sqbt', 501)]:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, status)
            error.exception.close()
        for document in ({'skuMode': 'WC'}, {'squadId': 900001}, {'squad': {'id': 900002}}, {'squadId': '0900001'}, {'squadId': 900001.0}):
            with self.subTest(document=document), self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/ut/game/fifa19/squad', 'PUT', document)
            self.assertEqual(error.exception.code, 501)
            error.exception.close()
        for path in ('/ut/game/fifa19/purchased/items', '/ut/game/fifa19/purchased'):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path, 'POST', {'packId': 210})
            self.assertEqual(error.exception.code, 501)
            error.exception.close()
        settings, _ = self.request('/ut/game/fifa19/settings')
        for entry in settings['configs']:
            if 'draft' in entry['type'].lower():
                self.assertEqual(entry['value'], '0')

    def test_offline_and_no_fifa18_catalogue(self):
        self.assertTrue(self.engine.OFFLINE_ONLY)
        self.assertFalse(self.engine.ENABLE_BACKGROUND_REFRESH)
        with self.assertRaises(RuntimeError):
            self.engine._remote_urlopen('https://example.invalid')
        self.assertEqual(self.engine._special_player_defs(), [])
        self.assertEqual(self.engine._manager_defs(), [])

    def test_port_conflict_is_actionable(self):
        with self.assertRaisesRegex(RuntimeError, 'already in use'):
            localfut19.preflight(self.config)


class RosterTests(unittest.TestCase):
    def test_csv_row_number_is_never_player_identity(self):
        self.assertEqual(import_fut19.image_id('https://cdn.futbin.com/content/fifa19/img/players/p100684097.png?v=10', 'players'), 100684097)
        self.assertEqual(import_fut19.image_id('https://cdn.futbin.com/content/fifa19/img/clubs/45.png', 'clubs'), 45)
        with self.assertRaises(ValueError):
            import_fut19.image_id('https://example.invalid/player/0', 'players')

    def test_roster_provenance_and_fifa19_transfer(self):
        document = json.loads((ROOT/'data/fifa19-player-definitions.json').read_text(encoding='utf-8'))
        self.assertEqual(document['sourceSha256'], hashlib.sha256((ROOT/'data/FutBinCards19.csv').read_bytes()).hexdigest())
        ronaldo = next(x for x in document['players'] if x['assetId'] == 20801)
        self.assertEqual((ronaldo['teamId'], ronaldo['rating'], ronaldo['position']), (45, 94, 'ST'))
        self.assertEqual(document['gameYear'], 2019)

    def test_reference_snapshot_integrity(self):
        document = json.loads((ROOT/'docs/reference-manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(document['clientFilesCopied'])
        for entry in document['files']:
            self.assertEqual(hashlib.sha256((ROOT/'engine'/entry['file']).read_bytes()).hexdigest(), entry['snapshotSha256'])

    def test_invalid_configuration(self):
        config = localfut19.load_config()
        config['ports']['fut'] = config['ports']['blaze']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, 'unique'):
                localfut19.load_config(path)


if __name__ == '__main__':
    unittest.main()
