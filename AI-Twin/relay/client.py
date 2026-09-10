"""Standard-library relay client. Archive locally before acknowledging delivery."""
import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from configuration import default_client_config_path, load_client_config


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, decrypt=False, entropy=b'ai-twin-relay-v1'):
    if os.name != 'nt':
        raise RuntimeError('This protected credential belongs to a Windows user')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    def blob(value):
        buffer = ctypes.create_string_buffer(value)
        return Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer
    source, keep_source = blob(data)
    extra, keep_extra = blob(entropy)
    result = Blob()
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        second = None
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        second = 'AI Twin Relay'
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), second, ctypes.byref(extra), None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(result.pbData)


def digest_file(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name('.tmp-' + uuid.uuid4().hex[:16])
    with staging.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    staging.replace(path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Relay:
    def __init__(self, config):
        self.config = config
        self.url = config['url'].rstrip('/')
        parsed = urllib.parse.urlsplit(self.url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('An HTTPS origin without credentials is required')
        self.token = config.get('token') or dpapi(base64.b64decode(config['token_dpapi']), decrypt=True).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def open(self, method, path, body=None, headers=None):
        request = urllib.request.Request(self.url + path, data=body, method=method,
                    headers={'Authorization': 'Bearer ' + self.token} | (headers or {}))
        return self.opener.open(request, timeout=180)

    def request(self, method, path, value=None):
        body = None if value is None else json.dumps(value, ensure_ascii=False).encode()
        with self.open(method, path, body, {'Content-Type': 'application/json'}) as response:
            return json.load(response)

    def push(self, path, targets, category='files', project='ai-twin', key=None, metadata=None):
        path = Path(path)
        sha = digest_file(path)
        request = {'filename': path.name, 'size': path.stat().st_size, 'sha256': sha,
                   'targets': targets, 'category': category, 'project': project,
                   'idempotency_key': key or uuid.uuid4().hex, 'metadata': metadata or {}}
        item = self.request('POST', '/v1/transfers', request)
        if item['state'] == 'uploading':
            with path.open('rb') as handle:
                with self.open('PUT', f"/v1/transfers/{item['id']}/content", handle,
                               {'Content-Type': 'application/octet-stream', 'Content-Length': str(request['size'])}) as response:
                    item.update(json.load(response))
        return item

    def pull(self, archive, on_saved=None):
        archive = Path(archive).resolve()
        archive.mkdir(parents=True, exist_ok=True)
        received = []
        for item in self.request('GET', '/v1/inbox')['items']:
            uid = item['id']
            if not re.fullmatch('[0-9a-f]{32}', uid):
                raise ValueError('Invalid transfer identifier')
            try:
                claim = self.request('POST', f'/v1/transfers/{uid}/claim')
            except urllib.error.HTTPError as exc:
                if exc.code in (409, 410):
                    continue
                raise
            receipt = {'lease_token': claim['lease_token'], 'sha256': claim['sha256']}
            destination = None
            try:
                project, category = claim['project'], claim['category']
                if any(not re.fullmatch('[A-Za-z0-9_-]{1,48}', x) for x in (project, category)):
                    raise ValueError('Invalid archive category')
                date = datetime.fromtimestamp(claim['created_at'], timezone.utc).strftime('%Y-%m-%d')
                folder = (archive / project / category / date).resolve()
                if not folder.is_relative_to(archive):
                    raise ValueError('Archive destination is outside its configured folder')
                folder.mkdir(parents=True, exist_ok=True)
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', claim['filename']).rstrip(' .') or 'file'
                budget = min(140, 220 - len(str(folder)) - 34) if os.name == 'nt' else 140
                if budget < 16:
                    raise ValueError('Archive root is too long; choose a shorter local folder')
                suffix = Path(name).suffix[:16]
                if len(name) > budget:
                    name = name[:budget-len(suffix)] + suffix
                destination = folder / (uid + '_' + name)
                partial = folder / (uid + '.part')
                if not destination.exists() or digest_file(destination) != claim['sha256']:
                    sha, size = hashlib.sha256(), 0
                    try:
                        with self.open('GET', f'/v1/transfers/{uid}/content',
                                       headers={'X-Claim-Token': claim['lease_token']}) as response, partial.open('wb') as out:
                            while block := response.read(1024 * 1024):
                                size += len(block)
                                if size > claim['size']:
                                    raise ValueError('Unexpected download length')
                                out.write(block)
                                sha.update(block)
                            out.flush()
                            os.fsync(out.fileno())
                        if size != claim['size'] or sha.hexdigest() != claim['sha256']:
                            raise ValueError('Downloaded data failed checksum verification')
                        partial.replace(destination)
                    finally:
                        partial.unlink(missing_ok=True)
                else:
                    # A previous successful local save can outlive an interrupted receipt.
                    # Re-read under this lease so the server knows a full transfer was attempted.
                    with self.open('GET', f'/v1/transfers/{uid}/content',
                                   headers={'X-Claim-Token': claim['lease_token']}) as response:
                        check = hashlib.sha256()
                        while chunk := response.read(1024 * 1024):
                            check.update(chunk)
                        if check.hexdigest() != claim['sha256']:
                            raise ValueError('Retransmission checksum mismatch')
                manifest = {k: v for k, v in claim.items() if k != 'lease_token'}
                manifest['archived_at'] = datetime.now(timezone.utc).isoformat()
                write_json(folder / (uid + '.receipt.json'), manifest)
                if os.name == 'posix':
                    descriptor = os.open(folder, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                if on_saved:
                    on_saved(destination, manifest)
                for attempt in range(4):
                    try:
                        ack = self.request('POST', f'/v1/transfers/{uid}/ack', receipt)
                        break
                    except urllib.error.HTTPError as exc:
                        if exc.code != 409 or attempt == 3:
                            raise
                        time.sleep(0.25 * (attempt + 1))
                received.append({'file': str(destination), 'id': uid, 'server_state': ack['state']})
            except Exception:
                try:
                    self.request('POST', f'/v1/transfers/{uid}/release', receipt)
                except Exception:
                    pass
                raise
        return received


def main():
    parser = argparse.ArgumentParser(description='AI Twin secure temporary file relay')
    parser.add_argument('--config', default=str(default_client_config_path()))
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    push = sub.add_parser('push')
    push.add_argument('file')
    push.add_argument('--target', action='append', required=True)
    push.add_argument('--category', default='files')
    pull = sub.add_parser('pull')
    pull.add_argument('--archive')
    args = parser.parse_args()
    config = load_client_config(args.config)
    relay = Relay(config)
    if args.action == 'status':
        result = relay.request('GET', '/v1/status')
    elif args.action == 'push':
        result = relay.push(args.file, args.target, args.category)
    else:
        result = relay.pull(args.archive or config['archive_dir'])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
