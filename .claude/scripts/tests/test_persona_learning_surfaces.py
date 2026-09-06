"""Host learning receipts with synthetic services and no provider execution."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from personas.learning import hooks
from runtime.base import RuntimeRequest, RuntimeResult, assert_model_only_contract

class FakeService:

    def __init__(self, enabled=True):
        self.calls = []
        self._enabled = enabled
        self.store = SimpleNamespace(event=lambda *a, **kw: self.calls.append(('event', a, kw)))

    def enabled(self):
        return self._enabled

    def capture_experience(self, *a, **kw):
        self.calls.append(('experience', a, kw))
        return {'id': 'exp-1'}

    def render_context(self, *a, **kw):
        return SimpleNamespace(text='Ask a diagnostic question before discounting.', versions=(), context_hash='c1')

    def record_context_receipt(self, *a, **kw):
        self.calls.append(('context', a, kw))

    def commit_expectation(self, *a, **kw):
        self.calls.append(('expectation', a, kw))
        return {'id': 'expect-1'}

    def record_execution(self, *a, **kw):
        self.calls.append(('execution', a, kw))

    def record_observation(self, *a, **kw):
        self.calls.append(('observation', a, kw))

def req(**kw):
    return RuntimeRequest(prompt='Help with this objection', cwd=Path('.'), task_name='test', **kw)

def prepare(service, request=None, **kw):
    return hooks.prepare_turn(request or req(), persona_id='sales', surface='test', origin_id='client:turn1', service=service, **kw)

def prediction():
    return {'claim': 'Next reply reveals value concern', 'check_by': '2030-01-01T00:00:00+00:00', 'resolution_rule': 'Observe next reply', 'situation': {'stage': 'price'}}

def test_actual_content_survives_full_system_prompt():
    svc = FakeService()
    turn = prepare(svc, req(system_prompt={'append': 'x' * 27000}))
    assert len(turn.request.system_prompt['append']) == 27000
    assert 'Ask a diagnostic question' in turn.request.prompt
    assert not [c for c in svc.calls if c[0] == 'context']
    turn.complete(RuntimeResult(text='used it', provider='fake', model='m1', runtime_lane='generic_runtime'))
    assert next((c for c in svc.calls if c[0] == 'context'))[1][2] == turn.request.prompt

def test_model_only_stays_toolless():
    turn = prepare(FakeService(), req(model_only=True, disallowed_tools=['*']))
    assert_model_only_contract(turn.request)
    assert turn.request.tool_dispatch is None

def test_missing_claim_visible_not_invented(monkeypatch):
    from runtime import tool_registry
    monkeypatch.setattr(tool_registry, 'get_entry', lambda name: SimpleNamespace(effect='write'))
    svc = FakeService()
    called = []
    turn = prepare(svc, req(tool_dispatch=lambda n, a: called.append(n) or 'done'))
    turn.request.tool_dispatch('inspect', {})
    assert called == ['inspect']
    assert not [c for c in svc.calls if c[0] == 'expectation']
    assert 'pre_action:missing_actor_expectation' in turn.failures

def test_trial_refuses_action_without_capture():
    called = []
    turn = prepare(FakeService(), req(tool_dispatch=lambda n, a: called.append(n)), require_capture=True)
    with pytest.raises(RuntimeError, match='capture required'):
        turn.request.tool_dispatch('inspect', {})
    assert called == []

def test_claim_committed_before_action_and_scoped():
    svc = FakeService()

    def dispatch(n, a):
        if n == 'record_expectation':
            return hooks.record_actor_expectation(a, persona_id='sales')
        assert any((c[0] == 'expectation' for c in svc.calls))
        assert svc.calls[-1][1][1]['stage'] == 'started'
        return 'actual receipt'
    turn = prepare(svc, req(tool_dispatch=dispatch))
    turn.request.tool_dispatch('record_expectation', prediction())
    turn.request.tool_dispatch('inspect', {})
    assert not turn.failures
    assert turn.expectation is None
    with pytest.raises(ValueError, match='host-owned'):
        hooks.record_actor_expectation(prediction(), persona_id='sales')

def test_cross_persona_expectation_rejected():
    turn = prepare(FakeService(), req(tool_dispatch=lambda n, a: hooks.record_actor_expectation(a, persona_id='crypto')))
    with pytest.raises(ValueError, match='host-owned'):
        turn.request.tool_dispatch('record_expectation', prediction())

def test_final_marker_prepublication_and_never_delivery_claim():
    svc = FakeService()
    turn = prepare(svc)
    text = turn.complete(RuntimeResult(text='Ask about value.\n<<LEARNING_EXPECTATION:' + json.dumps(prediction()) + '>>', runtime_lane='generic_runtime', provider='fake', model='m2'))
    assert text == 'Ask about value.'
    assert next((c for c in svc.calls if c[0] == 'expectation'))[1][1]['phase'] == 'pre_publication'
    execution = next((c for c in svc.calls if c[0] == 'execution'))[1][1]
    assert execution['publication_confirmed'] is False
    assert execution['runtime']['model'] == 'm2'
    assert next((c for c in svc.calls if c[0] == 'observation'))[1][1]['domain_outcome_observed'] is False

def test_disabled_learning_leaves_content_unchanged():
    svc = FakeService(False)
    original = req()
    turn = prepare(svc, original)
    assert turn.request.prompt == original.prompt
    assert svc.calls == []

def test_capture_failure_visible_without_secret():
    svc = FakeService()

    def fail(*a, **kw):
        raise OSError('private token')
    svc.render_context = fail
    turn = prepare(svc)
    assert turn.failures == ['prepare:OSError']
    assert 'private token' not in str(turn.request.metadata)

def test_retry_has_actual_prompt_receipt_and_separate_attempt():
    svc = FakeService()
    turn = prepare(svc)
    first = turn.attempt_id
    retry = turn.retry_request(replace(turn.request, prompt='No tools\n' + turn.request.prompt), reason='transport')
    assert turn.attempt_id != first
    assert retry.metadata['learning']['origin_key'] == 'client:turn1'
    assert not [c for c in svc.calls if c[0] == 'context']
    turn.complete(RuntimeResult(text='retried', provider='fake', model='m1', runtime_lane='generic_runtime'))
    assert next(c for c in svc.calls if c[0] == 'context')[1][2] == retry.prompt

def test_origin_survives_approval_and_generated_ids(monkeypatch):
    monkeypatch.delenv('HOMIE_LEARNING_ORIGIN_KEY', raising=False)
    incoming = SimpleNamespace(raw_event={'elevation_original_turn_id': 'original'}, platform_message_id='retry')
    assert hooks.incoming_origin(incoming, 'discord:chan') == 'discord:chan:original'
    blank = SimpleNamespace(raw_event={}, platform_message_id=None)
    assert hooks.incoming_origin(blank, 'web:c') == hooks.incoming_origin(blank, 'web:c')
    other = SimpleNamespace(raw_event={}, platform_message_id=None)
    assert hooks.incoming_origin(blank, 'web:c') != hooks.incoming_origin(other, 'web:c')

def test_registered_tool_overwrites_spoofed_persona(monkeypatch):
    from runtime import persona_tools, tool_registry, tool_impl_learning
    monkeypatch.setattr(tool_registry, '_REGISTRY', {})
    monkeypatch.setattr(persona_tools, '_audit', lambda **kw: None)
    tool_impl_learning.register_tools()
    captured = []
    monkeypatch.setattr(hooks, 'record_actor_expectation', lambda p, *, persona_id: captured.append(persona_id) or {'id': 'e'})
    dispatch = persona_tools._make_dispatch('sales', frozenset({'record_expectation'}))
    dispatch('record_expectation', prediction() | {'_persona_id': 'crypto'})
    assert captured == ['sales']
    assert tool_registry.get_entry('record_expectation').toolset == 'cognitive_learning'
