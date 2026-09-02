import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";

import { gateApi } from "./api";
import { AutomationControl, HealthGrains, RegionTable, SocksAuthDialog, useGateStream } from "./App";
import type { HealthCheck, Region } from "./types";

function wrapper({ children }: PropsWithChildren) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value() {
      this.setAttribute("open", "");
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value() {
      this.removeAttribute("open");
    },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

class FakeEventSource {
  static instance: FakeEventSource;

  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listener: (() => void) | null = null;
  closed = false;

  constructor(public readonly url: string) {
    FakeEventSource.instance = this;
  }

  addEventListener(type: string, listener: () => void) {
    if (type === "gate-event") this.listener = listener;
  }

  close() {
    this.closed = true;
  }

  emit() {
    this.listener?.();
  }
}

function StreamHarness() {
  const state = useGateStream(true);
  return <span>{state}</span>;
}

describe("automatic checks", () => {
  it("renders a reversible automation switch", () => {
    const changed = vi.fn();
    render(<AutomationControl disabled={false} enabled onChange={changed} pending={false} />);

    fireEvent.click(screen.getByRole("checkbox", { name: "自动检查" }));

    expect(changed).toHaveBeenCalledWith(false);
    expect(screen.getByText("运行中")).toBeInTheDocument();
  });

  it("coalesces an event burst into one query refresh batch", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue();
    const view = render(
      <QueryClientProvider client={client}><StreamHarness /></QueryClientProvider>,
    );

    for (let index = 0; index < 20; index += 1) FakeEventSource.instance.emit();

    await waitFor(() => expect(invalidate).toHaveBeenCalledTimes(7), { timeout: 1_000 });
    view.unmount();
    expect(FakeEventSource.instance.closed).toBe(true);
  });
});

describe("region table", () => {
  it("shows the current exit IP for every SOCKS port", () => {
    const regions: Region[] = [
      {
        id: "jp",
        group_id: "jp",
        name: "日本 01",
        countries: ["JP"],
        socks_port: 11081,
        network_index: 1,
        enabled: true,
        mode: "locked",
        status: "healthy",
        active_node_id: 1,
        active_egress_ip: "203.0.113.10",
        candidate_count: 5,
        updated_at: "2026-09-02T00:00:00Z",
      },
      {
        id: "jp-02",
        group_id: "jp",
        name: "日本 02",
        countries: ["JP"],
        socks_port: 11101,
        network_index: 2,
        enabled: false,
        mode: "disabled",
        status: "disabled",
        active_node_id: null,
        active_egress_ip: null,
        candidate_count: 5,
        updated_at: "2026-09-02T00:00:00Z",
      },
    ];

    render(
      <RegionTable
        healthHistory={{
          checks: [{
            id: 1,
            region_id: "jp",
            result: "succeeded",
            egress_ip: "203.0.113.10",
            latency_median_ms: 42,
            error_code: null,
            started_at: "2026-09-02T01:58:00Z",
            finished_at: "2026-09-02T01:58:05Z",
          }],
          generated_at: "2026-09-02T02:00:00Z",
          window_hours: 2,
        }}
        healthHistoryLoading={false}
        healthHistoryUnavailable={false}
        jobs={[]}
        listen="0.0.0.0"
        modePendingRegionId={null}
        onSelect={() => undefined}
        onToggle={() => undefined}
        regions={regions}
        runtimeUnavailable={false}
        selectedId="jp"
        slots={[]}
      />,
    );

    expect(screen.getByText("0.0.0.0:11081")).toBeInTheDocument();
    expect(screen.getByText("203.0.113.10")).toBeInTheDocument();
    expect(screen.getByText("未分配")).toBeInTheDocument();
    expect(screen.getByText("1/1 成功")).toBeInTheDocument();
    expect(screen.getByText("暂无检查")).toBeInTheDocument();
  });
});

