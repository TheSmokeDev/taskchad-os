"""Voice authorization across the REAL process boundary.

The Discord voice sidecar runs as its own process in its own venv (py-cord
shares the `discord` namespace with discord.py, so the two can never cohabit).
It knows who is speaking. The process that AUTHORIZES tool calls is the main
API. Round 5 stored the speaker in the sidecar's `talk_tools` globals and read
it from the main process's `talk_tools` globals — two different interpreters,
so the value never crossed. The in-process tests missed it precisely because
they imported both halves into one interpreter.

This spawns the sidecar half as an ACTUAL subprocess in its OWN venv, has it
build the tool-call payload the way the bridge does, and hands that payload to
the main process's real authorization path. Two interpreters, one wire.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
_SIDECAR = _SCRIPTS / "discord_voice"
_SIDECAR_PY = _SIDECAR / ".venv" / "Scripts" / "python.exe"
_SIDECAR_PY_POSIX = _SIDECAR / ".venv" / "bin" / "python"


def _sidecar_python() -> str | None:
    for candidate in (_SIDECAR_PY, _SIDECAR_PY_POSIX):
        if candidate.is_file():
            return str(candidate)
    return None


#: Runs INSIDE the sidecar venv. Drives the REAL bridge objects AND the REAL
#: `_api_tool_executor`, stubbing only the HTTP transport — so the payload
#: asserted here is the one the sidecar would actually put on the wire, not a
#: dict the test wrote itself.
_SIDECAR_SCRIPT = """
import json, sys, asyncio
import urllib.request
from types import SimpleNamespace
sys.path.insert(0, r"{sidecar}")
import bridge

captured = {{}}


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({{"output": "relayed"}}).encode("utf-8")


def fake_urlopen(req, timeout=None):
    # The real executor built this request; read what it is actually sending.
    captured["body"] = json.loads(req.data.decode("utf-8"))
    return _Resp()


urllib.request.urlopen = fake_urlopen

import speaker_auth

inst = bridge.VoiceBridge.__new__(bridge.VoiceBridge)
inst._mic_queue = asyncio.Queue(maxsize=200)
inst._current_speaker = None
inst._speaker_ledger = speaker_auth.DiscordSpeakerLedger()

# A real packet from the speaker under test, through the real sink callback.
inst.client = SimpleNamespace(loop=SimpleNamespace(call_soon_threadsafe=lambda fn: fn()))
inst._on_mic_pcm(bytes(8), {speaker}, ssrc=1)
_pcm, _ssrc, speaker = inst._mic_queue.get_nowait()
inst._publish_speaker(speaker)

# The REAL ledger, driven by the REAL VAD/response events, so the payload
# below carries a binding this sidecar actually resolved rather than one the
# test asserted into existence.
ledger = inst._speaker_ledger
ledger.record_packet({{"user_id": speaker}}, bytes(20 * 24 * 2))
ledger.note_speech_started({{"item_id": "i1", "audio_start_ms": 0}})
ledger.note_speech_stopped({{"item_id": "i1", "audio_end_ms": {end_ms}}})
create = ledger.response_for_commit({{"item_id": "i1"}})
ledger.note_response_created(
    {{"response": {{"id": "resp_1", "metadata": create["response"]["metadata"]}}}}
)
bound = ledger.bind_tool_event(
    {{"response_id": "resp_1", "call_id": "c1", "name": "homie_command"}}
)

