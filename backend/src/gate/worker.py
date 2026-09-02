from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gate.commands import SubprocessCommandRunner
from gate.config import default_socks_auth_path, load_settings
from gate.errors import GateError
from gate.network import ExecutablePaths, LinuxNetworkManager
from gate.worker_protocol import (
    REQUEST_ADAPTER,
    DestroySlotRequest,
    HealthRequest,
    InspectRequest,
    ProvisionSlotRequest,
    Request,
    UpdateSocksAuthRequest,
    WorkerErrorResponse,
    WorkerResponse,
)

MAX_REQUEST_BYTES = 512 * 1024


class WorkerDispatcher:
    def __init__(self, manager: LinuxNetworkManager) -> None:
        self.manager = manager

    async def dispatch(self, request: Request) -> WorkerResponse:
        if isinstance(request, HealthRequest):
            return WorkerResponse(ok=True, data={"status": "ok"})
        if isinstance(request, InspectRequest):
            return WorkerResponse(ok=True, data={"slots": await self.manager.inspect()})
        if isinstance(request, ProvisionSlotRequest):
            spec = await self.manager.provision(request)
            return WorkerResponse(
                ok=True,
                data={
                    "region_id": spec.region_id,
                    "slot": spec.slot,
                    "namespace": spec.namespace,
                    "namespace_ip": spec.namespace_ip,
                },
            )
        if isinstance(request, DestroySlotRequest):
            spec = await self.manager.destroy(request.region_id, request.slot)
            return WorkerResponse(
                ok=True,
                data={
                    "region_id": spec.region_id,
                    "slot": spec.slot,
                    "namespace": spec.namespace,
                },
            )
        if isinstance(request, UpdateSocksAuthRequest):
            auth = await self.manager.update_socks_auth(request)
            return WorkerResponse(
                ok=True,
                data={
                    "enabled": auth.enabled,
                    "username": auth.username,
                    "password_set": bool(auth.password),
                },
            )
        raise AssertionError("unhandled worker request")


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    peer_socket: socket.socket | None = writer.get_extra_info("socket")
    if peer_socket is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    credentials = peer_socket.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return int(uid)


class WorkerServer:
    def __init__(
        self,
        dispatcher: WorkerDispatcher,
        *,
        socket_path: Path,
        allowed_uids: frozenset[int],
        socket_gid: int,
    ) -> None:
        self.dispatcher = dispatcher
        self.socket_path = socket_path
        self.allowed_uids = allowed_uids
        self.socket_gid = socket_gid
        self.server: asyncio.AbstractServer | None = None

    async def _send(self, writer: asyncio.StreamWriter, response: WorkerResponse) -> None:
        writer.write(response.model_dump_json().encode("utf-8") + b"\n")
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            uid = _peer_uid(writer)
            if uid is None or uid not in self.allowed_uids:
                await self._send(
                    writer,
                    WorkerResponse(
                        ok=False,
                        error=WorkerErrorResponse(
                            code="UNAUTHORIZED_PEER",
                            message="worker socket peer is not authorized",
                        ),
                    ),
                )
                return
            raw = await reader.readline()
            if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                await self._send(
                    writer,
                    WorkerResponse(
                        ok=False,
                        error=WorkerErrorResponse(
                            code="INVALID_REQUEST",
                            message="worker request is empty, too large, or incomplete",
                        ),
                    ),
                )
                return
            try:
                payload: Any = json.loads(raw)
                request = REQUEST_ADAPTER.validate_python(payload)
                response = await self.dispatcher.dispatch(request)
            except (json.JSONDecodeError, ValidationError) as exc:
                response = WorkerResponse(
                    ok=False,
                    error=WorkerErrorResponse(
                        code="INVALID_REQUEST",
                        message=str(exc),
                    ),
                )
            except GateError as exc:
                response = WorkerResponse(
                    ok=False,
                    error=WorkerErrorResponse(code=exc.code, message=str(exc)),
                )
            except Exception:
                response = WorkerResponse(
                    ok=False,
                    error=WorkerErrorResponse(
                        code="INTERNAL_ERROR",
                        message="worker operation failed unexpectedly",
                    ),
                )
            await self._send(writer, response)
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self) -> None:
        if os.name != "posix":
            raise RuntimeError("gate-worker requires Linux")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.socket_path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        os.chown(self.socket_path, 0, self.socket_gid)
        os.chmod(self.socket_path, 0o660)

    async def serve_forever(self) -> None:
        await self.start()
        assert self.server is not None
        async with self.server:
            await self.server.serve_forever()


def _resolve_identity(user: str, group: str) -> tuple[int, int]:
    import grp
    import pwd

    return pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid


async def run_worker() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("gate-worker must run as root on Linux")
    settings = load_settings()
    worker_user = os.environ.get("GATE_WORKER_USER", "gate")
    worker_group = os.environ.get("GATE_WORKER_GROUP", "gate-worker")
    worker_uid, worker_gid = _resolve_identity(worker_user, worker_group)
    manager = LinuxNetworkManager(
        settings,
        SubprocessCommandRunner(),
        ExecutablePaths.discover(),
        state_root=Path(os.environ.get("GATE_SLOT_ROOT", "/var/lib/gate/slots")),
        netns_config_root=Path(os.environ.get("GATE_NETNS_CONFIG_ROOT", "/etc/netns")),
        socks_auth_path=default_socks_auth_path(),
        socks_auth_gid=worker_gid,
    )
    server = WorkerServer(
        WorkerDispatcher(manager),
        socket_path=Path(os.environ.get("GATE_WORKER_SOCKET", "/run/gate/worker.sock")),
        allowed_uids=frozenset({0, worker_uid}),
        socket_gid=worker_gid,
    )
    await server.serve_forever()


def main() -> None:
    asyncio.run(run_worker())
