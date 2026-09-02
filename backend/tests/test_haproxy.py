from __future__ import annotations

import pytest
from gate.haproxy import HaProxyError, HaProxyRuntime


@pytest.mark.asyncio
async def test_switch_commands_use_fixed_backend_names() -> None:
    commands: list[str] = []

    async def sender(command: str) -> str:
        commands.append(command)
        return ""

    runtime = HaProxyRuntime(sender=sender)
    await runtime.ready("jp", "b")
    await runtime.drain("jp", "a")
    await runtime.disable("jp", "a")

    assert commands == [
        "set server gate_jp_slots/jp-b state ready",
        "set server gate_jp_slots/jp-b health up",
        "set server gate_jp_slots/jp-a state drain",
        "set server gate_jp_slots/jp-a state maint",
    ]


@pytest.mark.asyncio
async def test_rejects_runtime_command_injection() -> None:
    runtime = HaProxyRuntime(sender=lambda _command: _unused_sender())

    with pytest.raises(HaProxyError, match="invalid"):
        await runtime.ready("jp\nshow info", "a")


async def _unused_sender() -> str:
    raise AssertionError("sender should not be called")
