"""Durable phone -> local application -> phone bridge. Never executes received files."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from client import Relay, NoRedirect, dpapi, write_json
from configuration import default_client_config_path, load_client_config, local_data_dir
from sync_protocol import MAX_BYTES, PROJECT, digest, encode, error_response, validate


class LocalBackend:
    def __init__(self, config):
        self.config = config
        self.url = config['backend_url'].rstrip('/')
        parsed = urllib.parse.urlsplit(self.url)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('The processing backend must be a direct loopback HTTP origin')
        if config.get('token_protection') != 'windows-dpapi':
            raise ValueError('A Windows-protected local session is required')
        self.token = dpapi(base64.b64decode(config['bearer_token_protected']), decrypt=True,
                           entropy=config.get('dpapi_entropy', 'mouchen-desktop-v1').encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def apply(self, request):
        call = urllib.request.Request(self.url + '/v1/relay/import', data=encode(request), method='POST',
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
                     'X-Proactive-Cloud-Approved': 'false', 'X-Raw-Cloud-Approved': 'false'})
        with self.opener.open(call, timeout=60) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Local response exceeds the sync message limit')
        return json.loads(raw)


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS inbox (
        sender TEXT NOT NULL, message_id TEXT NOT NULL, digest TEXT NOT NULL,
        request TEXT NOT NULL, response TEXT, status TEXT NOT NULL DEFAULT 'pending',
        last_error TEXT, created_at REAL NOT NULL, PRIMARY KEY(sender,message_id));
      CREATE TABLE IF NOT EXISTS deliveries (
        sender TEXT NOT NULL, message_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
        transfer_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
        PRIMARY KEY(sender,message_id));
      CREATE TABLE IF NOT EXISTS received (
        transfer_id TEXT PRIMARY KEY, path TEXT NOT NULL, outcome TEXT NOT NULL, received_at REAL NOT NULL);
    ''')
    return db


class Bridge:
    def __init__(self, config, relay, backend):
        self.config, self.relay, self.backend = config, relay, backend
        self.device = config.get('device_id') or config.get('device')
        self.peers = config.get('peer_devices') or config.get('targets') or []
        if not self.device or not self.peers or self.device in self.peers:
            raise ValueError('Configure a unique device and its paired peer devices')
        self.state = Path(config['state_dir'])
        self.db = connect(self.state / 'bridge.sqlite3')

    def close(self):
        self.db.close()

    def saved(self, path, manifest):
        """Commit a durable inbox before transport ACK; processing can retry offline."""
        if self.db.execute('SELECT 1 FROM received WHERE transfer_id=?', (manifest['id'],)).fetchone():
            return
        outcome = 'archived'
        request = None
        if manifest.get('project') == PROJECT and manifest.get('category') == 'sync-request':
            outcome = 'quarantined'
            try:
                source = manifest['source']
                if source not in self.peers or Path(path).stat().st_size > MAX_BYTES:
                    raise ValueError('Unpaired sender or oversized request')
                request = validate(json.loads(Path(path).read_bytes()), kind='request', sender=source)
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                request = None
        with self.db:
            if request is not None:
                key = (request['sender'], request['message_id'])
                old = self.db.execute('SELECT digest FROM inbox WHERE sender=? AND message_id=?', key).fetchone()
                if old and old['digest'] != digest(request):
                    outcome = 'conflict'
                else:
                    self.db.execute('INSERT OR IGNORE INTO inbox(sender,message_id,digest,request,created_at) VALUES(?,?,?,?,?)',
                                    (*key, digest(request), encode(request).decode(), time.time()))
                    # A new transmission may be a retry after a lost application receipt.
                    self.db.execute("UPDATE deliveries SET status='pending', transfer_id=NULL, attempt=attempt+1 WHERE sender=? AND message_id=? AND status='delivered'", key)
                    outcome = 'queued'
            self.db.execute('INSERT INTO received VALUES(?,?,?,?)',
                            (manifest['id'], str(path), outcome, time.time()))

    def process(self):
        count = 0
        for row in self.db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY created_at LIMIT 100").fetchall():
            request = json.loads(row['request'])
            key = (row['sender'], row['message_id'])
            try:
                try:
                    response = self.backend.apply(request)
                except urllib.error.HTTPError as exc:
                    if exc.code not in (400, 409, 413, 422):
                        raise
                    response = error_response(request, self.device, 'IMPORT_HTTP_' + str(exc.code))
                validate(response, kind='response', sender=self.device)
                if response['in_reply_to'] != request['message_id'] or response['operation'] != request['operation']:
                    raise ValueError('Local application returned a mismatched receipt')
                with self.db:
                    self.db.execute("UPDATE inbox SET response=?, status='applied',last_error=NULL WHERE sender=? AND message_id=?",
                                    (encode(response).decode(), *key))
                    self.db.execute('INSERT OR IGNORE INTO deliveries(sender,message_id) VALUES(?,?)', key)
                count += 1
            except Exception as exc:
                # Keep private content and backend responses out of status/log files.
                with self.db:
                    self.db.execute('UPDATE inbox SET last_error=? WHERE sender=? AND message_id=?',
                                    (type(exc).__name__, *key))
        return count

    def send(self):
        count = 0
        rows = self.db.execute("""SELECT d.*,i.response FROM deliveries d JOIN inbox i
          USING(sender,message_id) WHERE d.status!='delivered' ORDER BY i.created_at LIMIT 100""").fetchall()
        for row in rows:
            key = (row['sender'], row['message_id'])
            attempt = row['attempt']
            if row['transfer_id']:
                try:
                    item = self.relay.request('GET', '/v1/transfers/' + row['transfer_id'])
                except urllib.error.HTTPError as exc:
                    if exc.code not in (404, 410):
                        raise
                    item = {'state': 'expired'}
                if item['state'] == 'delivered':
                    with self.db:
                        self.db.execute("UPDATE deliveries SET status='delivered' WHERE sender=? AND message_id=?", key)
                    continue
                if item['state'] not in ('expired', 'cancelled') and item.get('expires_at', time.time() + 1) > time.time():
                    continue
                attempt += 1
                with self.db:
                    self.db.execute("UPDATE deliveries SET attempt=?,transfer_id=NULL,status='pending' WHERE sender=? AND message_id=?", (attempt, *key))
            value = json.loads(row['response'])
            staging = self.state / 'bridge-staging' / (value['message_id'] + '.json')
            staging.parent.mkdir(parents=True, exist_ok=True)
            # The on-wire limit applies to the exact bytes, including whitespace.
            with staging.open('wb') as handle:
                handle.write(encode(value))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                transport_key = digest({'response': value, 'attempt': attempt, 'target': row['sender']})
                item = self.relay.push(staging, [row['sender']], category='sync-response', project=PROJECT,
                                       key=transport_key, metadata={'schema': 'ai-twin.sync/v1'})
            finally:
                staging.unlink(missing_ok=True)
            with self.db:
                if item['state'] in ('expired', 'cancelled'):
                    self.db.execute('UPDATE deliveries SET attempt=attempt+1,transfer_id=NULL WHERE sender=? AND message_id=?', key)
                else:
                    self.db.execute('UPDATE deliveries SET transfer_id=?,status=? WHERE sender=? AND message_id=?',
                                    (item['id'], 'delivered' if item['state'] == 'delivered' else 'uploaded', *key))
            count += 1
        return count

    def cycle(self):
        errors = []
        downloaded = 0
        identity_verified = False
        try:
            remote = self.relay.request('GET', '/v1/status')
            if remote.get('device') != self.device:
                raise ValueError('Relay credential belongs to a different device')
            identity_verified = True
            downloaded = len(self.relay.pull(self.config['archive_dir'], self.saved))
        except Exception as exc:
            errors.append('receive:' + type(exc).__name__)
        processed = self.process()
        uploaded = 0
        try:
            if identity_verified:
                uploaded = self.send()
        except Exception as exc:
            errors.append('send:' + type(exc).__name__)
        pending = self.db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0]
        awaiting = self.db.execute("SELECT count(*) FROM deliveries WHERE status!='delivered'").fetchone()[0]
        result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'mode': 'local-rules',
                  'downloaded': downloaded, 'processed': processed, 'uploaded': uploaded,
                  'pending_processing': pending, 'awaiting_phone_receipt': awaiting,
                  'errors': errors, 'healthy': not errors and pending == 0}
        write_json(self.state / 'bridge-status.json', result)
        return result


@contextmanager
def worker_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(default_client_config_path()))
    parser.add_argument('--backend-config', default=str(local_data_dir() / 'Mouchen' / 'LocalMode' / 'bridge-config.json'))
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--pause', action='store_true')
    args = parser.parse_args()
    config = load_client_config(args.config)
    state = Path(config['state_dir'])
    state.mkdir(parents=True, exist_ok=True)
    if args.pause:
        (state / 'stop-sync').write_text('paused', encoding='utf-8')
        return
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        state / 'bridge.log', maxBytes=512000, backupCount=2, encoding='utf-8')],
        format='%(asctime)s %(levelname)s %(message)s')
    with worker_lock(state / 'worker.lock'):
        local_config = json.loads(Path(args.backend_config).read_text(encoding='utf-8-sig'))
        if local_config['desktop_device_id'] != (config.get('device_id') or config.get('device')):
            raise ValueError('Local profile device does not match relay configuration')
        bridge = Bridge(config, Relay(config), LocalBackend(local_config))
        try:
            (state / 'stop-sync').unlink(missing_ok=True)
            while not (state / 'stop-sync').exists():
                result = bridge.cycle()
                logging.info('Cycle processed=%s pending=%s healthy=%s',
                             result['processed'], result['pending_processing'], result['healthy'])
                if not args.watch:
                    print(json.dumps(result, ensure_ascii=False))
                    break
                for _ in range(30):
                    if (state / 'stop-sync').exists():
                        break
                    time.sleep(1)
        finally:
            bridge.close()


if __name__ == '__main__':
    main()
