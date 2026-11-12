"""Connect to the desktop's existing Codex app-server over a local socket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import aiohttp


class AppServerClient:
    """Multiplex RPC responses and live notifications on one shared connection."""

    def __init__(self, socket_path: str, on_event: Callable[[dict], None]) -> None:
        """Prepare a client without starting or modifying the shared daemon.

        Args:
            socket_path: Explicit local socket, or empty for discovery.
            on_event: Synchronous consumer of server notifications.
        """
        self.socket_path = socket_path
        self.on_event = on_event
        self.session = None
        self.ws = None
        self.reader = None
        self.pending = {}
        self.sequence = 0
        self.closed = asyncio.Event()

    async def connect(self) -> None:
        """Discover the existing desktop socket and initialize the connection.

        Raises:
            ValueError: If the desktop socket is missing or ambiguous.
        """
        if os.name != "posix":
            raise ValueError("桌面联动需要本机 Unix socket；此系统请使用 CLI 模式。")
        path = Path(self.socket_path).expanduser() if self.socket_path else None
        if path is None:
            directory = Path(tempfile.gettempdir()) / f"codex-daemon-{os.getuid()}"
            candidates = [p for p in directory.glob("*") if p.is_socket()]
            if len(candidates) != 1:
                raise ValueError(
                    "找不到唯一的 Codex 应用 socket，请先连接本机 Codex 应用，"
                    "或配置 app_server_socket。"
                )
            path = candidates[0]
        if not path.is_absolute() or not path.is_socket():
            raise ValueError("Codex 应用 socket 不存在，请检查 app_server_socket。")
        self.session = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=str(path))
        )
        try:
            self.ws = await asyncio.wait_for(
                self.session.ws_connect("http://localhost", max_msg_size=16 * 1024**2),
                timeout=10,
            )
            self.reader = asyncio.create_task(self._read())
            await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "astrbot_codex", "version": "0.3.1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.ws.send_json({"method": "initialized", "params": {}})
        except BaseException:
            await self.close()
            raise

    async def request(self, method: str, params: dict) -> dict:
        """Send one RPC without retrying side effects after an uncertain response.

        Args:
            method: App-server method name.
            params: Trusted, structured method parameters.

        Returns:
            Server result object.

        Raises:
            RuntimeError: If the connection closes or the server rejects the call.
        """
        if self.ws is None or self.closed.is_set():
            raise RuntimeError("Codex 应用连接已断开。")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.ws.send_json(
                {"id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout=30)
        finally:
            self.pending.pop(request_id, None)

    async def _read(self) -> None:
        """Resolve responses while forwarding notifications to the job monitor."""
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                event = json.loads(message.data)
                request_id = event.get("id")
                if request_id is not None and "method" not in event:
                    future = self.pending.get(request_id)
                    if future is not None and not future.done():
                        if "error" in event:
                            future.set_exception(
                                RuntimeError(
                                    str(event["error"].get("message", "RPC failed"))
                                )
                            )
                        else:
                            future.set_result(event.get("result", {}))
                elif request_id is not None:
                    # Unattended jobs must never silently accept interactive requests.
                    await self.ws.send_json(
                        {
                            "id": request_id,
                            "error": {
                                "code": -32601,
                                "message": "Interactive request unsupported by AstrBot",
                            },
                        }
                    )
                else:
                    self.on_event(event)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self.closed.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex 应用连接已断开。"))

    async def close(self) -> None:
        """Close only this client, leaving the shared desktop daemon running."""
        if self.ws is not None:
            await self.ws.close()
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        if self.session is not None:
            await self.session.close()
