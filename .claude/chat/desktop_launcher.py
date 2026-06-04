"""Desktop/dev stack launcher for The Homie dashboard."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DesktopLaunchConfig:
    api_port: int = 4322
    dashboard_port: int = 3141
    vite_port: int = 5173
    open_browser: bool = True
    use_vite: bool = True


@dataclass(frozen=True)
class DesktopCommand:
    name: str
    cwd: Path
    argv: list[str]
    env: dict[str, str]


def desktop_target_url(config: DesktopLaunchConfig) -> str:
    return (
        f"http://127.0.0.1:{config.vite_port}/teams"
        if config.use_vite
        else f"http://127.0.0.1:{config.dashboard_port}/teams"
    )


def build_desktop_commands(config: DesktopLaunchConfig) -> list[DesktopCommand]:
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / ".claude" / "scripts"
    server_dir = root / "dashboard" / "server"
    web_dir = root / "dashboard" / "web"
    env = os.environ.copy()
    env.update(
        {
            "ORCHESTRATION_API_PORT": str(config.api_port),
            "DASHBOARD_PORT": str(config.dashboard_port),
            "DASHBOARD_BIND": "127.0.0.1",
            "DASHBOARD_DEV_MODE_NO_AUTH": "true",
            "DASHBOARD_PROXY_TARGET": f"http://127.0.0.1:{config.dashboard_port}",
            "DASHBOARD_WEB_PORT": str(config.vite_port),
            "FRAMEWORK_API_URL": f"http://127.0.0.1:{config.api_port}",
            "VITE_PORT": str(config.vite_port),
        }
    )
    commands = [
        DesktopCommand(
            name="python-api",
            cwd=scripts_dir,
            argv=["uv", "run", "python", "-m", "orchestration.run_api"],
            env=env,
        ),
        DesktopCommand(
            name="hono-dashboard",
            cwd=server_dir,
            argv=["npm", "run", "start"],
            env=env,
        ),
    ]
    if config.use_vite:
        commands.append(
            DesktopCommand(
                name="vite-web",
                cwd=web_dir,
                argv=[
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(config.vite_port),
                ],
                env=env,
            )
        )
    return commands


def build_desktop_shell_command(config: DesktopLaunchConfig) -> DesktopCommand:
    root = Path(__file__).resolve().parents[2]
    desktop_dir = root / "dashboard" / "desktop"
    env = os.environ.copy()
    env.update(
        {
            "ORCHESTRATION_API_PORT": str(config.api_port),
            "DASHBOARD_PORT": str(config.dashboard_port),
            "DASHBOARD_BIND": "127.0.0.1",
            "DASHBOARD_DEV_MODE_NO_AUTH": "true",
            "DASHBOARD_STATIC_DIR": str(root / "dashboard" / "web" / "dist"),
            "FRAMEWORK_API_URL": f"http://127.0.0.1:{config.api_port}",
        }
    )
    return DesktopCommand(
        name="electron-shell",
        cwd=desktop_dir,
        argv=["npm", "run", "start"],
        env=env,
    )


def describe_desktop_launch(config: DesktopLaunchConfig) -> dict[str, object]:
    return {
        "target_url": desktop_target_url(config),
        "commands": [
            {
                "name": command.name,
                "cwd": str(command.cwd),
                "argv": command.argv,
                "env": {
                    key: command.env[key]
                    for key in (
                        "ORCHESTRATION_API_PORT",
                        "DASHBOARD_PORT",
                        "DASHBOARD_BIND",
                        "DASHBOARD_DEV_MODE_NO_AUTH",
                        "DASHBOARD_PROXY_TARGET",
                        "DASHBOARD_WEB_PORT",
                        "FRAMEWORK_API_URL",
                        "VITE_PORT",
                    )
                    if key in command.env
                },
            }
            for command in build_desktop_commands(config)
        ],
    }


def describe_desktop_shell_launch(config: DesktopLaunchConfig) -> dict[str, object]:
    command = build_desktop_shell_command(config)
    return {
        "target_url": f"http://127.0.0.1:{config.dashboard_port}/teams",
        "commands": [
            {
                "name": command.name,
                "cwd": str(command.cwd),
                "argv": command.argv,
                "env": {
                    key: command.env[key]
                    for key in (
                        "ORCHESTRATION_API_PORT",
                        "DASHBOARD_PORT",
                        "DASHBOARD_BIND",
                        "DASHBOARD_DEV_MODE_NO_AUTH",
                        "DASHBOARD_STATIC_DIR",
                        "FRAMEWORK_API_URL",
                    )
                    if key in command.env
                },
            }
        ],
    }


def launch_desktop_shell(config: DesktopLaunchConfig) -> int:
    command = build_desktop_shell_command(config)
    print(f"[desktop] starting {command.name}: {' '.join(command.argv)}")
    return subprocess.call(command.argv, cwd=command.cwd, env=command.env)


def launch_desktop(config: DesktopLaunchConfig) -> int:
    commands = build_desktop_commands(config)
    processes: list[tuple[str, subprocess.Popen]] = []
    target_url = desktop_target_url(config)

    try:
        for command in commands:
            print(f"[desktop] starting {command.name}: {' '.join(command.argv)}")
            proc = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                env=command.env,
            )
            processes.append((command.name, proc))
            time.sleep(1.0)
            if proc.poll() is not None:
                raise RuntimeError(
                    f"{command.name} exited early with code {proc.returncode}"
                )
        print(f"[desktop] dashboard: {target_url}")
        if config.open_browser:
            webbrowser.open(target_url)
        _wait_forever(processes)
        return 0
    except KeyboardInterrupt:
        print("\n[desktop] stopping")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failure.
        print(f"[desktop] error: {exc}", file=sys.stderr)
        return 1
    finally:
        _stop_processes(processes)


def _wait_forever(processes: list[tuple[str, subprocess.Popen]]) -> None:
    while True:
        for name, proc in processes:
            code = proc.poll()
            if code is not None:
                raise RuntimeError(f"{name} exited with code {code}")
        time.sleep(1.0)


def _stop_processes(processes: list[tuple[str, subprocess.Popen]]) -> None:
    for name, proc in reversed(processes):
        if proc.poll() is not None:
            continue
        print(f"[desktop] stopping {name}")
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                proc.terminate()
        except Exception:
            proc.terminate()
    deadline = time.time() + 5
    for _name, proc in reversed(processes):
        if proc.poll() is not None:
            continue
        remaining = max(0.1, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
