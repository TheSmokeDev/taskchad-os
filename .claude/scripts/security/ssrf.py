"""Resolve-and-verify guard for outbound fetches of an operator-supplied URL.

Any surface that lets an operator point the bot at an arbitrary URL (`/learn`,
`/skills link`, #429's linked-skill intake) has THIS process make the request
with THIS process's network identity. A URL that reads as
``https://example.com`` can still resolve to ``127.0.0.1``, a cloud metadata
endpoint (``169.254.169.254``), or an internal RFC1918 address — either
directly, or through a redirect a hostile server issues AFTER the initial
check already passed. A role gate on who may call ``/skills link`` is not an
SSRF defense: every fetch this module guards runs regardless of role, because
the attack is against the FETCHING PROCESS, not against an authorization
boundary.

House pattern (see ``reference_ssrf_safe_fetch_undici`` in memory for the
Node/undici sibling of this guard), which has TWO halves and needs both:

1. DNS lookup returns an ARRAY of addresses for one hostname. A guard that
   checks only the first is bypassable — a hostile authoritative server can
   answer with a public address first and a private one second, and whichever
   the client actually connects to is the one that matters, so EVERY resolved
   address must be checked before the request goes out.
2. The connection must then be PINNED to an address that was checked. A
   pre-fetch DNS check alone is not a rebinding defense: handing the client a
   HOSTNAME means the client resolves it again at connect time, and the second
   answer is the one it dials. :func:`resolve_pinned_target` therefore hands
   the caller a URL whose host is the validated IP LITERAL, plus the ``Host``
   header and the ``sni_hostname`` extension that keep the request (and
   certificate verification) addressed to the original hostname. The undici
   sibling spells this out: the lookup hook "pins undici to an address you
   validated, which a separate pre-fetch dns check cannot".
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

#: Every redirect hop re-validates AND re-pins via ``resolve_pinned_target``
#: before the next request goes out, so a hostile server cannot walk a
#: validated public host to an internal one through a ``Location`` header.
#: This bounds the chain length so a redirect loop cannot hang the caller.
MAX_REDIRECTS = 5

#: Hard cap on a fetched body, read via streaming so a hostile server cannot
#: exhaust memory by simply never closing the connection.
MAX_RESPONSE_BYTES = 2_000_000

#: Connect/read timeouts for the whole fetch (a "time cap", not just a size
#: cap) — a slow-loris response must not hang the caller indefinitely.
CONNECT_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 30.0


class SSRFBlocked(ValueError):
    """*url* (or a redirect target) resolves somewhere it must not."""


@dataclass(frozen=True)
class PinnedTarget:
    """One validated URL, addressed so the client cannot re-resolve it.

    ``url`` carries the validated IP LITERAL in place of the hostname, so the
    TCP connect goes to the address this module checked. ``headers`` and
    ``extensions`` carry the original hostname back into the request:
    ``Host`` keeps virtual hosting working, and ``sni_hostname`` keeps TLS SNI
    — and therefore certificate verification — pointed at the real name (see
    httpcore's ``_connect``: it dials ``origin.host`` and passes
    ``sni_hostname`` as ``server_hostname``).
    """

    url: str
    host: str
    address: str
    headers: dict[str, str] = field(default_factory=dict)
    extensions: dict[str, str] = field(default_factory=dict)


def _is_blocked_address(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Unparsable is refused, not guessed at.
        return True
    # Allow only globally-routable addresses — the INVERSION of a flag list
    # (#429 codex R7 MAJOR / design-seat MINOR 1): `is_private` et al. miss
    # CGNAT (100.64.0.0/10) and 192.88.99.0/24, which Python does not classify
    # as private/reserved even though they are not globally reachable, so a
    # pinned fetch could be steered at shared-address-space internal services.
    return not ip.is_global


def _netloc_for_address(address: str, port: int | None) -> str:
    """Authority component addressing *address* directly (IPv6 bracketed)."""
    literal = f"[{address}]" if ":" in address else address
    return f"{literal}:{port}" if port else literal


def resolve_pinned_target(url: str) -> PinnedTarget:
    """Validate *url* and return the connection PINNED to a checked address.

    Enforces, in order:

    1. ``https://`` only — no ``http``, no ``file:``, no ``ftp:``, nothing
       else. A downgrade to plaintext is also a downgrade in what an
       on-path attacker can rewrite.
    2. No embedded credentials (``user:pass@host``) — a URL shape that is
       never a legitimate "here's a doc" link.
    3. The hostname resolves via ``getaddrinfo`` to at least one address,
       and EVERY resolved address (not just the first) is public —
       excludes private/loopback/link-local/multicast/reserved/unspecified
       ranges (covers RFC1918, ``127.0.0.0/8``, ``169.254.0.0/16`` — the
       cloud metadata range — and their IPv6 equivalents).

    The returned :class:`PinnedTarget` is what closes DNS rebinding: the
    caller connects to ``target.address``, not to a name the HTTP client
    would resolve a SECOND time (a second lookup is a second chance for the
    attacker's authoritative server to answer with an internal address). An
    unresolvable host refuses closed (never treated as "no restriction").
    """
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme != "https":
        raise SSRFBlocked(
            f"refused: only https:// URLs are fetchable, got "
            f"{parsed.scheme or '(none)'!r}"
        )
    host = parsed.hostname
    if not host:
        raise SSRFBlocked("refused: URL has no host")
    if parsed.username or parsed.password:
        raise SSRFBlocked("refused: credentialed URLs are not fetchable")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFBlocked(f"refused: URL has an invalid port: {exc}") from exc

    try:
        addr_infos = socket.getaddrinfo(host, port or 443)
    except OSError as exc:
        raise SSRFBlocked(f"refused: could not resolve {host!r}: {exc}") from exc
    if not addr_infos:
        raise SSRFBlocked(f"refused: {host!r} resolved to no addresses")

    # getaddrinfo returns an ARRAY — check every address, never just [0].
    addresses: list[str] = []
    for info in addr_infos:
        ip_str = info[4][0]
        if _is_blocked_address(ip_str):
            raise SSRFBlocked(
                f"refused: {host!r} resolves to a non-public address "
                f"({ip_str}) — private/loopback/link-local/reserved ranges "
                "are not fetchable"
            )
        addresses.append(ip_str)

    # Any of them is fine — every one was checked above. Pinning to the first
    # is what makes "checked" and "connected to" the same address.
    address = addresses[0]
    pinned = urlunsplit(
        (
            parsed.scheme,
            _netloc_for_address(address, port),
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
    return PinnedTarget(
        url=pinned,
        host=host,
        address=address,
        headers={"Host": f"{host}:{port}" if port else host},
        extensions={"sni_hostname": host},
    )


def assert_public_https_url(url: str) -> str:
    """Validate *url* and return its hostname (see :func:`resolve_pinned_target`).

    Kept for callers that only need the yes/no. A caller that actually makes
    the request should use :func:`resolve_pinned_target` instead — a hostname
    is re-resolved by the HTTP client, which is the rebinding hole this
    module exists to close.
    """
    return resolve_pinned_target(url).host


__all__ = [
    "CONNECT_TIMEOUT_S",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "TOTAL_TIMEOUT_S",
    "PinnedTarget",
    "SSRFBlocked",
    "assert_public_https_url",
    "resolve_pinned_target",
]
