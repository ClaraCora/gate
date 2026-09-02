import { describe, expect, it } from "vitest";

import { countryFlag, countryNameZh } from "./countries";

describe("country presentation", () => {
  it("renders ISO countries as flags with Chinese names", () => {
    expect(countryFlag("jp")).toBe("🇯🇵");
    expect(countryNameZh("JP", "Japan")).toBe("日本");
    expect(countryNameZh("GB", "United Kingdom")).toBe("英国");
  });

  it("falls back safely for invalid country codes", () => {
    expect(countryFlag("unknown")).toBe("🌐");
    expect(countryNameZh("unknown", "未知地区")).toBe("未知地区");
  });
});
