import { describe, expect, it } from "vitest";

import { eventDescription, eventLevelLabel, eventTitle } from "./events";
import type { GateEvent } from "./types";

function event(overrides: Partial<GateEvent> = {}): GateEvent {
  return {
    id: 1,
    code: "SWITCH_ROLLED_BACK",
    level: "error",
    message: "Japan switch failed and was rolled back",
    region_id: "jp",
    node_id: 4,
    details: {},
    created_at: "2026-09-02T00:00:00Z",
    ...overrides,
  };
}

describe("event presentation", () => {
  it("translates legacy English event records", () => {
    expect(eventTitle(event().code)).toBe("线路切换已回退");
    expect(eventDescription(event())).toContain("恢复原线路");
    expect(eventLevelLabel(event().level)).toBe("错误");
  });

  it("keeps current Chinese descriptions", () => {
    const current = event({ message: "日本 01 已切换到出口 203.0.113.10" });
    expect(eventDescription(current)).toBe(current.message);
  });

  it("labels SOCKS access changes in Chinese", () => {
    const auth = event({ code: "SOCKS_AUTH_UPDATED", message: "SOCKS 统一认证已启用" });
    expect(eventTitle(auth.code)).toBe("SOCKS 接入设置已更新");
    expect(eventDescription(auth)).toBe(auth.message);
  });

  it("labels automation changes in Chinese", () => {
    const automation = event({ code: "AUTOMATION_ENABLED_CHANGED", message: "" });
    expect(eventTitle(automation.code)).toBe("自动检查设置已更新");
    expect(eventDescription(automation)).toBe("自动检查开关已更新。");
  });
});
