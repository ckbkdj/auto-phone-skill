"""One observed action per call; SQLite receipts survive Skill/MCP process restarts."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid

from .contracts import COMMANDS, DECISION, Fault, dumps, loads, result, validate
from .phone import Phone, conditions_met, find_node, node_risk, public_screen

TERMINAL = {'succeeded', 'failed', 'cancelled', 'initialization_failed'}
WAITING = {'waiting_handoff', 'outcome_unknown'}


class Store:
    def __init__(self, home):
        self.db = sqlite3.connect(str(home / 'state.sqlite3'), timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(task TEXT, op TEXT, digest TEXT, status TEXT, response TEXT,
                PRIMARY KEY(task,op));
            CREATE TABLE IF NOT EXISTS sessions(device TEXT PRIMARY KEY, session TEXT NOT NULL);
        ''')
        if __import__('os').name != 'nt':
            (home / 'state.sqlite3').chmod(0o600)

    def close(self):
        self.db.close()

    def get(self, task_id):
        row = self.db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise Fault('TASK_NOT_FOUND')
        return loads(row[0], 2097152)

    def save(self, task):
        task['updated'] = time.time()
        self.db.execute('UPDATE tasks SET body=? WHERE id=?', (dumps(task), task['id']))

    def effect_free(self, task):
        return (task.get('steps', 0) == 0 and not task.get('receipts')
                and self.db.execute('SELECT 1 FROM operations WHERE task=? LIMIT 1', (task['id'],)).fetchone() is None)

    def reclaim_initializations(self, device_id):
        # Called only with the selected device lock. No observed/actionable task is reclaimed.
        with self.db:
            for (body,) in self.db.execute('SELECT body FROM tasks').fetchall():
                task = loads(body, 2097152)
                if (task['request']['device_id'] == device_id and task['status'] == 'active'
                        and task.get('observation') is None and self.effect_free(task)):
                    task['status'], task['init_error'] = 'initialization_failed', 'INITIALIZATION_INTERRUPTED'
                    self.save(task)

    def new(self, request, identity):
        with self.db:
            row = self.db.execute('SELECT body FROM tasks WHERE idem=?', (request['idempotency_key'],)).fetchone()
            digest = hashlib.sha256(dumps(request).encode()).hexdigest()
            if row:
                task = loads(row[0], 2097152)
                if task['request_digest'] != digest or task['identity'] != identity:
                    raise Fault('IDEMPOTENCY_CONFLICT')
                if task['status'] == 'initialization_failed':
                    for (body,) in self.db.execute('SELECT body FROM tasks WHERE id<>?', (task['id'],)):
                        other = loads(body, 2097152)
                        if (other['identity'] == identity or other['request']['device_id'] == request['device_id']) and other['status'] not in TERMINAL:
                            raise Fault('DEVICE_HAS_ACTIVE_TASK')
                return task, False
            # One unfinished owner for each private physical phone; no queued task cross-clicks.
            for (body,) in self.db.execute('SELECT body FROM tasks'):
                existing = loads(body, 2097152)
                if (existing['identity'] == identity or existing['request']['device_id'] == request['device_id']) and existing['status'] not in TERMINAL:
                    raise Fault('DEVICE_HAS_ACTIVE_TASK')
            task = {'id': uuid.uuid4().hex, 'request': request, 'request_digest': digest,
                    'identity': identity, 'status': 'active', 'observation': None,
                    'receipts': [], 'gate': None, 'steps': 0, 'seen': [],
                    'created': time.time(), 'updated': time.time()}
            self.db.execute('INSERT INTO tasks VALUES(?,?,?)',
                            (task['id'], request['idempotency_key'], dumps(task)))
            return task, True

    def operation(self, task_id, op):
        return self.db.execute('SELECT digest,status,response FROM operations WHERE task=? AND op=?', (task_id, op)).fetchone()

    def start(self, task, op, digest):
        with self.db:
            self.db.execute('INSERT INTO operations VALUES(?,?,?,?,?)', (task['id'], op, digest, 'started', None))
            task['status'] = 'executing'
            task['steps'] += 1
            task['gate'] = None
            self.save(task)

    def complete(self, task, op, status, response):
        with self.db:
            self.db.execute('UPDATE operations SET status=?,response=? WHERE task=? AND op=?',
                            (status, dumps(response), task['id'], op))
            self.save(task)

    def session(self, identity):
        row = self.db.execute('SELECT session FROM sessions WHERE device=?', (identity,)).fetchone()
        return row[0] if row else None

    def save_session(self, identity, session):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)', (identity, session))

    def prune(self):
        # Only terminal records; unresolved side effects never age out automatically.
        cutoff = time.time() - 7 * 86400
        with self.db:
            for task_id, body in self.db.execute('SELECT id,body FROM tasks').fetchall():
                task = loads(body, 2097152)
                if task['status'] in TERMINAL and task['updated'] < cutoff:
                    self.db.execute('DELETE FROM operations WHERE task=?', (task_id,))
                    self.db.execute('DELETE FROM tasks WHERE id=?', (task_id,))


def view(task, *, code='OK', ok=True, include_observation=True):
    fields = {'task_id': task['id'], 'status': task['status'],
              'goal': task['request']['goal'], 'receipts': task['receipts'][-3:]}
    if task['observation'] and include_observation:
        fields['observation'] = public_screen(task['observation'])
    if task['gate']:
        fields['gate'] = {k: task['gate'][k] for k in ('token', 'kind', 'message', 'expires_at')}
    if not ok:
        from .environment import hint
        fields['hint'] = hint(code)
    return result(code=code, ok=ok, **fields)


def gate(task, kind, message, *, decision=None, digest=None):
    task['status'] = {'confirmation': 'waiting_confirmation', 'handoff': 'waiting_handoff',
                      'unknown': 'outcome_unknown'}[kind]
    task['gate'] = {'token': secrets.token_urlsafe(24), 'kind': kind,
                    'message': message[:1024], 'expires_at': time.time() + 300,
                    'decision': decision, 'digest': digest,
                    'observation_id': task['observation']['id'] if task['observation'] else None}


class Runtime:
    def __init__(self, config, phone_factory=Phone):
        self.config, self.phone_factory = config, phone_factory
        self.store = Store(config.home)

    def close(self):
        self.store.close()

    def call(self, command, payload):
        try:
            if command not in COMMANDS:
                raise Fault('UNKNOWN_COMMAND')
            # In-process callers cannot bypass JSON types or Unicode limits.
            payload = loads(dumps(payload))
            validate(COMMANDS[command], payload)
            if command == 'doctor':
                report = self.config.doctor()
                return result(code='SETUP_REQUIRED' if report['issues'] else 'OK', report=report)
            if command == 'setup_status':
                from .environment import setup_status
                return result(setup=setup_status(self.config))
            if command == 'tasks':
                rows = self.store.db.execute('SELECT body FROM tasks ORDER BY rowid DESC LIMIT 16').fetchall()
                return result(tasks=[{'task_id': t['id'], 'device_id': t['request']['device_id'],
                    'status': t['status'], 'steps': t['steps']} for (body,) in rows
                    for t in [loads(body, 2097152)]])
            if command == 'prepare':
                from .environment import prepare, setup_status
                prepare(self.config, payload['device_id'])
                return result(setup=setup_status(self.config))
            if command == 'begin':
                if not payload['goal'].strip():
                    raise Fault('EMPTY_GOAL')
                device_id = payload['device_id']
            else:
                device_id = self.store.get(payload['task_id'])['request']['device_id']
            with self.config.lock(device_id):
                return self._call_locked(command, payload, device_id)
        except Fault as exc:
            from .environment import hint
            return result(ok=False, code=exc.code, hint=hint(exc.code))
        except Exception:
            # Never disclose Appium responses, model keys, stack traces, or endpoints.
            return result(ok=False, code='INTERNAL_ERROR')

    def _call_locked(self, command, payload, device_id):
        identity = self.config.identity(device_id)
        if command == 'begin':
            self.store.reclaim_initializations(device_id)
            task, created = self.store.new(payload, identity)
            if not created and not (task['status'] == 'initialization_failed' and self.store.effect_free(task)):
                return view(task, code='IDEMPOTENT_REPLAY')
            task['status'] = 'active'
            task.pop('init_error', None)
            with self.store.db:
                self.store.save(task)
            try:
                phone = self.phone_factory(self.config, device_id, self.store)
                task['observation'] = phone.observe()
            except Exception as exc:
                code = exc.code if isinstance(exc, Fault) else 'INITIALIZATION_FAILED'
                task['status'], task['init_error'] = 'initialization_failed', code
                with self.store.db:
                    self.store.save(task)
                return view(task, code=code, ok=False)
            with self.store.db:
                self.store.save(task)
            return view(task)
        task = self.store.get(payload['task_id'])
        if task['identity'] != identity:
            raise Fault('DEVICE_CONFIGURATION_CHANGED')
        # A dead process may have left a command in flight. No new command may dispatch.
        if task['status'] == 'executing':
            gate(task, 'unknown', '上次进程退出时动作结果未知。请检查手机，确认远端动作已结束后再恢复；不会重放。')
            with self.store.db:
                self.store.save(task)
        if command == 'status':
            return view(task, include_observation=False)
        if command == 'cancel':
            if task['status'] in TERMINAL - {'initialization_failed'}:
                return view(task)
            if task['status'] == 'outcome_unknown':
                return view(task, code='RECONCILE_REQUIRED', ok=False)
            task['status'], task['gate'] = 'cancelled', None
            with self.store.db:
                self.store.save(task)
            return view(task)
        if command == 'resume':
            pending = task['gate']
            if not pending or pending['kind'] not in {'handoff', 'unknown'} or time.time() > pending['expires_at'] or not secrets.compare_digest(pending['token'], payload['token']):
                raise Fault('INVALID_RESUME_TOKEN')
            phone = self.phone_factory(self.config, device_id, self.store)
            task['observation'] = phone.observe()
            task['status'], task['gate'] = 'needs_decision', None
            with self.store.db:
                self.store.save(task)
            return view(task, code='REOBSERVED_AFTER_USER_HANDOFF')
        if command == 'observe':
            phone = self.phone_factory(self.config, device_id, self.store)
            task['observation'] = phone.observe()
            if task['status'] == 'waiting_confirmation':
                task['status'], task['gate'] = 'needs_decision', None
            elif task['status'] in WAITING and task['gate']['expires_at'] < time.time():
                gate(task, task['gate']['kind'], task['gate']['message'])
            with self.store.db:
                self.store.save(task)
            return view(task)
        return self._step(task, payload, device_id)

    def _step(self, task, payload, device_id):
        decision = payload['decision']
        digest = hashlib.sha256(dumps({k: payload[k] for k in
            ('task_id', 'observation_id', 'operation_id', 'decision')}).encode()).hexdigest()
        op = payload['operation_id']
        existing = self.store.operation(task['id'], op)
        if existing:
            if existing[0] != digest:
                raise Fault('OPERATION_ID_CONFLICT')
            if existing[1] == 'started':
                gate(task, 'unknown', '动作曾开始但没有持久化完成记录；检查手机后人工恢复，不会重放。')
                with self.store.db:
                    self.store.save(task)
                return view(task, code='OUTCOME_UNKNOWN', ok=False)
            return loads(existing[2], 2097152)
        if task['status'] in TERMINAL or task['status'] in WAITING:
            return view(task, code='TASK_NOT_ACTIONABLE', ok=False)
        before = task['observation']
        if not before or payload['observation_id'] != before['id'] or time.time() - before['_captured'] > 60:
            raise Fault('STALE_OBSERVATION')
        if task['steps'] >= self.config.max_steps:
            gate(task, 'handoff', '已达到动作预算；请人工检查，不再自动执行。')
            with self.store.db:
                self.store.save(task)
            return view(task, code='STEP_LIMIT', ok=False)
        phone = self.phone_factory(self.config, device_id, self.store)
        live = phone.observe()
        if live['fingerprint'] != before['fingerprint']:
            task['observation'], task['gate'], task['status'] = live, None, 'needs_decision'
            with self.store.db:
                self.store.save(task)
            return view(task, code='SCREEN_CHANGED_REPLAN', ok=False)
        if decision['action'] == 'handoff':
            gate(task, 'handoff', decision['message'])
            with self.store.db:
                self.store.save(task)
            return view(task)
        if decision['action'] == 'finish':
            checks = [*task['request'].get('success', []), *decision['expect']]
            if not any(c['kind'] in {'text_present', 'package_is'} for c in checks) or not conditions_met(checks, live, before):
                raise Fault('FINISH_NOT_VERIFIED')
            task['status'], task['observation'], task['gate'] = 'succeeded', live, None
            response = view(task)
            with self.store.db:
                self.store.save(task)
                self.store.db.execute('INSERT INTO operations VALUES(?,?,?,?,?)', (task['id'], op, digest, 'done', dumps(response)))
            return response
        risk, message = node_risk(decision, live, self.config.confirm_all)
        if risk == 'handoff':
            gate(task, 'handoff', message)
            with self.store.db:
                self.store.save(task)
            return view(task)
        if risk == 'confirmation':
            pending = task['gate']
            approved = (pending and pending['kind'] == 'confirmation' and pending['digest'] == digest
                        and pending['observation_id'] == before['id'] and time.time() <= pending['expires_at']
                        and secrets.compare_digest(pending['token'], payload.get('confirmation_token', '')))
            if not approved:
                gate(task, 'confirmation', message, decision=decision, digest=digest)
                with self.store.db:
                    self.store.save(task)
                return view(task)
        elif task['gate']:
            raise Fault('PENDING_ACTION_MISMATCH')
        signature = hashlib.sha256(dumps([live['fingerprint'], decision]).encode()).hexdigest()
        limit = 3 if decision['action'] == 'wait' else 1
        if task['seen'].count(signature) >= limit:
            gate(task, 'handoff', '相同页面上的相同动作已执行过；拒绝盲目重复，请检查实际状态。')
            with self.store.db:
                self.store.save(task)
            return view(task, code='REPEATED_ACTION_BLOCKED', ok=False)
        # Resolve a unique current Android element BEFORE marking the action as dispatched.
        element_id = phone.prepare(decision, live)
        self.store.start(task, op, digest)
        started, dispatched, observed = time.perf_counter(), 0.0, 0.0
        try:
            phone.act(decision, element_id)
            dispatched = (time.perf_counter() - started) * 1000
            observe_start = time.perf_counter()
            after = phone.observe()
            # Verify expectations with bounded observation-only polling; never repeat mutation.
            expect = decision.get('expect', [])
            while expect and not conditions_met(expect, after, live) and time.perf_counter() - observe_start < 2.5:
                time.sleep(0.08)
                after = phone.observe()
            observed = (time.perf_counter() - observe_start) * 1000
            changed = after['fingerprint'] != live['fingerprint']
            outcome = 'verified' if expect and conditions_met(expect, after, live) else ('observed' if changed else 'no_change')
            task['status'], task['observation'] = 'needs_decision', after
            task['seen'] = (task['seen'] + [signature])[-200:]
            code = 'EXPECTATION_NOT_MET' if expect and not conditions_met(expect, after, live) else 'OK'
            task['receipts'] = (task['receipts'] + [{'operation_id': op, 'action': decision['action'],
                'outcome': outcome, 'dispatch_ms': round(dispatched, 3), 'observation_ms': round(observed, 3)}])[-3:]
            response = view(task, code=code, ok=code == 'OK')
            self.store.complete(task, op, outcome, response)
            return response
        except Exception:
            # ANY failure after dispatch is conservatively unknown, even if HTTP returned.
            gate(task, 'unknown', '动作可能已执行，但回执或后置观察未确认。请检查手机；不会重试或自动修复。')
            task['receipts'] = (task['receipts'] + [{'operation_id': op, 'action': decision['action'],
                'outcome': 'unknown', 'dispatch_ms': round((time.perf_counter()-started)*1000, 3),
                'observation_ms': 0.0}])[-3:]
            response = view(task, code='OUTCOME_UNKNOWN', ok=False)
            self.store.complete(task, op, 'unknown', response)
            return response
