import { mutationHeaders, setCsrfToken } from "./api";

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
