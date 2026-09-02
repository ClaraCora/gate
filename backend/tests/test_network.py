from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from gate.commands import CommandResult, CommandRunner
from gate.config import SocksAuthConfig, load_settings
from gate.errors import ProfileRejectedError
from gate.haproxy import HaProxyRuntime
from gate.network import ExecutablePaths, LinuxNetworkManager, NetworkOperationError, slot_spec
from gate.profiles import sanitize_openvpn_profile
from gate.worker_protocol import ProvisionSlotRequest, UpdateSocksAuthRequest


class FakeRunner(CommandRunner):
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.namespaces: set[str] = set()
        self.active_units: set[str] = set()

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
        if command and command[0] == "systemd-run":
            self.active_units.update(
                argument.removeprefix("--unit=")
                for argument in command
                if argument.startswith("--unit=")
            )
        if command[:2] == ("systemctl", "stop"):
            self.active_units.discard(command[2])
        if command[-4:] == ("ip", "link", "show", "tun0"):
            return CommandResult(command, 0, "tun0: UP", "")
        if "ss" in command:
            return CommandResult(command, 0, "LISTEN 0 4096 10.253.0.2:1080 0.0.0.0:*", "")
        if command[:2] == ("systemctl", "is-active"):
            states = ["active" if unit in self.active_units else "inactive" for unit in command[2:]]
            active = all(state == "active" for state in states)
            return CommandResult(
                command,
                0 if active else 3,
                "\n".join(states) + "\n",
                "",
            )
        return CommandResult(command, 0, "", "")


class FailingRestartRunner(FakeRunner):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_restart = False

    async def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        if tuple(args[:2]) == ("systemctl", "restart") and self.fail_next_restart:
            self.fail_next_restart = False
            raise RuntimeError("simulated restart failure")
        return await super().run(args, check=check, input_text=input_text)


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

    runner.commands.clear()
    inventory = await manager.inspect()
    jp_a = next(item for item in inventory if item["region_id"] == "jp" and item["slot"] == "a")
    assert jp_a["exists"] is False
    assert jp_a["tunnel_up"] is False
    assert sum(command == ("ip", "netns", "list") for command in runner.commands) == 1
    assert sum(command[:2] == ("systemctl", "is-active") for command in runner.commands) == 1


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


@pytest.mark.asyncio
async def test_socks_auth_rewrites_and_restarts_active_sing_box(
    tmp_path: Path, encoded_profile: str
) -> None:
    settings = load_settings().model_copy(
        update={
            "socks_auth": SocksAuthConfig(
                enabled=True,
                username="gate_user",
                password="strong!proxy#password",
            )
        }
    )
    runner = FakeRunner()
    auth_path = tmp_path / "etc" / "socks-auth.json"
    manager = LinuxNetworkManager(
        settings,
        runner,
        _executables(),
        state_root=tmp_path / "state",
        netns_config_root=tmp_path / "netns",
        socks_auth_path=auth_path,
    )
    spec = await manager.provision(_request(encoded_profile))
    config_path = tmp_path / "state" / spec.namespace / "sing-box.json"

    configured = json.loads(config_path.read_text(encoding="utf-8"))
    assert configured["inbounds"][0]["users"] == [
        {"username": "gate_user", "password": "strong!proxy#password"}
    ]

    updated = await manager.update_socks_auth(
        UpdateSocksAuthRequest(
            action="update_socks_auth",
            enabled=False,
            username="",
            password="",
        )
    )

    assert updated.enabled is False
    assert "users" not in json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]
    assert json.loads(auth_path.read_text(encoding="utf-8")) == {
        "enabled": False,
        "username": "",
        "password": "",
        "listen": "127.0.0.1",
    }
    assert ("systemctl", "restart", spec.socks_unit) in runner.commands


@pytest.mark.asyncio
async def test_socks_auth_update_restores_file_config_and_runtime_on_failure(
    tmp_path: Path, encoded_profile: str
) -> None:
    old_auth = SocksAuthConfig(
        enabled=True,
        username="old_user",
        password="old!proxy#password",
    )
    settings = load_settings().model_copy(update={"socks_auth": old_auth})
    runner = FailingRestartRunner()
    auth_path = tmp_path / "etc" / "socks-auth.json"
    manager = LinuxNetworkManager(
        settings,
        runner,
        _executables(),
        state_root=tmp_path / "state",
        netns_config_root=tmp_path / "netns",
        socks_auth_path=auth_path,
    )
    spec = await manager.provision(_request(encoded_profile))
    config_path = tmp_path / "state" / spec.namespace / "sing-box.json"
    old_config = config_path.read_text(encoding="utf-8")
    runner.fail_next_restart = True

    with pytest.raises(NetworkOperationError, match="update failed"):
        await manager.update_socks_auth(
            UpdateSocksAuthRequest(
                action="update_socks_auth",
                enabled=True,
                username="new_user",
                password="new!proxy#password",
            )
        )

    assert settings.socks_auth == old_auth
    assert config_path.read_text(encoding="utf-8") == old_config
    assert not auth_path.exists()
    restarts = [command for command in runner.commands if command[:2] == ("systemctl", "restart")]
    assert restarts[-1] == ("systemctl", "restart", spec.socks_unit)


@pytest.mark.asyncio
async def test_public_listener_rewrites_validates_and_reloads_haproxy(tmp_path: Path) -> None:
    base_settings = load_settings()
    japan = next(region for region in base_settings.regions if region.id == "jp")
    settings = base_settings.model_copy(
        update={
            "regions": (japan,),
            "socks_auth": SocksAuthConfig(
                enabled=True,
                username="gate_user",
                password="strong!proxy#password",
            ),
        }
    )
    runner = FakeRunner()
    auth_path = tmp_path / "etc" / "gate" / "socks-auth.json"
    haproxy_path = tmp_path / "etc" / "haproxy" / "haproxy.cfg"
    runtime_commands: list[str] = []

    async def runtime_sender(command: str) -> str:
        runtime_commands.append(command)
        if command == "show stat":
            return (
                "# pxname,svname,status,type\ngate_jp_slots,jp-a,MAINT,2\ngate_jp_slots,jp-b,UP,2\n"
            )
        return ""

    manager = LinuxNetworkManager(
        settings,
        runner,
        _executables(),
        state_root=tmp_path / "state",
        netns_config_root=tmp_path / "netns",
        socks_auth_path=auth_path,
        haproxy_config_path=haproxy_path,
        haproxy_runtime=HaProxyRuntime(sender=runtime_sender),
    )

    updated = await manager.update_socks_auth(
        UpdateSocksAuthRequest(
            action="update_socks_auth",
            enabled=True,
            username="gate_user",
            password="strong!proxy#password",
            listen="0.0.0.0",
        )
    )

    assert updated.listen == "0.0.0.0"
    assert "bind 0.0.0.0:11081" in haproxy_path.read_text(encoding="utf-8")
    assert json.loads(auth_path.read_text(encoding="utf-8"))["listen"] == "0.0.0.0"
    assert any(command[:3] == ("haproxy", "-c", "-f") for command in runner.commands)
    assert ("systemctl", "reload-or-restart", "haproxy.service") in runner.commands
    assert runtime_commands == [
        "show stat",
        "set server gate_jp_slots/jp-a state maint",
        "set server gate_jp_slots/jp-b state ready",
        "set server gate_jp_slots/jp-b health up",
    ]
