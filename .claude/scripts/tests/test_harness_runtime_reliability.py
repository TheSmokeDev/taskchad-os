"""Adversarial host-runtime learning checks; synthetic profiles, no providers."""
import asyncio
import inspect
import sqlite3
import time
from types import SimpleNamespace

import pytest
from personas.learning import hooks
from runtime.base import RuntimeRequest, RuntimeResult
from tests.test_persona_learning_surface_integration import learning_service, expectation
from tests.test_web_persona_runtime import persona_env
from tests.test_persona_learning_surfaces import FakeService, req, prepare, prediction


@pytest.mark.asyncio
async def test_async_dispatch_keeps_context_until_completion():
    service = FakeService()
    async def execute(name, args):
        await asyncio.sleep(0)
        assert hooks._CURRENT_TURN.get().persona_id == 'sales'
        if name == 'record_expectation':
            return await asyncio.to_thread(hooks.record_actor_expectation, args, persona_id='sales')
        return {'actual': args['value']}
    turn = prepare(service, req(tool_dispatch=execute))
    assert inspect.iscoroutinefunction(turn.request.tool_dispatch)
    await turn.request.tool_dispatch('record_expectation', prediction())
    result = await turn.request.tool_dispatch('inspect', {'value': 7})
    assert result == {'actual': 7}
    returned = [row[1][1] for row in service.calls if row[0] == 'execution' and row[1][1]['stage'] == 'returned']
    assert returned[0]['result'] == result
    assert returned[0]['expectation_id'] == 'expect-1'
    assert hooks._CURRENT_TURN.get() is None


@pytest.mark.asyncio
async def test_sync_dispatch_returning_awaitable_records_only_awaited_result():
    async def result():
        await asyncio.sleep(0)
        assert hooks._CURRENT_TURN.get().persona_id == 'sales'
        return {'finished': True}
    svc = FakeService()
    turn = prepare(svc, req(tool_dispatch=lambda n, a: result()))
    pending = turn.request.tool_dispatch('inspect', {})
    assert not [c for c in svc.calls if c[0] == 'execution' and c[1][1]['stage'] == 'returned']
    assert await pending == {'finished': True}
    assert hooks._CURRENT_TURN.get() is None


@pytest.mark.asyncio
async def test_parallel_dispatch_consumes_prediction_once_and_keeps_snapshots(monkeypatch):
    from runtime import tool_registry
    monkeypatch.setattr(tool_registry, 'get_entry', lambda name: SimpleNamespace(effect='write'))
    svc = FakeService()
    started = asyncio.Event()
    finish = asyncio.Event()
    async def execute(name, args):
        if args['which'] == 'first':
            started.set()
            await finish.wait()
        return args['which']
    turn = prepare(svc, req(tool_dispatch=execute))
    turn.commit_actor_expectation(prediction())
    first = asyncio.create_task(turn.request.tool_dispatch('inspect', {'which': 'first'}))
    await started.wait()
    second = await turn.request.tool_dispatch('inspect', {'which': 'second'})
    finish.set()
    assert await first == 'first' and second == 'second'
    rows = [c[1][1] for c in svc.calls if c[0] == 'execution']
    assert len({r['action_key'] for r in rows}) == 2
    receipts = {r['result']: r for r in rows if r['stage'] == 'returned'}
    assert receipts['first']['expectation_id'] == 'expect-1'
    assert receipts['second']['expectation_id'] is None
    assert len(turn.failures) == 1


async def while_database_locked(path, operation):
    lock = sqlite3.connect(path)
    lock.execute('BEGIN IMMEDIATE')
    ticks = []
    started = time.monotonic()
    async def ticker():
        for _ in range(8):
            ticks.append(time.monotonic() - started)
            await asyncio.sleep(.02)
        lock.rollback()
    try:
        answer, _ = await asyncio.gather(operation(), ticker())
        assert ticks[-1] < 1, f'event loop stalled for {ticks[-1]:.2f}s on SQLite'
        return answer
    finally:
        lock.rollback()
        lock.close()


