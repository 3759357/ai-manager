"""Persistent instruction inbox. Recording a directive never executes an order."""
from __future__ import annotations

import json
import re
import uuid
from core.events import utc_now_iso


class InstructionStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS instructions (
                id TEXT PRIMARY KEY, message TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT,
                events TEXT NOT NULL)''')

    def submit(self, message, request_id=None):
        message = message.strip()
        if not message or len(message) > 4000 or any(ord(c) < 32 and c not in '\n\t' for c in message):
            raise ValueError('지시는 공백을 제외한 1~4000자이며 제어 문자를 포함할 수 없습니다.')
        request_id = request_id or uuid.uuid4().hex
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', request_id):
            raise ValueError('잘못된 지시 ID입니다.')
        now = utc_now_iso()
        events = json.dumps([dict(state='waiting', at=now, result=None)])
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO instructions VALUES (?,?,?,?,?,?,?)',
                       (request_id, message, 'waiting', now, now, None, events))
            row = db.execute('SELECT * FROM instructions WHERE id=?', (request_id,)).fetchone()
            if row['message'] != message:
                raise ValueError('같은 지시 ID에 다른 내용을 사용할 수 없습니다.')
        return self.get(request_id)

    @staticmethod
    def decode(row):
        if row is None:
            raise ValueError('지시를 찾을 수 없습니다.')
        result = dict(row)
        result['events'] = json.loads(result['events'])
        return result

    def get(self, request_id):
        with self.store.connect() as db:
            return self.decode(db.execute('SELECT * FROM instructions WHERE id=?', (request_id,)).fetchone())

    def list(self, limit=30):
        if not 1 <= limit <= 100:
            raise ValueError('limit은 1~100이어야 합니다.')
        with self.store.connect() as db:
            return [self.decode(r) for r in db.execute(
                'SELECT * FROM instructions ORDER BY created_at DESC,id DESC LIMIT ?', (limit,))]

    def transition(self, request_id, state, result=None):
        allowed = {'waiting': {'running', 'cancelled'}, 'running': {'completed', 'failed'}}
        if state in {'completed', 'failed'} and not (result and result.strip()):
            raise ValueError('완료·실패 상태에는 결과 내용이 필요합니다.')
        if result is not None and len(result) > 8000:
            raise ValueError('결과는 8000자 이하여야 합니다.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = self.decode(db.execute('SELECT * FROM instructions WHERE id=?', (request_id,)).fetchone())
            if state == item['state'] and result == item['result']:
                return item
            if state not in allowed.get(item['state'], set()):
                raise ValueError('허용되지 않는 상태 변경입니다.')
            now = utc_now_iso()
            item['events'].append(dict(state=state, at=now, result=result))
            db.execute('UPDATE instructions SET state=?,updated_at=?,result=?,events=? WHERE id=?',
                       (state, now, result, json.dumps(item['events'], ensure_ascii=False), request_id))
        return self.get(request_id)