describe("health grains", () => {
  it("distinguishes intermittent and continuous failures within the time window", () => {
    const check = (
      id: number,
      result: "succeeded" | "failed",
      finishedAt: string,
    ): HealthCheck => ({
      id,
      region_id: "jp",
      result,
      egress_ip: result === "succeeded" ? "203.0.113.10" : null,
      latency_median_ms: result === "succeeded" ? 80 : null,
      error_code: result === "failed" ? "PROBE_FAILED" : null,
      started_at: finishedAt,
      finished_at: finishedAt,
    });
    const { container } = render(
      <HealthGrains
        checks={[
          check(1, "failed", "2026-09-02T01:51:00Z"),
          check(2, "succeeded", "2026-09-02T01:56:00Z"),
          check(3, "failed", "2026-09-02T01:57:00Z"),
        ]}
        generatedAt="2026-09-02T02:00:00Z"
        windowHours={2}
      />,
    );

    expect(screen.getByRole("group", { name: "最近 2 小时：1 次成功，2 次失败" })).toBeInTheDocument();
    expect(container.querySelectorAll(".health-grain--failed")).toHaveLength(1);
    expect(container.querySelectorAll(".health-grain--mixed")).toHaveLength(1);
    expect(container.querySelector(".health-grain--mixed")).toHaveAttribute("title", expect.stringContaining("间歇波动"));
  });
});

describe("SocksAuthDialog", () => {
  it("validates new credentials and submits the unified SOCKS settings", async () => {
    vi.spyOn(gateApi, "socksAuth").mockResolvedValue({
      enabled: false,
      username: "",
      password_set: false,
      listen: "127.0.0.1",
    });
    const update = vi.spyOn(gateApi, "updateSocksAuth").mockResolvedValue({
      enabled: true,
      username: "gate_user",
      password_set: true,
      listen: "127.0.0.1",
    });
    const changed = vi.fn();
    render(<SocksAuthDialog onChanged={changed} onClose={() => undefined} open />, { wrapper });

    const toggle = await screen.findByRole("checkbox", { name: /要求身份验证/ });
    fireEvent.click(toggle);
    fireEvent.change(screen.getByLabelText("统一用户名"), { target: { value: "ab" } });
    fireEvent.click(screen.getByRole("button", { name: "保存认证设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("用户名须为 3-32 位");
    expect(update).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("统一用户名"), { target: { value: "gate_user" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "1234567" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "1234567" } });
    fireEvent.click(screen.getByRole("button", { name: "保存认证设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("密码须为 8-128 个字符");
    expect(update).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "p@ssw0rd" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "p@ssw0rd" } });
    fireEvent.click(screen.getByRole("button", { name: "保存认证设置" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith({
      enabled: true,
      username: "gate_user",
      password: "p@ssw0rd",
      listen: "127.0.0.1",
    }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith({
      enabled: true,
      username: "gate_user",
      password_set: true,
      listen: "127.0.0.1",
    }));
  });

  it("keeps the existing password when both password fields are blank", async () => {
    vi.spyOn(gateApi, "socksAuth").mockResolvedValue({
      enabled: true,
      username: "gate_user",
      password_set: true,
      listen: "127.0.0.1",
    });
    const update = vi.spyOn(gateApi, "updateSocksAuth").mockResolvedValue({
      enabled: true,
      username: "proxy_user",
      password_set: true,
      listen: "127.0.0.1",
    });
    render(<SocksAuthDialog onChanged={() => undefined} onClose={() => undefined} open />, { wrapper });

    const username = await screen.findByLabelText("统一用户名");
    fireEvent.change(username, { target: { value: "proxy_user" } });
    fireEvent.click(screen.getByRole("button", { name: "保存认证设置" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith({
      enabled: true,
      username: "proxy_user",
      password: null,
      listen: "127.0.0.1",
    }));
  });

  it("forces authentication before enabling a public listener", async () => {
    vi.spyOn(gateApi, "socksAuth").mockResolvedValue({
      enabled: false,
      username: "",
      password_set: false,
      listen: "127.0.0.1",
    });
    const update = vi.spyOn(gateApi, "updateSocksAuth").mockResolvedValue({
      enabled: true,
      username: "public_user",
      password_set: true,
      listen: "0.0.0.0",
    });
    render(<SocksAuthDialog onChanged={() => undefined} onClose={() => undefined} open />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /全部网卡/ }));
    const toggle = screen.getByRole("checkbox", { name: /要求身份验证/ });
    expect(toggle).toBeChecked();
    expect(toggle).toBeDisabled();
    fireEvent.change(screen.getByLabelText("统一用户名"), { target: { value: "public_user" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "p@ssw0rd" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "p@ssw0rd" } });
    fireEvent.click(screen.getByRole("button", { name: "保存认证设置" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith({
      enabled: true,
      username: "public_user",
      password: "p@ssw0rd",
      listen: "0.0.0.0",
    }));
  });
});
