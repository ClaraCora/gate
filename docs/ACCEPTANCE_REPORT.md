# Gate v0.1 生产验收报告

> 验收日期：2026-09-02
>
> 最终验证 release：`20260902-082423-d4aee84-dirty`
>
> 部署目标：`HK-Aliyun`，Debian 13，2 vCPU，约 4 GiB RAM

## 1. 验收结论

Gate v0.1 已完成 Windows 开发、SSH 自动发布和真实 VPS 验收。最终 release 的控制面、数据面、
认证、防泄漏策略和四个当前可用地区均工作正常。欧洲候选在最终验证时无法建立隧道，系统按
设计将该地区标记为 `unavailable` 并关闭后端，没有改用错误地区或 VPS 原始公网出口。

本次结论适用于单机 MVP。VPN Gate 是无 SLA 的公共志愿者网络，单个地区的实时可用性不能作为
Gate 自身的持续 SLA。尚未执行 VPS 整机重启；启动 reconcile 已通过服务重启和新 release 发布
验证，整机重启恢复仍列为下一次维护窗口的运维检查项。

## 2. 自动化质量门禁

| 检查 | 结果 |
| --- | --- |
| Ruff lint | 通过 |
| Ruff format check | 通过 |
| mypy strict | 通过 |
| 后端 pytest | `44 passed` |
| 前端 Vitest | `1 passed` |
| 前端 production build | 通过，主 JS gzip 约 `94.69 KiB` |
| systemd unit verify | 通过 |
| HAProxy config check | 通过 |
| WebUI 完成度评审 | `ship` |
| 桌面与 390px 移动视口 | 无根级横向溢出 |

## 3. 最终 release 运行状态

- `gate-firewall`、`haproxy`、`gate-worker`、`gate-api` 均为 `enabled` 和 `active`。
- readiness 接口返回 `{"status":"ok"}`。
- WebUI `18080` 和 SOCKS `11081` 至 `11085` 均只监听 `127.0.0.1`。
- `/opt/gate/current` 指向最终验证 release，只保留最近 3 个 release。
- `nftables` NAT、`GATE-FORWARD` 转发链和 HAProxy 配置检查通过。
- 登录要求允许的 Origin 与 `X-Gate-Request`；认证后的写操作缺少 CSRF token 时返回 403。
- 节点发现通过配置的只读 fallback 接收 `99/99` 个有效节点；响应会记录实际来源 URL。

## 4. 真实出口验证

最终 release 通过 Windows 到 VPS 的 SSH 端口转发，以 `socks5h` 请求 Cloudflare trace。VPS
原始公网出口为 `HK`，IP 为 `47.238.204.223`。

| 地区 | 端口 | 最终状态 | 实测出口国家 | 实测结果 |
| --- | ---: | --- | --- | --- |
| 日本 | `11081` | healthy | `JP` | 成功，出口 IP 不同于 VPS |
| 韩国 | `11082` | healthy | `KR` | 成功，出口 IP 不同于 VPS |
| 北美 | `11083` | healthy | `US` | 成功，出口 IP 不同于 VPS |
| 欧洲 | `11084` | unavailable | 无 | SOCKS 拒绝连接，无 VPS 回落 |
| 东南亚 | `11085` | healthy | `TH` | 成功，出口 IP 不同于 VPS |

欧洲线路在较早的生产验证中曾成功使用 `NL` 出口。最终时点的两个欧洲候选均未能建立
OpenVPN 隧道，因此控制器清除了 active node、禁用了 HAProxy 后端，并清理了两个 slot 的
namespace 和 transient unit。这是公共节点失效时的预期行为。

## 5. 数据面与状态机验证

### 5.1 断线阻断

在受控测试中停止日本活动 OpenVPN unit，同时保留 SOCKS 和 HAProxy。代理请求以 curl 退出码
`97` 失败，没有返回 VPS 公网 IP；对应 namespace 的 nftables 默认策略仍为 drop。恢复控制器
后，日本线路由启动 reconcile 和自动策略恢复到有效 `JP` 出口。

### 5.2 A/B 切换与清理

真实日本线路完成一次 A 到 B 的验证后切换。新 slot 成为 active，旧 slot 进入 draining；排空
窗口结束后写入 `DRAIN_COMPLETED` 事件，旧 namespace 和 transient units 均被删除。最终运行态
只有 `gate-jp-b` 存在，`gate-jp-a` 不存在。

### 5.3 失败恢复

- 非活动 slot 在探测或切换前会幂等清理，重复切换不会被残留 namespace 阻塞。
- 启动 reconcile 会销毁非 active 的孤儿 slot。
- 从已失效线路发起的 failover 若所有候选失败，地区保持 `unavailable`，不会把旧线路伪装成
  healthy。
- locked 模式下健康检查失败会保持 `unavailable`，不会自动切换；该分支由单元测试覆盖。
- 候选探测失败不会把内部 `2000 ms` 评分惩罚值展示为实测延迟。

## 6. 资源实测

测量时有 JP、KR、NA、SEA 四条活动 OpenVPN 与 sing-box 链路，欧洲无活动 slot。

| 资源 | 实测值 | 说明 |
| --- | ---: | --- |
| Gate systemd cgroup 内存 | 约 `152 MiB` | API、worker、HAProxy、4 组 OpenVPN/sing-box 合计 |
| 进程 CPU 快照 | 约 `2.5%` | 空闲短时快照，不是峰值 |
| 当前 release | `99 MiB` | 其中 Python venv 约 `98 MiB` |
| 前端 dist | `328 KiB` | 已包含于 release |
| 3 个保留 release | `297 MiB` | 自动发布保留上限 |
| SQLite 数据库 | `396 KiB` | 短期验收数据 |
| `/var/lib/gate` | `916 KiB` | 数据库和当前运行材料 |
| systemd journal | `1.3 GiB` | 整台 VPS 的 journal 总量，不是 Gate 独占 |

内存数使用各相关 systemd cgroup 的 `MemoryCurrent` 求和，避免把共享页按每个进程 RSS 重复
计算。磁盘和数据库会随日志保留周期、事件量和 release 数量增长。

## 7. 已知边界与后续运维

- 未执行整机 reboot；应在有控制台和维护窗口时补做，并按运维手册复核 readiness、namespace、
  SOCKS 国家和防泄漏规则。
- VPN Gate 国家分布不均，北美、欧洲和东南亚可能正常进入 `unavailable`。
- MVP 主要保证 SOCKS5 TCP 与远端 DNS，不承诺 UDP。
- WebUI 和 SOCKS 默认只通过 SSH 隧道访问，不应直接暴露公网。
- 验收使用的管理员密码不进入仓库；交付后应按运维手册第 10 节轮换。
