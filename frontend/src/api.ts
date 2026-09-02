import type {
  Candidate,
  DiscoveryResult,
  GateEvent,
  Job,
  Region,
  RegionMode,
  RuntimeSlot,
  SessionState,
} from "./types";

let csrfToken: string | null = null;

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

export function setCsrfToken(value: string | null): void {
  csrfToken = value;
}

export function mutationHeaders(): Record<string, string> {
  return {
    "X-Gate-Request": "webui",
    ...(csrfToken ? { "X-Gate-CSRF": csrfToken } : {}),
  };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const hasBody = init.body !== undefined;
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (hasBody) headers.set("Content-Type", "application/json");
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    for (const [name, value] of Object.entries(mutationHeaders())) headers.set(name, value);
  }
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") message = payload.detail;
      if (
        payload.detail &&
        typeof payload.detail === "object" &&
        "message" in payload.detail
      ) {
        message = String(payload.detail.message);
      }
    } catch {
      // The status code remains actionable when an upstream returns non-JSON.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const gateApi = {
  session: () => request<SessionState>("/api/v1/session"),
  login: (password: string) =>
    request<SessionState>("/api/v1/session/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  logout: () => request<void>("/api/v1/session", { method: "DELETE" }),
  regions: () => request<Region[]>("/api/v1/regions"),
  candidates: (regionId: string) =>
    request<Candidate[]>(`/api/v1/regions/${regionId}/candidates?limit=100`),
  slots: () => request<RuntimeSlot[]>("/api/v1/runtime/slots"),
  jobs: () => request<Job[]>("/api/v1/jobs?limit=80"),
  events: () => request<GateEvent[]>("/api/v1/events?limit=120"),
  refreshDiscovery: () =>
    request<DiscoveryResult>("/api/v1/discovery/refresh", { method: "POST" }),
  probeRegion: (regionId: string) =>
    request<Job>(`/api/v1/regions/${regionId}/probe`, { method: "POST" }),
  reconnectRegion: (regionId: string) =>
    request<Job>(`/api/v1/regions/${regionId}/reconnect`, { method: "POST" }),
  probeCandidate: (regionId: string, nodeId: number) =>
    request<Job>(`/api/v1/regions/${regionId}/candidates/${nodeId}/probe`, {
      method: "POST",
    }),
  switchCandidate: (regionId: string, nodeId: number) =>
    request<Job>(`/api/v1/regions/${regionId}/candidates/${nodeId}/switch`, {
      method: "POST",
    }),
  setMode: (regionId: string, mode: RegionMode) =>
    request<Region>(`/api/v1/regions/${regionId}/mode`, {
      method: "PUT",
      body: JSON.stringify({ mode }),
    }),
  cancelJob: (jobId: string) =>
    request<Job>(`/api/v1/jobs/${jobId}/cancel`, { method: "POST" }),
};
