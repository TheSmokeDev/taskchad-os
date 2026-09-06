"""Actual persistence and final surface request tests, all under temporary targets."""
from datetime import datetime, timedelta, timezone
import json
import pytest
from personas.learning import hooks
from personas.learning.models import LearningTarget, LearningContext, content_hash
from personas.learning.service import LearningService
from runtime.base import RuntimeRequest, RuntimeResult
from tests.test_web_persona_runtime import persona_env, _incoming, _fake_result

@pytest.fixture
def learning_service(tmp_path):
    base = tmp_path / 'learning-sales'
    return LearningService(LearningTarget('sales', base / 'memory', base / 'data', base / 'state', base / 'skills'))

def expectation():
    return {'claim': 'The next reply describes a value concern', 'check_by': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), 'resolution_rule': 'Inspect the next linked reply for a named value concern', 'situation': {'conversation': 'synthetic sales objection'}}

def test_real_store_commits_actor_claim_before_handler(learning_service):
    svc = learning_service

    def execute(name, args):
        if name == 'record_expectation':
            return hooks.record_actor_expectation(args, persona_id='sales')
        claims = svc.store.all('expectation')
        starts = svc.store.all('execution')
        assert len(claims) == 1 and claims[0]['phase'] == 'pre_action'
        assert starts[-1]['expectation_id'] == claims[0]['id']
        assert starts[-1]['stage'] == 'started'
        return {'draft_id': 'synthetic-1'}
    request = RuntimeRequest(prompt='Handle this price objection', cwd='.', task_name='test', tool_dispatch=execute)
    turn = hooks.prepare_turn(request, persona_id='sales', surface='test', origin_id='stable-origin', service=svc)
    turn.request.tool_dispatch('record_expectation', expectation())
    turn.request.tool_dispatch('draft_reply', {})
    assert len(svc.store.all('expectation')) == 1
    assert len(svc.store.all('execution')) == 2
    turn.complete(RuntimeResult(text='Draft written', runtime_lane='generic_runtime', provider='fake', model='test-actual'))
    assert not turn.failures
    assert any((row.get('model') == 'test-actual' for row in svc.store.all('execution')))
    assert all((row.get('domain_outcome_observed') is False for row in svc.store.all('observation')))

def test_real_origin_idempotence_keeps_distinct_execution_attempts(learning_service):
    svc = learning_service
    req = RuntimeRequest(prompt='Original task', cwd='.', task_name='test')
    first = hooks.prepare_turn(req, persona_id='sales', surface='test', origin_id='same', task='Original task', service=svc)
    second = hooks.prepare_turn(req, persona_id='sales', surface='test', origin_id='same', task='Original task', service=svc)
    assert first.experience['id'] == second.experience['id']
    assert first.attempt_id != second.attempt_id
    assert len(svc.store.all('experience')) == 1
    assert not svc.store.all('context')
    first.complete(RuntimeResult(text='one', provider='fake', model='m1', runtime_lane='generic_runtime'))
    second.complete(RuntimeResult(text='two', provider='fake', model='m1', runtime_lane='generic_runtime'))
    assert len(svc.store.all('context')) == 2

def test_real_context_receipt_detects_loss_after_rendering(learning_service):
    svc = learning_service
    exp = svc.capture_experience('render', 'test', 'objection')
    method = 'Ask a diagnostic question before discounting.'
    ctx = LearningContext(method, ({'candidate_id': 'c', 'activation_id': 'a', 'content_hash': content_hash(method), 'content': method, 'rendered_block': method},), content_hash(method))
    kept = svc.record_context_receipt(exp['id'], ctx, 'User prompt ' + method, attempt_key='full')
    lost = svc.record_context_receipt(exp['id'], ctx, 'User prompt', attempt_key='truncated')
    assert len(kept['included']) == 1 and (not kept['dropped'])
    assert not lost['included'] and len(lost['dropped']) == 1

