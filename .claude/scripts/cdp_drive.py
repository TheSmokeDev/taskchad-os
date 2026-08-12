"""Minimal CDP driver for the Homie browser on 127.0.0.1:18222.

Usage (from .claude/scripts, so the venv provides websockets):
  uv run python cdp_drive.py eval "<js expression>" [--url-contains bing.com]
  uv run python cdp_drive.py nav "https://example.com" [--url-contains bing.com] [--wait 5]

Prints the evaluation result (ascii-safe). Avoids agent-browser daemon issues;
talks raw CDP over the page websocket.
"""

import asyncio
import json
import sys
import urllib.request

CDP_LIST = "http://127.0.0.1:18222/json/list"


def pick_tab(url_contains: str | None):
    tabs = json.load(urllib.request.urlopen(CDP_LIST))
    pages = [t for t in tabs if t.get("type") == "page"]
    if url_contains:
        hits = [t for t in pages if url_contains in t.get("url", "")]
        if not hits:
            raise SystemExit(f"no tab containing {url_contains!r}; open pages: {[p['url'][:60] for p in pages]}")
        return hits[0]
    return pages[-1]


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a.split("=", 1)[0]: (a.split("=", 1)[1] if "=" in a else True) for a in sys.argv[1:] if a.startswith("--")}
    cmd, payload = args[0], args[1]
    url_contains = flags.get("--url-contains") if isinstance(flags.get("--url-contains"), str) else None
    tab = pick_tab(url_contains)

    import websockets

    mid = 0

    async def call(ws, method, params=None):
        nonlocal mid
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == mid:
                return msg

    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=10 * 1024 * 1024) as ws:
        if cmd == "nav":
            await call(ws, "Page.enable")
            await call(ws, "Page.navigate", {"url": payload})
            wait = flags.get("--wait")
            await asyncio.sleep(float(wait) if isinstance(wait, str) else 5)
            r = await call(ws, "Runtime.evaluate", {"expression": "document.title + ' ||| ' + location.href", "returnByValue": True})
        else:
            r = await call(ws, "Runtime.evaluate", {"expression": payload, "returnByValue": True})
        val = r.get("result", {}).get("result", {}).get("value", "")
        print(str(val).encode("ascii", "replace").decode())


if __name__ == "__main__":
    asyncio.run(main())
