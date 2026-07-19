"""Public-safe contracts for the optional operator-local extension seam."""

from __future__ import annotations

import sys

import pytest

import local_extension_loader as loader


@pytest.fixture(autouse=True)
def _reset_discovery_cache():
    loader.local_extension_modules.cache_clear()
    yield
    loader.local_extension_modules.cache_clear()
    for name in tuple(sys.modules):
        if name == loader._PACKAGE or name.startswith(f"{loader._PACKAGE}."):
            sys.modules.pop(name, None)


def test_missing_local_package_is_a_quiet_empty_registry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(loader, "_LOCAL_DIR", tmp_path / "missing")

    assert loader.local_extension_modules() == ()
    assert loader.apply_local_extension_hook("unknown_hook") == ()
    assert loader.any_local_extension_hook("unknown_hook") is False


def test_discovery_is_anchored_to_the_exact_sibling_directory(tmp_path, monkeypatch) -> None:
    local_dir = tmp_path / "private_extensions"
    local_dir.mkdir()
    (local_dir / "__init__.py").write_text("", encoding="utf-8")
    (local_dir / "sample.py").write_text(
        "def marker(values):\n    values.append('local')\n    return True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_LOCAL_DIR", local_dir)

    values: list[str] = []
    assert loader.any_local_extension_hook("marker", values) is True
    assert values == ["local"]
    assert all(
        str(local_dir) in str(module.__file__)
        for module in loader.local_extension_modules()
    )


@pytest.mark.asyncio
async def test_async_dispatch_stops_at_the_first_explicit_handler(tmp_path, monkeypatch) -> None:
    local_dir = tmp_path / "private_extensions"
    local_dir.mkdir()
    (local_dir / "__init__.py").write_text("", encoding="utf-8")
    (local_dir / "a.py").write_text(
        "async def route(values):\n    values.append('a')\n    return True\n",
        encoding="utf-8",
    )
    (local_dir / "b.py").write_text(
        "def route(values):\n    values.append('b')\n    return True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_LOCAL_DIR", local_dir)

    values: list[str] = []
    assert await loader.dispatch_local_extension_hook("route", values) is True
    assert values == ["a"]
