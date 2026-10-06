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
  listen: SocksListenAddress;
}

export type SocksListenAddress = "127.0.0.1" | "0.0.0.0";

export interface SocksAuthUpdate {
  enabled: boolean;
  username: string;
  password: string | null;
  listen: SocksListenAddress;
}

export interface AutomationState {
  enabled: boolean;
}

export interface SettingsBackup {
  format: "gate-settings-backup";
  version: number;
  exported_at: string;
  redacted_fields: string[];
  settings: Record<string, unknown>;
}

export interface TelegramSettings {
  enabled: boolean;
  bot_token_set: boolean;
  bot_token_masked: string | null;
  chat_id: string;
  api_base_url: string;
}

export interface TelegramSettingsUpdate {
  enabled: boolean;
  bot_token: string | null;
  chat_id: string;
  api_base_url: string;
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
  active_node_ip?: string | null;
  active_egress_ip: string | null;
  candidate_count: number;
  updated_at: string;
  standby_state?: "switching" | "draining" | null;
  standby_node_id?: number | null;
  standby_egress_ip?: string | null;
  conflict_region_name?: string | null;
  conflict_reason?: string | null;
}

export interface HealthCheck {
  id: number;
  region_id: string;
  result: "succeeded" | "failed";
  egress_ip: string | null;
  latency_median_ms: number | null;
  error_code: string | null;
  started_at: string;
  finished_at: string;
}

export interface HealthHistory {
  window_hours: number;
  generated_at: string;
  checks: HealthCheck[];
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

export interface MonitoringPolicy {
  health_interval_seconds: number;
  full_verification_hours: number;
  discovery_interval_minutes: number;
  optimization_enabled: boolean;
  failure_confirm_seconds: number;
  probe_timeout_seconds: number;
  max_concurrent_probes: number;
  daily_budget_mib: number;
  noise_guard_enabled: boolean;
  noise_bytes_per_second: number;
  noise_observation_seconds: number;
  noise_confirmation_windows: number;
  noise_switch_cooldown_minutes: number;
}

export interface TrafficSummary {
  window: "today" | "24h" | "7d";
  since: string;
  until: string;
  sources: Array<Record<string, string | number | null>>;
  buckets: Array<Record<string, string | number | null>>;
  budget: Record<string, string | number | boolean>;
  collector: Record<string, unknown>;
  collector_error: Record<string, unknown>;
  layers_overlap: boolean;
  notes: Record<string, string>;
}
