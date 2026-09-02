from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest
from gate.commands import CommandResult, CommandRunner
from gate.config import load_settings
from gate.errors import ProfileRejectedError
from gate.network import ExecutablePaths, LinuxNetworkManager, slot_spec
from gate.profiles import sanitize_openvpn_profile
from gate.worker_protocol import ProvisionSlotRequest


class FakeRunner(CommandRunner):
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.namespaces: set[str] = set()

    async def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        del check, input_text
        command = tuple(args)
        self.commands.append(command)
        if command == ("ip", "netns", "list"):
            stdout = "\n".join(sorted(self.namespaces))
            return CommandResult(command, 0, stdout, "")
        if command[:3] == ("ip", "netns", "add"):
            self.namespaces.add(command[3])
        elif command[:3] == ("ip", "netns", "delete"):
            self.namespaces.discard(command[3])
        if command[-4:] == ("ip", "link", "show", "tun0"):
            return CommandResult(command, 0, "tun0: UP", "")
        if "ss" in command:
            return CommandResult(command, 0, "LISTEN 0 4096 10.253.0.2:1080 0.0.0.0:*", "")
        if "is-active" in command:
            return CommandResult(command, 3, "inactive\n", "")
        return CommandResult(command, 0, "", "")


def _executables() -> ExecutablePaths:
    return ExecutablePaths(
        ip="ip",
        nft="nft",
        systemctl="systemctl",
        systemd_run="systemd-run",
        sysctl="sysctl",
        ss="ss",
        openvpn="openvpn",
        sing_box="sing-box",
    )


def _request(encoded_profile: str) -> ProvisionSlotRequest:
    profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")
    return ProvisionSlotRequest(
        action="provision_slot",
        region_id="jp",
        slot="a",
        remote_ip=profile.remote_ip,
        remote_port=profile.remote_port,
        transport=profile.transport,
        profile_fingerprint=profile.fingerprint,
        config_text=profile.config_text,
    )


def test_slot_addresses_are_deterministic_and_non_overlapping() -> None:
    settings = load_settings()
    japan = next(region for region in settings.regions if region.id == "jp")
    korea = next(region for region in settings.regions if region.id == "kr")

    jp_a = slot_spec(japan, "a")
    jp_b = slot_spec(japan, "b")
    kr_a = slot_spec(korea, "a")

    assert jp_a.subnet == "10.253.0.0/30"
    assert jp_a.host_ip == "10.253.0.1"
    assert jp_a.namespace_ip == "10.253.0.2"
    assert len({jp_a.subnet, jp_b.subnet, kr_a.subnet}) == 3


@pytest.mark.asyncio
async def test_provision_builds_kill_switch_and_tunnel_routes(
    tmp_path: Path, encoded_profile: str
) -> None:
    runner = FakeRunner()
    manager = LinuxNetworkManager(
        load_settings(),
        runner,
        _executables(),
        state_root=tmp_path / "state",
        netns_config_root=tmp_path / "netns",
        tunnel_timeout_seconds=0.1,
    )
    request = _request(encoded_profile)

    spec = await manager.provision(request)

    assert spec.namespace in runner.namespaces
    assert (tmp_path / "state" / spec.namespace / "client.ovpn").read_text() == request.config_text
    assert (tmp_path / "state" / spec.namespace / "sing-box.json").exists()
    flattened = [" ".join(command) for command in runner.commands]
    assert any("policy drop" in command and "gate output" in command for command in flattened)
    assert any(
        "oifname eth0 ip daddr 128.211.249.131/32 udp dport 1195 accept" in command
        for command in flattened
    )
    assert any("route replace default dev tun0" in command for command in flattened)
    transient_units = [command for command in flattened if command.startswith("systemd-run ")]
    assert len(transient_units) == 2
    assert all("--property=Group=gate-worker" in command for command in transient_units)
    assert all("shell" not in command for command in flattened)

    await manager.destroy("jp", "a")
    assert spec.namespace not in runner.namespaces
    assert not (tmp_path / "state" / spec.namespace).exists()

    inventory = await manager.inspect()
    jp_a = next(item for item in inventory if item["region_id"] == "jp" and item["slot"] == "a")
    assert jp_a["exists"] is False
    assert jp_a["tunnel_up"] is False


@pytest.mark.asyncio
async def test_manager_rejects_noncanonical_profile_before_network_changes(
    tmp_path: Path, encoded_profile: str
) -> None:
    runner = FakeRunner()
    manager = LinuxNetworkManager(
        load_settings(),
        runner,
        _executables(),
        state_root=tmp_path / "state",
        netns_config_root=tmp_path / "netns",
    )
    valid = _request(encoded_profile)
    dangerous = valid.config_text.replace("verb 3", "plugin /tmp/unsafe.so")
    request = valid.model_copy(
        update={
            "config_text": dangerous,
            "profile_fingerprint": hashlib.sha256(dangerous.encode()).hexdigest(),
        }
    )

    with pytest.raises(ProfileRejectedError, match="not allowed: plugin"):
        await manager.provision(request)

    assert runner.commands == []
