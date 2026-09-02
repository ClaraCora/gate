import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";

import { gateApi } from "./api";
import { SocksAuthDialog } from "./App";

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

afterEach(() => vi.restoreAllMocks());

describe("SocksAuthDialog", () => {
  it("validates new credentials and submits the unified SOCKS settings", async () => {
    vi.spyOn(gateApi, "socksAuth").mockResolvedValue({
      enabled: false,
      username: "",
      password_set: false,
    });
    const update = vi.spyOn(gateApi, "updateSocksAuth").mockResolvedValue({
      enabled: true,
      username: "gate_user",
      password_set: true,
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
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "p@ss:/?#[]!word" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "p@ss:/?#[]!word" } });
    fireEvent.click(screen.getByRole("button", { name: "保存认证设置" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith({
      enabled: true,
      username: "gate_user",
      password: "p@ss:/?#[]!word",
    }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith({
      enabled: true,
      username: "gate_user",
      password_set: true,
    }));
  });

  it("keeps the existing password when both password fields are blank", async () => {
    vi.spyOn(gateApi, "socksAuth").mockResolvedValue({
      enabled: true,
      username: "gate_user",
      password_set: true,
    });
    const update = vi.spyOn(gateApi, "updateSocksAuth").mockResolvedValue({
      enabled: true,
      username: "proxy_user",
      password_set: true,
    });
    render(<SocksAuthDialog onChanged={() => undefined} onClose={() => undefined} open />, { wrapper });

    const username = await screen.findByLabelText("统一用户名");
    fireEvent.change(username, { target: { value: "proxy_user" } });
    fireEvent.click(screen.getByRole("button", { name: "保存认证设置" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith({
      enabled: true,
      username: "proxy_user",
      password: null,
    }));
  });
});
