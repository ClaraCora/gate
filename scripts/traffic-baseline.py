"""Passive 48-hour baseline. No payload capture or configuration changes."""

import csv
import io
import json
import pathlib
import socket
import subprocess
import time


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return result.stdout if result.returncode == 0 else ""


def sample():
    result = {"time": time.time(), "boot": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    result["host"] = json.loads(command("ip", "-j", "-s", "link") or "[]")
    tunnels = {}
    for line in command("ip", "netns", "list").splitlines():
        name = line.split()[0]
        if name.startswith("gate-"):
            tunnels[name] = json.loads(command("ip", "netns", "exec", name, "ip", "-j", "-s", "link", "show", "tun0") or "[]")
    result["tunnels"] = tunnels
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(5)
            connection.connect("/run/haproxy/gate-admin.sock")
            connection.sendall(b"show stat\n")
            connection.shutdown(socket.SHUT_WR)
            parts = []
            while chunk := connection.recv(65536):
                parts.append(chunk)
        result["proxy"] = [
            {key: row.get(key) for key in ("# pxname", "bin", "bout", "scur", "pid")}
            for row in csv.DictReader(io.StringIO(b"".join(parts).decode()))
            if row.get("svname") == "FRONTEND"
        ]
    except OSError as error:
        result["proxy_error"] = type(error).__name__
    return result


if __name__ == "__main__":
    output = pathlib.Path("/var/lib/gate/traffic-baseline.jsonl")
    deadline = time.monotonic() + 48 * 3600
    while time.monotonic() < deadline:
        started = time.monotonic()
        try:
            value = sample()
        except Exception as error:
            value = {"time": time.time(), "error": type(error).__name__}
        with output.open("a") as stream:
            stream.write(json.dumps(value, separators=(",", ":")) + "\n")
        time.sleep(max(1, 60 - (time.monotonic() - started)))
