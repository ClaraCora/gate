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
async def test_snapshot_and_restore_preserve_server_administrative_states() -> None:
    commands: list[str] = []

    async def sender(command: str) -> str:
        commands.append(command)
        if command == "show stat":
            return (
                "# pxname,svname,status,type\n"
                "gate_jp_slots,jp-a,MAINT,2\n"
                "gate_jp_slots,jp-b,UP,2\n"
                "gate_kr_slots,kr-a,DRAIN,2\n"
                "gate_kr_slots,kr-b,DOWN,2\n"
            )
        return ""

    runtime = HaProxyRuntime(sender=sender)
    states = await runtime.snapshot(["jp", "kr"])
    await runtime.restore(states)

    assert states == {
        ("jp", "a"): "maint",
        ("jp", "b"): "ready",
        ("kr", "a"): "drain",
        ("kr", "b"): "maint",
    }
    assert commands == [
        "show stat",
        "set server gate_jp_slots/jp-a state maint",
        "set server gate_jp_slots/jp-b state ready",
        "set server gate_jp_slots/jp-b health up",
        "set server gate_kr_slots/kr-a state drain",
        "set server gate_kr_slots/kr-b state maint",
    ]


@pytest.mark.asyncio
async def test_snapshot_rejects_missing_configured_server() -> None:
    async def sender(_command: str) -> str:
        return "# pxname,svname,status,type\ngate_jp_slots,jp-a,UP,2\n"

    with pytest.raises(HaProxyError, match="jp/b"):
        await HaProxyRuntime(sender=sender).snapshot(["jp"])


@pytest.mark.asyncio
async def test_rejects_runtime_command_injection() -> None:
    runtime = HaProxyRuntime(sender=lambda _command: _unused_sender())

    with pytest.raises(HaProxyError, match="invalid"):
        await runtime.ready("jp\nshow info", "a")


async def _unused_sender() -> str:
    raise AssertionError("sender should not be called")
