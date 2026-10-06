from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gate.network import LinuxNetworkManager, SlotSpec


# Counter-only chains run before the existing fail-closed firewall. Return ends
# classification so a DHCP broadcast is counted once, without accepting it.
NOISE_RULES = """
table inet gate_meter {
    counter noise { }
    chain classify {
        udp sport { 67, 68 } udp dport { 67, 68 } counter name noise return
        ip daddr { 224.0.0.0/4, 255.255.255.255 } counter name noise return
    }
    chain observe {
        type filter hook prerouting priority -300; policy accept;
        iifname "tun0" jump classify
    }
}
"""


async def tunnel_counter(manager: LinuxNetworkManager, spec: SlotSpec) -> dict[str, Any] | None:
    prefix = [manager.executables.ip, "netns", "exec", spec.namespace]
    link = await manager.runner.run(
        [*prefix, manager.executables.ip, "-j", "-s", "link", "show", "tun0"], check=False
    )
    if link.returncode or not link.stdout.strip():
        return None
    links = json.loads(link.stdout)
    if not links:
        return None
    noise = await manager.runner.run(
        [*prefix, manager.executables.nft, "-j", "list", "counter", "inet", "gate_meter", "noise"],
        check=False,
    )
    if noise.returncode:
        await manager.runner.run(
            [*prefix, manager.executables.nft, "-f", "-"], input_text=NOISE_RULES
        )
        # Installation creates the counter at zero. Read it again so the next
        # collector tick is not the first usable observation window.
        noise = await manager.runner.run(
            [
                *prefix,
                manager.executables.nft,
                "-j",
                "list",
                "counter",
                "inet",
                "gate_meter",
                "noise",
            ],
            check=False,
        )
        if noise.returncode:
            return None  # A missing counter is not a measured zero.
    values = json.loads(noise.stdout).get("nftables", [])
    count = next((item["counter"]["bytes"] for item in values if "counter" in item), None)
    if count is None:
        return None
    link_data = links[0]
    stats = link_data.get("stats64", link_data.get("stats", {}))
    identity = f"{Path('/run/netns', spec.namespace).stat().st_ino}:{link_data['ifindex']}"
    return {
        "scope": "tunnel",
        "source": spec.namespace,
        "region_id": spec.region_id,
        "identity": identity,
        "rx_bytes": int(stats["rx"]["bytes"]),
        "tx_bytes": int(stats["tx"]["bytes"]),
        "noise_bytes": int(count),
    }


async def collect_counters(manager: LinuxNetworkManager) -> dict[str, object]:
    from gate.network import slot_spec

    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    counters: list[dict[str, Any]] = []
    errors: list[str] = []
    routes = await manager.runner.run([manager.executables.ip, "-j", "route", "show", "default"])
    devices = {route["dev"] for route in json.loads(routes.stdout)}
    links = await manager.runner.run([manager.executables.ip, "-j", "-s", "link"])
    for link in json.loads(links.stdout):
        if link["ifname"] not in devices:
            continue
        stats = link.get("stats64", link.get("stats", {}))
        counters.append(
            {
                "scope": "host",
                "source": link["ifname"],
                "identity": f"{boot}:{link['ifindex']}",
                "rx_bytes": int(stats["rx"]["bytes"]),
                "tx_bytes": int(stats["tx"]["bytes"]),
            }
        )
    namespaces = await manager.runner.run([manager.executables.ip, "netns", "list"])
    present = {line.split()[0] for line in namespaces.stdout.splitlines() if line}
    for region in manager.settings.regions:
        for slot in ("a", "b"):
            spec = slot_spec(region, slot)
            if spec.namespace not in present:
                continue
            # Do not race namespace creation/destruction or attach to temporary half-state.
            if manager._lock(spec).locked():
                continue
            try:
                async with manager._lock(spec):
                    counter = await tunnel_counter(manager, spec)
                if counter is not None:
                    counter["identity"] = f"{boot}:{counter['identity']}"
                    counters.append(counter)
            except Exception:
                errors.append(spec.namespace)
    try:
        info = await manager.haproxy_runtime._send("show info")
        info_fields = dict(line.split(": ", 1) for line in info.splitlines() if ": " in line)
        # PID plus process start timestamp survives app restarts, changes on reload.
        pid = info_fields.get("Pid", "")
        process_start = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
        stats_text = await manager.haproxy_runtime._send("show stat")
        for row in csv.DictReader(io.StringIO(stats_text)):
            name = row.get("# pxname", "")
            if row.get("svname") != "FRONTEND" or not name.startswith("gate_"):
                continue
            counters.append(
                {
                    "scope": "proxy",
                    "source": name,
                    "region_id": name.removeprefix("gate_"),
                    "identity": f"{boot}:{pid}:{process_start}",
                    "rx_bytes": int(row["bin"]),
                    "tx_bytes": int(row["bout"]),
                }
            )
    except Exception:
        errors.append("haproxy")
    return {"counters": counters, "errors": errors}
