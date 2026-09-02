import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Activity,
  ArrowLeftRight,
  Check,
  CircleAlert,
  CircleCheck,
  CircleOff,
  Clock3,
  Gauge,
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  Network,
  PlugZap,
  Radio,
  RefreshCw,
  RotateCcw,
  ServerCog,
  ShieldCheck,
  ShieldOff,
  TriangleAlert,
  X,
} from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError, gateApi, setCsrfToken } from "./api";
import { AnimatedList } from "./components/AnimatedList";
import { countryFlag, countryNameZh } from "./countries";
import { eventDescription, eventLevelLabel, eventTitle } from "./events";
import type {
  Candidate,
  GateEvent,
  Job,
  Region,
  RegionMode,
  RegionStatus,
  RuntimeSlot,
  SessionState,
} from "./types";

const REGION_LABELS: Record<string, string> = {
  jp: "日本",
  kr: "韩国",
  na: "北美",
  eu: "欧洲",
  sea: "东南亚",
};

function groupLabel(region: Region): string {
  return REGION_LABELS[region.group_id] ?? countryNameZh(region.countries[0] ?? "", region.name);
}

function entryLabel(region: Region): string {
  return region.name || groupLabel(region);
}

const STATUS_META: Record<
  RegionStatus,
  { label: string; tone: string; Icon: typeof CircleCheck }
> = {
  healthy: { label: "健康", tone: "healthy", Icon: CircleCheck },
  degraded: { label: "降级", tone: "warning", Icon: TriangleAlert },
  switching: { label: "切换中", tone: "working", Icon: LoaderCircle },
  starting: { label: "启动中", tone: "working", Icon: LoaderCircle },
  unavailable: { label: "不可用", tone: "danger", Icon: CircleAlert },
  disabled: { label: "已停用", tone: "muted", Icon: CircleOff },
};

const MODE_LABELS: Record<RegionMode, string> = {
  auto: "自动",
  locked: "锁定",
  disabled: "停用",
};

function formatTime(value: string | undefined): string {
  if (!value) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatDuration(milliseconds: number): string {
  const hours = Math.floor(milliseconds / 3_600_000);
  const days = Math.floor(hours / 24);
  if (days > 0) return `${days} 天`;
  if (hours > 0) return `${hours} 小时`;
  return `${Math.max(1, Math.floor(milliseconds / 60_000))} 分钟`;
}

function formatSpeed(bitsPerSecond: number): string {
  if (bitsPerSecond >= 1_000_000_000) return `${(bitsPerSecond / 1_000_000_000).toFixed(1)} Gbps`;
  return `${(bitsPerSecond / 1_000_000).toFixed(0)} Mbps`;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "操作失败，请检查服务状态后重试";
}

function jobDescription(job: Job): string {
  if (!job.error_code) return String(job.detail.message ?? JOB_STATUS_LABELS[job.status] ?? "状态已更新");
  const suggestions: Record<string, string> = {
    WORKER_CLIENT_ERROR: "网络 worker 不可用，请检查 gate-worker 服务",
    PROBE_FAILED: "出口验证失败，请重试或改测其他节点",
    SWITCH_FAILED: "线路未通过切换验证，活动出口保持不变",
    PROCESS_RESTARTED: "控制进程曾重启，请重新提交任务",
  };
  return suggestions[job.error_code] ?? "任务失败, 请查看事件记录";
}

function StatusBadge({ status }: { status: RegionStatus }) {
  const meta = STATUS_META[status];
  return (
    <span className={`status-badge status-badge--${meta.tone}`}>
      <meta.Icon
        aria-hidden="true"
        className={status === "switching" || status === "starting" ? "spin" : ""}
        size={15}
      />
      {meta.label}
    </span>
  );
}

function SkeletonRows({ count = 4 }: { count?: number }) {
  return (
    <div aria-label="正在加载" className="skeleton-stack" role="status">
      {Array.from({ length: count }, (_, index) => (
        <div className="skeleton-row" key={index} />
      ))}
    </div>
  );
}

function LoginView({ onAuthenticated }: { onAuthenticated: (value: SessionState) => void }) {
  const [password, setPassword] = useState("");
  const mutation = useMutation({
    mutationFn: gateApi.login,
    onSuccess: onAuthenticated,
  });

  return (
    <main className="login-shell">
      <div className="login-rail" aria-hidden="true">
        <span>11081</span><i /><span>11082</span><i /><span>11083</span><i />
        <span>11084</span><i /><span>11085</span>
      </div>
      <section className="login-workbench" aria-labelledby="login-title">
        <div className="brand-lockup">
          <span className="brand-mark"><Network size={24} /></span>
          <span>GATE</span>
        </div>
        <h1 id="login-title">出口控制台</h1>
        <p>验证操作员身份后，连接固定端口、测试出口并管理地区线路。</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            mutation.mutate(password);
          }}
        >
          <label htmlFor="password">管理员密码</label>
          <div className="password-field">
            <KeyRound aria-hidden="true" size={18} />
            <input
              autoComplete="current-password"
              autoFocus
              id="password"
              onChange={(event) => setPassword(event.target.value)}
              placeholder="输入首次部署生成的密码"
              type="password"
              value={password}
            />
          </div>
          {mutation.isError ? (
            <p className="form-error" role="alert"><CircleAlert size={16} />{errorMessage(mutation.error)}</p>
          ) : null}
          <button className="button button--primary login-submit" disabled={!password || mutation.isPending} type="submit">
            {mutation.isPending ? <LoaderCircle className="spin" size={17} /> : <ShieldCheck size={17} />}
            {mutation.isPending ? "正在验证" : "进入控制台"}
          </button>
        </form>
        <p className="login-footnote"><LockKeyhole size={14} /> 默认仅允许通过 VPS 回环地址和 SSH 隧道访问</p>
      </section>
    </main>
  );
}

