import type { GateEvent } from "./types";

const EVENT_TITLES: Record<string, string> = {
  ACTIVE_HEALTH_CHECK_FAILED: "活动出口检查失败",
  AUTOMATION_DISCOVERY_FAILED: "自动刷新失败",
  AUTOMATION_INTERNAL_ERROR: "自动维护异常",
  AUTO_CANDIDATE_FAILED: "自动候选切换失败",
  AUTO_OPTIMIZATION_FAILED: "线路优化失败",
  AUTO_OPTIMIZATION_PENDING: "优选节点等待确认",
  AUTO_QUALITY_SWITCH: "已自动优化线路",
  AUTO_REGION_UNAVAILABLE: "没有可用候选节点",
  CANDIDATE_PROBE_COMPLETED: "候选节点测试通过",
  CANDIDATE_PROBE_FAILED: "候选节点测试失败",
  DISCOVERY_COMPLETED: "节点刷新完成",
  DISCOVERY_FAILED: "节点刷新失败",
  DRAIN_CLEANUP_FAILED: "旧隧道清理失败",
  DRAIN_COMPLETED: "旧隧道已清理",
  LOCKED_REGION_UNAVAILABLE: "锁定出口不可用",
  RECONCILE_DUPLICATE_EXIT: "检测到重复出口",
  RECONCILE_REJECTED_ACTIVE_SLOT: "启动校验未通过",
  RECONCILE_VERIFIED_ACTIVE_SLOT: "启动校验通过",
  SWITCH_COMPLETED: "线路切换完成",
  SWITCH_ROLLED_BACK: "线路切换已回退",
};

const EVENT_FALLBACKS: Record<string, string> = {
  ACTIVE_HEALTH_CHECK_FAILED: "活动出口未通过健康检查, 系统将按失败次数决定是否重选线路。",
  AUTOMATION_DISCOVERY_FAILED: "系统未能刷新节点列表, 请检查上游连接后重试。",
  AUTOMATION_INTERNAL_ERROR: "自动维护周期发生异常, 请查看服务日志。",
  AUTO_CANDIDATE_FAILED: "候选节点未能完成自动切换, 当前活动线路保持不变。",
  AUTO_OPTIMIZATION_FAILED: "质量优化未能完成, 当前活动线路保持不变。",
  AUTO_OPTIMIZATION_PENDING: "候选线路尚未满足确认轮次或切换冷却条件。",
  AUTO_QUALITY_SWITCH: "系统已切换到实测质量更高的出口。",
  AUTO_REGION_UNAVAILABLE: "没有候选节点通过验证, 入口暂时不可用。",
  CANDIDATE_PROBE_COMPLETED: "候选节点已通过出口国家、网络和延迟验证。",
  CANDIDATE_PROBE_FAILED: "候选节点未通过出口验证, 可测试其他节点。",
  DISCOVERY_COMPLETED: "节点列表已刷新, 候选数据已更新。",
  DISCOVERY_FAILED: "节点列表刷新失败, 请稍后重试。",
  DRAIN_CLEANUP_FAILED: "旧连接排空后未能清理隧道, 请检查网络工作进程。",
  DRAIN_COMPLETED: "旧连接已排空, 对应隧道资源已清理。",
  LOCKED_REGION_UNAVAILABLE: "锁定线路连续检查失败, 系统不会自动切换。",
  RECONCILE_DUPLICATE_EXIT: "同地区入口使用了相同的真实出口, 后接入的入口已关闭。",
  RECONCILE_REJECTED_ACTIVE_SLOT: "活动隧道未通过启动校验, 已停止转发。",
  RECONCILE_VERIFIED_ACTIVE_SLOT: "活动隧道已通过启动校验并恢复转发。",
  SWITCH_COMPLETED: "新出口已通过验证, 固定端口已完成切换。",
  SWITCH_ROLLED_BACK: "新出口未通过验证, 固定端口已恢复原线路。",
};

const LEVEL_LABELS: Record<string, string> = {
  info: "正常",
  warning: "警告",
  error: "错误",
};

export function eventTitle(code: string): string {
  return EVENT_TITLES[code] ?? "系统事件";
}

export function eventLevelLabel(level: string): string {
  return LEVEL_LABELS[level] ?? "记录";
}

export function eventDescription(event: GateEvent): string {
  if (/\p{Script=Han}/u.test(event.message)) return event.message;
  return EVENT_FALLBACKS[event.code] ?? "系统记录了一条运行状态变化。";
}
