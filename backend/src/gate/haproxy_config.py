from __future__ import annotations

import argparse
from pathlib import Path

from gate.config import GateSettings, load_settings
from gate.network import slot_spec


def render_haproxy_config(settings: GateSettings) -> str:
    lines = [
        "# Managed by Gate. See docs/PROJECT_PLAN.md before editing.",
        "global",
        "    stats socket /run/haproxy/gate-admin.sock mode 660 level admin group gate-worker",
        "    maxconn 4096",
        "",
        "defaults",
        "    mode tcp",
        "    timeout connect 5s",
        "    timeout client 1h",
        "    timeout server 1h",
        "    timeout check 3s",
        "",
    ]
    for region in settings.regions:
        slot_a = slot_spec(region, "a")
        slot_b = slot_spec(region, "b")
        lines.extend(
            [
                f"frontend gate_{region.id}",
                f"    bind {settings.socks_auth.listen}:{region.socks_port}",
                f"    default_backend gate_{region.id}_slots",
                "",
                f"backend gate_{region.id}_slots",
                "    option tcp-check",
                f"    server {region.id}-a {slot_a.namespace_ip}:1080 check disabled",
                f"    server {region.id}-b {slot_b.namespace_ip}:1080 check disabled",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Gate's HAProxy configuration")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    content = render_haproxy_config(load_settings(args.config))
    args.output.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
