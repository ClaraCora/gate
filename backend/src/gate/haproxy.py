from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Literal

from gate.errors import GateError


class HaProxyError(GateError):
    code = "HAPROXY_ERROR"


CommandSender = Callable[[str], Awaitable[str]]
ServerState = Literal["ready", "drain", "maint"]


class HaProxyRuntime:
    def __init__(
        self,
        socket_path: Path = Path("/run/haproxy/gate-admin.sock"),
        *,
        timeout_seconds: float = 5.0,
        sender: CommandSender | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self.sender = sender

    async def _send(self, command: str) -> str:
        if "\n" in command or "\r" in command:
            raise HaProxyError("HAProxy runtime command contains a newline")
        if self.sender is not None:
            return await self.sender(command)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path),
                timeout=self.timeout_seconds,
            )
            writer.write(command.encode("ascii") + b"\n")
            await writer.drain()
            writer.write_eof()
            raw = await asyncio.wait_for(reader.read(), timeout=self.timeout_seconds)
        except (OSError, TimeoutError) as exc:
            raise HaProxyError(f"HAProxy runtime socket failed: {exc}") from exc
        finally:
            if "writer" in locals():
                writer.close()
                await writer.wait_closed()
        response = raw.decode("utf-8", errors="replace").strip()
        if response.startswith("Unknown command") or response.startswith("Can't find"):
            raise HaProxyError(response)
        return response

    @staticmethod
    def _server(region_id: str, slot: str) -> str:
        if not region_id.replace("-", "").isalnum() or slot not in {"a", "b"}:
            raise HaProxyError("invalid HAProxy region or slot")
        return f"gate_{region_id}_slots/{region_id}-{slot}"

    async def ready(self, region_id: str, slot: str) -> None:
        server = self._server(region_id, slot)
        await self._send(f"set server {server} state ready")
        await self._send(f"set server {server} health up")

    async def drain(self, region_id: str, slot: str) -> None:
        await self._send(f"set server {self._server(region_id, slot)} state drain")

    async def disable(self, region_id: str, slot: str) -> None:
        await self._send(f"set server {self._server(region_id, slot)} state maint")

    async def snapshot(self, region_ids: Iterable[str]) -> dict[tuple[str, str], ServerState]:
        expected = {
            (f"gate_{region_id}_slots", f"{region_id}-{slot}"): (region_id, slot)
            for region_id in region_ids
            for slot in ("a", "b")
        }
        response = await self._send("show stat")
        states: dict[tuple[str, str], ServerState] = {}
        for row in csv.DictReader(io.StringIO(response)):
            key = (row.get("# pxname", ""), row.get("svname", ""))
            server = expected.get(key)
            if server is None or row.get("type") != "2":
                continue
            status = row.get("status", "").upper()
            if status.startswith("UP"):
                states[server] = "ready"
            elif status.startswith(("DRAIN", "NOLB")):
                states[server] = "drain"
            else:
                states[server] = "maint"
        missing = set(expected.values()) - states.keys()
        if missing:
            names = ", ".join(f"{region_id}/{slot}" for region_id, slot in sorted(missing))
            raise HaProxyError(f"HAProxy runtime snapshot is incomplete: {names}")
        return states

    async def restore(self, states: Mapping[tuple[str, str], ServerState]) -> None:
        for (region_id, slot), state in states.items():
            if state == "ready":
                await self.ready(region_id, slot)
            elif state == "drain":
                await self.drain(region_id, slot)
            else:
                await self.disable(region_id, slot)