@pytest.mark.asyncio
async def test_real_sqlite_contention_does_not_stall_prepare_complete_or_failure(learning_service):
    svc = learning_service
    svc.capture_experience('bootstrap', 'test', 'bootstrap')
    turn = await while_database_locked(svc.store.path, lambda: hooks.prepare_turn_async(
        req(), persona_id='sales', surface='lock', origin_id='locked-turn', service=svc))
    assert turn.experience and not turn.failures
    assert await while_database_locked(svc.store.path, lambda: turn.acomplete(
        RuntimeResult(text='actual output', provider='fake', model='m1', runtime_lane='generic_runtime'))) == 'actual output'
    await while_database_locked(svc.store.path, lambda: turn.afailed(asyncio.CancelledError()))
    assert any(r['stage'] == 'failed' for r in svc.store.all('execution'))


@pytest.mark.asyncio
async def test_real_activity_acquire_and_release_do_not_stall_loop(tmp_path, monkeypatch):
    from runtime import activity
    path = tmp_path / 'foreground.db'
    monkeypatch.setenv('SECOND_BRAIN_RUNTIME_ACTIVITY_DB', str(path))
    seed = activity.acquire_lease('seed', owner='test')
    activity.release_lease(seed)
    manager = activity.foreground_request(req(conversational=True))
    await while_database_locked(path, lambda: manager.__aenter__())
    assert activity.foreground_active()
    with sqlite3.connect(path) as db:
        lease = db.execute("SELECT lease_id FROM runtime_activity WHERE kind='foreground'").fetchone()[0]
    assert await while_database_locked(path, lambda: activity._renew_foreground_lease(lease))
    await while_database_locked(path, lambda: manager.__aexit__(None, None, None))
    assert not activity.foreground_active()


@pytest.mark.asyncio
async def test_fallback_tools_keep_actual_provider_attempt_and_failed_effect(learning_service, monkeypatch):
    from runtime import lane_router
    from runtime.errors import RuntimeRetryableError
    from runtime.profiles import RuntimeProfile
    profiles = [RuntimeProfile(key='one', provider='fake-one', model='model-one'),
                RuntimeProfile(key='two', provider='fake-two', model='model-two')]
    monkeypatch.setattr(lane_router, '_resolve_lane_profiles', lambda request: profiles)
    monkeypatch.setattr(lane_router, 'mark_profile_retryable_failure', lambda *args: None)
    monkeypatch.setattr(lane_router, 'mark_profile_success', lambda *args: None)
    class Adapter:
        def __init__(self, profile): self.profile = profile
        def supports(self, request): return True
        async def run(self, request):
            await asyncio.to_thread(request.tool_dispatch, 'record_expectation', expectation())
            await asyncio.to_thread(request.tool_dispatch, 'inspect', {'provider': self.profile.provider})
            if self.profile.key == 'one':
                raise RuntimeRetryableError('synthetic provider failed after tool effect')
            return RuntimeResult(text='done', provider=self.profile.provider, model=self.profile.model, runtime_lane='generic_runtime')
    monkeypatch.setattr(lane_router, '_adapter_for', Adapter)
    def dispatch(name, args):
        if name == 'record_expectation':
            return hooks.record_actor_expectation(args, persona_id='sales')
        return {'actual_effect_provider': args['provider']}
    turn = await hooks.prepare_turn_async(req(tool_dispatch=dispatch, runtime_lane='generic_runtime'),
        persona_id='sales', surface='test', origin_id='fallback', service=learning_service)
    result = await lane_router.run_with_runtime_lanes(turn.request)
    await turn.acomplete(result)
    rows = learning_service.store.all('execution')
    tools = sorted((r for r in rows if r['stage'] == 'returned'), key=lambda r: r['runtime']['provider'])
    assert [r['runtime']['provider'] for r in tools] == ['fake-one', 'fake-two']
    assert tools[0]['attempt_id'] != tools[1]['attempt_id']
    assert {r['experience_id'] for r in rows} == {turn.experience['id']}
    failed = [r for r in rows if r['stage'] == 'runtime_failed']
    assert failed[0]['runtime']['provider'] == 'fake-one'
    assert failed[0]['runtime']['attempt_id'] == tools[0]['attempt_id']
    contexts = learning_service.store.all('context')
    assert len([r for r in contexts if r['phase'] == 'submitted']) == 2
    assert len([r for r in contexts if r['phase'] == 'executed']) == 1
    assert not turn.failures


