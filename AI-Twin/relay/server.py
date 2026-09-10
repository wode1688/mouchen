"""Bounded temporary transfer store. Content processing belongs to clients."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

CONFIG = json.loads(Path(os.environ.get('RELAY_CONFIG', '/etc/ai-relay/config.json')).read_text(encoding='utf-8-sig'))
ROOT = Path(CONFIG.get('data_dir', '/var/lib/ai-relay'))
BLOBS = ROOT / 'blobs'
DB = ROOT / 'relay.sqlite3'
QUOTA = CONFIG.get('quota_bytes', 2 * 1024**3)
MAX_FILE = CONFIG.get('max_file_bytes', 100 * 1024**2)
MIN_FREE = CONFIG.get('minimum_free_bytes', 2 * 1024**3)
DEFAULT_TTL = CONFIG.get('default_ttl_hours', 24)
MAX_TTL = CONFIG.get('max_ttl_hours', 72)
RECORD_DAYS = CONFIG.get('record_retention_days', 7)
RECORD_LIMIT = CONFIG.get('completed_record_limit', 10000)
LEASE_SECONDS = 900
LIVE = ('uploading', 'writing', 'ready')
log = logging.getLogger('ai-relay')


@contextmanager
def db(write=False):
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=10000')
    try:
        if write:
            con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def initialize():
    BLOBS.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute('PRAGMA auto_vacuum=INCREMENTAL')
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA journal_size_limit=4194304')
        con.execute('PRAGMA max_page_count=16384')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS transfers(
            id TEXT PRIMARY KEY, source TEXT NOT NULL, request_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL, filename TEXT NOT NULL, project TEXT NOT NULL,
            category TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
            metadata TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
            upload_deadline REAL NOT NULL, state TEXT NOT NULL, completed_at REAL,
            UNIQUE(source,request_key));
        CREATE TABLE IF NOT EXISTS deliveries(
            transfer_id TEXT NOT NULL REFERENCES transfers(id) ON DELETE CASCADE,
            device TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            lease_token TEXT, lease_until REAL, downloaded INTEGER NOT NULL DEFAULT 0,
            acked_at REAL, PRIMARY KEY(transfer_id,device));
        CREATE INDEX IF NOT EXISTS deliveries_device ON deliveries(device,state);
        CREATE INDEX IF NOT EXISTS transfers_expiry ON transfers(state,expires_at);
        ''')
        # Only one service worker runs. Interrupted uploads may be retried.
        con.execute("UPDATE transfers SET state='uploading' WHERE state='writing'")
        for row in con.execute("SELECT id FROM transfers WHERE state='uploading'"):
            (BLOBS / row['id']).unlink(missing_ok=True)
    for path in BLOBS.glob('*.part'):
        path.unlink(missing_ok=True)
    cleanup()


def cleanup():
    now = time.time()
    with db(write=True) as con:
        con.execute('''UPDATE transfers SET state='expired', completed_at=?
                       WHERE state IN ('uploading','writing','ready') AND
                       (expires_at<=? OR (state IN ('uploading','writing') AND upload_deadline<=?))''',
                    (now, now, now))
        terminal = con.execute("SELECT id FROM transfers WHERE state IN ('expired','delivered','cancelled')").fetchall()
        for row in terminal:
            (BLOBS / row['id']).unlink(missing_ok=True)
            (BLOBS / (row['id'] + '.part')).unlink(missing_ok=True)
        con.execute('DELETE FROM transfers WHERE completed_at<?', (now - RECORD_DAYS * 86400,))
        con.execute('''DELETE FROM transfers WHERE id IN
                       (SELECT id FROM transfers WHERE completed_at IS NOT NULL
                        ORDER BY completed_at DESC LIMIT -1 OFFSET ?)''', (RECORD_LIMIT,))
        active = {row[0] for row in con.execute('SELECT id FROM transfers')}
    for path in BLOBS.iterdir():
        if path.is_file() and path.name.removesuffix('.part') not in active and path.stat().st_mtime < now - 1200:
            path.unlink()
    with db() as con:
        con.execute('PRAGMA incremental_vacuum(128)')
        con.execute('PRAGMA wal_checkpoint(PASSIVE)')


async def janitor():
    while True:
        await asyncio.sleep(60)
        try:
            cleanup()
        except Exception:
            log.exception('Temporary store cleanup failed')


@asynccontextmanager
async def lifespan(app):
    initialize()
    task = asyncio.create_task(janitor())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title='AI Twin Temporary Relay', version='1.0.0', docs_url=None,
              redoc_url=None, openapi_url=None, lifespan=lifespan)


class BodyLimit:
    """Bound metadata bodies before JSON parsing; stream file bodies separately."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        is_content = scope['method'] == 'PUT' and scope['path'].endswith('/content')
        limit = MAX_FILE if is_content else 16384
        total = 0
        async def bounded():
            nonlocal total
            message = await receive()
            total += len(message.get('body', b''))
            if total > limit:
                raise HTTPException(413, 'Request body is too large')
            return message
        headers = dict(scope['headers'])
        try:
            size = int(headers.get(b'content-length', b'0'))
        except ValueError:
            return await JSONResponse({'detail': 'Invalid Content-Length'}, 400)(scope, receive, send)
        if size > limit or size < 0:
            return await JSONResponse({'detail': 'Request body is too large'}, 413)(scope, receive, send)
        await self.app(scope, bounded, send)


