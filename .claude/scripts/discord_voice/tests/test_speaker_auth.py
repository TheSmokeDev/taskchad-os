"""Interval-bound speaker authorization — the ledger's own contract.

Test shapes ported from hermes-talk's `tests/test_operator_auth.py` alongside
the module itself. The assertions changed shape in exactly one way: hermes-talk
decided allow/deny here because it owned the operator allowlist, while this
sidecar owns none and can only resolve WHO. So "denied" reads as
`WireBinding.trusted is False` with no `user_id`, and the allowlist half is
proven in the main process (`tests/test_ingress_role_seam.py`) and across the
real boundary (`tests/test_talk_two_process_authorization.py`).

The property under test throughout: a tool call resolves its speaker ONLY
through the response id it came from plus that response's opaque token. There
is no path from "who is talking now" to "who authorized this".
"""

from __future__ import annotations

import json

import pytest

import speaker_auth

OPERATOR_ID = 111111111111111111
OTHER_ID = 123456789012345678


def _speaker(user_id: int | None) -> dict:
    """A real Discord frame. Extra fields are deliberate: the ledger must read
    only `user_id` and never let display data near an authorization decision."""
    return {"ssrc": 11, "user_id": user_id, "display_name": "display data"}


def _pcm(ms: int) -> bytes:
    """`ms` of PCM16 mono at the 24 kHz session rate."""
    return bytes(ms * 24 * 2)


def _ledger(**kwargs) -> speaker_auth.DiscordSpeakerLedger:
    return speaker_auth.DiscordSpeakerLedger(**kwargs)


def _bind_response(ledger, *, response_id="resp_1", start_ms=0, end_ms=20, item="item_1"):
    """Drive one full VAD turn: speech start -> stop -> commit -> response."""
    ledger.note_speech_started(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": item,
            "audio_start_ms": start_ms,
        }
    )
    ledger.note_speech_stopped(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": item,
            "audio_end_ms": end_ms,
        }
    )
    create = ledger.response_for_commit(
        {"type": "input_audio_buffer.committed", "item_id": item}
    )
    ledger.note_response_created(
        {
            "type": "response.created",
            "response": {"id": response_id, "metadata": create["response"]["metadata"]},
        }
    )
    return create


def _call(ledger, *, response_id="resp_1", call_id="call_1", name="homie_command"):
    return ledger.bind_tool_event(
        {
            "response_id": response_id,
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps({"command": "diagnostics"}),
        }
    )


# -- interval resolution -------------------------------------------------------


def test_one_speaker_through_one_vad_turn_resolves_to_that_user() -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    create = _bind_response(ledger)

    verdict = ledger.resolve_for_wire(_call(ledger))

    assert verdict.trusted is True
    assert verdict.user_id == OPERATOR_ID
    assert verdict.reason == "resolved immutable Discord user ID"
    # The response that will carry it was minted by US, with the token in it.
    assert create["type"] == "response.create"
    assert create["response"]["metadata"][speaker_auth.BINDING_METADATA_KEY]


def test_two_speakers_in_one_vad_turn_are_ambiguous_and_untrusted() -> None:
    """The R7 BLOCKER's shape at the ledger level: a turn nobody can own."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger, end_ms=40)

    verdict = ledger.resolve_for_wire(_call(ledger))

    assert verdict.trusted is False
    assert verdict.user_id is None
    assert verdict.reason == "ambiguous speakers"


def test_synthesized_silence_covers_the_interval_without_claiming_an_author() -> None:
    """The pump's gated zeros are silence, not an unknown person.

    If silence tainted, every mid-sentence pause would refuse the operator's
    own command — the failure mode that makes a fail-closed design unusable.
    """
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(10))
    ledger.record_packet(None, _pcm(10))  # gate shut mid-turn
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(10))
    _bind_response(ledger, end_ms=30)

    verdict = ledger.resolve_for_wire(_call(ledger))

    assert verdict.trusted is True
    assert verdict.user_id == OPERATOR_ID


def test_a_real_frame_with_no_resolved_discord_user_taints_the_turn() -> None:
    """Distinct from silence: audio arrived and we could not say from whom."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(10))
    ledger.record_packet(_speaker(None), _pcm(10))
    _bind_response(ledger, end_ms=20)

    verdict = ledger.resolve_for_wire(_call(ledger))

    assert verdict.trusted is False
    assert verdict.reason == "missing or unresolved speaker attribution"


