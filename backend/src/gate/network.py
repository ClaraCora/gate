from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from gate.commands import CommandRunner
from gate.config import GateSettings, RegionConfig
from gate.domain import Transport
from gate.errors import GateError
from gate.profiles import validate_sanitized_profile
from gate.worker_protocol import ProvisionSlotRequest


class NetworkOperationError(GateError):
    code = "NETWORK_OPERATION_FAILED"


@dataclass(frozen=True, slots=True)
class ExecutablePaths:
    ip: str
    nft: str
    systemctl: str
    systemd_run: str
    sysctl: str
    ss: str
    openvpn: str
    sing_box: str

    @classmethod
    def discover(cls) -> ExecutablePaths:
        def require(name: str) -> str:
            path = shutil.which(name)
            if path is None:
                raise NetworkOperationError(f"required executable is not installed: {name}")
            return path

        return cls(
            ip=require("ip"),
            nft=require("nft"),
            systemctl=require("systemctl"),
            systemd_run=require("systemd-run"),
            sysctl=require("sysctl"),
            ss=require("ss"),
            openvpn=require("openvpn"),
            sing_box=require("sing-box"),
        )


@dataclass(frozen=True, slots=True)
class SlotSpec:
    region_id: str
    slot: str
    namespace: str
    host_veth: str
    subnet: str
    host_ip: str
    namespace_ip: str
    openvpn_unit: str
    socks_unit: str


def _interface_name(region_id: str, slot: str) -> str:
    readable = f"g-{region_id}-{slot}"
    if len(readable) <= 15:
        return readable
    digest = hashlib.blake2s(region_id.encode(), digest_size=5).hexdigest()
    return f"g-{digest}-{slot}"


def slot_spec(region: RegionConfig, slot: str) -> SlotSpec:
    if slot not in {"a", "b"}:
        raise ValueError("slot must be a or b")
    subnet_offset = (region.network_index - 1) * 8 + (4 if slot == "b" else 0)
    base = int(ipaddress.IPv4Address("10.253.0.0")) + subnet_offset
    network = ipaddress.IPv4Network((base, 30))
    hosts = list(network.hosts())
    namespace = f"gate-{region.id}-{slot}"
    return SlotSpec(
        region_id=region.id,
        slot=slot,
        namespace=namespace,
        host_veth=_interface_name(region.id, slot),
        subnet=str(network),
        host_ip=str(hosts[0]),
        namespace_ip=str(hosts[1]),
        openvpn_unit=f"gate-openvpn-{region.id}-{slot}.service",
        socks_unit=f"gate-socks-{region.id}-{slot}.service",
    )