def test_elevation_and_learning_share_generated_origin(monkeypatch):
    from runtime import persona_elevation
    monkeypatch.delenv('HOMIE_LEARNING_ORIGIN_KEY', raising=False)
    incoming = SimpleNamespace(raw_event={}, platform_message_id=None)
    context = persona_elevation.build_turn_context('sales', incoming, session_key='cli:session')
    original = incoming.raw_event['elevation_original_turn_id']
    assert context['turn_id'] == original
    assert hooks.incoming_origin(incoming, 'cli:session') == f'cli:session:{original}'
    resumed = SimpleNamespace(raw_event={'elevation_original_turn_id': original}, platform_message_id='approval')
    assert hooks.incoming_origin(resumed, 'cli:session') == f'cli:session:{original}'


def test_talk_delegation_claim_belongs_to_initiator_and_has_no_phantom_context(tmp_path, monkeypatch):
    import personas, talk_tools
    from personas import lifecycle
    from personas.learning.models import LearningTarget
    from personas.learning.service import LearningService
    services = {name: LearningService(LearningTarget(name, tmp_path/name/'memory', tmp_path/name/'data',
                    tmp_path/name/'state', tmp_path/name/'skills')) for name in ('default', 'sales')}
    monkeypatch.setattr(personas, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(hooks, '_service_for', services.__getitem__)
    monkeypatch.setattr(lifecycle, 'show_profile', lambda name: None)
    monkeypatch.setattr(talk_tools, 'start_agent_run', lambda *a, **kw: (12, 13))
    talk_tools._handle_delegate_task({'task': 'Draft the response', 'target_persona': 'sales',
        '_learning_origin_key': 'talk:browser:realtime:call-1', 'expectation': expectation()})
    claims = services['default'].store.all('expectation')
    assert len(claims) == 1 and claims[0]['author_persona_id'] == 'default'
    experience = services['default'].store.all('experience')[0]
    assert experience['metadata']['target_persona'] == 'sales'
    assert not services['default'].store.all('context')
    assert not services['sales'].store.all()


def test_read_tool_records_receipt_without_consuming_action_prediction(monkeypatch):
    from runtime import tool_registry
    monkeypatch.setattr(tool_registry, 'get_entry', lambda name: SimpleNamespace(effect='read' if name == 'lookup' else 'write'))
    service = FakeService()
    turn = prepare(service, req(tool_dispatch=lambda n, a: 'done'))
    turn.request.tool_dispatch('lookup', {})
    assert not turn.failures
    turn.commit_actor_expectation(prediction())
    turn.request.tool_dispatch('lookup', {})
    assert turn.expectation['id'] == 'expect-1'
    turn.request.tool_dispatch('apply', {})
    assert turn.expectation is None and not turn.failures
    actions = [r[1][1] for r in service.calls if r[0] == 'execution' and r[1][1]['stage'] == 'returned']
    assert actions[0]['effect'] == 'read' and actions[0]['expectation_id'] is None
    assert actions[-1]['expectation_id'] == 'expect-1'


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['web', 'discord'])
@pytest.mark.parametrize('exception_type', [asyncio.CancelledError, ValueError])
async def test_persona_surfaces_persist_cancellation_and_unexpected_failure(surface, exception_type, persona_env, learning_service, monkeypatch):
    from session import get_session_store
    monkeypatch.setattr(hooks, '_service_for', lambda persona: learning_service)
    async def fail(request):
        raise exception_type('synthetic failure')
    monkeypatch.setattr('runtime.lane_router.run_with_runtime_lanes', fail)
    if surface == 'web':
        from web_persona_runtime import run_web_persona_turn
        from tests.test_web_persona_runtime import _incoming
        call = run_web_persona_turn(incoming=_incoming(), persona_id='sales',
            session_store=get_session_store(persona_env / 'failed-web.db'), project_root=persona_env)
    else:
        from discord_persona_runtime import run_discord_persona_channel_turn
        from discord_channel_bindings import DiscordChannelBinding
        from tests.test_discord_persona_channels import _incoming
        call = run_discord_persona_channel_turn(incoming=_incoming('channel'),
            binding=DiscordChannelBinding('channel', 'Sales', 'persona', 'sales'),
            session_store=get_session_store(persona_env / 'failed-discord.db'), project_root=persona_env)
    with pytest.raises(exception_type):
        await call
    failures = [r for r in learning_service.store.all('execution') if r['stage'] == 'failed']
    assert len(failures) == 1 and failures[0]['error_type'] == exception_type.__name__
    assert not learning_service.store.all('context')


