"""Read-only deployment verification; credentials stay inside the remote process."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from gate.config import load_settings
from gate.probes import probe_socks_exit


async def main():
    settings = load_settings(Path("/etc/gate/config.yaml"))
    auth = settings.socks_auth
    database = sqlite3.connect("file:/var/lib/gate/gate.db?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    rows = database.execute(
        "SELECT r.id,r.name,r.group_id,r.socks_port,r.status,r.active_node_id,"
        "r.active_egress_ip,s.slot,s.started_at FROM regions r LEFT JOIN region_slots s "
        "ON s.region_id=r.id AND s.state='active' WHERE r.enabled=1 ORDER BY r.socks_port"
    ).fetchall()
    semaphore = asyncio.Semaphore(2)

    async def verify(row):
        value = dict(row)
        region = next(region for region in settings.regions if region.id == row["id"])
        try:
            async with semaphore:
                result = await probe_socks_exit(
                    "127.0.0.1", row["socks_port"], expected_countries=set(region.countries),
                    username=auth.username if auth.enabled else None,
                    password=auth.password if auth.enabled else None,
                )
            value.update(verified=True, actual_ip=result.egress_ip, country=result.country_code,
                         ip_matches=result.egress_ip == row["active_egress_ip"])
        except Exception as error:
            value.update(verified=False, error_code=getattr(error, "code", type(error).__name__))
        return value

    results = await asyncio.gather(*(verify(row) for row in rows))
    pairs = [(row["group_id"], row["actual_ip"]) for row in results if row.get("verified")]
    report = {"at": datetime.now(UTC).isoformat(), "auth_enabled": auth.enabled,
              "entrances": results, "duplicate_exits": len(pairs) != len(set(pairs))}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    database.close()


if __name__ == "__main__":
    asyncio.run(main())
