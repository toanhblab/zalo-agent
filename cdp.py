"""Minimal Chrome DevTools Protocol client for a local Electron app."""
from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any

import websockets


class CDPError(RuntimeError):
    pass


def list_targets(port: int = 9222) -> list[dict[str, Any]]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as r:
        return json.load(r)


def pick_target(port: int = 9222, title: str | None = None,
                url_contains: str = "index.html") -> dict[str, Any]:
    pages = [t for t in list_targets(port) if t.get("type") == "page"]
    if not pages:
        raise CDPError("No page target. Launch the app with --remote-debugging-port.")
    if title:
        named = [t for t in pages if t.get("title") == title]
        if named:
            return named[0]
    hits = [t for t in pages if url_contains in (t.get("url") or "")]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise CDPError(f"No page target whose URL contains {url_contains!r}.")
    raise CDPError(f"Ambiguous page targets: {[t.get('title') for t in hits]}")


class CDP:
    """One websocket to one page target."""

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._ws: websockets.ClientConnection | None = None
        self._next_id = 1

    async def __aenter__(self) -> "CDP":
        self._ws = await websockets.connect(self.ws_url, max_size=64 * 1024 * 1024)
        await self.send("Runtime.enable")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def send(self, method: str, params: dict[str, Any] | None = None,
                   timeout: float = 15.0) -> dict[str, Any]:
        if self._ws is None:
            raise CDPError("Not connected.")
        message_id = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({"id": message_id, "method": method,
                                        "params": params or {}}))
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CDPError(f"{method} timed out.")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            message = json.loads(raw)
            if message.get("id") != message_id:
                continue  # event or an earlier reply; ignore
            if "error" in message:
                raise CDPError(f"{method}: {message['error']}")
            return message.get("result", {})

    async def evaluate(self, expression: str, timeout: float = 15.0) -> Any:
        """Run JS in the page and return its JSON value.

        The expression is wrapped so it may use `await` and must return a
        JSON-serialisable value.
        """
        wrapped = (
            "(async () => { const __r = await (async () => {"
            + expression
            + "\n})(); return JSON.stringify(__r === undefined ? null : __r); })()"
        )
        result = await self.send("Runtime.evaluate", {
            "expression": wrapped,
            "awaitPromise": True,
            "returnByValue": True,
        }, timeout=timeout)
        details = result.get("exceptionDetails")
        if details:
            text = (details.get("exception") or {}).get("description") or details.get("text")
            raise CDPError(f"JS error: {text}")
        value = (result.get("result") or {}).get("value")
        return json.loads(value) if isinstance(value, str) else value


async def connect(port: int = 9222, title: str | None = None,
                  url_contains: str = "index.html") -> CDP:
    target = pick_target(port, title=title, url_contains=url_contains)
    return CDP(target["webSocketDebuggerUrl"])
