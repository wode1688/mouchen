"""Sync the installed AI Twin's existing records without altering its database."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import time
import urllib.error

from client import Relay, dpapi, write_json, digest_file
from configuration import default_client_config_path, load_client_config


def connect_state(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript('''
    CREATE TABLE IF NOT EXISTS exported(kind TEXT, record_id TEXT, payload_hash TEXT,
        transfer_id TEXT, updated_at REAL, PRIMARY KEY(kind,record_id));
    CREATE TABLE IF NOT EXISTS sent_files(path TEXT,sha256 TEXT,transfer_id TEXT,
        updated_at REAL,PRIMARY KEY(path,sha256));
    CREATE TABLE IF NOT EXISTS archive(id TEXT PRIMARY KEY,path TEXT,category TEXT,
        source TEXT,sha256 TEXT,created_at REAL,archived_at REAL);
    ''')
    return con


def export_existing(config, relay, state):
    source_path = Path(config['source_database'])
    if not source_path.is_file():
        return 0
    source = sqlite3.connect(source_path.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
    count = 0
    staging = Path(config['state_dir']) / 'staging'
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for kind in ('events', 'advice'):
            # Stream rows rather than copying the live SQLite database or its WAL.
            for rid, created, payload in source.execute('SELECT id,created_at,payload FROM '+kind+' ORDER BY rowid'):
                sha = hashlib.sha256(payload).hexdigest()
                old = state.execute('SELECT payload_hash FROM exported WHERE kind=? AND record_id=?', (kind, rid)).fetchone()
                if old and old[0] == sha:
                    continue
                value = json.loads(dpapi(bytes(payload), decrypt=True, entropy=b'mouchen-desktop-v1').decode('utf-8'))
                envelope = {'schema': 'mouchen-desktop-record/v1', 'kind': kind,
                            'record_id': rid, 'created_at': created, 'payload': value}
                key = hashlib.sha256((kind+'\0'+rid+'\0'+sha).encode()).hexdigest()
                path = staging / (key + '.json')
                write_json(path, envelope)
                try:
                    result = relay.push(path, config.get('targets', ['phone']), category=kind,
                                        key=key, metadata={'schema': 'mouchen-desktop-record/v1', 'kind': kind})
                    if result['state'] not in ('ready', 'delivered'):
                        raise RuntimeError('Record transfer has expired; manual resend required')
                    state.execute('INSERT OR REPLACE INTO exported VALUES(?,?,?,?,?)',
                                  (kind, rid, sha, result['id'], time.time()))
                    state.commit()
                    count += 1
                finally:
                    path.unlink(missing_ok=True)
                if count >= 100:
                    return count
    finally:
        source.close()
    return count


def push_outbox(config, relay, state):
    root = Path(config['outbox_dir'])
    root.mkdir(parents=True, exist_ok=True)
    sent = 0
    for path in root.iterdir():
        if not path.is_file() or path.is_symlink() or path.name.startswith('.') or path.suffix in ('.part', '.tmp'):
            continue
        stat = path.stat()
        if time.time() - stat.st_mtime < 5:
            continue
        sha = digest_file(path)
        if state.execute('SELECT 1 FROM sent_files WHERE path=? AND sha256=?', (str(path), sha)).fetchone():
            continue
        key = hashlib.sha256((str(path)+'\0'+sha).encode()).hexdigest()
        result = relay.push(path, config.get('targets', ['phone']), key=key)
        if result['state'] not in ('ready', 'delivered'):
            raise RuntimeError('Outbox transfer has expired; manual resend required')
        state.execute('INSERT INTO sent_files VALUES(?,?,?,?)', (str(path), sha, result['id'], time.time()))
        state.commit()
        sent += 1
        if sent >= 20:
            break
    return sent


def cycle(config):
    relay = Relay(config)
    state = connect_state(Path(config['state_dir']) / 'relay-local.sqlite3')
    try:
        remote = relay.request('GET', '/v1/status')
        def archived(path, item):
            state.execute('INSERT OR REPLACE INTO archive VALUES(?,?,?,?,?,?,?)',
                          (item['id'], str(path), item['category'], item['source'], item['sha256'],
                           item['created_at'], time.time()))
            state.commit()
        pulled = relay.pull(config['archive_dir'], archived)
        exported = export_existing(config, relay, state) if config.get('export_existing_records', False) else 0
        sent = push_outbox(config, relay, state)
        counts = {'events': 0, 'advice': 0}
        source_path = Path(config['source_database'])
        if config.get('export_existing_records', False) and source_path.is_file():
            with closing(sqlite3.connect(source_path.resolve().as_uri()+'?mode=ro',uri=True)) as source:
                counts = {kind: source.execute('SELECT count(*) FROM '+kind).fetchone()[0] for kind in counts}
        result = {'connected': True, 'checked_at': datetime.now(timezone.utc).isoformat(),
                  'downloaded': len(pulled), 'uploaded_records': exported, 'uploaded_files': sent,
                  'existing_records': counts, 'server': remote,
                  'archive_dir': config['archive_dir'], 'outbox_dir': config['outbox_dir'],
                  'original_app_analysis': 'unchanged; original backend is separate from this relay'}
        write_json(Path(config['state_dir'])/'status.json', result)
        return result
    finally:
        state.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(default_client_config_path()))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--watch', action='store_true')
    mode.add_argument('--pause', action='store_true', help='Ask the worker for this configuration to stop')
    args = parser.parse_args()
    config = load_client_config(args.config)
    state_dir = Path(config['state_dir'])
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.pause:
        (state_dir / 'stop-sync').write_text('paused', encoding='utf-8')
        print('Pause requested. An active transfer may finish before the worker stops.')
        return
    handler = RotatingFileHandler(state_dir/'sync.log', maxBytes=512000, backupCount=2, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    lock = (state_dir/'worker.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            logging.info('A sync worker is already running')
            lock.close()
            return
    else:
        import fcntl
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info('A sync worker is already running')
            lock.close()
            return
    try:
        (state_dir / 'stop-sync').unlink(missing_ok=True)
        while True:
            if (state_dir/'stop-sync').exists():
                logging.info('Sync paused by local stop marker')
                break
            try:
                result = cycle(config)
                logging.info('Sync connected; exported=%s sent=%s received=%s',
                             result['uploaded_records'], result['uploaded_files'], result['downloaded'])
                if not args.watch:
                    print(json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                error = f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                logging.error('Sync incomplete: %s; local source retained', error)
                write_json(state_dir/'status.json', {'connected': False, 'checked_at': datetime.now(timezone.utc).isoformat(),
                                                   'error': error, 'source_retained': True})
                if not args.watch:
                    raise
            if not args.watch:
                break
            time.sleep(30)
    finally:
        lock.close()


if __name__ == '__main__':
    main()