def test_an_all_silence_turn_has_no_speaker_to_resolve() -> None:
    ledger = _ledger()
    ledger.record_packet(None, _pcm(20))
    _bind_response(ledger)

    verdict = ledger.resolve_for_wire(_call(ledger))

    assert verdict.trusted is False
    assert verdict.reason == "no resolved speaker"


def test_a_vad_interval_past_the_recorded_audio_is_untrusted() -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger, end_ms=5_000)

    verdict = ledger.resolve_for_wire(_call(ledger))

    assert verdict.trusted is False
    assert verdict.reason == "VAD interval is outside recorded audio"


@pytest.mark.parametrize(
    "start_ms,end_ms",
    [(None, 20), (0, 0), (10, 5), (-1, 20), (True, 20)],
)
def test_malformed_vad_intervals_are_untrusted(start_ms, end_ms) -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    if start_ms is not None:
        ledger.note_speech_started({"item_id": "item_1", "audio_start_ms": start_ms})
    ledger.note_speech_stopped({"item_id": "item_1", "audio_end_ms": end_ms})
    create = ledger.response_for_commit({"item_id": "item_1"})
    ledger.note_response_created(
        {"response": {"id": "resp_1", "metadata": create["response"]["metadata"]}}
    )

    assert ledger.resolve_for_wire(_call(ledger)).trusted is False


def test_odd_length_pcm_is_recorded_as_unresolved_not_dropped() -> None:
    """A truncated frame is still audio the model heard; ignoring it would
    leave a coverage hole that reads as clean instead of suspect."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20) + b"\x00")
    _bind_response(ledger, end_ms=20)

    assert ledger.resolve_for_wire(_call(ledger)).trusted is False


# -- VAD item lifecycle --------------------------------------------------------


def test_speech_stop_without_a_matching_start_is_untrusted() -> None:
    """Half a VAD turn cannot be attributed: without a start timestamp there is
    no interval to resolve against, so the item is tainted at the stop and
    re-tainted at the commit that finds it already suspect."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    ledger.note_speech_stopped({"item_id": "item_1", "audio_end_ms": 20})
    create = ledger.response_for_commit({"item_id": "item_1"})
    ledger.note_response_created(
        {"response": {"id": "resp_1", "metadata": create["response"]["metadata"]}}
    )

    verdict = ledger.resolve_for_wire(_call(ledger))

    assert verdict.trusted is False
    assert verdict.user_id is None
    assert "VAD item" in verdict.reason


def test_a_duplicate_speech_start_cannot_narrow_a_mixed_turn(monkeypatch) -> None:
    """A replayed start must not let a later, cleaner interval be substituted
    for the ambiguous one the model actually answered."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    ledger.note_speech_started({"item_id": "item_1", "audio_start_ms": 0})
    ledger.note_speech_started({"item_id": "item_1", "audio_start_ms": 20})
    ledger.note_speech_stopped({"item_id": "item_1", "audio_end_ms": 40})
    create = ledger.response_for_commit({"item_id": "item_1"})
    ledger.note_response_created(
        {"response": {"id": "resp_1", "metadata": create["response"]["metadata"]}}
    )

    assert ledger.resolve_for_wire(_call(ledger)).trusted is False


def test_a_recycled_vad_item_never_emits_a_second_response() -> None:
    """One utterance, one response. A duplicate commit gets nothing to bind."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)

    assert ledger.response_for_commit({"item_id": "item_1"}) is None