type StreamState = "connecting" | "live" | "offline";

function useGateStream(enabled: boolean): StreamState {
  const queryClient = useQueryClient();
  const [state, setState] = useState<StreamState>("connecting");
  useEffect(() => {
    if (!enabled) return;
    const source = new EventSource("/api/v1/events/stream");
    source.onopen = () => setState("live");
    source.onerror = () => setState("offline");
    source.addEventListener("gate-event", () => {
      void queryClient.invalidateQueries({ queryKey: ["regions"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["events"] });
      void queryClient.invalidateQueries({ queryKey: ["slots"] });
    });
    return () => source.close();
  }, [enabled, queryClient]);
  return state;
}

function PortRail({
  regions,
  selectedId,
  onSelect,
}: {
  regions: Region[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <section aria-label="固定 SOCKS 端口" className="port-rail">
      <div className="port-rail__legend"><PlugZap size={16} />固定入口</div>
      <div className="port-rail__track">
        {regions.map((region, index) => {
          const meta = STATUS_META[region.status];
          return (
            <div className="port-node-wrap" key={region.id}>
              {index > 0 ? <span className="rail-link" aria-hidden="true" /> : null}
              <button
                aria-current={selectedId === region.id ? "true" : undefined}
                className={`port-node ${selectedId === region.id ? "port-node--selected" : ""}`}
                onClick={() => onSelect(region.id)}
                type="button"
              >
                <span className={`port-node__lamp port-node__lamp--${meta.tone}`} />
                <strong>{region.socks_port}</strong>
                <small>{entryLabel(region)}</small>
              </button>
            </div>
          );
        })}
      </div>
      <div className="port-rail__legend port-rail__legend--exit">动态出口<ArrowLeftRight size={16} /></div>
    </section>
  );
}

function SlotPair({ slots, unavailable = false }: { slots: RuntimeSlot[]; unavailable?: boolean }) {
  if (unavailable) {
    return <div className="slot-pair slot-pair--offline"><ShieldOff size={13} /><span>状态不可读</span></div>;
  }
  return (
    <div className="slot-pair" aria-label="A/B 运行槽">
      {(["a", "b"] as const).map((name) => {
        const slot = slots.find((item) => item.slot === name);
        const ready = Boolean(slot?.tunnel_up && slot?.socks_active);
        return (
          <div className={`slot ${ready ? "slot--ready" : ""}`} key={name}>
            <span className="slot__name">{name.toUpperCase()}</span>
            <span className="slot__state">{ready ? "隧道就绪" : slot?.exists ? "未就绪" : "空闲"}</span>
            <span className="slot__lights" aria-hidden="true">
              <i className={slot?.exists ? "on" : ""} />
              <i className={slot?.openvpn_active ? "on" : ""} />
              <i className={slot?.socks_active ? "on" : ""} />
            </span>
          </div>
        );
      })}
    </div>
  );
}

function RegionTable({
  regions,
  slots,
  jobs,
  selectedId,
  onSelect,
  onToggle,
  modePendingRegionId,
  runtimeUnavailable,
}: {
  regions: Region[];
  slots: RuntimeSlot[];
  jobs: Job[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onToggle: (region: Region) => void;
  modePendingRegionId: string | null;
  runtimeUnavailable: boolean;
}) {
  const grouped = new Map<string, Region[]>();
  for (const region of regions) {
    const entries = grouped.get(region.group_id) ?? [];
    entries.push(region);
    grouped.set(region.group_id, entries);
  }
  const groups = Array.from(grouped.entries());
  return (
    <div className="region-table-wrap">
      <table className="region-table">
        <thead>
          <tr>
            <th>地区 / 端口</th><th>出口</th><th>线路状态</th><th>A/B 数据面</th><th>候选</th><th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          {groups.map(([groupId, entries]) => {
            const first = entries[0];
            const enabledCount = entries.filter((entry) => entry.mode !== "disabled").length;
            return (
              <Fragment key={groupId}>
                <tr className="region-group-row">
                  <th colSpan={6} scope="rowgroup">
                    <span className="region-group-content">
                      <span aria-label={groupLabel(first)} className="region-group-flag" role="img">{countryFlag(first.countries[0] ?? "")}</span>
                      <strong>{groupLabel(first)}</strong>
                      <span>{entries.length} 个入口 · {enabledCount} 个已开启</span>
                    </span>
                  </th>
                </tr>
                {entries.map((region) => {
                  const activeJob = jobs.find((job) => job.region_id === region.id && ["queued", "running"].includes(job.status));
                  return (
                    <tr
                      className={`${selectedId === region.id ? "is-selected" : ""} ${region.mode === "disabled" ? "is-disabled" : ""}`}
                      key={region.id}
                      onClick={() => onSelect(region.id)}
                    >
                      <td>
                        <button className="region-name" onClick={() => onSelect(region.id)} type="button">
                          <strong>{entryLabel(region)}</strong>
                          <span>127.0.0.1:{region.socks_port}</span>
                        </button>
                      </td>
                      <td onClick={(event) => event.stopPropagation()}>
                        <RegionToggle
                          disabled={modePendingRegionId === region.id || Boolean(activeJob)}
                          enabled={region.mode !== "disabled"}
                          label={region.mode === "disabled" ? "已关闭" : MODE_LABELS[region.mode]}
                          name={entryLabel(region)}
                          onToggle={() => onToggle(region)}
                        />
                      </td>
                      <td>
                        <StatusBadge status={region.status} />
                        {activeJob ? <span className="job-inline">{Math.round(activeJob.progress * 100)}%</span> : null}
                      </td>
                      <td><SlotPair slots={slots.filter((slot) => slot.region_id === region.id)} unavailable={runtimeUnavailable} /></td>
                      <td><span className="numeric">{region.candidate_count}</span></td>
                      <td><time dateTime={region.updated_at}>{formatTime(region.updated_at)}</time></td>
                    </tr>
                  );
                })}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RegionToggle({
  enabled,
  disabled,
  label,
  name,
  onToggle,
}: {
  enabled: boolean;
  disabled: boolean;
  label: string;
  name: string;
  onToggle: () => void;
}) {
  return (
    <button
      aria-checked={enabled}
      aria-label={`${enabled ? "关闭" : "开启"}${name}出口`}
      className="route-toggle"
      disabled={disabled}
      onClick={onToggle}
      role="switch"
      type="button"
    >
      <span aria-hidden="true" className="route-toggle__track"><i /></span>
      <span className="route-toggle__label">{label}</span>
    </button>
  );
}

function ModeControl({
  value,
  disabled,
  onChange,
}: {
  value: RegionMode;
  disabled: boolean;
  onChange: (mode: RegionMode) => void;
}) {
  return (
    <div aria-label="地区运行模式" className="segmented" role="group">
      {(["auto", "locked"] as const).map((mode) => (
        <button
          aria-pressed={value === mode}
          disabled={disabled}
          key={mode}
          onClick={() => onChange(mode)}
          type="button"
        >
          {mode === "auto" ? <RotateCcw size={14} /> : <LockKeyhole size={14} />}
          {MODE_LABELS[mode]}
        </button>
      ))}
    </div>
  );
}

function RegionInspector({
  region,
  activeCandidate,
  slots,
  activeJob,
  modePending,
  probePending,
  reconnectPending,
  runtimeUnavailable,
  onMode,
  onToggle,
  onProbe,
  onReconnect,
}: {
  region: Region;
  activeCandidate: Candidate | undefined;
  slots: RuntimeSlot[];
  activeJob: Job | undefined;
  modePending: boolean;
  probePending: boolean;
  reconnectPending: boolean;
  runtimeUnavailable: boolean;
  onMode: (mode: RegionMode) => void;
  onToggle: () => void;
  onProbe: () => void;
  onReconnect: () => void;
}) {
  return (
    <aside className="inspector" aria-labelledby="inspector-title">
      <div className="inspector__heading">
        <div><h2 id="inspector-title">{entryLabel(region)}</h2><span className="port-address">127.0.0.1:{region.socks_port} · {groupLabel(region)}</span></div>
        <StatusBadge status={region.status} />
      </div>
      <div className="route-trace" aria-label="当前路由">
        <div className="route-endpoint"><span>SOCKS</span><strong>{region.socks_port}</strong></div>
        <span className="route-wire"><i /></span>
        <div className="route-switch"><ArrowLeftRight size={18} /><small>A / B</small></div>
        <span className={`route-wire ${region.status === "healthy" ? "route-wire--live" : ""}`}><i /></span>
        <div className="route-endpoint route-endpoint--exit"><span>出口</span><strong>{activeCandidate ? countryFlag(activeCandidate.country_code) : "--"}</strong></div>
      </div>
      <dl className="signal-grid">
        <div><dt>当前节点</dt><dd>{activeCandidate?.ip ?? (region.active_node_id ? `#${region.active_node_id}` : "未连接")}</dd></div>
        <div><dt>实际出口</dt><dd>{region.active_egress_ip ?? "--"}</dd></div>
        <div><dt>VPN 端点</dt><dd>{activeCandidate ? `${activeCandidate.transport.toUpperCase()} / ${activeCandidate.port}` : "--"}</dd></div>
        <div><dt>API 延迟</dt><dd>{activeCandidate?.api_ping_ms != null ? `${activeCandidate.api_ping_ms} ms` : "--"}</dd></div>
        <div><dt>出口国家</dt><dd>{activeCandidate ? countryNameZh(activeCandidate.country_code, activeCandidate.country_long) : "--"}</dd></div>
        <div><dt>线路评分</dt><dd>{activeCandidate?.quality_score != null ? activeCandidate.quality_score.toFixed(1) : "--"}</dd></div>
        <div><dt>候选线路</dt><dd>{region.candidate_count}</dd></div>
      </dl>
      <SlotPair slots={slots} unavailable={runtimeUnavailable} />
      {activeJob ? (
        <div className="active-job" aria-live="polite">
          <div><LoaderCircle className="spin" size={16} /><span>{String(activeJob.detail.message ?? "任务执行中")}</span><strong>{Math.round(activeJob.progress * 100)}%</strong></div>
          <progress max="1" value={activeJob.progress} />
        </div>
      ) : null}
      <div className="inspector__controls">
        <div className="region-power-row">
          <div><strong>入口开关</strong><span>{region.mode === "disabled" ? "端口停止转发" : "端口正在服务"}</span></div>
          <RegionToggle
            disabled={modePending || Boolean(activeJob)}
            enabled={region.mode !== "disabled"}
            label={region.mode === "disabled" ? "已关闭" : "已开启"}
            name={entryLabel(region)}
            onToggle={onToggle}
          />
        </div>
        <ModeControl disabled={modePending || Boolean(activeJob) || region.mode === "disabled"} onChange={onMode} value={region.mode} />
        <div className="inspector__actions">
          <button className="button button--primary" disabled={probePending || !["healthy", "degraded"].includes(region.status) || Boolean(activeJob)} onClick={onProbe} type="button">
            {probePending ? <LoaderCircle className="spin" size={16} /> : <Gauge size={16} />}
            {probePending ? "提交中" : "测试出口"}
          </button>
          <button className="button button--secondary" disabled={reconnectPending || !region.active_node_id || Boolean(activeJob)} onClick={onReconnect} type="button">
            {reconnectPending ? <LoaderCircle className="spin" size={16} /> : <PlugZap size={16} />}
            {reconnectPending ? "提交中" : "重新连接"}
          </button>
        </div>
      </div>
    </aside>
  );
}

function CandidateTable({
  candidates,
  activeNodeId,
  busy,
  onProbe,
  onSwitch,
}: {
  candidates: Candidate[];
  activeNodeId: number | null;
  busy: boolean;
  onProbe: (candidate: Candidate) => void;
  onSwitch: (candidate: Candidate) => void;
}) {
  if (candidates.length === 0) {
    return (
      <div className="empty-state">
        <Radio size={24} /><strong>当前入口没有可用候选节点</strong>
        <span>同地区其他入口已使用的节点不会重复显示。可刷新节点列表或暂时关闭此入口。</span>
      </div>
    );
  }
  return (
    <div className="candidate-table-wrap">
      <table className="candidate-table">
        <thead><tr><th>节点</th><th>端点</th><th>VPN Gate 指标</th><th>负载</th><th>持续在线</th><th><span className="sr-only">操作</span></th></tr></thead>
        <tbody>
          {candidates.map((candidate) => {
            const active = candidate.id === activeNodeId;
            return (
              <tr className={active ? "is-active" : ""} key={candidate.id}>
                <td>
                  <div className="candidate-id">
                    <span className="country-identity" title={candidate.country_code}>
                      <span aria-label={countryNameZh(candidate.country_code, candidate.country_long)} className="country-flag" role="img">{countryFlag(candidate.country_code)}</span>
                      <small>{countryNameZh(candidate.country_code, candidate.country_long)}</small>
                    </span>
                    <span><strong>{candidate.ip}</strong><small>{candidate.hostname}</small></span>
                  </div>
                </td>
                <td><span className="protocol">{candidate.transport.toUpperCase()}</span> {candidate.port}</td>
                <td><div className="metric-pair"><strong>{candidate.measured_latency_ms != null ? `实测 ${Math.round(candidate.measured_latency_ms)} ms` : candidate.api_ping_ms != null ? `API ${candidate.api_ping_ms} ms` : "--"}</strong><span>{candidate.quality_score != null ? `评分 ${candidate.quality_score.toFixed(1)} · 成功 ${Math.round((candidate.availability_24h ?? 0) * 100)}%` : formatSpeed(candidate.api_speed_bps)}</span></div></td>
                <td>{candidate.sessions} 会话</td>
                <td>{formatDuration(candidate.uptime_ms)}</td>
                <td className="candidate-actions">
                  <div className="candidate-action-set">
                    <button aria-label={`测试 ${candidate.ip}`} className="icon-button" disabled={busy} onClick={() => onProbe(candidate)} title="仅测试此候选" type="button"><Gauge size={17} /></button>
                    {active ? <span className="active-label"><Check size={14} />活动</span> : <button aria-label={`切换到 ${candidate.ip}`} className="icon-button" disabled={busy} onClick={() => onSwitch(candidate)} title="先验证再切换" type="button"><ArrowLeftRight size={17} /></button>}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const JOB_KIND_LABELS: Record<string, string> = {
  switch: "线路切换",
  candidate_probe: "候选测试",
  probe: "出口测试",
  reconnect: "重新连接",
  auto_switch: "自动接入",
  auto_candidate_probe: "自动候选测试",
  auto_quality_switch: "自动质量切换",
};

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "等待中",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function JobsView({ jobs, regions, cancellingId, onCancel }: { jobs: Job[]; regions: Region[]; cancellingId: string | null; onCancel: (jobId: string) => void }) {
  const names = new Map(regions.map((region) => [region.id, entryLabel(region)]));
  return (
    <div className="timeline-list">
      {jobs.length === 0 ? <div className="empty-state"><ServerCog size={24} /><strong>还没有控制任务</strong><span>测速或切换操作会在这里留下持久进度和结果。</span></div> : jobs.map((job) => (
        <article className="timeline-row" key={job.id}>
          <span className={`timeline-mark timeline-mark--${job.status}`} />
          <div><strong>{JOB_KIND_LABELS[job.kind] ?? "系统任务"}</strong><span>{job.region_id ? names.get(job.region_id) ?? "未知入口" : "系统"} · {jobDescription(job)}</span></div>
          <div className="timeline-result"><span>{JOB_STATUS_LABELS[job.status] ?? "未知状态"}</span><time>{formatTime(job.updated_at)}</time>{["queued", "running"].includes(job.status) ? <button aria-label={`取消任务 ${job.id}`} className="icon-button" disabled={cancellingId === job.id} onClick={() => onCancel(job.id)} title="取消任务" type="button">{cancellingId === job.id ? <LoaderCircle className="spin" size={15} /> : <X size={15} />}</button> : null}</div>
        </article>
      ))}
    </div>
  );
}

function EventsView({ events, regions }: { events: GateEvent[]; regions: Region[] }) {
  const names = new Map(regions.map((region) => [region.id, entryLabel(region)]));
  return (
    <AnimatedList
      className="timeline-list"
      empty={<div className="empty-state"><Activity size={24} /><strong>事件流为空</strong><span>发现、健康检查和切换状态会按时间出现在这里。</span></div>}
      itemKey={(event) => event.id}
      items={events}
      renderItem={(event) => (
        <article className="timeline-row">
          <span className={`timeline-mark timeline-mark--${event.level}`} />
          <div><strong>{eventTitle(event.code)}</strong><span>{event.region_id ? `${names.get(event.region_id) ?? "未知入口"} · ` : ""}{eventDescription(event)}</span></div>
          <div className="timeline-result"><span>{eventLevelLabel(event.level)}</span><time>{formatTime(event.created_at)}</time></div>
        </article>
      )}
    />
  );
}

function SwitchDialog({ candidate, region, busy, onCancel, onConfirm }: { candidate: Candidate | null; region: Region | null; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (candidate && dialog && !dialog.open) dialog.showModal();
    if (!candidate && dialog?.open) dialog.close();
  }, [candidate]);
  return (
    <dialog className="switch-dialog" onCancel={(event) => { event.preventDefault(); if (!busy) onCancel(); }} ref={ref}>
      <div className="dialog-heading"><div><ArrowLeftRight size={20} /><h2>切换地区出口</h2></div><button aria-label="关闭" className="icon-button" disabled={busy} onClick={onCancel} type="button"><X size={18} /></button></div>
      <p>Gate 会在备用 slot 建立隧道并验证出口国家。只有验证成功，固定端口才会切到新线路。</p>
      <dl className="dialog-route">
        <div><dt>入口</dt><dd>{region ? entryLabel(region) : "--"}</dd></div>
        <div><dt>固定端口</dt><dd>{region?.socks_port ?? "--"}</dd></div>
        <div><dt>候选节点</dt><dd>{candidate?.ip ?? "--"}</dd></div>
        <div><dt>VPN 端点</dt><dd>{candidate ? `${candidate.transport.toUpperCase()} / ${candidate.port}` : "--"}</dd></div>
      </dl>
      <div className="dialog-actions"><button className="button button--secondary" disabled={busy} onClick={onCancel} type="button">取消</button><button className="button button--primary" disabled={busy} onClick={onConfirm} type="button">{busy ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}{busy ? "正在提交" : "验证并切换"}</button></div>
    </dialog>
  );
}

function DisableRegionDialog({
  region,
  busy,
  onCancel,
  onConfirm,
}: {
  region: Region | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (region && dialog && !dialog.open) dialog.showModal();
    if (!region && dialog?.open) dialog.close();
  }, [region]);
  return (
    <dialog className="switch-dialog" onCancel={(event) => { event.preventDefault(); if (!busy) onCancel(); }} ref={ref}>
      <div className="dialog-heading">
        <div><ShieldOff size={20} /><h2>关闭{region ? entryLabel(region) : "当前入口"}</h2></div>
        <button aria-label="关闭" className="icon-button" disabled={busy} onClick={onCancel} type="button"><X size={18} /></button>
      </div>
      <p>固定端口 {region?.socks_port ?? "--"} 将停止转发，当前 OpenVPN 和 SOCKS 运行槽会被清理。候选数据与历史记录仍会保留。</p>
      <div className="dialog-actions">
        <button className="button button--secondary" disabled={busy} onClick={onCancel} type="button">取消</button>
        <button className="button button--danger" disabled={busy} onClick={onConfirm} type="button">
          {busy ? <LoaderCircle className="spin" size={16} /> : <ShieldOff size={16} />}
          {busy ? "正在关闭" : "关闭出口"}
        </button>
      </div>
    </dialog>
  );
}

function ConsoleView({ session, onLogout }: { session: SessionState; onLogout: () => void }) {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [switchTarget, setSwitchTarget] = useState<Candidate | null>(null);
  const [disableTarget, setDisableTarget] = useState<Region | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const streamState = useGateStream(session.authenticated);
  const regionsQuery = useQuery({ queryKey: ["regions"], queryFn: gateApi.regions, refetchInterval: 10_000 });
  const slotsQuery = useQuery({ queryKey: ["slots"], queryFn: gateApi.slots, refetchInterval: 10_000, retry: false });
  const jobsQuery = useQuery({ queryKey: ["jobs"], queryFn: gateApi.jobs, refetchInterval: 5_000 });
  const eventsQuery = useQuery({ queryKey: ["events"], queryFn: gateApi.events, refetchInterval: 15_000 });
  const regions = regionsQuery.data ?? [];
  const jobs = jobsQuery.data ?? [];
  const slots = slotsQuery.data ?? [];
  const selectedId = params.get("region") ?? regions[0]?.id ?? null;
  const tab = params.get("view") ?? "candidates";
  const selectedRegion = regions.find((region) => region.id === selectedId) ?? null;
  const candidatesQuery = useQuery({ queryKey: ["candidates", selectedId], queryFn: () => gateApi.candidates(selectedId!), enabled: Boolean(selectedId) });
  const candidates = candidatesQuery.data ?? [];
  const activeCandidate = candidates.find((candidate) => candidate.id === selectedRegion?.active_node_id);
  const activeJob = jobs.find((job) => job.region_id === selectedId && ["queued", "running"].includes(job.status));
  const selectedSlots = slots.filter((slot) => slot.region_id === selectedId);

  const refreshMutation = useMutation({
    mutationFn: gateApi.refreshDiscovery,
    onSuccess: (result) => {
      setNotice(`发现完成：接受 ${result.accepted}/${result.discovered} 个节点`);
      void queryClient.invalidateQueries({ queryKey: ["regions"] });
      void queryClient.invalidateQueries({ queryKey: ["candidates"] });
      void queryClient.invalidateQueries({ queryKey: ["events"] });
    },
  });
  const probeMutation = useMutation({
    mutationFn: (regionId: string) => gateApi.probeRegion(regionId),
    onSuccess: () => {
      setNotice("出口测试已进入任务队列");
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const modeMutation = useMutation({
    mutationFn: ({ regionId, mode }: { regionId: string; mode: RegionMode }) => gateApi.setMode(regionId, mode),
    onSuccess: (updated, variables) => {
      setDisableTarget(null);
      setNotice(
        variables.mode === "disabled"
          ? `${entryLabel(updated)}已关闭`
          : variables.mode === "auto"
            ? `${entryLabel(updated)}已开启并进入自动模式`
            : `${entryLabel(updated)}已锁定当前线路`,
      );
      void queryClient.invalidateQueries({ queryKey: ["regions"] });
      void queryClient.invalidateQueries({ queryKey: ["slots"] });
      void queryClient.invalidateQueries({ queryKey: ["events"] });
    },
    onError: () => setDisableTarget(null),
  });
  const candidateProbeMutation = useMutation({
    mutationFn: ({ regionId, nodeId }: { regionId: string; nodeId: number }) => gateApi.probeCandidate(regionId, nodeId),
    onSuccess: () => {
      setNotice("候选测试已进入任务队列；活动线路不会改变");
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const switchMutation = useMutation({
    mutationFn: ({ regionId, nodeId }: { regionId: string; nodeId: number }) => gateApi.switchCandidate(regionId, nodeId),
    onSuccess: () => {
      setSwitchTarget(null);
      setNotice("切换任务已提交；旧线路会保持到新出口验证成功");
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const reconnectMutation = useMutation({
    mutationFn: (regionId: string) => gateApi.reconnectRegion(regionId),
    onSuccess: () => {
      setNotice("重新连接任务已提交；固定端口会在新线路验证后恢复");
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => gateApi.cancelJob(jobId),
    onSuccess: () => {
      setNotice("任务已取消");
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const mutationError = refreshMutation.error ?? probeMutation.error ?? candidateProbeMutation.error ?? modeMutation.error ?? switchMutation.error ?? reconnectMutation.error ?? cancelMutation.error;
  const enabledRegions = useMemo(() => regions.filter((region) => region.mode !== "disabled"), [regions]);
  const liveRegions = useMemo(() => enabledRegions.filter((region) => region.status === "healthy").length, [enabledRegions]);
  const selectRegion = (id: string) => setParams((current) => { current.set("region", id); return current; });
  const selectTab = (value: string) => setParams((current) => { current.set("view", value); return current; });
  const toggleRegion = (region: Region) => {
    if (region.mode === "disabled") {
      modeMutation.mutate({ regionId: region.id, mode: "auto" });
    } else {
      setDisableTarget(region);
    }
  };

  return (
    <div className="app-shell">
      <header className="command-bar">
        <div className="brand-lockup brand-lockup--bar"><span className="brand-mark"><Network size={20} /></span><span>GATE</span><small>出口控制台</small></div>
        <div className="command-status">
          <span className={`stream-state stream-state--${streamState}`}><i />{streamState === "live" ? "事件流在线" : streamState === "connecting" ? "连接事件流" : "事件流重连中"}</span>
          <span className="system-count"><ShieldCheck size={15} />{liveRegions}/{enabledRegions.length} 个已启用入口健康</span>
        </div>
        <div className="command-actions">
          <button className="button button--dark" disabled={refreshMutation.isPending} onClick={() => refreshMutation.mutate()} type="button">{refreshMutation.isPending ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}{refreshMutation.isPending ? "正在发现" : "刷新节点"}</button>
          <button aria-label="退出登录" className="icon-button icon-button--dark" onClick={onLogout} title="退出登录" type="button"><LogOut size={17} /></button>
        </div>
      </header>

      {regionsQuery.isLoading ? <div className="page-loading"><SkeletonRows count={5} /></div> : regionsQuery.isError ? (
        <main className="fatal-state"><CircleAlert size={28} /><h1>控制面暂时不可用</h1><p>{errorMessage(regionsQuery.error)}</p><button className="button button--primary" onClick={() => void regionsQuery.refetch()} type="button"><RefreshCw size={16} />重新连接</button></main>
      ) : (
        <>
          <PortRail onSelect={selectRegion} regions={regions} selectedId={selectedId} />
          {(notice || mutationError) ? <div className={`notice ${mutationError ? "notice--error" : ""}`} role={mutationError ? "alert" : "status"}><span>{mutationError ? <CircleAlert size={16} /> : <CircleCheck size={16} />}{mutationError ? errorMessage(mutationError) : notice}</span><button aria-label="关闭通知" className="icon-button" onClick={() => { setNotice(null); refreshMutation.reset(); probeMutation.reset(); candidateProbeMutation.reset(); modeMutation.reset(); switchMutation.reset(); reconnectMutation.reset(); cancelMutation.reset(); }} type="button"><X size={15} /></button></div> : null}
          <main className="workspace">
            <section className="routes-panel" aria-labelledby="routes-title">
              <div className="section-heading"><div><h1 id="routes-title">地区入口</h1><p>同一地区可开启多个固定端口, 每个端口使用互不重复的真实出口。</p></div><span className="last-sync"><Clock3 size={14} />{formatTime(regions[0]?.updated_at)}</span></div>
              <RegionTable jobs={jobs} modePendingRegionId={modeMutation.isPending ? modeMutation.variables?.regionId ?? null : null} onSelect={selectRegion} onToggle={toggleRegion} regions={regions} runtimeUnavailable={slotsQuery.isError} selectedId={selectedId} slots={slots} />
            </section>
            {selectedRegion ? <RegionInspector activeCandidate={activeCandidate} activeJob={activeJob} modePending={modeMutation.isPending && modeMutation.variables?.regionId === selectedRegion.id} onMode={(mode) => modeMutation.mutate({ regionId: selectedRegion.id, mode })} onProbe={() => probeMutation.mutate(selectedRegion.id)} onReconnect={() => reconnectMutation.mutate(selectedRegion.id)} onToggle={() => toggleRegion(selectedRegion)} probePending={probeMutation.isPending} reconnectPending={reconnectMutation.isPending} region={selectedRegion} runtimeUnavailable={slotsQuery.isError} slots={selectedSlots} /> : null}
          </main>
          <section className="detail-bay">
            <div className="detail-tabs" role="tablist" aria-label="线路详情">
              <button aria-selected={tab === "candidates"} onClick={() => selectTab("candidates")} role="tab" type="button"><Radio size={15} />候选节点 <span>{candidates.length}</span></button>
              <button aria-selected={tab === "jobs"} onClick={() => selectTab("jobs")} role="tab" type="button"><ServerCog size={15} />任务 <span>{jobs.length}</span></button>
              <button aria-selected={tab === "events"} onClick={() => selectTab("events")} role="tab" type="button"><Activity size={15} />事件 <span>{eventsQuery.data?.length ?? 0}</span></button>
            </div>
            <div className="detail-content" role="tabpanel">
              {tab === "candidates" ? (candidatesQuery.isLoading ? <SkeletonRows count={5} /> : <CandidateTable activeNodeId={selectedRegion?.active_node_id ?? null} busy={selectedRegion?.mode === "disabled" || Boolean(activeJob) || switchMutation.isPending || candidateProbeMutation.isPending} candidates={candidates} onProbe={(candidate) => { if (selectedRegion) candidateProbeMutation.mutate({ regionId: selectedRegion.id, nodeId: candidate.id }); }} onSwitch={setSwitchTarget} />) : tab === "jobs" ? <JobsView cancellingId={cancelMutation.isPending ? cancelMutation.variables ?? null : null} jobs={jobs} onCancel={(jobId) => cancelMutation.mutate(jobId)} regions={regions} /> : <EventsView events={eventsQuery.data ?? []} regions={regions} />}
            </div>
          </section>
        </>
      )}
      <SwitchDialog busy={switchMutation.isPending} candidate={switchTarget} onCancel={() => setSwitchTarget(null)} onConfirm={() => { if (selectedRegion && switchTarget) switchMutation.mutate({ regionId: selectedRegion.id, nodeId: switchTarget.id }); }} region={selectedRegion} />
      <DisableRegionDialog busy={modeMutation.isPending && modeMutation.variables?.mode === "disabled"} onCancel={() => setDisableTarget(null)} onConfirm={() => { if (disableTarget) modeMutation.mutate({ regionId: disableTarget.id, mode: "disabled" }); }} region={disableTarget} />
    </div>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const sessionQuery = useQuery({ queryKey: ["session"], queryFn: gateApi.session, retry: false });
  const logoutMutation = useMutation({
    mutationFn: gateApi.logout,
    onSuccess: () => {
      setCsrfToken(null);
      queryClient.clear();
      void queryClient.setQueryData(["session"], { authenticated: false, csrf_token: null, expires_at: null });
    },
  });
  useEffect(() => setCsrfToken(sessionQuery.data?.csrf_token ?? null), [sessionQuery.data]);

  if (sessionQuery.isLoading) return <div className="boot-screen" role="status"><span className="brand-mark"><Network size={24} /></span><LoaderCircle className="spin" size={20} /><span>正在连接 Gate</span></div>;
  if (sessionQuery.isError) return <main className="fatal-state fatal-state--fullscreen"><CircleAlert size={30} /><h1>无法连接 Gate</h1><p>{errorMessage(sessionQuery.error)}</p><button className="button button--primary" onClick={() => void sessionQuery.refetch()} type="button"><RefreshCw size={16} />重新连接</button></main>;
  if (!sessionQuery.data?.authenticated) return <LoginView onAuthenticated={(value) => { setCsrfToken(value.csrf_token); queryClient.setQueryData(["session"], value); }} />;
  return <ConsoleView onLogout={() => logoutMutation.mutate()} session={sessionQuery.data} />;
}