app.add_middleware(BodyLimit)


def device(authorization: str = Header(default='')):
    if not authorization.startswith('Bearer ') or len(authorization) > 256:
        raise HTTPException(401, 'Device credential required')
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    for name, expected in CONFIG['devices'].items():
        if hmac.compare_digest(digest, expected):
            return name
    raise HTTPException(401, 'Invalid device credential')


class Transfer(BaseModel):
    filename: str = Field(min_length=1, max_length=160)
    project: str = Field(default='ai-twin', pattern=r'^[a-zA-Z0-9_-]{1,48}$')
    category: str = Field(default='files', pattern=r'^[a-zA-Z0-9_-]{1,48}$')
    size: int = Field(ge=0, le=MAX_FILE)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    targets: list[str] = Field(min_length=1, max_length=8)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[a-zA-Z0-9_.:-]+$')
    ttl_hours: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    metadata: dict = Field(default_factory=dict)

    @field_validator('filename')
    @classmethod
    def safe_filename(cls, name):
        if name in ('.', '..') or re.search(r'[\\/\x00-\x1f\x7f]', name):
            raise ValueError('A plain filename is required')
        return name

    @field_validator('metadata')
    @classmethod
    def small_metadata(cls, metadata):
        if len(json.dumps(metadata, ensure_ascii=False).encode()) > 4096:
            raise ValueError('Metadata exceeds 4096 bytes')
        return metadata

    @field_validator('targets')
    @classmethod
    def known_targets(cls, targets):
        if any(t not in CONFIG['devices'] for t in targets):
            raise ValueError('Unknown destination device')
        return sorted(set(targets))