@pytest.mark.asyncio
async def test_web_surface_delivers_real_learning_and_strips_private_envelope(persona_env, learning_service, monkeypatch):
    from personas.learning.models import LearningContext
    from web_persona_runtime import run_web_persona_turn
    from session import get_session_store
    svc = learning_service
    method = 'Ask what outcome would justify the price before discussing a discount.'
    ctx = LearningContext(method, ({'candidate_id': 'c', 'activation_id': 'a', 'content_hash': content_hash(method), 'content': method, 'rendered_block': method},), content_hash(method))
    monkeypatch.setattr(svc, 'render_context', lambda *a, **kw: ctx)
    monkeypatch.setattr(hooks, '_service_for', lambda persona: svc if persona == 'sales' else None)

    async def fake_run(request):
        assert method in request.prompt
        assert request.metadata['persona_id'] == 'sales'
        assert request.allowed_tools == [] and request.disallowed_tools == ['*']
        return _fake_result(text='What outcome do you need?\n<<LEARNING_EXPECTATION:' + json.dumps(expectation()) + '>>')
    monkeypatch.setattr('runtime.lane_router.run_with_runtime_lanes', fake_run)
    incoming = _incoming()
    incoming.text = 'The client says the price is too high.'
    reply = await run_web_persona_turn(incoming=incoming, persona_id='sales', session_store=get_session_store(persona_env / 'learning-chat.db'), project_root=persona_env)
    assert reply == 'What outcome do you need?'
    assert svc.store.all('expectation')[0]['phase'] == 'pre_publication'
    assert any((row['included'] for row in svc.store.all('context')))
    assert not any(('LEARNING_EXPECTATION' in row.get('artifact', '') for row in svc.store.all('execution')))

