"""Profile-owned configuration for the native Buzz collaboration adapter.

Secrets are deliberately kept out of status dictionaries and dataclass reprs.
All values are resolved at call time so ``thehomie -p <profile>`` retains the
existing profile isolation contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _pubkey_roles(value: str) -> tuple[tuple[str, str], ...]:
    """Parse ``pubkey=role`` entries without treating them as authorization yet.

    Pubkey normalization and role validation happen during adapter connection so
    malformed profile configuration fails closed through the normal Buzz status
    surface instead of becoming an import-time environment error.
    """
    parsed: list[tuple[str, str]] = []
    for item in _csv(value):
        pubkey, separator, role = item.partition("=")
        parsed.append((pubkey.strip(), role.strip().lower() if separator else ""))
    return tuple(parsed)


@dataclass(frozen=True)
class BuzzSettings:
    relay_url: str
    private_key: str = field(repr=False)
    allowed_pubkeys: tuple[str, ...]
    channels: tuple[str, ...]
    home_channel: str
    signal_channel: str
    cli_path: str
    transport: str
    require_mention: bool
    desktop_path: str
    pubkey_roles: tuple[tuple[str, str], ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.relay_url and self.private_key and self.allowed_pubkeys)

    @property
    def relay_host(self) -> str:
        parsed = urlparse(self.relay_url)
        return parsed.hostname or ""

    def missing_required(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.relay_url:
            missing.append("BUZZ_RELAY_URL")
        if not self.private_key:
            missing.append("BUZZ_PRIVATE_KEY")
        if not self.allowed_pubkeys:
            missing.append("BUZZ_ALLOWED_PUBKEYS")
        return tuple(missing)


def get_buzz_settings(environ: dict[str, str] | None = None) -> BuzzSettings:
    env = os.environ if environ is None else environ
    transport = env.get("BUZZ_TRANSPORT", "auto").strip().lower() or "auto"
    if transport not in {"auto", "websocket", "poll"}:
        transport = "auto"
    return BuzzSettings(
        relay_url=env.get("BUZZ_RELAY_URL", "").strip(),
        private_key=env.get("BUZZ_PRIVATE_KEY", "").strip(),
        allowed_pubkeys=_csv(env.get("BUZZ_ALLOWED_PUBKEYS", "")),
        channels=_csv(env.get("BUZZ_CHANNELS", "")),
        home_channel=env.get("BUZZ_HOME_CHANNEL", "").strip(),
        signal_channel=env.get("BUZZ_SIGNAL_CHANNEL", "").strip(),
        cli_path=env.get("BUZZ_CLI_PATH", "buzz").strip() or "buzz",
        transport=transport,
        require_mention=env.get("BUZZ_REQUIRE_MENTION", "true").strip().lower()
        not in {"0", "false", "no", "off"},
        desktop_path=env.get("BUZZ_DESKTOP_PATH", "").strip(),
        pubkey_roles=_pubkey_roles(env.get("BUZZ_PUBKEY_ROLES", "")),
    )


def buzz_state_path() -> Path:
    from config import STATE_DIR

    return STATE_DIR / "buzz-state.db"


def buzz_runtime_status_path() -> Path:
    from config import STATE_DIR

    return STATE_DIR / "buzz-runtime-status.json"


def buzz_media_dir() -> Path:
    """Profile-owned directory for bounded inbound media materialization."""
    from config import STATE_DIR

    return STATE_DIR / "buzz-media"