def test_a_commit_with_no_vad_item_is_untrusted() -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    create = ledger.response_for_commit({"item_id": "orphan"})
    ledger.note_response_created(
        {"response": {"id": "resp_1", "metadata": create["response"]["metadata"]}}
    )

    verdict = ledger.resolve_for_wire(_call(ledger))

    assert verdict.trusted is False
    assert verdict.reason == "commit arrived without matching VAD item"


# -- token mint / resolve ------------------------------------------------------


def test_a_response_without_our_token_borrows_nobody(monkeypatch) -> None:
    """Response ORDER is not identity. A server-created response — or a forged
    one — carries no token of ours and therefore no speaker."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger, response_id="resp_bound")
    ledger.note_response_created({"response": {"id": "resp_forged", "metadata": {}}})

    verdict = ledger.resolve_for_wire(_call(ledger, response_id="resp_forged"))

    assert verdict.trusted is False
    assert verdict.reason == "unbound response"


def test_a_copied_token_cannot_bind_a_second_response_id() -> None:
    """The token is single-use. Replaying it under another response id — the
    shape of a model that echoed metadata back — binds nothing."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    create = _bind_response(ledger, response_id="resp_1")
    ledger.note_response_created(
        {"response": {"id": "resp_clone", "metadata": create["response"]["metadata"]}}
    )

    clone = ledger.resolve_for_wire(_call(ledger, response_id="resp_clone", call_id="c_clone"))
    assert clone.trusted is False
    assert clone.reason == "unbound response"
    # ...and the genuine one still works; the clone did not revoke it.
    genuine = ledger.resolve_for_wire(_call(ledger, response_id="resp_1", call_id="c_real"))
    assert genuine.trusted is True
    assert genuine.user_id == OPERATOR_ID


def test_a_reused_response_id_taints_the_original_instead_of_rebinding() -> None:
    """Address reuse is an attack shape, not a rename: the delayed authority
    that already exists under that id is revoked rather than reassigned."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger, response_id="resp_1")
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    create2 = _bind_response(ledger, response_id="resp_2", item="item_2", start_ms=20, end_ms=40)
    # The server (or an attacker) re-announces resp_1 carrying resp_2's token.
    ledger.note_response_created(
        {"response": {"id": "resp_1", "metadata": create2["response"]["metadata"]}}
    )

    assert ledger.resolve_for_wire(_call(ledger, response_id="resp_1")).trusted is False


@pytest.mark.parametrize("response_id", [None, "", 42, "  padded  ", "x" * 600])
def test_malformed_response_ids_never_resolve(response_id) -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)

    assert ledger.resolve_for_wire(_call(ledger, response_id=response_id)).trusted is False


def test_binding_for_response_reads_the_same_authority() -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)

    binding = ledger.binding_for_response("resp_1")

    assert binding is not None
    assert binding.user_id == OPERATOR_ID
    assert ledger.binding_for_response("nope") is None


# -- single-use permits --------------------------------------------------------


def test_the_same_bound_event_cannot_authorize_twice() -> None:
    """A replayed `function_call_arguments.done` must not run twice."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _call(ledger)

    assert ledger.resolve_for_wire(event).trusted is True
    assert ledger.resolve_for_wire(event).trusted is False


def test_a_reused_call_id_gets_no_permit_even_on_a_live_response() -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)

    assert ledger.resolve_for_wire(_call(ledger, call_id="c1")).trusted is True
    assert ledger.resolve_for_wire(_call(ledger, call_id="c1")).trusted is False


@pytest.mark.parametrize("call_id", [None, "", 7, "   "])
def test_a_malformed_call_id_never_gets_a_permit(call_id) -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)

    assert ledger.resolve_for_wire(_call(ledger, call_id=call_id)).trusted is False


