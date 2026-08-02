from __future__ import annotations

import time

from buzz_nostr import (
    make_auth_event,
    normalize_private_key,
    normalize_pubkey,
    npub_from_hex,
    public_key_from_private,
    sign_event,
    verify_event,
)
from coincurve import PrivateKey


def test_nip42_event_is_signed_and_verifiable() -> None:
    private_key = PrivateKey().secret
    event = make_auth_event(
        relay_url="ws://localhost:3000",
        challenge="relay-challenge",
        private_key=private_key,
        created_at=int(time.time()),
    )

    assert event["kind"] == 22242
    assert ["relay", "ws://localhost:3000"] in event["tags"]
    assert ["challenge", "relay-challenge"] in event["tags"]
    assert verify_event(event) is True


def test_signature_and_event_id_tampering_are_rejected() -> None:
    private_key = PrivateKey().secret
    event = sign_event(
        {
            "pubkey": public_key_from_private(private_key),
            "created_at": int(time.time()),
            "kind": 9,
            "tags": [["h", "room-1"]],
            "content": "hello",
        },
        private_key,
    )
    tampered = dict(event, content="different")
    bad_signature = dict(event, sig="00" * 64)

    assert verify_event(event) is True
    assert verify_event(tampered) is False
    assert verify_event(bad_signature) is False


def test_npub_round_trip_and_hex_private_key() -> None:
    private_key = PrivateKey().secret
    pubkey = public_key_from_private(private_key)
    npub = npub_from_hex(pubkey)

    assert normalize_private_key(private_key.hex()) == private_key
    assert normalize_pubkey(npub) == pubkey
