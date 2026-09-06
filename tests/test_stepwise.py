from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from lobster_phone_agent.agent.conditions import ConditionEvaluator
from lobster_phone_agent.agent.dialogs import CommonDialogHandler
from lobster_phone_agent.agent.executor import ExecutionHooks
from lobster_phone_agent.agent.risk import RiskEngine
from lobster_phone_agent.agent.stepwise import StepwiseExecutor
from lobster_phone_agent.apps.registry import AppRegistry
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.errors import ExecutionError, PlanningError
from lobster_phone_agent.llm.client import OpenAICompatibleClient
from lobster_phone_agent.llm.next_action import NextAction, NextActionPlanner
from lobster_phone_agent.schemas import TaskRequest
from .fakes import FakeDevice, screen


def decision(**kwargs):
    return NextAction(observation_id='a' * 32, **kwargs)


@pytest.mark.parametrize('payload', [
    {'action':'tap'}, {'action':'tap','target_node':'1','expect':[{'kind':'screen_changed'}]},
    {'action':'tap','target_node':True,'expect':[{'kind':'screen_changed'}]},
    {'action':'tap','target_node':1}, {'action':'tap','target_node':1,'steps':[]},
    {'action':'tap','target_node':1,'risk':'low'}, {'action':'shell','text':'id'},
    {'action':'finish'}, {'action':'finish','expect':[{'kind':'screen_changed'}]},
    {'action':'finish','expect':[{'kind':'text_absent','value':'error'}]},
    {'action':'finish','expect':[{'kind':'any','children':[{'kind':'text_present','value':'ok'}]}]},
    {'action':'wait','wait_ms':2001}, {'action':'wait','wait_ms':True},
    {'action':'wait','wait_ms':-1}, {'action':'type','target_node':1},
    {'action':'type','target_node':1,'text':'ok','coordinates':[1,2]},
    {'action':'launch_app','app_package':'com.demo;reboot'},
    {'action':'launch_app','app_package':'com.demo','target_node':1},
    {'action':'handoff','text':''}, {'action':'home','text':'unused'},
])
def test_next_action_contract_rejects_wrong_shape(payload):
    with pytest.raises(ValidationError):
        decision(**payload)


@pytest.mark.parametrize('payload', [
    {'action':'tap','target_node':0,'expect':[{'kind':'screen_changed'}]},
    {'action':'type','target_node':0,'text':'北京南站'},
    {'action':'clear','target_node':0}, {'action':'wait','wait_ms':0},
    {'action':'swipe','direction':'up','expect':[{'kind':'screen_changed'}]},
    {'action':'finish','expect':[{'kind':'text_present','value':'已完成'}]},
    {'action':'handoff','text':'请登录'},
])
def test_next_action_accepts_one_valid_operation(payload):
    assert decision(**payload).action == payload['action']


class ScriptedPlanner:
    def __init__(self, actions, mutate=None):
        self.actions = iter(actions)
        self.seen = []
        self.mutate = mutate
    def launch_goal(self, *_):
        return None
    async def choose(self, **kw):
        self.seen.append(kw)
        action = next(self.actions)
        if self.mutate:
            self.mutate(len(self.seen))
        return NextAction(observation_id=kw['observation_id'], **action)


async def noop(*args, **kwargs):
    return None


def setup(planner, tmp_path, **config):
    settings = Settings(artifact_dir=tmp_path, action_settle_ms=0,
                        snapshot_stable_interval_ms=1, snapshot_stable_timeout_ms=50,
                        post_action_timeout_ms=2, max_steps=10, **config)
    matcher = SemanticMatcher()
    engine = StepwiseExecutor(settings=settings, planner=planner, matcher=matcher,
                              conditions=ConditionEvaluator(matcher, poll_ms=1),
                              dialogs=CommonDialogHandler(matcher), risk=RiskEngine())
    return engine


def hooks(confirm=noop, handoff=noop):
    return ExecutionHooks(emit=noop, confirm=confirm, handoff=handoff, trace=noop,
                          set_step_index=noop, is_cancelled=lambda:False)