@pytest.mark.asyncio
async def test_late_failed_provider_thread_cannot_inherit_fallback_attempt(learning_service, monkeypatch):
    import threading
    from runtime import lane_router
    from runtime.errors import RuntimeRetryableError
    from runtime.profiles import RuntimeProfile
    profiles = [RuntimeProfile(key='one', provider='fake-one', model='model-one'),
                RuntimeProfile(key='two', provider='fake-two', model='model-two')]
    monkeypatch.setattr(lane_router, '_resolve_lane_profiles', lambda request: profiles)
    monkeypatch.setattr(lane_router, 'mark_profile_retryable_failure', lambda *args: None)
    monkeypatch.setattr(lane_router, 'mark_profile_success', lambda *args: None)
    release = threading.Event()
    started = threading.Event()
    pending = []
    def late_dispatch(request):
        started.set()
        assert release.wait(5)
        claim = dict(expectation(), claim='old provider judgment')
        request.tool_dispatch('record_expectation', claim)
        return request.tool_dispatch('inspect', {'origin': 'old'})
    class Adapter:
        def __init__(self, profile): self.profile = profile
        def supports(self, request): return True
        async def run(self, request):
            if self.profile.key == 'one':
                pending.append(asyncio.create_task(asyncio.to_thread(late_dispatch, request)))
                while not started.is_set():
                    await asyncio.sleep(.001)
                raise RuntimeRetryableError('provider stops with a host thread still running')
            await asyncio.to_thread(request.tool_dispatch, 'record_expectation', dict(expectation(), claim='new provider judgment'))
            release.set()
            await pending[0]
            await asyncio.to_thread(request.tool_dispatch, 'inspect', {'origin': 'new'})
            return RuntimeResult(text='done', provider=self.profile.provider, model=self.profile.model, runtime_lane='generic_runtime')
    monkeypatch.setattr(lane_router, '_adapter_for', Adapter)
    def dispatch(name, args):
        if name == 'record_expectation': return hooks.record_actor_expectation(args, persona_id='sales')
        return args
    turn = await hooks.prepare_turn_async(req(tool_dispatch=dispatch, runtime_lane='generic_runtime'),
        persona_id='sales', surface='test', origin_id='late-fallback', service=learning_service)
    await lane_router.run_with_runtime_lanes(turn.request)
    rows = [r for r in learning_service.store.all('execution') if r['stage'] == 'returned']
    claims = {r['id']: r for r in learning_service.store.all('expectation')}
    for row in rows:
        old = row['result']['origin'] == 'old'
        assert row['runtime']['provider'] == ('fake-one' if old else 'fake-two')
        assert claims[row['expectation_id']]['claim'] == ('old provider judgment' if old else 'new provider judgment')
    assert len(rows) == 2
