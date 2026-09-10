"""Bounded application messages carried by the generic temporary file relay."""
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import UUID, uuid5, NAMESPACE_URL

SCHEMA = 'ai-twin.sync/v1'
PROJECT = 'ai-twin-sync-v1'
MAX_BYTES = 512 * 1024
OPERATIONS = {'goal.create', 'event.create', 'feedback.create', 'snapshot.get'}


def encode(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')
    if len(data) > MAX_BYTES:
        raise ValueError('Sync message exceeds 512 KiB')
    return data


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def validate(value, *, kind, sender):
    encode(value)
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('kind') != kind:
        raise ValueError('Unsupported sync envelope')
    if (not isinstance(sender, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', sender)
            or value.get('sender') != sender):
        raise ValueError('Sync sender does not match the authenticated relay source')
    uid = value.get('message_id')
    if not isinstance(uid, str) or str(UUID(uid)) != uid:
        raise ValueError('A canonical message UUID is required')
    created = value.get('created_at')
    if not isinstance(created, str) or datetime.fromisoformat(created.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('A timezone-aware timestamp is required')
    if value.get('operation') not in OPERATIONS:
        raise ValueError('Unsupported sync operation')
    if kind == 'request':
        if not isinstance(value.get('body'), dict):
            raise ValueError('A request body object is required')
    else:
        reply = value.get('in_reply_to')
        if not isinstance(reply, str) or str(UUID(reply)) != reply:
            raise ValueError('A canonical request UUID is required')
        if type(value.get('ok')) is not bool:
            raise ValueError('Response status must be boolean')
        if value['ok']:
            result = value.get('result')
            if not isinstance(result, dict):
                raise ValueError('Response result must be an object')
            for key in ('goals', 'advice'):
                if not isinstance(result.get(key), list) or len(result[key]) > 100:
                    raise ValueError('Response snapshot is bounded to 100 items per collection')
        elif not isinstance(value.get('error'), dict):
            raise ValueError('Failed response requires an error object')
    return value


def error_response(request, sender, code):
    return {
        'schema': SCHEMA, 'kind': 'response',
        'message_id': str(uuid5(NAMESPACE_URL, 'ai-twin-error/' + request['sender'] + '/' + request['message_id'])),
        'sender': sender, 'created_at': datetime.now(timezone.utc).isoformat(),
        'in_reply_to': request['message_id'], 'operation': request['operation'], 'ok': False,
        'error': {'code': code, 'message': '电脑未能导入这条请求，请检查内容后重新提交。'},
    }
