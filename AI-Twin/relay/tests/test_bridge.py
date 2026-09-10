"""Exercise durable imports and application receipts through the real relay API."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from uuid import uuid4

from fastapi.testclient import TestClient

from bridge import Bridge, LocalBackend
from client import Relay, write_json
from sync_protocol import PROJECT, SCHEMA, MAX_BYTES, encode


class TestTransport(Relay):
    def __init__(self, client, device):
        self.client, self.device = client, device

    def open(self, method, path, body=None, headers=None):
        if hasattr(body, 'read'):
            body = body.read()
        response = self.client.request(method, path, content=body,
            headers={'Authorization': 'Bearer test-only-' + self.device} | (headers or {}))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(path, response.status_code, 'synthetic error', {}, None)
        return io.BytesIO(response.content)


class IdempotentBackend:
    def __init__(self):
        self.receipts = {}
        self.failed = False
        self.config = {'backend_url': 'http://127.0.0.1:8788', 'session_user_id': 'synthetic-owner',
                       'desktop_device_id': 'desktop'}

    def verify_session(self):
        pass

    def apply(self, request):
        if self.failed:
            raise ConnectionError('synthetic offline backend')
        key = request['message_id']
        if key not in self.receipts:
            self.receipts[key] = dict(schema=SCHEMA, kind='response', message_id=str(uuid4()),
                sender='desktop', created_at=datetime.now(timezone.utc).isoformat(),
                in_reply_to=key, operation=request['operation'], ok=True,
                result={'goals': [{'id': key, 'title': '合成目标'}], 'advice': []})
        return self.receipts[key]


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = {'data_dir': str(self.root / 'server'), 'minimum_free_bytes': 0,
                  'devices': {name: hashlib.sha256(('test-only-' + name).encode()).hexdigest()
                              for name in ('desktop', 'phone', 'third')}}
        path = self.root / 'server.json'
        write_json(path, config)
        spec = importlib.util.spec_from_file_location('test_bridge_server', Path(__file__).resolve().parents[1] / 'server.py')
        self.server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'RELAY_CONFIG': str(path)}):
            spec.loader.exec_module(self.server)
        self.http = self.enterContext(TestClient(self.server.app))
        self.phone = TestTransport(self.http, 'phone')
        self.desktop = TestTransport(self.http, 'desktop')
        self.config = {'url': 'https://relay.example.invalid', 'state_dir': str(self.root / 'local'), 'archive_dir': str(self.root / 'archive'),
                       'device': 'desktop', 'targets': ['phone']}
        self.backend = IdempotentBackend()
        self.bridge = Bridge(self.config, self.desktop, self.backend)
        self.addCleanup(lambda: self.bridge.close())

    def request(self, **overrides):
        value = dict(schema=SCHEMA, kind='request', message_id=str(uuid4()), sender='phone',
                     created_at=datetime.now(timezone.utc).isoformat(), operation='goal.create',
                     body={'domain': 'work', 'title': '合成目标', 'quote': '示例', 'target': {}})
        return value | overrides

    def upload(self, value, transport=None):
        path = self.root / 'request.json'
        write_json(path, value)
        return (transport or self.phone).push(path, ['desktop'], project=PROJECT, category='sync-request')

    def restart(self):
        self.bridge.close()
        self.bridge = Bridge(self.config, self.desktop, self.backend)

    def test_round_trip_saves_locally_then_returns_application_receipt(self):
        request = self.request()
        uploaded = self.upload(request)
        status = self.bridge.cycle()
        self.assertTrue(status['healthy'])
        self.assertEqual(status['processed'], 1)
        self.assertEqual(self.phone.request('GET', '/v1/transfers/' + uploaded['id'])['state'], 'delivered')
        self.assertFalse((self.server.BLOBS / uploaded['id']).exists())
        saved = []
        self.phone.pull(self.root / 'phone', lambda path, manifest: saved.append(json.loads(path.read_bytes())))
        self.assertEqual(saved[0]['in_reply_to'], request['message_id'])
        self.assertEqual(saved[0]['result']['goals'][0]['title'], '合成目标')
        self.assertEqual(self.bridge.cycle()['awaiting_phone_receipt'], 0)

    def test_backend_offline_survives_transport_ack_and_restart(self):
        request = self.request()
        self.backend.failed = True
        self.upload(request)
        status = self.bridge.cycle()
        self.assertFalse(status['healthy'])
        self.assertEqual(status['pending_processing'], 1)
        self.assertEqual(self.phone.request('GET', '/v1/status')['pending_transfers'], 0)
        self.restart()
        self.backend.failed = False
        self.assertEqual(self.bridge.cycle()['processed'], 1)
        self.assertEqual(len(self.backend.receipts), 1)

    def test_expired_response_retries_with_new_transport_id_and_same_response(self):
        self.upload(self.request())
        self.bridge.cycle()
        first = self.bridge.db.execute('SELECT * FROM deliveries').fetchone()
        with self.server.db(write=True) as db:
            db.execute('UPDATE transfers SET expires_at=0 WHERE id=?', (first['transfer_id'],))
        self.bridge.cycle()
        second = self.bridge.db.execute('SELECT * FROM deliveries').fetchone()
        self.assertNotEqual(first['transfer_id'], second['transfer_id'])
        self.assertEqual(second['attempt'], 1)
        self.assertEqual(len(self.backend.receipts), 1)

    def test_retransmitted_request_after_receipt_is_deduplicated_and_response_replayed(self):
        value = self.request()
        self.upload(value)
        self.bridge.cycle()
        self.phone.pull(self.root / 'phone')
        self.bridge.cycle()
        self.upload(value)
        self.assertEqual(self.bridge.cycle()['processed'], 0)
        self.assertEqual(len(self.backend.receipts), 1)
        self.assertEqual(len(self.phone.request('GET', '/v1/inbox')['items']), 1)

    def test_unpaired_source_and_body_conflict_never_reach_backend(self):
        value = self.request()
        self.upload(value)
        self.bridge.cycle()
        self.upload(value | {'body': {'title': 'changed'}})
        self.upload(self.request(sender='third'), TestTransport(self.http, 'third'))
        self.bridge.cycle()
        self.assertEqual(len(self.backend.receipts), 1)
        outcomes = [row[0] for row in self.bridge.db.execute('SELECT outcome FROM received')]
        self.assertIn('conflict', outcomes)
        self.assertIn('quarantined', outcomes)

    def test_failed_phone_save_does_not_ack_response(self):
        self.upload(self.request())
        self.bridge.cycle()
        def fail(path, manifest):
            raise OSError('synthetic full disk')
        with self.assertRaises(OSError):
            self.phone.pull(self.root / 'phone', fail)
        self.assertEqual(len(self.phone.request('GET', '/v1/inbox')['items']), 1)
        self.assertEqual(self.bridge.cycle()['awaiting_phone_receipt'], 1)

    def test_local_import_committed_before_lost_http_response_is_retried_safely(self):
        request = self.request()
        self.upload(request)
        original = self.backend.apply
        def interrupted(value):
            original(value)
            raise ConnectionError('synthetic response loss after commit')
        self.backend.apply = interrupted
        self.bridge.cycle()
        self.restart()
        self.backend.apply = original
        self.assertEqual(self.bridge.cycle()['processed'], 1)
        self.assertEqual(len(self.backend.receipts), 1)

    def test_response_mismatch_is_retained_for_retry(self):
        self.upload(self.request())
        original = self.backend.apply
        self.backend.apply = lambda value: original(value) | {'in_reply_to': str(uuid4())}
        result = self.bridge.cycle()
        self.assertEqual(result['pending_processing'], 1)
        self.assertEqual(result['uploaded'], 0)

    def test_loopback_client_rejects_remote_redirect_and_credential_origins(self):
        for url in ('https://example.invalid', 'http://localhost', 'http://127.0.0.1@evil.invalid',
                    'http://127.0.0.1/extra', 'http://127.0.0.1?token=secret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalBackend({'backend_url': url})

    def test_oversized_request_stays_archived_without_import(self):
        self.upload(self.request(body={'text': 'x' * MAX_BYTES}))
        self.bridge.cycle()
        self.assertEqual(len(self.backend.receipts), 0)
        self.assertEqual(self.bridge.db.execute('SELECT outcome FROM received').fetchone()[0], 'quarantined')

    def test_state_cannot_be_reused_by_another_account_or_origin(self):
        other = IdempotentBackend()
        other.config = {**other.config, 'session_user_id': 'another-synthetic-owner'}
        with self.assertRaisesRegex(ValueError, 'another account'):
            Bridge(self.config, self.desktop, other)
        with self.assertRaisesRegex(ValueError, 'another account'):
            Bridge({**self.config, 'url': 'https://other.example.invalid'}, self.desktop, self.backend)

    def test_revoked_or_switched_local_session_stops_receive_and_send(self):
        self.upload(self.request())
        self.bridge.cycle()
        pending = len(self.phone.request('GET', '/v1/inbox')['items'])
        self.upload(self.request())
        def revoked():
            raise ValueError('synthetic account mismatch')
        self.backend.verify_session = revoked
        status = self.bridge.cycle()
        self.assertFalse(status['healthy'])
        self.assertEqual(status['downloaded'], 0)
        self.assertEqual(status['processed'], 0)
        self.assertEqual(status['uploaded'], 0)
        self.assertEqual(len(self.backend.receipts), 1)
        self.assertEqual(len(self.phone.request('GET', '/v1/inbox')['items']), pending)

    def test_pause_between_imports_keeps_remaining_records_pending(self):
        self.upload(self.request())
        self.upload(self.request())
        original = self.backend.apply
        def apply_then_pause(value):
            response = original(value)
            (self.bridge.state / 'stop-sync').touch()
            return response
        self.backend.apply = apply_then_pause
        result = self.bridge.cycle()
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['pending_processing'], 1)
        self.assertEqual(result['uploaded'], 0)
        self.assertEqual(len(self.backend.receipts), 1)

    def test_unbound_existing_inbox_does_not_adopt_current_identity(self):
        self.upload(self.request())
        self.bridge.cycle()
        with self.bridge.db:
            self.bridge.db.execute('DELETE FROM identity')
        with self.assertRaisesRegex(ValueError, 'unbound inbox'):
            Bridge(self.config, self.desktop, self.backend)


if __name__ == '__main__':
    unittest.main()
