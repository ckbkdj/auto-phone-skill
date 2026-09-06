from __future__ import annotations

import asyncio
import json
import socket
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from pydantic import ValidationError

from lobster_phone_agent.app import create_app
from lobster_phone_agent.bridge.contracts import GuardedParams, GuardedResult, validate_rpc_params
from lobster_phone_agent.bridge.protocol import RpcRequestMessage
from lobster_phone_agent.config import Settings
from lobster_phone_agent.errors import BridgeProtocolError
from lobster_phone_agent.skill.client import SkillBridgeClient
from lobster_phone_agent.skill.dispatcher import SkillRpcDispatcher
from lobster_phone_agent.skill.guarded import GuardedDispatcher
from lobster_phone_agent.skill.journal import OperationJournal
from lobster_phone_agent.skill.models import SkillConfig, SkillRuntimeConfig
from .fakes import FakeDevice, screen


class Pool:
    device_ids = ['phone']
    def __init__(self, phone, path):
        self.phone = phone
        self.settings = SkillRuntimeConfig(operation_journal_path=path)
    @asynccontextmanager
    async def lease(self, device_id):
        assert device_id == 'phone'
        yield self.phone


def payload(snapshot, operation='op-1'):
    x, y = snapshot.nodes[0].bounds.center
    return {'fingerprint': snapshot.fingerprint, 'method': 'tap',
            'params': {'x': x, 'y': y, 'operation_id': operation}}


@pytest.mark.parametrize('change', [
    {'method': 'shell'}, {'fingerprint': ''}, {'extra': 1},
    {'params': {'x': '1', 'y': 2, 'operation_id': 'a'}},
    {'params': {'x': True, 'y': 2, 'operation_id': 'a'}},
    {'params': {'x': 1, 'y': 2}},
    {'params': {'x': 1, 'y': 2, 'operation_id': 'a', 'script': 'x'}},
])
def test_guarded_contract_cannot_smuggle_untyped_or_unbound_commands(change):
    good = payload(screen('com.demo', {'text': '入口'}))
    with pytest.raises((ValidationError, BridgeProtocolError)):
        GuardedParams.model_validate({**good, **change})


@pytest.mark.parametrize('state,code', [('executed', 'outcome_unknown'), ('unknown', 'ok'), ('not_executed', 'ok')])
def test_receipt_consistency(state, code):
    with pytest.raises(ValidationError):
        GuardedResult(state=state, code=code)


@pytest.mark.asyncio
async def test_persistent_replay_and_conflicting_input(tmp_path):
    s = screen('com.demo', {'text': '入口'})
    phone = FakeDevice(states=[s])
    pool = Pool(phone, tmp_path / 'ops.sqlite3')
    d = GuardedDispatcher(pool, OperationJournal(pool.settings.operation_journal_path))
    replies = await asyncio.gather(*(d.dispatch('phone', payload(s)) for _ in range(10)))
    assert len(phone.actions) == 1
    assert replies == [{'state': 'executed', 'code': 'ok'}] * 10
    restarted = GuardedDispatcher(pool, OperationJournal(pool.settings.operation_journal_path))
    assert (await restarted.dispatch('phone', payload(s)))['state'] == 'executed'
    assert len(phone.actions) == 1
    changed = payload(s)
    changed['params']['x'] += 1
    with pytest.raises(BridgeProtocolError):
        await restarted.dispatch('phone', changed)


@pytest.mark.asyncio
async def test_crash_intent_fences_new_ids_until_local_reconciliation(tmp_path):
    s = screen('com.demo', {'text': '入口'})
    phone = FakeDevice(states=[s])
    pool = Pool(phone, tmp_path / 'ops.sqlite3')
    journal = OperationJournal(pool.settings.operation_journal_path)
    request = payload(s)
    digest = journal.digest(GuardedParams.model_validate(request).model_dump(mode='json'))
    assert journal.reserve('phone', 'op-1', digest)
    restarted = GuardedDispatcher(pool, OperationJournal(journal.path))
    assert (await restarted.dispatch('phone', request))['state'] == 'unknown'
    assert (await restarted.dispatch('phone', payload(s, 'new-id')))['state'] == 'unknown'
    assert phone.actions == []
    journal.reconcile('phone', 'op-1', executed=False)
    assert (await restarted.dispatch('phone', request))['code'] == 'reconciled_no_effect'
    assert (await restarted.dispatch('phone', payload(s, 'new-id')))['state'] == 'executed'
    assert len(phone.actions) == 1


@pytest.mark.asyncio
async def test_lost_ack_and_cancellation_leave_durable_fence(tmp_path):
    class Uncertain(FakeDevice):
        async def tap(self, x, y, *, operation_id=None):
            self.actions.append(('tap', None))
            raise RuntimeError('reply lost after effect')
    s = screen('com.demo', {'text': '入口'})
    phone = Uncertain(states=[s])
    pool = Pool(phone, tmp_path / 'ops.sqlite3')
    journal = OperationJournal(pool.settings.operation_journal_path)
    d = GuardedDispatcher(pool, journal)
    assert (await d.dispatch('phone', payload(s)))['state'] == 'unknown'
    assert (await d.dispatch('phone', payload(s, 'different')))['state'] == 'unknown'
    assert len(phone.actions) == 1 and journal.blocked('phone')


