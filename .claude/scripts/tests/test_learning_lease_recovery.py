"""A transient renewal failure cannot permanently erase foreground coverage."""
import asyncio

import pytest

from runtime import activity
from runtime.base import RuntimeRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_failure", [False, True])
async def test_renewal_retries_and_reacquires_until_request_finishes(tmp_path, monkeypatch, initial_failure):
    acquired, released = [], []
    recovered = asyncio.Event()
    ticks = [0]
    renewal_calls = [0]

    def acquire(*args, **kwargs):
        acquired.append(len(acquired) + 1)
        if initial_failure and len(acquired) == 1:
            raise OSError("temporary initial lock")
        return f"lease-{len(acquired)}"

    async def renew(lease):
        renewal_calls[0] += 1
        if renewal_calls[0] == 1:
            raise OSError("temporary renewal lock")
        if renewal_calls[0] == 2:
            return False
        recovered.set()
        return True

    async def tick():
        ticks[0] += 1
        if ticks[0] > 8:
            await asyncio.Event().wait()
        await asyncio.sleep(0)

    monkeypatch.setattr(activity, "acquire_lease", acquire)
    monkeypatch.setattr(activity, "release_lease", lambda lease: released.append(lease))
    monkeypatch.setattr(activity, "_renew_foreground_lease", renew)
    monkeypatch.setattr(activity, "_wait_foreground_refresh", tick)
    async with activity.foreground_request(RuntimeRequest("test", tmp_path, "foreground", conversational=True)):
        await asyncio.wait_for(recovered.wait(), 3)
    assert len(acquired) >= 2
    assert released == [f"lease-{len(acquired)}"]
