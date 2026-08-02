"""Cross-process Buzz identity lock scoped to relay URL plus Nostr pubkey."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO


class BuzzIdentityLock:
    def __init__(self, relay_url: str, pubkey: str, *, root: Path | None = None):
        digest = hashlib.sha256(f"{relay_url.rstrip('/')}|{pubkey}".encode()).hexdigest()
        self.key = digest
        self.root = root or (Path(tempfile.gettempdir()) / "thehomie-buzz-locks")
        self.path = self.root / f"{digest}.lock"
        self._stream: BinaryIO | None = None

    def acquire(self) -> bool:
        if self._stream is not None:
            return True
        self.root.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> BuzzIdentityLock:
        if not self.acquire():
            raise RuntimeError("Buzz identity is already active for this relay")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
