"""Probe the same service chain used by FIFA18, without importing its database."""
from __future__ import annotations

import json
import socket
import ssl
import struct
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


def _http(url, context=None):
    # Explicitly bypass system HTTP proxies: these are loopback-only probes.
    handlers = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    with urllib.request.build_opener(*handlers).open(url, timeout=2) as response:
        return response.read(2 * 1024 * 1024)


def _receive(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ValueError('Blaze closed before completing its response')
        data.extend(chunk)
    return bytes(data)


def _blaze(host, port, instance):
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.settimeout(2)
        for command in (2, 7):  # Util.Ping, Util.PreAuth, following the FIFA18 pipeline.
            frame = struct.pack('>IHHH', 0, 0, 9, command) + b'\x00\x00\x49\x00\x00\x00'
            connection.sendall(frame)
            header = _receive(connection, 16)
            size, metadata_size, component, returned_command = struct.unpack('>IHHH', header[:10])
            if (component, returned_command, header[10:13], header[13] >> 5) != (9, command, b'\x00\x00\x49', 1):
                raise ValueError('Unexpected Blaze response identity/type')
            if size > 1024 * 1024 or metadata_size > 65535:
                raise ValueError('Blaze response is too large')
            payload = _receive(connection, size + metadata_size)[metadata_size:]
            if not payload or (command == 7 and instance.encode() + b'\0' not in payload):
                raise ValueError('Blaze did not advertise the configured FIFA19 instance')
    return 'FIRE2 Ping and PreAuth; ' + instance


def diagnose(config: dict, runtime: Path, expected_pid=None) -> dict:
    host, ports = config['host'], config['ports']
    if host != '127.0.0.1':
        raise ValueError('Diagnostics must stay on loopback')
    checks = []

    def probe(name, action):
        try:
            detail = action()
            checks.append({'service': name, 'passed': True, 'detail': detail})
        except (OSError, ValueError, ET.ParseError) as exc:
            checks.append({'service': name, 'passed': False, 'detail': str(exc)})

    def health(name):
        document = json.loads(_http(f'http://{host}:{ports[name]}/health'))
        if document.get('product') != 'FIFA19LocalFUT' or document.get('ports') != ports:
            raise ValueError('Port belongs to a different application/configuration')
        if expected_pid is not None and document.get('processId') != expected_pid:
            raise ValueError('Port does not belong to this launcher worker')
        return f'FIFA19 backend, {document["playerCount"]} player definitions'

    def redirector():
        # Trust only our generated certificate for this probe. This does not
        # install a CA or establish that FIFA19 accepts this identity.
        context = ssl.create_default_context(cafile=str(Path(runtime)/'tls/localhost.pem'))
        document = ET.fromstring(_http(f'https://{host}:{ports["redirector"]}/redirector', context))
        if document.findtext('address/valu/hostname') != host or document.findtext('address/valu/port') != str(ports['blaze']):
            raise ValueError('Redirector returned the wrong Blaze destination')
        return f'TLS and XML route to {host}:{ports["blaze"]}; game certificate trust unverified'

    def easw():
        if _http(f'http://{host}:{ports["easw"]}/routing') != b'':
            raise ValueError('EASW routing response does not match the inherited adapter')
        return 'EASW adapter reachable; native FIFA19 routing schema unverified'

    probe('fut', lambda: health('fut'))
    probe('fut_secondary', lambda: health('fut_secondary'))
    probe('redirector', redirector)
    probe('blaze', lambda: _blaze(host, ports['blaze'], config['instance']))
    probe('easw', easw)
    return {'schemaVersion': 1, 'status': 'passed' if all(row['passed'] for row in checks) else 'failed',
            'checks': checks, 'clientVerified': False,
            'clientBlockers': ['FIFA19 executable/DLL fingerprints unavailable or unverified',
                               'Client URL routing and certificate pin compatibility',
                               'EA session and actual FUT19 request/response contracts'],
            'note': 'Service probes are backend checks, not an in-game test'}
