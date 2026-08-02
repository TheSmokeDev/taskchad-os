"""Nostr primitives for Buzz, backed by audited ``coincurve`` crypto.

This module owns serialization and Bech32 key conversion only. secp256k1 key
derivation, Schnorr signing, and verification are delegated to coincurve's
libsecp256k1 bindings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

_HEX_32 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_64 = re.compile(r"^[0-9a-fA-F]{128}$")
_BECH32_CHARS = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    chk = 1
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                chk ^= generator
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _convert_bits(data: list[int], from_bits: int, to_bits: int, *, pad: bool) -> bytes:
    accumulator = 0
    bits = 0
    result: list[int] = []
    max_value = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise ValueError("invalid Bech32 data")
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)
    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        raise ValueError("invalid Bech32 padding")
    return bytes(result)


def bech32_decode(value: str, expected_hrp: str) -> bytes:
    if not value or value.lower() != value and value.upper() != value:
        raise ValueError("invalid mixed-case Bech32 value")
    value = value.lower()
    split = value.rfind("1")
    if split < 1 or split + 7 > len(value):
        raise ValueError("invalid Bech32 value")
    hrp, encoded = value[:split], value[split + 1 :]
    if hrp != expected_hrp:
        raise ValueError(f"expected {expected_hrp} key")
    try:
        values = [_BECH32_CHARS.index(char) for char in encoded]
    except ValueError as exc:
        raise ValueError("invalid Bech32 character") from exc
    if _bech32_polymod(_hrp_expand(hrp) + values) != 1:
        raise ValueError("invalid Bech32 checksum")
    return _convert_bits(values[:-6], 5, 8, pad=False)


def _bech32_encode(hrp: str, raw: bytes) -> str:
    data = list(_convert_bits(list(raw), 8, 5, pad=True))
    values = _hrp_expand(hrp) + data + [0] * 6
    polymod = _bech32_polymod(values) ^ 1
    checksum = [(polymod >> (5 * (5 - index))) & 31 for index in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARS[value] for value in data + checksum)


def normalize_private_key(value: str) -> bytes:
    raw = bech32_decode(value, "nsec") if value.lower().startswith("nsec1") else None
    if raw is None:
        if not _HEX_32.fullmatch(value):
            raise ValueError("BUZZ_PRIVATE_KEY must be 64 hex characters or nsec")
        raw = bytes.fromhex(value)
    if len(raw) != 32 or not any(raw):
        raise ValueError("invalid Nostr private key")
    return raw


def normalize_pubkey(value: str) -> str:
    raw = bech32_decode(value, "npub") if value.lower().startswith("npub1") else None
    if raw is None:
        if not _HEX_32.fullmatch(value):
            raise ValueError("Nostr pubkey must be 64 hex characters or npub")
        raw = bytes.fromhex(value)
    if len(raw) != 32:
        raise ValueError("invalid Nostr public key")
    return raw.hex()


def public_key_from_private(private_key: bytes) -> str:
    from coincurve import PublicKeyXOnly

    return PublicKeyXOnly.from_secret(private_key).format().hex()


def npub_from_hex(pubkey: str) -> str:
    normalized = normalize_pubkey(pubkey)
    return _bech32_encode("npub", bytes.fromhex(normalized))


def canonical_event_id(event: dict[str, Any]) -> str:
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def sign_event(event: dict[str, Any], private_key: bytes) -> dict[str, Any]:
    from coincurve import PrivateKey

    signed = dict(event)
    signed["id"] = canonical_event_id(signed)
    signed["sig"] = (
        PrivateKey(private_key)
        .sign_schnorr(bytes.fromhex(signed["id"]), aux_randomness=os.urandom(32))
        .hex()
    )
    return signed


def make_auth_event(
    *, relay_url: str, challenge: str, private_key: bytes, created_at: int
) -> dict[str, Any]:
    event = {
        "pubkey": public_key_from_private(private_key),
        "created_at": int(created_at),
        "kind": 22242,
        "tags": [["relay", relay_url], ["challenge", challenge]],
        "content": "",
    }
    return sign_event(event, private_key)


def verify_event(event: dict[str, Any]) -> bool:
    try:
        if not _HEX_32.fullmatch(str(event.get("id", ""))):
            return False
        if not _HEX_32.fullmatch(str(event.get("pubkey", ""))):
            return False
        if not _HEX_64.fullmatch(str(event.get("sig", ""))):
            return False
        if not isinstance(event.get("created_at"), int):
            return False
        if not isinstance(event.get("kind"), int):
            return False
        if not isinstance(event.get("tags"), list) or not isinstance(event.get("content"), str):
            return False
        if canonical_event_id(event) != event["id"].lower():
            return False
        from coincurve import PublicKeyXOnly

        return PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(
            bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])
        )
    except (KeyError, TypeError, ValueError, ImportError):
        return False
