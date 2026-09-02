# Gate

Gate 是一个部署在单台 Linux VPS 上的 VPN Gate 多地区 SOCKS5 出口管理器。

系统为每个地区提供稳定的本地 SOCKS5 端口，定时获取 VPN Gate 公共节点，依据 VPS
真实链路质量选择出口，并通过 WebUI 提供状态查看、测速、手动切换、线路锁定和失败回滚。

## 当前状态

v0.1 MVP 已实现并部署到真实 VPS，已完成生产链路、断线阻断、A/B 切换和 WebUI 验收：

- 可按地区预置多个固定 SOCKS5 入口, 每个入口拥有独立 A/B 数据面
- 同地区活动入口排除重复节点, 切换提交前同时校验真实出口 IP 不重复
- VPN Gate CSV 容错解析、候选粗筛和定时发现
- OpenVPN 配置严格净化、PEM 与证书/私钥匹配校验
- Linux network namespace、nftables kill switch、sing-box 和 HAProxy A/B 数据面
- 基于 VPS 实测的候选探测、评分、15% 迟滞、连续确认和 30 分钟冷却
- 手动测试、验证后切换、重连、锁定、停用、任务取消和启动 reconcile
- FastAPI、SQLite、持久任务、事件历史、SSE 和数据保留
- Argon2 管理员密码、签名会话、HttpOnly Cookie、CSRF 与 Origin 校验
- React/TypeScript WebUI，覆盖桌面与 360px 以上移动视口
- systemd 加固、Windows 一键 SSH 发布、失败自动回滚和 GitHub Actions

- 部署目标：SSH 主机别名 `HK-Aliyun`
- 开发环境：Windows
- 生产环境：Debian 13，2 vCPU，约 4 GiB RAM
- 默认访问方式：SSH 隧道，不公开暴露 WebUI 和 SOCKS 端口

完整设计、实施范围和验收标准见：

- [完整项目方案书](docs/PROJECT_PLAN.md)
- [部署与运维手册](docs/OPERATIONS.md)
- [生产验收报告](docs/ACCEPTANCE_REPORT.md)
- [界面设计系统](DESIGN.md)

## 预设出口

| 出口 | 国家范围 | 本地 SOCKS5 端口 |
| --- | --- | ---: |
| 日本 | `JP` | `11081` |
| 韩国 | `KR` | `11082` |
| 北美 | `US, CA` | `11083` |
| 欧洲 | `DE, NL, FR, GB, RO, PL, ES, IT, SE, FI, CH, AT` | `11084` |
| 东南亚 | `SG, TH, VN, ID, MY, PH` | `11085` |

日本另外预置 `jp-02` 至 `jp-10` 九个入口, 端口为 `11101-11109`, 默认关闭。可在 WebUI
逐个开启需要的入口; 关闭状态不创建 OpenVPN、sing-box 或 network namespace。入口定义位于
`config/gate.example.yaml`, `group_id` 相同的入口属于同一地区并共享去重约束。

如果某地区没有合格节点，对应端口应明确报告不可用，不能静默使用错误地区出口。

## 本地使用方式

```powershell
ssh -N `
  -L 18080:127.0.0.1:18080 `
  -L 11081:127.0.0.1:11081 `
  -L 11082:127.0.0.1:11082 `
  -L 11083:127.0.0.1:11083 `
  -L 11084:127.0.0.1:11084 `
  -L 11085:127.0.0.1:11085 `
  HK-Aliyun
```

连接后：

- WebUI：`http://127.0.0.1:18080`
- 日本 SOCKS5：`127.0.0.1:11081`
- 其他地区依照上表端口访问

需要远端解析域名的客户端应选择 `socks5h`，避免本机 DNS 绕过代理。

## 安全说明

VPN Gate 是公共志愿者网络，出口稳定性、隐私和可用性均没有商业保证。系统必须净化远端
OpenVPN 配置、隔离每条线路、启用断线阻断，并默认只监听 VPS 回环地址。敏感业务仍应全程
使用 HTTPS/TLS，不应把公共 VPN 出口视为可信网络。

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\scripts\test.ps1
.\scripts\dev-api.ps1
```

API 文档默认位于 `http://127.0.0.1:18080/api/docs`。手动触发一次节点发现：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:18080/api/v1/discovery/refresh
```

节点发现默认直连 VPN Gate 官方 API；若源站对 VPS 出口返回无效内容，则按配置回退到
`r.jina.ai` 对同一公开 feed 的只读中继。API 响应中的 `source_url` 会标明本次实际来源。
中继会增加一个外部信任边界；如有自建中继，应在 `discovery.fallback_urls` 中替换它。

## 部署

首次部署到已配置的 SSH 主机别名：

```powershell
.\scripts\deploy.ps1 -HostAlias HK-Aliyun -Bootstrap
```

后续发布：

```powershell
.\scripts\deploy.ps1 -HostAlias HK-Aliyun
```

脚本要求 SSH 远端用户为 root，发布前自动执行前后端检查和生产构建。首次部署生成的管理员
明文密码只在终端显示一次，不会写入 Git。完整前置条件、备份、回滚、诊断和凭据轮换见
[运维手册](docs/OPERATIONS.md)。

## 资源占用

Gate 不在代理流量热路径中运行 Python；主要资源来自已开启入口的 OpenVPN 和 sing-box
进程。关闭的预置入口仅增加少量数据库记录和 HAProxy 静态配置, 不会常驻隧道进程。五个
活动入口的规划预算为：

| 资源 | 预算 |
| --- | ---: |
| 常驻内存 | 300-600 MiB |
| A/B 切换临时增加 | 每条 50-100 MiB |
| 空闲 CPU | 通常低于 5% |
| 程序与 Python 依赖磁盘 | 0.5-1 GiB |
| SQLite 与保留日志 | 0.5 GiB 内 |
| 自动探测流量 | 约 5-15 GiB/月 |

热备用会让对应地区的隧道资源接近翻倍。实际 SOCKS 业务流量远大于控制面和自动探测开销；
部分 VPS 厂商同时计算入站和出站流量。

2026-09-02 在 Debian 13、4 个活动地区的空闲快照中，Gate 相关 systemd cgroup 合计约
`152 MiB`，进程 CPU 快照约 `2.5%`；单个 release 约 `99 MiB`，保留 3 个 release 共
`297 MiB`，SQLite 数据库约 `396 KiB`。这些是短期验收值，不代表长期流量峰值；完整测量边界
见[生产验收报告](docs/ACCEPTANCE_REPORT.md)。