def test_talk_quick_child_receives_target_and_origin(monkeypatch):
    import talk_tools
    seen = {}

    class Child:
        pid = 112233
        returncode = 0

        def communicate(self, timeout):
            return (json.dumps({'success': True, 'response': 'ok', 'session_id': 's'}), '')

    def spawn(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return Child()
    monkeypatch.setattr(talk_tools.subprocess, 'Popen', spawn)
    monkeypatch.setattr(talk_tools.talk_runs, 'attach_pid', lambda *a: True)
    monkeypatch.setattr(talk_tools.talk_runs, 'annotate_run', lambda *a, **kw: None)
    talk_tools._run_agent_turn('draft', 'codex', 10, 5, target_persona='sales', origin_key='talk:call1')
    argv = seen['argv']
    assert argv[argv.index('--profile') + 1] == 'sales'
    assert seen['kwargs']['env']['HOMIE_LEARNING_ORIGIN_KEY'] == 'talk:call1'

def test_talk_origin_is_transport_field_not_model_argument(monkeypatch):
    import talk_tools
    seen = {}
    monkeypatch.setattr(talk_tools, 'resolve_request_role', lambda **kw: 'admin')
    monkeypatch.setattr(talk_tools, 'talk_tool_denial', lambda *a, **kw: None)
    monkeypatch.setitem(talk_tools._HANDLERS, 'delegate_task', lambda args: seen.update(args) or 'ok')
    talk_tools.execute_talk_tool('delegate_task', {'task': 'draft', '_learning_origin_key': 'spoof'}, origin_key='call1')
    assert seen['_learning_origin_key'] == 'talk:browser:call1'

@pytest.mark.asyncio
async def test_engine_learning_keeps_session_brief_last(tmp_path, monkeypatch):
    import engine, personas
    from session import SQLiteSessionStore
    from tests.test_chat_runtime_engine import _make_project_root, _make_message
    monkeypatch.setenv('PERSONA_LEARNING_ENABLED', 'true')
    monkeypatch.setattr(personas, 'get_active_profile_name', lambda: 'default')
    base = tmp_path / 'default-learning'
    svc = LearningService(LearningTarget('default', base / 'memory', base / 'data', base / 'state', base / 'skills'))
    method = 'Investigate the prospect value concern before offering a discount.'
    ctx = LearningContext(method, ({'candidate_id': 'c', 'activation_id': 'a', 'content_hash': content_hash(method), 'content': method, 'rendered_block': method},), content_hash(method))
    monkeypatch.setattr(svc, 'render_context', lambda *a, **kw: ctx)
    monkeypatch.setattr(hooks, '_service_for', lambda persona: svc)
    convo = engine.ConversationEngine(SQLiteSessionStore(tmp_path / 'engine-chat.db'), _make_project_root(tmp_path))
    monkeypatch.setattr(convo, '_maybe_session_brief', lambda *a, **kw: ('SESSION BRIEF LAST', None))

    async def fake_run(request):
        assert method in request.prompt
        assert request.prompt.endswith('SESSION BRIEF LAST')
        return RuntimeResult(text='What outcome matters?', runtime_lane='generic_runtime', provider='fake', model='actual-model')
    monkeypatch.setattr(engine, 'run_with_runtime_lanes', fake_run)
    outputs = [out async for out in convo.handle_message(_make_message('The client objects to the price.'))]
    assert outputs[-1].text == 'What outcome matters?'
    assert any((row['included'] for row in svc.store.all('context')))
    assert svc.store.all('execution')

@pytest.mark.asyncio
async def test_discord_direct_surface_owns_learning(persona_env, learning_service, monkeypatch):
    from discord_persona_runtime import run_discord_persona_channel_turn
    from discord_channel_bindings import DiscordChannelBinding
    from tests.test_discord_persona_channels import _incoming as discord_incoming
    from session import get_session_store
    monkeypatch.setattr(hooks, '_service_for', lambda persona: learning_service)

    async def fake_run(request):
        assert request.metadata['learning']['surface'] == 'discord_persona_channel'
        assert request.metadata['persona_id'] == 'sales'
        return _fake_result(text='A sales answer')
    monkeypatch.setattr('runtime.lane_router.run_with_runtime_lanes', fake_run)
    out = await run_discord_persona_channel_turn(incoming=discord_incoming('channel'), binding=DiscordChannelBinding('channel', 'Sales', 'persona', 'sales'), session_store=get_session_store(persona_env / 'discord-learning.db'), project_root=persona_env)
    assert out.text == 'A sales answer'
    assert learning_service.store.all('execution')
    assert learning_service.store.all('experience')[0]['surface'] == 'discord_persona_channel'

from tests.test_cabinet_profile_execution import tmp_dashboard_db, tmp_homie_root

@pytest.mark.asyncio
async def test_cabinet_toolless_participant_delivers_learning(tmp_dashboard_db, tmp_homie_root, learning_service, monkeypatch):
    from cabinet import text_orchestrator, meeting_channel
    from tests.test_cabinet_profile_execution import _make_profile, _make_meeting, _roster
    _make_profile(tmp_homie_root, 'sales')
    monkeypatch.delenv('CABINET_PERSONA_FULL_TOOLS', raising=False)
    monkeypatch.setattr(hooks, '_service_for', lambda persona: learning_service)
    meeting_channel._reset_channels()
    seen = []
    async def fake_run(request):
        if request.task_name == 'cabinet_persona_turn':
            seen.append(request)
            assert request.metadata['persona_id'] == 'sales'
            assert request.metadata['learning']['surface'] == 'cabinet_text'
            assert request.tool_defs is None
            assert request.allowed_tools == []
            assert request.disallowed_tools == ['*']
        return _fake_result(text='Sales room reply')
    monkeypatch.setattr(text_orchestrator.lane_router, 'run_with_runtime_lanes', fake_run)
    try:
        result = await text_orchestrator.handle_text_turn(meeting_id=_make_meeting(_roster('sales')), user_text='@sales the prospect raised price', client_msg_id='cabinet-learning-1')
        assert result.accepted
        assert len(seen) == 1
        assert learning_service.store.all('experience')[0]['surface'] == 'cabinet_text'
        assert learning_service.store.all('execution')
    finally:
        meeting_channel._reset_channels()

def test_talk_actor_expectation_commits_before_delegation(learning_service, monkeypatch):
    import talk_tools
    from personas import lifecycle
    monkeypatch.setattr(hooks, '_service_for', lambda persona: learning_service)
    monkeypatch.setattr(lifecycle, 'show_profile', lambda name: None)
    def start(task, title, lane, **kwargs):
        claims = learning_service.store.all('expectation')
        assert len(claims) == 1 and claims[0]['phase'] == 'pre_action'
        assert kwargs['target_persona'] == 'sales'
        assert kwargs['origin_key'] == 'talk:browser:call-1'
        return 12, 13
    monkeypatch.setattr(talk_tools, 'start_agent_run', start)
    receipt = talk_tools._handle_delegate_task({'task': 'Draft a price objection reply', 'target_persona': 'sales', '_learning_origin_key': 'talk:browser:call-1', 'expectation': expectation()})
    assert '12' in receipt
    execution = learning_service.store.all('execution')[0]
    assert execution['stage'] == 'dispatch_returned'
    assert execution['completion_observed'] is False

def test_structured_tool_secret_is_not_stringified_past_rejection(learning_service):
    req = RuntimeRequest(prompt='Test synthetic secret rejection', cwd='.', task_name='test', tool_dispatch=lambda n, a: json.dumps({'api_key': 'SYNTHETIC_NOT_A_REAL_KEY'}))
    turn = hooks.prepare_turn(req, persona_id='sales', surface='test', origin_id='secret-rejection', service=learning_service)
    turn.request.tool_dispatch('test_tool', {})
    assert 'action_result:LearningError' in turn.failures
    assert 'SYNTHETIC_NOT_A_REAL_KEY' not in json.dumps(learning_service.store.all())
