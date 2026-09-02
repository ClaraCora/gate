from __future__ import annotations

import asyncio
from pathlib import Path

from gate.errors import GateError
from gate.worker_protocol import Request, WorkerResponse


class WorkerClientError(GateError):
    code = "WORKER_CLIENT_ERROR"


class WorkerClient:
    def __init__(
        self,
        socket_path: Path = Path("/run/gate/worker.sock"),
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def request(self, request: Request) -> dict[str, object]:
        try:
            connector = asyncio.open_unix_connection
        except AttributeError as exc:
            message = "Unix socket transport is unavailable on this platform"
            raise WorkerClientError(message) from exc
        try:
            reader, writer = await asyncio.wait_for(
                connector(self.socket_path),
                timeout=self.timeout_seconds,
            )
        except (OSError, TimeoutError) as exc:
            raise WorkerClientError(f"unable to connect to gate-worker: {exc}") from exc

        try:
            writer.write(request.model_dump_json().encode("utf-8") + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=self.timeout_seconds)
            raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
        except (OSError, TimeoutError) as exc:
            raise WorkerClientError(f"gate-worker request failed: {exc}") from exc
        finally:
            writer.close()
            await writer.wait_closed()

        if not raw:
            raise WorkerClientError("gate-worker returned an empty response")
        response = WorkerResponse.model_validate_json(raw)
        if not response.ok:
            error = response.error
            message = error.message if error else "gate-worker rejected the request"
            failure = WorkerClientError(message)
            if error:
                failure.code = error.code
            raise failure
        return response.data or {}