inst._bound_tool_executor("homie_command", {{"command": "diagnostics", "args": ""}}, bound)
print(json.dumps(captured["body"]))
"""


def _payload_from_sidecar_process(speaker: int, *, end_ms: int = 20) -> dict:
    """Run the sidecar half in its own interpreter; return what it would POST.

    `end_ms` is the VAD interval's close. The default matches the audio the
    ledger recorded; a longer one reaches past it, which is how an utterance
    whose attribution cannot be proven is produced with real objects.
    """
    python = _sidecar_python()
    if python is None:  # pragma: no cover - environment guard
        pytest.skip("sidecar venv not provisioned (uv run --project .claude/scripts/discord_voice)")
    script = _SIDECAR_SCRIPT.format(
        sidecar=str(_SIDECAR), speaker=speaker, end_ms=end_ms
    )
    proc = subprocess.run(
        [python, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_SIDECAR),
    )
    assert proc.returncode == 0, f"sidecar process failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def main_process_authorizer(monkeypatch: pytest.MonkeyPatch):
    """The MAIN process's real tool surface + real command registry."""
    import talk_tools
    from commands import CATEGORIES, COMMANDS, CORE_INTENTS
    from core_handlers import CORE_HANDLERS
    from extension_manager import ExtensionManager

    manager = ExtensionManager()
    manager.register_core_commands(COMMANDS, CATEGORIES, CORE_HANDLERS)
    manager.register_core_intents(CORE_INTENTS)
    assert manager.get_command_min_role("diagnostics") == "admin"

    reached: list[str] = []

    async def sentinel(_adapter, _incoming, _args, *, collect_only=False):
        reached.append("diagnostics")
        return "diagnostics ran"

    manager._commands["diagnostics"].handler = sentinel
    monkeypatch.setattr(talk_tools, "_COMMAND_MANAGER", manager)
    monkeypatch.setattr(talk_tools, "_COMMAND_MANAGER_FAILED", False)
    # A browser Talk session was minted in THIS process first — the exact
    # precondition that turned the cross-process gap into stranger-admin.
    monkeypatch.setattr(talk_tools, "_BROWSER_SESSION_ROLE", "admin")

    import config

    monkeypatch.setattr(config, "DISCORD_ALLOWED_USERS", ["555"])

    def serve(payload: dict) -> str:
        """Exactly what `POST /api/talk/tool` does with a relayed payload."""
        return talk_tools.execute_talk_tool(
            payload["name"],
            payload["arguments"],
            transport=payload.get("transport"),
            speaker_id=payload.get("speakerId"),
            binding=payload.get("speakerBinding"),
        )

    return serve, reached


def test_the_operators_spoken_command_succeeds_across_the_boundary(
    main_process_authorizer,
) -> None:
    """The allowlisted operator speaks in the sidecar; the main process runs it."""
    serve, reached = main_process_authorizer

    payload = _payload_from_sidecar_process(555)
    assert payload["speakerId"] == "555", "the speaker must ride the wire"
    assert payload["transport"] == "discord_voice"
    # R7: the id alone is no longer the contract — the sidecar must also send
    # the verdict that it resolved that id from the utterance being answered.
    assert payload["speakerBinding"]["trusted"] is True
    assert payload["speakerBinding"]["token"], "the binding token must ride the wire"

    assert serve(payload) == "diagnostics ran"
    assert reached == ["diagnostics"]


def test_an_unprovable_utterance_carries_no_speaker_across_the_boundary(
    main_process_authorizer,
) -> None:
    """R7 BLOCKER across the REAL boundary.

    The sidecar resolves the allowlisted operator's own id but cannot tie it to
    the interval the model answered. Two independent things must hold: the
    payload it PUTS ON THE WIRE carries no speaker at all, and the main
    process refuses it even though a browser mint is sitting at admin.
    """
    serve, reached = main_process_authorizer

    payload = _payload_from_sidecar_process(555, end_ms=9_000)

    assert payload["speakerId"] is None, "an unprovable utterance must name nobody"
    assert payload["speakerBinding"]["trusted"] is False
    assert payload["speakerBinding"]["reason"]

    assert "was not run" in serve(payload)
    assert reached == [], "an unbound utterance reached an admin handler"


def test_a_stranger_speaking_is_refused_in_the_main_process(
    main_process_authorizer,
) -> None:
    """R6 BLOCKER, the deployment-shaped proof.

    An unallowlisted member speaks an admin-gated command in the SIDECAR
    process. Authorization happens in the MAIN process, which has a browser
    mint sitting at `admin` — under the old code that global is what the call
    read, so the stranger got admin. Now the request carries their id and the
    main process resolves it against its own allowlist: refused, zero handler
    calls, provider budget unspent and doctrine unmutated.
    """
    serve, reached = main_process_authorizer

    payload = _payload_from_sidecar_process(999)
    assert payload["speakerId"] == "999"
    # The sidecar CAN prove who spoke — it just has no say in what they may do.
    assert payload["speakerBinding"]["trusted"] is True

    # R8: the refusal now fires at the tool chokepoint on the resolved role,
    # before the command registry is reached at all. Same outcome, one layer
    # earlier — and the `reached` assertion is the property that matters.
    assert "was not run" in serve(payload)
    assert reached == [], "an unallowlisted speaker reached an admin handler"