async def run(engine, phone, *, policy=None, on_decision=noop, hook=None):
    request = TaskRequest(instruction='完成页面任务', device={'id':'dev','bridge_id':'home'}, policy=policy or {})
    return await engine.execute_live(request=request, device=phone, hooks=hook or hooks(),
        snapshot=await phone.snapshot(), installed_apps=[], on_decision=on_decision)


@pytest.mark.asyncio
async def test_each_successful_action_replans_from_new_screen_only(tmp_path):
    a = screen('com.demo', {'text':'入口','class_name':'android.widget.Button'})
    b = screen('com.demo', {'text':'确认浏览','class_name':'android.widget.Button'})
    c = screen('com.demo', {'text':'完成'})
    p = ScriptedPlanner([
        {'action':'tap','target_node':0,'expect':[{'kind':'text_present','value':'确认浏览'}]},
        {'action':'tap','target_node':0,'expect':[{'kind':'text_present','value':'完成'}]},
        {'action':'finish','expect':[{'kind':'text_present','value':'完成'}]},
    ])
    plans = []
    async def save(plan):
        plans.append(plan)
    phone = FakeDevice(states=[a,b,c])
    result = await run(setup(p,tmp_path), phone, on_decision=save)
    assert [x['snapshot'].fingerprint for x in p.seen] == [a.fingerprint,b.fingerprint,c.fingerprint]
    assert len({x['observation_id'] for x in p.seen}) == 3
    assert all(len(x.steps)==1 for x in plans)
    assert [x[0] for x in phone.actions] == ['tap','tap']
    assert result['verified_actions']==2
    assert p.seen[1]['recent_receipts'][0]['outcome']=='verified'


@pytest.mark.asyncio
async def test_changed_screen_during_model_thinking_prevents_old_click(tmp_path):
    a=screen('com.demo', {'text':'入口','class_name':'android.widget.Button'})
    b=screen('com.demo', {'text':'完成'})
    phone=FakeDevice(states=[a,b])
    p=ScriptedPlanner([
        {'action':'tap','target_node':0,'expect':[{'kind':'screen_changed'}]},
        {'action':'finish','expect':[{'kind':'text_present','value':'完成'}]},
    ], mutate=lambda n: phone.advance() if n==1 else None)
    await run(setup(p,tmp_path), phone)
    assert phone.actions==[]
    assert p.seen[1]['recent_receipts'][0]['outcome']=='stale_not_executed'


@pytest.mark.asyncio
async def test_confirmed_button_moving_never_uses_old_coordinates(tmp_path):
    a=screen('com.demo', {'text':'立即叫车','class_name':'android.widget.Button'})
    b=screen('com.demo', {'text':'完成'})
    phone=FakeDevice(states=[a,b])
    p=ScriptedPlanner([
        {'action':'tap','target_node':0,'expect':[{'kind':'screen_changed'}]},
        {'action':'finish','expect':[{'kind':'text_present','value':'完成'}]},
    ])
    approvals=[]
    async def confirm(step,risk):
        approvals.append(step); phone.advance()
    await run(setup(p,tmp_path),phone,policy={'allow_irreversible':True,'confirmation_mode':'none',
              'preauthorized_risks':['high']},hook=hooks(confirm=confirm))
    assert len(approvals)==1
    assert phone.actions==[]


@pytest.mark.asyncio
async def test_post_dispatch_failure_is_not_retried_or_replanned(tmp_path):
    class UncertainPhone(FakeDevice):
        async def tap(self,x,y,**kwargs):
            self.actions.append(('tap',(x,y)))
            raise TimeoutError('lost acknowledgment')
    phone=UncertainPhone(states=[screen('com.demo',{'text':'入口','class_name':'android.widget.Button'})])
    p=ScriptedPlanner([{'action':'tap','target_node':0,'expect':[{'kind':'screen_changed'}]}])
    with pytest.raises(ExecutionError,match='outcome unverified'):
        await run(setup(p,tmp_path),phone)
    assert len(phone.actions)==1 and len(p.seen)==1


