"""Fail-quiet hooks for operator-local, non-exported framework extensions.

The public framework owns this tiny discovery seam.  Machine- or tenant-specific
code lives in the sibling ``private_extensions`` package, which may be absent in
fresh/public installs.  Extension modules expose named hook functions and receive
the owning registry objects as arguments; they never need to import a partially
initialized shared module.

Discovery and hook failures are isolated and logged without exception details so
private configuration or payloads cannot leak through public diagnostics.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import pkgutil
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

_PACKAGE = "_homie_local_private_extensions"
_LOCAL_DIR = Path(__file__).resolve().parent / "private_extensions"


@lru_cache(maxsize=1)
def local_extension_modules() -> tuple[ModuleType, ...]:
    """Return discovered local extension modules, or an empty tuple.

    A missing package is the normal public-install state.  One broken private
    module does not prevent other local modules or the core framework loading.
    """

    init_path = _LOCAL_DIR / "__init__.py"
    if not _LOCAL_DIR.is_dir() or not init_path.is_file():
        return ()

    try:
        spec = importlib.util.spec_from_file_location(
            _PACKAGE,
            init_path,
            submodule_search_locations=[str(_LOCAL_DIR)],
        )
        if spec is None or spec.loader is None:
            return ()
        package = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE] = package
        spec.loader.exec_module(package)
    except Exception as exc:  # noqa: BLE001 - local code must not break core startup
        sys.modules.pop(_PACKAGE, None)
        logger.warning("Local extension package import failed: %s", type(exc).__name__)
        return ()

    modules: list[ModuleType] = []
    names = sorted(
        info.name
        for info in pkgutil.iter_modules([str(_LOCAL_DIR)], prefix=f"{_PACKAGE}.")
        if not info.name.rsplit(".", 1)[-1].startswith("_")
    )
    for name in names:
        try:
            short_name = name.rsplit(".", 1)[-1]
            module_path = _LOCAL_DIR / f"{short_name}.py"
            spec = importlib.util.spec_from_file_location(name, module_path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            modules.append(module)
        except Exception as exc:  # noqa: BLE001 - isolate a broken local extension
            sys.modules.pop(name, None)
            logger.warning(
                "Local extension module failed to load: %s (%s)",
                name,
                type(exc).__name__,
            )
    return tuple(modules)


def apply_local_extension_hook(hook_name: str, /, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
    """Run a synchronous hook on every discovered module.

    Awaitable results are rejected rather than leaked into synchronous import
    paths.  Callers needing async dispatch use :func:`dispatch_local_extension_hook`.
    """

    results: list[Any] = []
    for module in local_extension_modules():
        hook = getattr(module, hook_name, None)
        if not callable(hook):
            continue
        try:
            result = hook(*args, **kwargs)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                logger.warning(
                    "Local extension hook must be synchronous: %s.%s",
                    module.__name__,
                    hook_name,
                )
                continue
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - isolate local hook failures
            logger.warning(
                "Local extension hook failed: %s.%s (%s)",
                module.__name__,
                hook_name,
                type(exc).__name__,
            )
    return tuple(results)


def any_local_extension_hook(hook_name: str, /, *args: Any, **kwargs: Any) -> bool:
    """Return true when any synchronous local hook explicitly returns true."""

    return any(
        result is True
        for result in apply_local_extension_hook(hook_name, *args, **kwargs)
    )


async def dispatch_local_extension_hook(
    hook_name: str,
    /,
    *args: Any,
    **kwargs: Any,
) -> bool:
    """Run async-or-sync hooks until one explicitly handles the event."""

    for module in local_extension_modules():
        hook = getattr(module, hook_name, None)
        if not callable(hook):
            continue
        try:
            result = hook(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - isolate local hook failures
            logger.warning(
                "Local extension hook failed: %s.%s (%s)",
                module.__name__,
                hook_name,
                type(exc).__name__,
            )
            continue
        if result is True:
            return True
    return False


__all__ = [
    "any_local_extension_hook",
    "apply_local_extension_hook",
    "dispatch_local_extension_hook",
    "local_extension_modules",
]