def test_an_untrusted_event_still_consumes_its_permit() -> None:
    """Consumption is unconditional, so an event that was refused under one
    tool name cannot be re-presented later under another."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _call(ledger, name="memory_search")

    ledger.resolve_for_wire(event)  # read-only handling, still terminal

    assert ledger.resolve_for_wire(event).trusted is False


# -- continuations -------------------------------------------------------------


def test_a_tool_continuation_carries_a_fresh_token_for_the_same_speaker() -> None:
    """The speaker survives into the response that speaks the tool's result."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    create = _bind_response(ledger)
    event = _call(ledger)

    continuation = event[speaker_auth.TRUSTED_CONTINUATION_EVENT_KEY]
    token = continuation["response"]["metadata"][speaker_auth.BINDING_METADATA_KEY]
    assert token != create["response"]["metadata"][speaker_auth.BINDING_METADATA_KEY]

    ledger.note_response_created(
        {"response": {"id": "resp_cont", "metadata": continuation["response"]["metadata"]}}
    )
    verdict = ledger.resolve_for_wire(_call(ledger, response_id="resp_cont", call_id="c2"))

    assert verdict.trusted is True
    assert verdict.user_id == OPERATOR_ID


def test_recycling_the_parent_revokes_its_unconsumed_continuation() -> None:
    """One tainted chain, not one tainted link: reusing the parent response id
    kills the continuation that had not been spent yet."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    event = _call(ledger) if _bind_response(ledger) else None
    continuation = event[speaker_auth.TRUSTED_CONTINUATION_EVENT_KEY]

    ledger.note_response_created({"response": {"id": "resp_1", "metadata": {}}})
    ledger.note_response_created(
        {"response": {"id": "resp_cont", "metadata": continuation["response"]["metadata"]}}
    )

    assert ledger.resolve_for_wire(_call(ledger, response_id="resp_cont", call_id="c2")).trusted is False


def test_an_unbound_call_gets_a_continuation_with_no_binding() -> None:
    """The assistant keeps talking; it just does not keep an authority."""
    ledger = _ledger()
    event = _call(ledger, response_id="resp_unknown")

    assert event[speaker_auth.TRUSTED_CONTINUATION_EVENT_KEY] == {"type": "response.create"}


def test_completing_an_uncontinued_response_drops_its_continuation_token() -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _call(ledger)
    continuation = event[speaker_auth.TRUSTED_CONTINUATION_EVENT_KEY]

    ledger.complete_response("resp_1", continued=False)
    ledger.note_response_created(
        {"response": {"id": "resp_cont", "metadata": continuation["response"]["metadata"]}}
    )

    assert ledger.resolve_for_wire(_call(ledger, response_id="resp_cont", call_id="c2")).trusted is False


# -- capacity poison + teardown ------------------------------------------------


def test_response_tombstone_exhaustion_poisons_instead_of_reopening_replay() -> None:
    ledger = _ledger(max_seen_response_ids=2)
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(200))
    _bind_response(ledger, response_id="resp_1")
    ledger.note_response_created({"response": {"id": "resp_2", "metadata": {}}})
    ledger.note_response_created({"response": {"id": "resp_3", "metadata": {}}})

    assert ledger.resolve_for_wire(_call(ledger, response_id="resp_1")).trusted is False


def test_call_tombstone_exhaustion_poisons_delayed_authority() -> None:
    ledger = _ledger(max_seen_call_ids=1)
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)

    assert ledger.resolve_for_wire(_call(ledger, call_id="c1")).trusted is True
    assert ledger.resolve_for_wire(_call(ledger, call_id="c2")).trusted is False


def test_clear_revokes_events_already_handed_out() -> None:
    """Teardown must reach bound events living outside our indexes — a task
    still in flight holds the authority object itself."""
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _call(ledger)

    ledger.clear()

    assert ledger.resolve_for_wire(event).trusted is False
    assert ledger.segment_count == 0
    assert ledger.response_count == 0


def test_clear_lets_the_next_session_bind_normally() -> None:
    ledger = _ledger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    ledger.clear()

    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger)

    verdict = ledger.resolve_for_wire(_call(ledger))
    assert verdict.trusted is True
    assert verdict.user_id == OTHER_ID
