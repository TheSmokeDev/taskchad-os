"""Tests for security.ssrf — the resolve-and-verify SSRF guard for outbound
fetches of an operator-supplied URL (#429 round-2 BLOCKER: the fetch itself
must be hardened regardless of the caller's role).
"""

from __future__ import annotations

import socket

import pytest

from security import ssrf


def _fake_getaddrinfo(addresses):
    def _fn(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))
            for addr in addresses
        ]

    return _fn


# --------------------------------------------------------------------------- #
# assert_public_https_url
# --------------------------------------------------------------------------- #


def test_rejects_non_https_scheme():
    with pytest.raises(ssrf.SSRFBlocked, match="https"):
        ssrf.assert_public_https_url("http://example.com/doc")


def test_rejects_file_scheme():
    with pytest.raises(ssrf.SSRFBlocked):
        ssrf.assert_public_https_url("file:///etc/passwd")


def test_rejects_missing_host():
    with pytest.raises(ssrf.SSRFBlocked, match="no host"):
        ssrf.assert_public_https_url("https:///path")


def test_rejects_credentialed_url():
    with pytest.raises(ssrf.SSRFBlocked, match="credentialed"):
        ssrf.assert_public_https_url("https://user:pass@example.com/doc")


def test_rejects_loopback_resolution(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["127.0.0.1"]))
    with pytest.raises(ssrf.SSRFBlocked, match="non-public"):
        ssrf.assert_public_https_url("https://evil.example.com/doc")


def test_rejects_private_rfc1918_resolution(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["10.0.0.5"]))
    with pytest.raises(ssrf.SSRFBlocked, match="non-public"):
        ssrf.assert_public_https_url("https://evil.example.com/doc")


def test_rejects_link_local_metadata_endpoint(monkeypatch):
    """169.254.169.254 — the cloud metadata endpoint. The single highest-value
    SSRF target; must never pass."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["169.254.169.254"]))
    with pytest.raises(ssrf.SSRFBlocked, match="non-public"):
        ssrf.assert_public_https_url("https://evil.example.com/doc")


def test_checks_every_resolved_address_not_just_the_first(monkeypatch):
    """A hostname with multiple A records — a public address FIRST and a
    private one SECOND — must still be refused. Checking only index [0]
    would let the attacker order the records so the guard passes while the
    client still might connect to the private one."""
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34", "127.0.0.1"])
    )
    with pytest.raises(ssrf.SSRFBlocked, match="non-public"):
        ssrf.assert_public_https_url("https://evil.example.com/doc")


def test_rejects_unresolvable_host(monkeypatch):
    def _boom(host, port, *args, **kwargs):
        raise OSError("name resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(ssrf.SSRFBlocked, match="could not resolve"):
        ssrf.assert_public_https_url("https://nowhere.invalid/doc")


def test_accepts_a_genuinely_public_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))
    assert ssrf.assert_public_https_url("https://example.com/doc") == "example.com"


# --------------------------------------------------------------------------- #
# resolve_pinned_target — the half a pre-fetch check cannot cover (M2)
# --------------------------------------------------------------------------- #


def test_pins_the_connection_to_the_address_it_validated(monkeypatch):
    """The returned URL addresses the CHECKED IP; the hostname survives only
    as the Host header and the TLS SNI, so certificate verification is
    unchanged while the client has no name left to resolve."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))

    target = ssrf.resolve_pinned_target("https://example.com/doc?q=1")

    assert target.url == "https://93.184.216.34/doc?q=1"
    assert target.address == "93.184.216.34"
    assert target.host == "example.com"
    assert target.headers == {"Host": "example.com"}
    assert target.extensions == {"sni_hostname": "example.com"}


def test_pinned_target_survives_a_rebinding_resolver(monkeypatch):
    """First lookup public, every later lookup private — the shape of a DNS
    rebinding attack. The pin is taken from the lookup that was CHECKED, so a
    second resolution cannot change where the request goes."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"], ["127.0.0.1"]])

    def _flapping(host, port, *args, **kwargs):
        return _fake_getaddrinfo(next(answers))(host, port)

    monkeypatch.setattr(socket, "getaddrinfo", _flapping)

    target = ssrf.resolve_pinned_target("https://evil.example.com/doc")
    assert target.url == "https://93.184.216.34/doc"
    assert "evil.example.com" not in target.url


def test_pinned_target_preserves_a_nondefault_port(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))
    target = ssrf.resolve_pinned_target("https://example.com:8443/doc")
    assert target.url == "https://93.184.216.34:8443/doc"
    assert target.headers["Host"] == "example.com:8443"


def test_pinned_target_brackets_an_ipv6_literal(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["2606:2800:220::1"]))
    target = ssrf.resolve_pinned_target("https://example.com/doc")
    assert target.url == "https://[2606:2800:220::1]/doc"


def test_pinned_target_refuses_the_same_things_the_assert_does(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["10.0.0.5"]))
    with pytest.raises(ssrf.SSRFBlocked, match="non-public"):
        ssrf.resolve_pinned_target("https://evil.example.com/doc")


def test_rejects_cgnat_shared_address_space(monkeypatch):
    """#429 codex R7 MAJOR: 100.64.0.0/10 (CGNAT / Tailscale-style shared
    space) is neither private nor reserved in Python's classification, so the
    old flag-list guard ALLOWED it — a pinned fetch could be steered at an
    internal HTTPS service in that space. The guard now allows only
    globally-routable addresses (``not ip.is_global``)."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["100.100.100.200"]))
    with pytest.raises(ssrf.SSRFBlocked, match="non-public"):
        ssrf.assert_public_https_url("https://evil.example.com/doc")


def test_rejects_documentation_and_benchmark_ranges(monkeypatch):
    """The same inversion covers the other non-global ranges the flag list
    missed (192.0.0.0/24, 198.18.0.0/15 benchmarking, 240.0.0.0/4).
    192.88.99.0/24 (deprecated 6to4 anycast) still reads as global in
    Python's registry — noted, accepted, not load-bearing here."""
    for addr in ("192.0.0.8", "198.18.0.1", "240.0.0.1"):
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo([addr]))
        with pytest.raises(ssrf.SSRFBlocked, match="non-public"):
            ssrf.assert_public_https_url("https://evil.example.com/doc")
