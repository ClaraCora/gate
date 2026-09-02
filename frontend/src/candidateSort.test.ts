import { describe, expect, it } from "vitest";

import { filterAndSortCandidates } from "./candidateSort";
import type { Candidate } from "./types";

function candidate(id: number, ip: string, overrides: Partial<Candidate> = {}): Candidate {
  return {
    id,
    hostname: `vpn-${id}`,
    ip,
    country_code: "JP",
    country_long: "Japan",
    transport: "udp",
    port: 1194,
    api_score: id,
    api_ping_ms: null,
    api_speed_bps: 0,
    sessions: 0,
    uptime_ms: 0,
    log_type: "",
    operator: "",
    last_seen_at: "2026-09-02T00:00:00Z",
    availability_24h: null,
    measured_latency_ms: null,
    measured_throughput_mbps: null,
    quality_score: null,
    ...overrides,
  };
}

describe("candidate filtering and sorting", () => {
  const candidates = [
    candidate(1, "203.0.113.20", { api_speed_bps: 20, quality_score: 80 }),
    candidate(2, "203.0.113.3", { api_speed_bps: 100, quality_score: 90 }),
    candidate(3, "198.51.100.8", { api_speed_bps: 50, quality_score: null }),
  ];

  it("filters by partial IP", () => {
    expect(filterAndSortCandidates(candidates, "203.0.113", "recommended")).toHaveLength(2);
  });

  it("sorts IP addresses numerically", () => {
    expect(filterAndSortCandidates(candidates, "", "ip").map((item) => item.id)).toEqual([
      3, 2, 1,
    ]);
  });

  it("sorts speed descending and leaves missing measured values last", () => {
    expect(filterAndSortCandidates(candidates, "", "api_speed")[0].id).toBe(2);
    const measured = [
      candidate(1, "203.0.113.1"),
      candidate(2, "203.0.113.2", { measured_throughput_mbps: 12 }),
    ];
    expect(filterAndSortCandidates(measured, "", "measured_speed")[0].id).toBe(2);
  });
});
