from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from gate.errors import GateError


class CommandFailedError(GateError):
    code = "COMMAND_FAILED"

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.args_list = tuple(args)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"command failed with exit code {returncode}: {args[0]}")


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    async def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        raise NotImplementedError


class SubprocessCommandRunner(CommandRunner):
    async def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        if not args or any("\x00" in argument for argument in args):
            raise ValueError("command arguments must be non-empty and contain no NUL bytes")
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate(
            input_text.encode("utf-8") if input_text is not None else None
        )
        result = CommandResult(
            args=tuple(args),
            returncode=process.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            raise CommandFailedError(args, result.returncode, result.stderr)
        return result
