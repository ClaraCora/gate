import { mutationHeaders, setCsrfToken } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("mutationHeaders", () => {
  afterEach(() => setCsrfToken(null));

  it("always marks browser mutations and includes CSRF after login", () => {
    expect(mutationHeaders()).toEqual({ "X-Gate-Request": "webui" });
    setCsrfToken("csrf-value");
    expect(mutationHeaders()).toEqual({
      "X-Gate-Request": "webui",
      "X-Gate-CSRF": "csrf-value",
    });
  });
});

describe("settings backup", () => {
  it("requests the authenticated backup endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ format: "gate-settings-backup", version: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { gateApi } = await import("./api");
    await gateApi.settingsBackup();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/settings/backup",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });
});
