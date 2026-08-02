"""Talk mode — OpenAI Realtime browser session minting.

Server-side half of the Homie Talk slice (OpenClaw PR #100671 port):
resolves OpenAI Platform auth via ``runtime.openai_platform_auth``
(configured key -> OPENAI_API_KEY -> Codex OAuth), assembles the Homie
persona instructions, and mints an ephemeral Realtime client secret. The
browser only ever receives the ephemeral secret — the underlying API key /
OAuth token never leaves this process.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

import config
import talk_tools
from runtime import openai_platform_auth
from security import kill_switches

DEFAULT_TALK_MODEL = "gpt-realtime-2.1"
DEFAULT_TALK_VOICE = "cedar"
OPENAI_REALTIME_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)
OPENAI_REALTIME_OFFER_URL = "https://api.openai.com/v1/realtime/calls"
_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
_INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
_MINT_TIMEOUT_S = 30.0
_MAX_SOUL_CHARS = 12_000

#: Which identity files ride the voice prompt, and how much of each. A Realtime
#: session prompt is paid for on EVERY turn, so this is a budget, not a
#: preference — SOUL is the behavioural contract and keeps the largest share.
_IDENTITY_CAPS: dict[str, int] = {
    "SOUL": _MAX_SOUL_CHARS,
    "USER": 4_000,
    "MEMORY": 6_000,
    "WORKING": 2_000,
}

#: Render order. SOUL first: if anything is going to be skimmed by the model,
#: it must not be the rules.
_IDENTITY_ORDER: tuple[str, ...] = ("SOUL", "USER", "MEMORY", "WORKING")
_DEFAULT_IDENTITY_INCLUDE: tuple[str, ...] = ("SOUL",)

#: Section headers — what each file IS, so the model knows how to weigh it.
_IDENTITY_HEADERS: dict[str, str] = {
    "SOUL": "Your standing identity and behavior rules",
    "USER": "Who you are talking to",
    "MEMORY": "What you already know (durable memory — do not ask for these)",
    "WORKING": "What is currently open (working memory)",
}

_IDENTITY_INCLUDE_ENV = "TALK_IDENTITY_INCLUDE"

_VOICE_PREAMBLE = (
    "You are The Homie — owner's personal AI partner and second brain — "
    "speaking live over a voice call. Reply conversationally: natural, "
    "spoken-style, one to three sentences unless owner asks for depth. "
    "Everything you say is spoken aloud, so no markdown, no bullet lists, "
    "no emoji, no code blocks. If you do not know something, say so plainly. "
    "You have function tools for facts and actions: use them when owner asks "
    "for anything you could not know offhand (his memory vault, calendar, "
    "business stats, commands, or delegating real work). After a tool "
    "result, answer in one to three spoken sentences — never read raw "
    "output verbatim. "
    "You can also deploy real work and drive this computer: run_archon fires "
    "heavy workflows including full CLUTCH team builds, delegate_task runs a "
    "background agent, computer opens terminals and apps and types into "
    "windows and looks at the screen, and browse drives his visible Chrome. "
    "Judge how much permission something needs by what it can DAMAGE, not by "
    "how big it feels. Work that lands in an isolated worktree and costs only "
    "tokens — Archon workflows, CLUTCH, code, research, drafts — you fire on "
    "his word: say in one short sentence what you are about to run, then run "
    "it, no confirmation. Plain lookups need not even that. But anything that "
    "spends real money, reaches a real person, or deploys to PRODUCTION — a "
    "paid render, a trade, a text or DM to a customer, a social post, a live "
    "deploy — you prepare fully and then STOP and ask before it fires; a "
    "preview deploy is free and needs no approval. If a workflow you started "
    "reaches something expensive it pauses itself and asks. "
    "When you deploy through Archon — run_archon, or delegate_task with scope "
    "substantial — the brief you pass is the ONLY thing the worker ever sees. "
    "It starts in a fresh checkout of the repo with no access to this call, "
    "so never pass 'yeah do that', 'what we discussed', or any pointer back "
    "to what was said. Write the whole task out yourself from the "
    "conversation: what to build, where it lives, and what done looks like, "
    "as if to someone who never heard a word of it. A brief that only points "
    "back gets refused and you will have to restate it. "
    "When a tool returns a WORK_STARTED receipt, tell owner "
    "it is running and move on: the result is handed to you when it lands, "
    "and you summarize it in a sentence or two. If he asks how the work is "
    "going, use check_work. "
    "You can also correct a run that is already going, with manage_run. If a "
    "run is paused it is waiting on owner: tell him what it is asking, and "
    "when he answers, use approve, reject, or say — 'say' sends his own words "
    "to the run, and on a paused run that IS the approval, so it can only "
    "approve, never refuse. Never invent an approval phrase; the gate's own "
    "phrase is attached for you. Background agents from delegate_task steer "
    "too: 'say' with the receipt number QUEUES owner's words for the agent's "
    "next turn boundary — tell him it's queued, not instant — and 'cancel' "
    "stops the agent now. Rejecting, cancelling or abandoning destroys "
    "work in flight, so call manage_run once without confirm, tell owner what "
    "would happen in one sentence, and only call again with confirm after he "
    "says yes."
)


class TalkSessionError(Exception):
    """Base Talk session error."""


class TalkAuthError(TalkSessionError):
    """No usable OpenAI Platform credential for Talk mode."""


class TalkUpstreamError(TalkSessionError):
    """OpenAI Realtime client-secret mint failed upstream."""


@dataclass(frozen=True, slots=True)
class TalkSessionDescriptor:
    """Browser-facing Talk session metadata (ephemeral secret only)."""

    client_secret: str
    expires_at_ms: int | None
    offer_url: str
    model: str
    voice: str
    auth_source: str

    def to_wire(self) -> dict:
        return {
            "clientSecret": self.client_secret,
            "expiresAt": self.expires_at_ms,
            "offerUrl": self.offer_url,
            "model": self.model,
            "voice": self.voice,
            "authSource": self.auth_source,
        }


def talk_openai_model() -> str:
    """Resolve the Realtime model at call time."""

    return (os.environ.get("TALK_OPENAI_MODEL") or DEFAULT_TALK_MODEL).strip() or DEFAULT_TALK_MODEL


def talk_openai_voice() -> str:
    """Resolve the Realtime voice at call time, fail-closed on unknown ids."""

    raw = (os.environ.get("TALK_OPENAI_VOICE") or DEFAULT_TALK_VOICE).strip().lower() or DEFAULT_TALK_VOICE
    if raw not in OPENAI_REALTIME_VOICES:
        raise TalkSessionError(
            f"TALK_OPENAI_VOICE '{raw}' is not a built-in Realtime voice "
            f"({', '.join(OPENAI_REALTIME_VOICES)})"
        )
    return raw


def talk_configured_api_key() -> str | None:
    """Return the Talk-scoped configured key (``TALK_OPENAI_API_KEY``).

    ``None`` when unset; a set-but-blank value is passed through so the
    resolver can fail closed instead of silently falling through.
    """

    raw = os.environ.get("TALK_OPENAI_API_KEY")
    return raw if raw is not None else None


def _identity_include() -> tuple[str, ...]:
    """Which identity files to carry. Rule 1 — resolved at CALL time.

    The safe default sends SOUL only. Comma-separated
    ``TALK_IDENTITY_INCLUDE`` is the operator opt-in for USER, MEMORY, WORKING,
    GOALS, or SELF. Unknown names are dropped rather than raising: a typo in a
    knob must not silence the voice surface.
    """

    raw = os.environ.get(_IDENTITY_INCLUDE_ENV, "").strip()
    if not raw:
        return _DEFAULT_IDENTITY_INCLUDE
    names = tuple(
        part.strip().upper() for part in raw.split(",") if part.strip()
    )
    return names or _DEFAULT_IDENTITY_INCLUDE


def _soul_only_instructions() -> str:
    """The pre-identity behaviour, preserved verbatim as the fail-open path."""

    soul = ""
    try:
        soul_path = config.SOUL_FILE
        if soul_path.exists():
            soul = soul_path.read_text(encoding="utf-8").strip()[:_MAX_SOUL_CHARS]
    except OSError:
        soul = ""
    if not soul:
        return _VOICE_PREAMBLE
    return f"{_VOICE_PREAMBLE}\n\nYour standing identity and behavior rules:\n\n{soul}"


def build_talk_instructions() -> str:
    """Assemble the Realtime session prompt server-side.

    The browser never supplies instructions. The voice preamble plus SOUL are
    the safe provider-data default. Additional identity files travel only when
    the operator explicitly configures ``TALK_IDENTITY_INCLUDE``.

    All reads still go through ``cognition.identity_payload``, the same shim
    used by chat and cognition. Fail open to the SOUL-only prompt on any read
    failure; a voice surface without optional recall is preferable to an
    outage or accidental provider-data expansion.
    """

    payload: dict[str, str] = {}
    try:
        # M4 import-order pattern (memory_reflect.py) — the established way the
        # scripts slice reaches the chat slice.
        chat_dir = Path(__file__).resolve().parent.parent / "chat"
        if str(chat_dir) not in sys.path:
            sys.path.insert(0, str(chat_dir))
        from cognition.identity_payload import build_identity_payload

        payload = build_identity_payload(
            config.MEMORY_DIR, include=_identity_include()
        )
    except Exception:  # noqa: BLE001 — the voice surface must still come up
        payload = {}

    sections: list[str] = []
    for name in _identity_include():
        body = (payload.get(name) or "").strip()
        if not body:
            continue
        cap = _IDENTITY_CAPS.get(name, 4_000)
        header = _IDENTITY_HEADERS.get(name, name.title())
        sections.append(f"{header}:\n\n{body[:cap]}")

    if not sections:
        return _soul_only_instructions()
    return _VOICE_PREAMBLE + "\n\n" + "\n\n".join(sections)


def build_session_payload(*, model: str, voice: str, instructions: str, tools: list[dict] | None = None) -> dict:
    """OpenAI Realtime session config (mirrors OpenClaw's browser session)."""

    payload: dict = {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "audio": {
            "input": {
                "noise_reduction": {"type": "near_field"},
                "turn_detection": {
                    "type": "server_vad",
                    "create_response": True,
                    "interrupt_response": True,
                },
                "transcription": {"model": _INPUT_TRANSCRIPTION_MODEL},
            },
            "output": {"voice": voice},
        },
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _post_client_secret(auth_token: str, session: dict) -> dict:
    """POST the session to the client_secrets endpoint. Isolated for tests."""

    response = httpx.post(
        _CLIENT_SECRETS_URL,
        json={"session": session},
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        },
        timeout=_MINT_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise TalkUpstreamError(
            f"OpenAI Realtime client secret failed ({response.status_code}): "
            f"{response.text[:300] or response.reason_phrase}"
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise TalkUpstreamError("OpenAI Realtime client secret returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise TalkUpstreamError("OpenAI Realtime client secret returned invalid payload")
    return payload


def _parse_client_secret(payload: dict) -> tuple[str, int | None]:
    """Accept both flat ``{value, expires_at}`` and nested client_secret shapes."""

    value = payload.get("value")
    nested = payload.get("client_secret")
    if not isinstance(value, str) or not value:
        value = nested.get("value") if isinstance(nested, dict) else None
    if not isinstance(value, str) or not value:
        raise TalkUpstreamError("OpenAI Realtime client secret response did not include a value")
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)) and isinstance(nested, dict):
        expires_at = nested.get("expires_at")
    expires_at_ms = int(expires_at * 1000) if isinstance(expires_at, (int, float)) else None
    return value, expires_at_ms


def create_talk_session(*, voice: str | None = None, model: str | None = None) -> TalkSessionDescriptor:
    """Mint an ephemeral Realtime client secret for one browser Talk session."""

    kill_switches.requireEnabled("voice", caller="talk_session")

    selected_voice = (voice or "").strip().lower() or talk_openai_voice()
    if selected_voice not in OPENAI_REALTIME_VOICES:
        raise TalkSessionError(
            f"voice '{selected_voice}' is not a built-in Realtime voice "
            f"({', '.join(OPENAI_REALTIME_VOICES)})"
        )
    selected_model = (model or "").strip() or talk_openai_model()

    try:
        auth = openai_platform_auth.resolve_openai_platform_auth(
            configured_api_key=talk_configured_api_key()
        )
    except openai_platform_auth.OpenAIPlatformAuthError as exc:
        raise TalkAuthError(str(exc)) from exc

    session = build_session_payload(
        model=selected_model,
        voice=selected_voice,
        instructions=build_talk_instructions(),
        tools=talk_tools.default_talk_tools(),
    )
    try:
        payload = _post_client_secret(auth.token, session)
    except TalkUpstreamError as exc:
        if "(401)" in str(exc):
            remediation = (
                "the configured OpenAI API key was rejected"
                if auth.source != openai_platform_auth.SOURCE_CODEX_OAUTH
                else "the Codex OAuth token was rejected — run `codex login` to refresh your sign-in"
            )
            raise TalkUpstreamError(f"OpenAI Realtime auth failed (401): {remediation}") from exc
        raise
    secret, expires_at_ms = _parse_client_secret(payload)

    return TalkSessionDescriptor(
        client_secret=secret,
        expires_at_ms=expires_at_ms,
        offer_url=OPENAI_REALTIME_OFFER_URL,
        model=selected_model,
        voice=selected_voice,
        auth_source=auth.source,
    )


def talk_status() -> dict:
    """Operator-facing Talk status — which auth source would be used."""

    status = openai_platform_auth.openai_platform_auth_status(
        configured_api_key=talk_configured_api_key()
    )
    return {
        **status,
        "model": talk_openai_model(),
        "voice": (
            os.environ.get("TALK_OPENAI_VOICE", "").strip().lower() or DEFAULT_TALK_VOICE
        ),
        "voices": list(OPENAI_REALTIME_VOICES),
        "tools": [tool["name"] for tool in talk_tools.default_talk_tools()],
        "killSwitchVoiceDisabled": kill_switches.is_disabled("voice"),
    }


__all__ = [
    "DEFAULT_TALK_MODEL",
    "DEFAULT_TALK_VOICE",
    "OPENAI_REALTIME_OFFER_URL",
    "OPENAI_REALTIME_VOICES",
    "TalkAuthError",
    "TalkSessionDescriptor",
    "TalkSessionError",
    "TalkUpstreamError",
    "build_session_payload",
    "build_talk_instructions",
    "create_talk_session",
    "talk_configured_api_key",
    "talk_openai_model",
    "talk_openai_voice",
    "talk_status",
]
