"""APNs outbox. Payloads contain only a generic alert and an opaque instruction ID."""
from __future__ import annotations
import json
import os
import re
import time
import uuid
from core.events import utc_now_iso


class PushStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            from core.instructions import InstructionStore
            InstructionStore(store)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS push_device (id TEXT PRIMARY KEY, token TEXT NOT NULL, environment TEXT NOT NULL, active INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS push_events (id TEXT PRIMARY KEY, instruction_id TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS push_delivery (event_id TEXT NOT NULL, device_id TEXT NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, error TEXT,
                    PRIMARY KEY(event_id,device_id));
            ''')

    def register(self, device_id, token, environment):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', device_id) or not re.fullmatch(r'[a-fA-F0-9]{32,512}', token) or len(token) % 2 or environment not in {'sandbox', 'production'}:
            raise ValueError('올바르지 않은 기기 등록입니다.')
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO push_device VALUES (?,?,?,1)', (device_id, token.lower(), environment))
        return {'registered': True}

    def unregister(self, device_id):
        with self.store.connect() as db:
            db.execute('UPDATE push_device SET active=0 WHERE id=?', (device_id,))
        return {'registered': False}

    def collect(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT id,state,updated_at FROM instructions WHERE state IN ('completed','failed')").fetchall():
                event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ai-manager:{row['id']}:{row['state']}"))
                inserted = db.execute('INSERT OR IGNORE INTO push_events VALUES (?,?,?,?)', (event_id, row['id'], row['state'], row['updated_at'])).rowcount
                if inserted:
                    db.execute("INSERT OR IGNORE INTO push_delivery(event_id,device_id,state) SELECT ?,id,'pending' FROM push_device WHERE active=1", (event_id,))
            has_fills = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_fills'").fetchone()
            if has_fills:
                for row in db.execute('SELECT id,instruction_id,created_at FROM paper_fills').fetchall():
                    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'ai-manager:paper-fill:' + row['id']))
                    inserted = db.execute('INSERT OR IGNORE INTO push_events VALUES (?,?,?,?)', (event_id, row['instruction_id'], 'paper_fill', row['created_at'])).rowcount
                    if inserted:
                        db.execute("INSERT OR IGNORE INTO push_delivery(event_id,device_id,state) SELECT ?,id,'pending' FROM push_device WHERE active=1", (event_id,))

    def deliver_once(self, sender):
        self.collect()
        with self.store.connect() as db:
            row = db.execute('''SELECT d.*,e.instruction_id,e.kind,p.token,p.environment FROM push_delivery d
                JOIN push_events e ON e.id=d.event_id JOIN push_device p ON p.id=d.device_id
                WHERE d.state='pending' AND d.next_attempt<=? AND p.active=1 ORDER BY e.created_at LIMIT 1''', (time.time(),)).fetchone()
        if row is None:
            return {'push': 'idle'}
        payload = {'aps': {'alert': {'title': 'AI Manager', 'body': '투자 지시 처리 결과가 도착했습니다. 앱에서 확인하세요.'}, 'sound': 'default'},
                   'instruction_id': row['instruction_id'], 'event_id': row['event_id']}
        if row['kind'] == 'paper_fill':
            payload['aps']['alert']['body'] = '모의 거래가 체결되었습니다. 앱에서 확인하세요.'
        try:
            status, reason = sender(row['token'], row['environment'], payload, row['event_id'])
        except Exception:
            status, reason = 503, 'TransportError'
        with self.store.connect() as db:
            if status == 200:
                db.execute("UPDATE push_delivery SET state='sent',error=NULL WHERE event_id=? AND device_id=?", (row['event_id'], row['device_id']))
            else:
                permanent = status in {400, 410, 413}
                if status == 410 or reason in {'BadDeviceToken', 'DeviceTokenNotForTopic'}:
                    db.execute('UPDATE push_device SET active=0 WHERE id=? AND token=?', (row['device_id'], row['token']))
                attempts = row['attempts'] + 1
                db.execute('UPDATE push_delivery SET state=?,attempts=?,next_attempt=?,error=? WHERE event_id=? AND device_id=?',
                           ('failed' if permanent or attempts >= 12 else 'pending', attempts,
                            time.time() + min(3600, 10 * 2 ** min(attempts, 8)), str(status), row['event_id'], row['device_id']))
        return {'push': 'sent' if status == 200 else 'retry_or_failed'}

    def status(self):
        with self.store.connect() as db:
            counts = {r['state']: r['n'] for r in db.execute('SELECT state,count(*) n FROM push_delivery GROUP BY state')}
            active = db.execute('SELECT count(*) FROM push_device WHERE active=1').fetchone()[0]
        return {'configured': APNsSender.configured(), 'active_devices': active, 'delivery_counts': counts}


class APNsSender:
    def __init__(self):
        self.token = None
        self.issued = 0
        self.client = None

    @staticmethod
    def configured():
        return all(os.getenv(k) for k in ('AI_MANAGER_APNS_TEAM_ID', 'AI_MANAGER_APNS_KEY_ID', 'AI_MANAGER_APNS_KEY_PATH', 'AI_MANAGER_APNS_TOPIC')) and os.path.isfile(os.getenv('AI_MANAGER_APNS_KEY_PATH', ''))

    def __call__(self, device_token, environment, payload, event_id):
        import httpx
        import jwt
        if not self.configured():
            raise ValueError('APNs configuration missing')
        if not self.token or time.time() - self.issued > 3000:
            self.issued = int(time.time())
            with open(os.environ['AI_MANAGER_APNS_KEY_PATH']) as key:
                self.token = jwt.encode({'iss': os.environ['AI_MANAGER_APNS_TEAM_ID'], 'iat': self.issued}, key.read(), algorithm='ES256', headers={'kid': os.environ['AI_MANAGER_APNS_KEY_ID']})
        if self.client is None:
            self.client = httpx.Client(http2=True, timeout=10)
        host = 'api.sandbox.push.apple.com' if environment == 'sandbox' else 'api.push.apple.com'
        response = self.client.post(f'https://{host}/3/device/{device_token}', json=payload, headers={
            'authorization': f'bearer {self.token}', 'apns-topic': os.environ['AI_MANAGER_APNS_TOPIC'],
            'apns-push-type': 'alert', 'apns-priority': '10', 'apns-id': event_id,
            'apns-collapse-id': event_id, 'apns-expiration': str(int(time.time()) + 86400)})
        reason = ''
        if response.status_code != 200:
            try:
                reason = response.json().get('reason', '')
            except ValueError:
                pass
        if reason == 'ExpiredProviderToken':
            self.token = None
        return response.status_code, reason