@pytest.mark.asyncio
async def test_false_finish_and_unobservable_goal_are_rejected(tmp_path):
    phone=FakeDevice(states=[screen('com.demo',{'text':'加载中'})])
    p=ScriptedPlanner([{'action':'finish','expect':[{'kind':'text_present','value':'成功'}]}])
    with pytest.raises(ExecutionError,match='completion evidence'):
        await run(setup(p,tmp_path),phone)
    assert phone.actions==[]


@pytest.mark.asyncio
async def test_payment_button_cannot_be_labeled_low_by_model(tmp_path):
    phone=FakeDevice(states=[screen('com.demo',{'text':'立即支付','class_name':'android.widget.Button'})])
    p=ScriptedPlanner([{'action':'tap','target_node':0,'expect':[{'kind':'screen_changed'}]}])
    with pytest.raises(ExecutionError,match='策略未授权支付'):
        await run(setup(p,tmp_path),phone)
    assert phone.actions==[]


@pytest.mark.asyncio
async def test_hidden_or_unknown_node_never_executes(tmp_path):
    phone=FakeDevice(states=[screen('com.demo',{'text':'入口','class_name':'android.widget.Button'})])
    p=ScriptedPlanner([{'action':'tap','target_node':9,'expect':[{'kind':'screen_changed'}]}])
    with pytest.raises(ExecutionError,match='target is absent'):
        await run(setup(p,tmp_path),phone)
    assert phone.actions==[]


@pytest.mark.asyncio
async def test_same_no_progress_action_is_not_issued_twice(tmp_path):
    phone=FakeDevice(states=[screen('com.demo',{'text':'入口','class_name':'android.widget.Button'})])
    tap={'action':'tap','target_node':0,'expect':[{'kind':'text_present','value':'入口'}]}
    p=ScriptedPlanner([tap,tap])
    with pytest.raises(ExecutionError,match='repeated action'):
        await run(setup(p,tmp_path),phone)
    assert len(phone.actions)==1


@pytest.mark.asyncio
async def test_history_sent_to_llm_is_bounded_facts_not_multi_turn_reasoning():
    captured=[]
    oid=uuid4().hex
    def handler(req):
        payload=json.loads(req.content)
        captured.append(payload)
        inp=json.loads(payload['messages'][1]['content'])
        content={'observation_id':inp['observation']['id'],'action':'handoff','text':'请确认目标'}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(content)}}]})
    s=Settings(llm_model='test')
    llm=OpenAICompatibleClient(s)
    await llm._client.aclose()
    llm._client=httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url='http://llm/')
    p=NextActionPlanner(s,AppRegistry([]),llm)
    req=TaskRequest(instruction='执行任意页面任务',device={'id':'dev','bridge_id':'home'})
    try:
        for _ in range(2):
            await p.choose(request=req,snapshot=screen('com.demo'),observation_id=oid,
                           installed_apps=[],recent_receipts=[{'action':'wait','outcome':'verified'}]*20)
    finally:
        await llm.close()
    assert len(captured)==2
    for payload in captured:
        assert [m['role'] for m in payload['messages']]==['system','user']
        assert payload['max_tokens']<=700
        assert len(json.loads(payload['messages'][1]['content'])['recent_receipts'])==3
        assert 'steps' not in payload['response_format']['json_schema']['schema']['properties']


@pytest.mark.asyncio
async def test_planner_rejects_expired_observation_identifier():
    class LLM:
        async def complete_json(self,**kw):
            return {'observation_id':'0'*32,'action':'handoff','text':'请检查'}
    p=NextActionPlanner(Settings(llm_model='test'),AppRegistry([]),LLM())
    req=TaskRequest(instruction='执行页面任务',device={'id':'dev','bridge_id':'home'})
    with pytest.raises(PlanningError,match='expired observation'):
        await p.choose(request=req,snapshot=screen('com.demo'),observation_id='a'*32,
                       installed_apps=[],recent_receipts=[])
