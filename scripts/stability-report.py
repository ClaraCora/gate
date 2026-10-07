"""Passive stability report. Read SQLite only; never start probes or switch routes."""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()


def report(database, since, until):
    start, end = at(since), at(until)
    query_time = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%d %H:%M:%S")
    end_time = datetime.fromtimestamp(end, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    events = database.execute(
        "SELECT code,region_id,node_id,details,created_at FROM events "
        "WHERE created_at>=? AND created_at<=? ORDER BY created_at,id", (query_time, end_time)
    ).fetchall()
    reasons = Counter()
    recovered_attempts = []
    failed_batches = []
    outages = defaultdict(list)
    opened = {}
    for row in events:
        detail, occurred = json.loads(row["details"]), at(row["created_at"])
        code, region = row["code"], row["region_id"]
        if code == "STABLE_SELECTION_SWITCH_COMPLETED":
            reasons[detail.get("reason", "unknown")] += 1
            if detail.get("reason") == "confirmed_failure_recovery":
                recovered_attempts.append({"region_id": region, "attempts": detail["attempted"]})
        elif code == "NOISE_REPLACEMENT_COMPLETED":
            reasons["sustained_noise_reduction"] += 1
        elif code == "AUTO_CANDIDATE_BATCH_FAILED":
            failed_batches.append({"region_id": region, "attempts": detail.get("attempted")})
        if code == "ACTIVE_OUTAGE_CONFIRMED":
            opened.setdefault(region, occurred)
        elif code in {"SWITCH_COMPLETED", "ACTIVE_OUTAGE_RECOVERED"} and region in opened:
            outages[region].append((opened.pop(region), occurred))
    for region, began in opened.items():
        outages[region].append((began, end))
    probes = database.execute(
        "SELECT region_id,result,finished_at FROM probe_runs WHERE probe_type='active_health' "
        "AND finished_at>=? AND finished_at<=? ORDER BY finished_at,id", (query_time, end_time)
    ).fetchall()
    previous = {}
    known_unavailable = defaultdict(float)
    outcomes = Counter()
    gaps = defaultdict(float)
    for row in probes:
        region, current = row["region_id"], at(row["finished_at"])
        outcomes[row["result"]] += 1
        if region in previous:
            earlier, outcome = previous[region]
            duration = current - earlier
            if duration > 450 or outcome not in {"succeeded", "failed"}:
                gaps[region] += duration
            elif outcome == "failed":
                for began, finished in outages[region]:
                    known_unavailable[region] += max(0, min(current, finished) - max(earlier, began))
        previous[region] = (current, row["result"])
    traffic = [dict(row) for row in database.execute(
        "SELECT scope,source,SUM(rx_bytes) AS rx_bytes,SUM(tx_bytes) AS tx_bytes,"
        "SUM(noise_bytes) AS noise_bytes,SUM(requests) AS requests,SUM(seconds) AS observed_seconds "
        "FROM traffic_samples WHERE observed_at>=? AND observed_at<=? GROUP BY scope,source",
        (query_time, end_time)
    )]
    return {"since": since, "until": until, "switches_by_reason": dict(reasons),
            "successful_recovery_batches": recovered_attempts, "failed_batches": failed_batches,
            "candidate_node_failures": sum(row["code"] == "AUTO_CANDIDATE_FAILED" for row in events),
            "confirmed_unavailable_observed_seconds": dict(known_unavailable),
            "unclassified_health_gaps_seconds": dict(gaps), "health_outcomes": dict(outcomes),
            "traffic_layers_do_not_sum": traffic,
            "notes": "Unavailable duration covers confirmed incidents between valid checks <=450s apart. "
                     "Missing startup state and unobserved gaps are not inferred. HTTP bytes are response bodies."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="/var/lib/gate/gate.db")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", default=datetime.now(UTC).isoformat())
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.database}?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        print(json.dumps(report(database, args.since, args.until), ensure_ascii=False, indent=2))