@pytest.mark.asyncio
async def test_private_fresh_screen_and_target_checks_prevent_old_click(tmp_path):
    a, b = {'text': '继续', 'bounds': '[0,0][100,100]'}, {'text': '删除', 'bounds': '[200,0][300,100]'}
    old, new = screen('com.demo', a, b), screen('com.demo', b, a)
    phone = FakeDevice(states=[new])
    pool = Pool(phone, tmp_path / 'ops.sqlite3')
    d = GuardedDispatcher(pool, OperationJournal(pool.settings.operation_journal_path))
    assert (await d.dispatch('phone', payload(old)))['code'] == 'stale_screen'
    invalid = payload(new)
    invalid['params']['x'] = 800
    assert (await d.dispatch('phone', invalid))['code'] == 'invalid_target'
    assert not phone.actions


def test_fingerprint_includes_case_punctuation_order_and_tail_nodes():
    first = screen('com.demo', {'text': 'Amount 1.00'})
    second = screen('com.demo', {'text': 'Amount 100'})
    assert first.fingerprint != second.fingerprint
    assert screen('com.demo', {'text': 'AbC'}).fingerprint != screen('com.demo', {'text': 'abc'}).fingerprint
    many = [{'text': f'item-{i}'} for i in range(400)]
    before = screen('com.demo', *many)
    many[-1] = {'text': 'changed'}
    assert before.fingerprint != screen('com.demo', *many).fingerprint


def test_guarded_rpc_roundtrip_preserves_validated_nested_params():
    body = payload(screen('com.demo', {'text': '入口'}))
    req = RpcRequestMessage(sequence=1, device_id='phone', method='guarded_action', params=body)
    roundtrip = RpcRequestMessage.model_validate_json(req.model_dump_json())
    assert validate_rpc_params('guarded_action', roundtrip.params) == body


@pytest.mark.asyncio
async def test_actual_http_websocket_journal_roundtrip_with_synthetic_phone(tmp_path):
    phone = FakeDevice(states=[screen('com.launcher'), screen('com.android.settings')],
                       installed_apps=[{'package': 'com.android.settings', 'name': '系统设置'}])
    pool = Pool(phone, tmp_path / 'ops.sqlite3')
    settings = Settings(api_key='a'*32, bridge_token='b'*32, llm_model=None,
                        artifact_dir=tmp_path, action_settle_ms=0,
                        snapshot_stable_interval_ms=1, snapshot_stable_timeout_ms=30)
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    config = SkillConfig(server_url=origin, allow_insecure_http=True, bridge_id='home',
        api_token='a'*32, bridge_token='b'*32, local_token='c'*32,
        devices=[{'id':'phone','udid':'fixture','appium_url':'http://127.0.0.1:4723'}])
    bridge = SkillBridgeClient(config, SkillRpcDispatcher(pool)); job = None
    try:
        for _ in range(150):
            if server.started: break
            await asyncio.sleep(.02)
        assert server.started
        job = asyncio.create_task(bridge.run_forever())
        await asyncio.wait_for(bridge.connected.wait(), 5)
        async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=8) as client:
            response = await client.post('/v1/execute?wait_seconds=5', headers={'Authorization':'Bearer '+'a'*32},
                json={'instruction':'打开系统设置','app_package':'com.android.settings','device':{'id':'phone','bridge_id':'home'}})
        assert response.status_code == 200
        task = response.json()
        assert task['status'] == 'succeeded', task
        assert task['result']['mode'] == 'single_step'
        assert phone.actions == [('launch','com.android.settings')]
        assert pool.settings.operation_journal_path.is_file()
    finally:
        await bridge.stop()
        if job:
            job.cancel(); await asyncio.gather(job, return_exceptions=True)
        server.should_exit = True
        await asyncio.to_thread(thread.join, 3)
        sock.close()

@pytest.mark.asyncio
async def test_cancellation_after_intent_does_not_drop_fence(tmp_path):
    entered = asyncio.Event()
    class Slow(FakeDevice):
        async def tap(self, x, y, *, operation_id=None):
            self.actions.append(('tap',None)); entered.set(); await asyncio.sleep(60)
    s = screen('com.demo', {'text':'入口'})
    phone = Slow(states=[s]); pool = Pool(phone,tmp_path/'ops.sqlite3')
    journal = OperationJournal(pool.settings.operation_journal_path)
    d = GuardedDispatcher(pool,journal)
    task = asyncio.create_task(d.dispatch('phone',payload(s)))
    await asyncio.wait_for(entered.wait(),1); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert journal.blocked('phone')
    assert (await d.dispatch('phone',payload(s,'after-reconnect')))['state'] == 'unknown'
    assert len(phone.actions) == 1


@pytest.mark.asyncio
async def test_private_bridge_refuses_legacy_unbound_mutations(tmp_path):
    class Deny:
        async def dispatch(self,*args): raise AssertionError('must not dispatch')
    config = SkillConfig(server_url='https://phone.example.com',bridge_id='home',
        api_token='a'*32,bridge_token='b'*32,local_token='c'*32,
        devices=[{'id':'phone','udid':'fixture','appium_url':'http://127.0.0.1:4723'}])
    client = SkillBridgeClient(config,Deny()); sent=[]
    class Socket:
        async def send(self,text): sent.append(json.loads(text))
    req = RpcRequestMessage(sequence=1,device_id='phone',method='tap',params={'x':1,'y':2,'operation_id':'x'})
    await client._handle_rpc(Socket(),req)
    assert sent[0]['ok'] is False