class LinuxNetworkManager:
    def __init__(
        self,
        settings: GateSettings,
        runner: CommandRunner,
        executables: ExecutablePaths,
        *,
        state_root: Path = Path("/var/lib/gate/slots"),
        netns_config_root: Path = Path("/etc/netns"),
        tunnel_timeout_seconds: float = 30.0,
        socks_timeout_seconds: float = 5.0,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.executables = executables
        self.state_root = state_root
        self.netns_config_root = netns_config_root
        self.tunnel_timeout_seconds = tunnel_timeout_seconds
        self.socks_timeout_seconds = socks_timeout_seconds
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _region(self, region_id: str) -> RegionConfig:
        region = next((item for item in self.settings.regions if item.id == region_id), None)
        if region is None:
            raise NetworkOperationError(f"unknown region: {region_id}")
        return region

    def get_spec(self, region_id: str, slot: str) -> SlotSpec:
        return slot_spec(self._region(region_id), slot)

    def _lock(self, spec: SlotSpec) -> asyncio.Lock:
        return self._locks.setdefault((spec.region_id, spec.slot), asyncio.Lock())

    async def _netns(
        self,
        spec: SlotSpec,
        *args: str,
        check: bool = True,
    ) -> None:
        await self.runner.run(
            [self.executables.ip, "netns", "exec", spec.namespace, *args],
            check=check,
        )

    async def namespace_exists(self, spec: SlotSpec) -> bool:
        result = await self.runner.run(
            [self.executables.ip, "netns", "list"],
            check=False,
        )
        return any(
            line.split(maxsplit=1)[0] == spec.namespace for line in result.stdout.splitlines()
        )

    @staticmethod
    def _secure_write(path: Path, text: str, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise NetworkOperationError(f"refusing to replace a symlink: {path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, mode)
        try:
            os.fchmod(descriptor, mode)
            content = text.encode("utf-8")
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
        finally:
            os.close(descriptor)

    async def _install_namespace_firewall(
        self,
        spec: SlotSpec,
        *,
        remote_ip: str,
        remote_port: int,
        transport: Transport,
    ) -> None:
        nft = self.executables.nft
        commands = [
            ("add", "table", "inet", "gate"),
            (
                "add",
                "chain",
                "inet",
                "gate",
                "input",
                "{",
                "type",
                "filter",
                "hook",
                "input",
                "priority",
                "0",
                ";",
                "policy",
                "drop",
                ";",
                "}",
            ),
            (
                "add",
                "chain",
                "inet",
                "gate",
                "output",
                "{",
                "type",
                "filter",
                "hook",
                "output",
                "priority",
                "0",
                ";",
                "policy",
                "drop",
                ";",
                "}",
            ),
            ("add", "rule", "inet", "gate", "input", "iifname", "lo", "accept"),
            (
                "add",
                "rule",
                "inet",
                "gate",
                "input",
                "ct",
                "state",
                "established,related",
                "accept",
            ),
            (
                "add",
                "rule",
                "inet",
                "gate",
                "input",
                "iifname",
                "eth0",
                "ip",
                "saddr",
                f"{spec.host_ip}/32",
                "tcp",
                "dport",
                "1080",
                "accept",
            ),
            ("add", "rule", "inet", "gate", "output", "oifname", "lo", "accept"),
            (
                "add",
                "rule",
                "inet",
                "gate",
                "output",
                "ct",
                "state",
                "established,related",
                "accept",
            ),
            (
                "add",
                "rule",
                "inet",
                "gate",
                "output",
                "oifname",
                "eth0",
                "ip",
                "daddr",
                f"{remote_ip}/32",
                transport.value,
                "dport",
                str(remote_port),
                "accept",
            ),
            ("add", "rule", "inet", "gate", "output", "oifname", "tun0", "accept"),
        ]
        for command in commands:
            await self._netns(spec, nft, *command)

    async def _prepare_namespace(self, spec: SlotSpec, request: ProvisionSlotRequest) -> None:
        if await self.namespace_exists(spec):
            raise NetworkOperationError(f"slot namespace already exists: {spec.namespace}")

        ip = self.executables.ip
        temporary_peer = f"p{hashlib.blake2s(spec.namespace.encode(), digest_size=4).hexdigest()}"
        try:
            await self.runner.run([ip, "netns", "add", spec.namespace])
            await self.runner.run(
                [ip, "link", "add", spec.host_veth, "type", "veth", "peer", "name", temporary_peer]
            )
            await self.runner.run([ip, "link", "set", temporary_peer, "netns", spec.namespace])
            await self.runner.run(
                [ip, "address", "add", f"{spec.host_ip}/30", "dev", spec.host_veth]
            )
            await self.runner.run([ip, "link", "set", spec.host_veth, "up"])
            await self._netns(spec, ip, "link", "set", "lo", "up")
            await self._netns(spec, ip, "link", "set", temporary_peer, "name", "eth0")
            await self._netns(
                spec,
                ip,
                "address",
                "add",
                f"{spec.namespace_ip}/30",
                "dev",
                "eth0",
            )
            await self._netns(spec, ip, "link", "set", "eth0", "up")
            await self._netns(spec, ip, "route", "add", "default", "via", spec.host_ip)
            await self._netns(
                spec,
                ip,
                "route",
                "replace",
                f"{request.remote_ip}/32",
                "via",
                spec.host_ip,
                "dev",
                "eth0",
            )
            await self._netns(
                spec,
                self.executables.sysctl,
                "-q",
                "-w",
                "net.ipv6.conf.all.disable_ipv6=1",
                "net.ipv6.conf.default.disable_ipv6=1",
            )
            await self._install_namespace_firewall(
                spec,
                remote_ip=request.remote_ip,
                remote_port=request.remote_port,
                transport=request.transport,
            )
            self._secure_write(
                self.netns_config_root / spec.namespace / "resolv.conf",
                "nameserver 1.1.1.1\nnameserver 1.0.0.1\noptions timeout:2 attempts:2\n",
                0o644,
            )
        except Exception:
            await self._destroy_unlocked(spec)
            raise

    async def _wait_for_tunnel(self, spec: SlotSpec) -> None:
        deadline = asyncio.get_running_loop().time() + self.tunnel_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            result = await self.runner.run(
                [
                    self.executables.ip,
                    "netns",
                    "exec",
                    spec.namespace,
                    self.executables.ip,
                    "link",
                    "show",
                    "tun0",
                ],
                check=False,
            )
            if result.returncode == 0:
                return
            unit_state = await self.runner.run(
                [self.executables.systemctl, "is-active", spec.openvpn_unit],
                check=False,
            )
            if unit_state.stdout.strip() in {"failed", "inactive"}:
                raise NetworkOperationError(f"OpenVPN exited before tunnel setup: {spec.namespace}")
            await asyncio.sleep(0.25)
        raise NetworkOperationError(f"OpenVPN tunnel did not become ready: {spec.namespace}")

    def _slot_directory(self, spec: SlotSpec) -> Path:
        return self.state_root / spec.namespace

    async def _start_openvpn(self, spec: SlotSpec, request: ProvisionSlotRequest) -> None:
        directory = self._slot_directory(spec)
        profile_path = directory / "client.ovpn"
        self._secure_write(profile_path, request.config_text, 0o600)
        await self.runner.run(
            [
                self.executables.systemd_run,
                f"--unit={spec.openvpn_unit}",
                "--collect",
                "--service-type=simple",
                "--property=Restart=on-failure",
                "--property=RestartSec=5s",
                "--property=TimeoutStopSec=10s",
                f"--property=NetworkNamespacePath=/run/netns/{spec.namespace}",
                "--property=NoNewPrivileges=yes",
                "--property=Group=gate-worker",
                "--property=ProtectSystem=strict",
                "--property=ProtectHome=yes",
                "--property=PrivateTmp=yes",
                "--property=CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW",
                "--property=AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW",
                "--property=DevicePolicy=closed",
                "--property=DeviceAllow=/dev/net/tun rw",
                self.executables.openvpn,
                "--config",
                str(profile_path),
            ]
        )
        await self._wait_for_tunnel(spec)
        await self._netns(
            spec,
            self.executables.ip,
            "route",
            "replace",
            f"{request.remote_ip}/32",
            "via",
            spec.host_ip,
            "dev",
            "eth0",
        )
        await self._netns(
            spec,
            self.executables.ip,
            "route",
            "replace",
            "default",
            "dev",
            "tun0",
        )

    async def _start_socks(self, spec: SlotSpec) -> None:
        directory = self._slot_directory(spec)
        config_path = directory / "sing-box.json"
        config = {
            "log": {"level": "warn", "timestamp": True},
            "dns": {
                "servers": [{"type": "udp", "tag": "resolver", "server": "1.1.1.1"}],
                "strategy": "ipv4_only",
            },
            "inbounds": [
                {
                    "type": "socks",
                    "tag": "socks-in",
                    "listen": spec.namespace_ip,
                    "listen_port": 1080,
                }
            ],
            "outbounds": [{"type": "direct", "tag": "direct"}],
            "route": {"auto_detect_interface": True, "final": "direct"},
        }
        self._secure_write(config_path, json.dumps(config, indent=2) + "\n", 0o600)
        await self.runner.run(
            [
                self.executables.systemd_run,
                f"--unit={spec.socks_unit}",
                "--collect",
                "--service-type=simple",
                "--property=Restart=on-failure",
                "--property=RestartSec=3s",
                "--property=TimeoutStopSec=10s",
                f"--property=NetworkNamespacePath=/run/netns/{spec.namespace}",
                "--property=NoNewPrivileges=yes",
                "--property=Group=gate-worker",
                "--property=ProtectSystem=strict",
                "--property=ProtectHome=yes",
                "--property=PrivateTmp=yes",
                self.executables.sing_box,
                "run",
                "-c",
                str(config_path),
            ]
        )

    async def _wait_for_socks(self, spec: SlotSpec) -> None:
        deadline = asyncio.get_running_loop().time() + self.socks_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            result = await self.runner.run(
                [
                    self.executables.ip,
                    "netns",
                    "exec",
                    spec.namespace,
                    self.executables.ss,
                    "-H",
                    "-lnt",
                    "sport",
                    "=",
                    ":1080",
                ],
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return
            await asyncio.sleep(0.1)
        raise NetworkOperationError(f"SOCKS listener did not become ready: {spec.namespace}")

    async def provision(self, request: ProvisionSlotRequest) -> SlotSpec:
        spec = self.get_spec(request.region_id, request.slot)
        async with self._lock(spec):
            validate_sanitized_profile(
                request.config_text,
                expected_ip=request.remote_ip,
                expected_port=request.remote_port,
                expected_transport=request.transport,
                expected_fingerprint=request.profile_fingerprint,
            )
            await self._prepare_namespace(spec, request)
            try:
                await self._start_openvpn(spec, request)
                await self._start_socks(spec)
                await self._wait_for_socks(spec)
            except Exception:
                await self._destroy_unlocked(spec)
                raise
        return spec

    async def _destroy_unlocked(self, spec: SlotSpec) -> None:
        for unit in (spec.socks_unit, spec.openvpn_unit):
            await self.runner.run(
                [self.executables.systemctl, "stop", unit],
                check=False,
            )
            await self.runner.run(
                [self.executables.systemctl, "reset-failed", unit],
                check=False,
            )
        await self.runner.run(
            [self.executables.ip, "netns", "delete", spec.namespace],
            check=False,
        )
        await self.runner.run(
            [self.executables.ip, "link", "delete", spec.host_veth],
            check=False,
        )
        for path in (
            self._slot_directory(spec) / "client.ovpn",
            self._slot_directory(spec) / "sing-box.json",
            self.netns_config_root / spec.namespace / "resolv.conf",
        ):
            path.unlink(missing_ok=True)
        for directory in (
            self._slot_directory(spec),
            self.netns_config_root / spec.namespace,
        ):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # Unknown files are deliberately left for an operator to inspect.
                pass

    async def destroy(self, region_id: str, slot: str) -> SlotSpec:
        spec = self.get_spec(region_id, slot)
        async with self._lock(spec):
            await self._destroy_unlocked(spec)
        return spec

    async def inspect(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for region in self.settings.regions:
            for slot in ("a", "b"):
                spec = slot_spec(region, slot)
                exists = await self.namespace_exists(spec)
                tunnel_up = False
                if exists:
                    tunnel = await self.runner.run(
                        [
                            self.executables.ip,
                            "netns",
                            "exec",
                            spec.namespace,
                            self.executables.ip,
                            "link",
                            "show",
                            "tun0",
                        ],
                        check=False,
                    )
                    tunnel_up = tunnel.returncode == 0
                openvpn = await self.runner.run(
                    [self.executables.systemctl, "is-active", spec.openvpn_unit],
                    check=False,
                )
                socks = await self.runner.run(
                    [self.executables.systemctl, "is-active", spec.socks_unit],
                    check=False,
                )
                result.append(
                    {
                        "region_id": region.id,
                        "slot": slot,
                        "namespace": spec.namespace,
                        "namespace_ip": spec.namespace_ip,
                        "exists": exists,
                        "tunnel_up": tunnel_up,
                        "openvpn_active": openvpn.stdout.strip() == "active",
                        "socks_active": socks.stdout.strip() == "active",
                    }
                )
        return result