class Receipt(BaseModel):
    lease_token: str = Field(min_length=20, max_length=100)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def transfer_row(con, transfer_id):
    row = con.execute('SELECT * FROM transfers WHERE id=?', (transfer_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Transfer not found')
    return row


def public(row):
    return {k: row[k] for k in ('id', 'source', 'filename', 'project', 'category', 'size',
                               'sha256', 'created_at', 'expires_at', 'state')} | {'metadata': json.loads(row['metadata'])}


def live(row):
    if row['state'] in ('expired', 'cancelled') or row['expires_at'] <= time.time():
        raise HTTPException(410, 'Transfer expired or cancelled; resend from the source')


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'ai-twin-relay', 'version': '1.0.0'}


@app.get('/v1/status')
def status(who=Depends(device)):
    with db() as con:
        size, count = con.execute("SELECT coalesce(sum(size),0),count(*) FROM transfers WHERE state IN ('uploading','writing','ready')").fetchone()
        pending = con.execute('''SELECT count(*) FROM deliveries d JOIN transfers t ON t.id=d.transfer_id
                                 WHERE d.device=? AND d.state!='acked' AND t.state='ready' AND t.expires_at>?''',
                              (who, time.time())).fetchone()[0]
    return {'device': who, 'reserved_bytes': size, 'pending_transfers': count,
            'inbox_count': pending, 'quota_bytes': QUOTA, 'max_file_bytes': MAX_FILE,
            'default_ttl_hours': DEFAULT_TTL, 'max_ttl_hours': MAX_TTL,
            'record_retention_days': RECORD_DAYS, 'completed_record_limit': RECORD_LIMIT,
            'devices': list(CONFIG['devices'])}


@app.post('/v1/transfers')
def reserve(spec: Transfer, who=Depends(device)):
    cleanup()
    fingerprint = hashlib.sha256(json.dumps(spec.model_dump(), sort_keys=True).encode()).hexdigest()
    now = time.time()
    with db(write=True) as con:
        old = con.execute('SELECT * FROM transfers WHERE source=? AND request_key=?', (who, spec.idempotency_key)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise HTTPException(409, 'Idempotency key was already used for different content')
            return public(old)
        used, count = con.execute("SELECT coalesce(sum(size),0),count(*) FROM transfers WHERE state IN ('uploading','writing','ready')").fetchone()
        if used + spec.size > QUOTA or count >= 5000 or con.execute('SELECT count(*) FROM transfers').fetchone()[0] >= 20000:
            raise HTTPException(507, 'Temporary store limit reached; retain source and retry later')
        if shutil.disk_usage(ROOT).free < MIN_FREE + used + spec.size:
            raise HTTPException(507, 'Reserved disk headroom reached')
        uid = uuid.uuid4().hex
        con.execute('''INSERT INTO transfers(id,source,request_key,fingerprint,filename,project,category,size,sha256,
                       metadata,created_at,expires_at,upload_deadline,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (uid, who, spec.idempotency_key, fingerprint, spec.filename, spec.project, spec.category,
                     spec.size, spec.sha256, json.dumps(spec.metadata, ensure_ascii=False), now,
                     now + spec.ttl_hours * 3600, now + 900, 'uploading'))
        con.executemany('INSERT INTO deliveries(transfer_id,device) VALUES(?,?)', [(uid, t) for t in spec.targets])
        return public(transfer_row(con, uid))


@app.get('/v1/transfers/{uid}')
def describe(uid: str, who=Depends(device)):
    with db() as con:
        row = transfer_row(con, uid)
        targets = con.execute('SELECT device,state,acked_at FROM deliveries WHERE transfer_id=?', (uid,)).fetchall()
        if who != row['source'] and who not in [r['device'] for r in targets]:
            raise HTTPException(404, 'Transfer not found')
        return public(row) | {'deliveries': [dict(r) for r in targets]}


@app.put('/v1/transfers/{uid}/content')
async def upload(uid: str, request: Request, who=Depends(device)):
    with db(write=True) as con:
        row = transfer_row(con, uid)
        if who != row['source']:
            raise HTTPException(404, 'Transfer not found')
        live(row)
        if row['state'] != 'uploading':
            raise HTTPException(409, 'Transfer is already uploaded or has an active upload')
        if row['upload_deadline'] <= time.time():
            raise HTTPException(410, 'Upload reservation expired; create a new transfer')
        con.execute("UPDATE transfers SET state='writing' WHERE id=?", (uid,))
    part = BLOBS / (uid + '.part')
    destination = BLOBS / uid
    try:
        digest, received = hashlib.sha256(), 0
        async with asyncio.timeout(600):
            with part.open('wb') as handle:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > row['size']:
                        raise HTTPException(413, 'Uploaded content exceeds its declared size')
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if received != row['size'] or digest.hexdigest() != row['sha256']:
            raise HTTPException(422, 'Size or checksum mismatch; source must retry')
        with db(write=True) as con:
            current = transfer_row(con, uid)
            live(current)
            if current['state'] != 'writing':
                raise HTTPException(409, 'Upload reservation no longer active')
            part.replace(destination)
            if os.name == 'posix':
                directory = os.open(BLOBS, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            con.execute("UPDATE transfers SET state='ready' WHERE id=?", (uid,))
        return {'id': uid, 'state': 'ready', 'sha256': digest.hexdigest()}
    except BaseException:
        part.unlink(missing_ok=True)
        with db(write=True) as con:
            con.execute("UPDATE transfers SET state='uploading' WHERE id=? AND state='writing'", (uid,))
        raise


@app.get('/v1/inbox')
def inbox(who=Depends(device)):
    now = time.time()
    with db() as con:
        rows = con.execute('''SELECT t.* FROM transfers t JOIN deliveries d ON d.transfer_id=t.id
                              WHERE d.device=? AND t.state='ready' AND t.expires_at>?
                              AND (d.state='pending' OR (d.state='leased' AND d.lease_until<=?))
                              ORDER BY t.created_at LIMIT 100''', (who, now, now)).fetchall()
        return {'items': [public(row) for row in rows]}


@app.post('/v1/transfers/{uid}/claim')
def claim(uid: str, who=Depends(device)):
    with db(write=True) as con:
        row = transfer_row(con, uid)
        target = con.execute('SELECT * FROM deliveries WHERE transfer_id=? AND device=?', (uid, who)).fetchone()
        if target is None:
            raise HTTPException(404, 'Transfer not found')
        live(row)
        if row['state'] != 'ready' or target['state'] == 'acked':
            raise HTTPException(409, 'Transfer is not pending')
        if target['state'] == 'leased' and target['lease_until'] > time.time():
            raise HTTPException(409, 'Transfer is being received; retry after the lease expires')
        token = secrets.token_urlsafe(32)
        until = min(time.time() + LEASE_SECONDS, row['expires_at'])
        con.execute("UPDATE deliveries SET state='leased',lease_token=?,lease_until=?,downloaded=0 WHERE transfer_id=? AND device=?",
                    (token, until, uid, who))
        return public(row) | {'lease_token': token, 'lease_until': until}


def verify_lease(con, uid, who, token):
    target = con.execute('SELECT * FROM deliveries WHERE transfer_id=? AND device=?', (uid, who)).fetchone()
    if target is None:
        raise HTTPException(404, 'Transfer not found')
    if not target['lease_token'] or not hmac.compare_digest(target['lease_token'], token):
        raise HTTPException(409, 'Invalid receipt token')
    return target


@app.get('/v1/transfers/{uid}/content')
def download(uid: str, x_claim_token: str = Header(default=''), who=Depends(device)):
    with db() as con:
        row = transfer_row(con, uid)
        target = verify_lease(con, uid, who, x_claim_token)
        live(row)
        if row['state'] != 'ready' or target['state'] != 'leased' or target['lease_until'] <= time.time():
            raise HTTPException(409, 'A current receive lease is required')
        handle = (BLOBS / uid).open('rb')
    def chunks():
        try:
            while chunk := handle.read(1024 * 1024):
                yield chunk
            with db(write=True) as con:
                con.execute('''UPDATE deliveries SET downloaded=1 WHERE transfer_id=? AND device=?
                               AND lease_token=? AND state='leased' ''', (uid, who, x_claim_token))
        finally:
            handle.close()
    return StreamingResponse(chunks(), media_type='application/octet-stream',
                             headers={'Content-Length': str(row['size']), 'X-Content-SHA256': row['sha256'],
                                      'Cache-Control': 'no-store'})


@app.post('/v1/transfers/{uid}/ack')
def acknowledge(uid: str, receipt: Receipt, who=Depends(device)):
    now = time.time()
    with db(write=True) as con:
        row = transfer_row(con, uid)
        target = verify_lease(con, uid, who, receipt.lease_token)
        if not hmac.compare_digest(row['sha256'], receipt.sha256):
            raise HTTPException(422, 'Checksum mismatch; server copy retained')
        if target['state'] == 'acked':
            result = {'id': uid, 'acknowledged': True, 'state': row['state']}
        else:
            live(row)
            if row['state'] != 'ready' or target['lease_until'] <= now or not target['downloaded']:
                raise HTTPException(409, 'Complete the download under a current lease before confirming')
            con.execute("UPDATE deliveries SET state='acked',acked_at=? WHERE transfer_id=? AND device=?", (now, uid, who))
            remaining = con.execute("SELECT count(*) FROM deliveries WHERE transfer_id=? AND state!='acked'", (uid,)).fetchone()[0]
            if not remaining:
                con.execute("UPDATE transfers SET state='delivered',completed_at=? WHERE id=?", (now, uid))
            result = {'id': uid, 'acknowledged': True, 'state': 'ready' if remaining else 'delivered'}
    if result['state'] == 'delivered':
        (BLOBS / uid).unlink(missing_ok=True)
    return result


@app.post('/v1/transfers/{uid}/release')
def release(uid: str, receipt: Receipt, who=Depends(device)):
    with db(write=True) as con:
        target = verify_lease(con, uid, who, receipt.lease_token)
        if target['state'] == 'leased':
            con.execute("UPDATE deliveries SET state='pending',lease_token=NULL,lease_until=NULL,downloaded=0 WHERE transfer_id=? AND device=?", (uid, who))
    return {'released': True}
