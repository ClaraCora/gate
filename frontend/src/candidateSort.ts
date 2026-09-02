import type { Candidate } from "./types";

export type CandidateSortKey =
  | "recommended"
  | "ip"
  | "api_speed"
  | "measured_speed"
  | "latency"
  | "availability";

function descendingNullable(left: number | null, right: number | null): number {
  if (left == null) return right == null ? 0 : 1;
  if (right == null) return -1;
  return right - left;
}

function ascendingNullable(left: number | null, right: number | null): number {
  if (left == null) return right == null ? 0 : 1;
  if (right == null) return -1;
  return left - right;
}

function effectiveLatency(candidate: Candidate): number | null {
  return candidate.measured_latency_ms ?? candidate.api_ping_ms;
}

export function filterAndSortCandidates(
  candidates: Candidate[],
  query: string,
  sortKey: CandidateSortKey,
): Candidate[] {
  const normalizedQuery = query.trim().toLowerCase();
  const filtered = normalizedQuery
    ? candidates.filter((candidate) => candidate.ip.toLowerCase().includes(normalizedQuery))
    : candidates;

  return [...filtered].sort((left, right) => {
    let result = 0;
    switch (sortKey) {
      case "ip":
        result = left.ip.localeCompare(right.ip, undefined, { numeric: true });
        break;
      case "api_speed":
        result = right.api_speed_bps - left.api_speed_bps;
        break;
      case "measured_speed":
        result = descendingNullable(
          left.measured_throughput_mbps,
          right.measured_throughput_mbps,
        );
        break;
      case "latency":
        result = ascendingNullable(effectiveLatency(left), effectiveLatency(right));
        break;
      case "availability":
        result = descendingNullable(left.availability_24h, right.availability_24h);
        break;
      case "recommended":
        result = descendingNullable(left.quality_score, right.quality_score);
        if (result === 0) result = right.api_score - left.api_score;
        break;
    }
    return result || left.id - right.id;
  });
}
