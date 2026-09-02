export type RegionMode = "auto" | "locked" | "disabled";
export type RegionStatus =
  | "disabled"
  | "starting"
  | "healthy"
  | "degraded"
  | "switching"
  | "unavailable";

export interface SessionState {
  authenticated: boolean;
  security_enabled: boolean;
  csrf_token: string | null;
  expires_at: string | null;
}

export interface SocksAuthState {
  enabled: boolean;
  username: string;
  password_set: boolean;
}

export interface SocksAuthUpdate {
  enabled: boolean;
  username: string;
  password: string | null;
}

export interface Region {
  id: string;
  group_id: string;
  name: string;
  countries: string[];
  socks_port: number;
  network_index: number;
  enabled: boolean;
  mode: RegionMode;
  status: RegionStatus;
  active_node_id: number | null;
  active_egress_ip: string | null;
  candidate_count: number;
  updated_at: string;
}

export interface Candidate {
  id: number;
  hostname: string;
  ip: string;
  country_code: string;
  country_long: string;
  transport: string;
  port: number;
  api_score: number;
  api_ping_ms: number | null;
  api_speed_bps: number;
  sessions: number;
  uptime_ms: number;
  log_type: string;
  operator: string;
  last_seen_at: string;
  availability_24h: number | null;
  measured_latency_ms: number | null;
  measured_throughput_mbps: number | null;
  quality_score: number | null;
}

export interface RuntimeSlot {
  region_id: string;
  slot: "a" | "b";
  namespace: string;
  namespace_ip: string;
  exists: boolean;
  tunnel_up: boolean;
  openvpn_active: boolean;
  socks_active: boolean;
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  region_id: string | null;
  progress: number;
  error_code: string | null;
  detail: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface GateEvent {
  id: number;
  code: string;
  level: "info" | "warning" | "error" | string;
  message: string;
  region_id: string | null;
  node_id: number | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface DiscoveryResult {
  discovered: number;
  accepted: number;
  rejected_feed_rows: number;
  rejected_profiles: number;
  warnings: string[];
  observed_at: string;
  source_url: string;
}
