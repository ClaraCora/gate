# Gate 运维手册

本文面向单台 Debian VPS 上的 Gate 实例。示例使用 Windows PowerShell 和 SSH 主机别名
`HK-Aliyun`。除非明确说明，VPS 命令需要 root 权限；当前发布脚本要求该 SSH 别名直接登录
root 账户。

## 1. 目录与服务

| 路径或服务 | 用途 |
| --- | --- |
| `/opt/gate/current` | 指向当前 release 的原子符号链接 |
| `/opt/gate/releases/<release-id>` | 不可变发布目录，自动保留最近 3 个 |
| `/etc/gate/config.yaml` | 非敏感运行配置 |
| `/etc/gate/secrets.env` | Argon2 管理员密码散列和会话签名密钥 |
| `/etc/gate/firewall.env` | VPS 公网接口名 |
| `/var/lib/gate/gate.db` | SQLite 主数据库 |
| `/var/lib/gate/slots` | 净化后的 slot 运行材料 |
| `/run/gate/worker.sock` | API 到特权 worker 的 Unix socket |
| `/run/haproxy/gate-admin.sock` | HAProxy runtime socket |
| `gate-api.service` | FastAPI、WebUI、调度器和控制器 |
| `gate-worker.service` | root 网络 worker |
| `gate-firewall.service` | 宿主转发和 NAT 规则 |
| `haproxy.service` | 五个固定 SOCKS5 入口 |

OpenVPN 和 sing-box 以 transient systemd unit 运行，命名分别为
`gate-openvpn-<region>-<slot>.service` 和 `gate-socks-<region>-<slot>.service`。network
namespace 命名为 `gate-<region>-<slot>`。

## 2. 首次部署

### 2.1 前置检查

在 Windows 项目目录执行：

```powershell
ssh HK-Aliyun "id -u; cat /etc/os-release | head; uname -m"
```

期望第一行是 `0`，系统为 Debian，架构为 `x86_64`。确认仓库内检查通过：

```powershell
.\scripts\test.ps1
```

### 2.2 Bootstrap 与发布

首次部署安装 OpenVPN、HAProxy、nftables、sing-box、Python 等依赖：

```powershell
.\scripts\deploy.ps1 -HostAlias HK-Aliyun -Bootstrap
```

后续发布不需要重复 bootstrap：

```powershell
.\scripts\deploy.ps1 -HostAlias HK-Aliyun
```

首次安装会在终端输出一次 `Gate WebUI initial admin password`。立即放入密码管理器；脚本只把
Argon2 散列写入 `/etc/gate/secrets.env`，不会把明文写入仓库或 VPS 文件。若终端输出丢失，按
第 10 节轮换密码，不要尝试从散列恢复。

发布脚本会在本地执行检查和前端构建，将 release 上传到 `/tmp`，在 VPS 创建独立虚拟环境，
原子切换 `/opt/gate/current`，重启服务，并等待就绪接口。安装失败时会把 current 链接和服务
恢复到前一版本。

### 2.3 首次就绪检查

```powershell
ssh HK-Aliyun "systemctl --no-pager --full status gate-firewall haproxy gate-worker gate-api"
ssh HK-Aliyun "curl --fail --silent http://127.0.0.1:18080/api/v1/health/ready"
ssh HK-Aliyun "ss -lntp | grep -E ':(18080|11081|11082|11083|11084|11085)[[:space:]]'"
```

所有 WebUI 和 SOCKS 监听都必须是 `127.0.0.1`。如果出现 `0.0.0.0` 或 VPS 公网地址，停止
服务并修正配置，不要依赖云防火墙掩盖错误监听。

## 3. Windows 访问

保持以下 PowerShell 会话运行：

```powershell
ssh -N `
  -o ExitOnForwardFailure=yes `
  -o ServerAliveInterval=30 `
  -L 18080:127.0.0.1:18080 `
  -L 11081:127.0.0.1:11081 `
  -L 11082:127.0.0.1:11082 `
  -L 11083:127.0.0.1:11083 `
  -L 11084:127.0.0.1:11084 `
  -L 11085:127.0.0.1:11085 `
  HK-Aliyun
```

WebUI 位于 `http://127.0.0.1:18080`。应用应使用 `socks5h://127.0.0.1:<port>`，其中
`socks5h` 让域名在远端解析，避免本机 DNS 绕过代理。

快速测试：

```powershell
curl.exe --fail --max-time 20 --proxy socks5h://127.0.0.1:11081 https://www.cloudflare.com/cdn-cgi/trace
curl.exe --fail --max-time 20 --proxy socks5h://127.0.0.1:11082 https://www.cloudflare.com/cdn-cgi/trace
```

返回中的 `ip=` 应不同于 VPS 原始公网 IP，`loc=` 应分别为 `JP` 和 `KR`。公共节点随时可能
失效；超时本身不等于 Gate 泄漏，必须同时确认请求没有返回 VPS 原始公网 IP。

## 4. 常规状态检查

```powershell
ssh HK-Aliyun "systemctl is-active gate-firewall haproxy gate-worker gate-api"
ssh HK-Aliyun "systemctl list-units --all 'gate-openvpn-*' 'gate-socks-*' --no-pager"
ssh HK-Aliyun "ip netns list"
ssh HK-Aliyun "journalctl -u gate-api -u gate-worker -u haproxy --since '-30 min' --no-pager"
```

检查运行态 slot：

```powershell
ssh HK-Aliyun "curl --silent http://127.0.0.1:18080/api/v1/runtime/slots"
```

启用认证后，除健康接口外的 API 需要浏览器会话；运行态查询若返回 `401`，应从 WebUI 检查，
或仅用 worker、systemd 和 namespace 命令诊断。

## 5. 分层诊断

按以下顺序定位，避免直接删除 namespace 或防火墙规则。

### 5.1 控制面

```powershell
ssh HK-Aliyun "curl -i http://127.0.0.1:18080/api/v1/health/live"
ssh HK-Aliyun "curl -i http://127.0.0.1:18080/api/v1/health/ready"
ssh HK-Aliyun "journalctl -u gate-api -n 200 --no-pager"
```

`live=200` 但 `ready=503` 通常表示数据库、认证环境或 worker 不可用。

### 5.2 Worker 与 namespace

```powershell
ssh HK-Aliyun "systemctl status gate-worker --no-pager --full"
ssh HK-Aliyun "journalctl -u gate-worker -n 200 --no-pager"
ssh HK-Aliyun "ls -l /run/gate/worker.sock; ip netns list"
ssh HK-Aliyun "ip netns exec gate-jp-a ip -brief address; ip netns exec gate-jp-a ip route"
```

不存在的 slot 会让最后一条命令失败，这是正常状态，不应手工创建。

### 5.3 OpenVPN 与 SOCKS

```powershell
ssh HK-Aliyun "systemctl list-units --all 'gate-openvpn-jp-*' 'gate-socks-jp-*' --no-pager"
ssh HK-Aliyun "journalctl -u 'gate-openvpn-jp-*' -u 'gate-socks-jp-*' -n 200 --no-pager"
```

不要直接运行下载的 VPN Gate `.ovpn` 文件。只有 worker 生成的净化配置允许进入特权路径。

### 5.4 HAProxy

```powershell
ssh HK-Aliyun "haproxy -c -f /etc/haproxy/haproxy.cfg"
ssh HK-Aliyun "ss -lntp | grep -E ':(11081|11082|11083|11084|11085)[[:space:]]'"
ssh HK-Aliyun "printf 'show stat\n' | socat stdio /run/haproxy/gate-admin.sock"
```

若 VPS 未安装 `socat`，最后一条可省略；Gate 自身不依赖它。

### 5.5 防火墙

```powershell
ssh HK-Aliyun "systemctl status gate-firewall --no-pager --full"
ssh HK-Aliyun "nft list table ip gate_nat"
ssh HK-Aliyun "iptables -S GATE-FORWARD"
ssh HK-Aliyun "sysctl net.ipv4.ip_forward"
```

规则丢失时使用 systemd 恢复：

```powershell
ssh HK-Aliyun "systemctl restart gate-firewall gate-worker gate-api"
```

不要手工执行来源不明的 nftables 脚本。

## 6. 地区、端口与模式

地区和固定端口由 `/etc/gate/config.yaml` 管理。修改前备份并检查端口冲突：

```powershell
ssh HK-Aliyun "cp -a /etc/gate/config.yaml /etc/gate/config.yaml.bak; ss -lnt | grep 1108 || true"
```

修改后重启 API 和 worker：

```powershell
ssh HK-Aliyun "systemctl restart gate-worker gate-api; curl --fail http://127.0.0.1:18080/api/v1/health/ready"
```

HAProxy 的五个监听端口是发布配置的一部分。仅修改 YAML 中的端口不会自动重写
`/etc/haproxy/haproxy.cfg`；改变预设端口时必须同步修改 HAProxy 配置并重新发布。

日常停用、锁定和恢复自动模式应通过 WebUI 完成，这样操作会进入任务和事件记录。不要用
`systemctl stop gate-openvpn-*` 代替模式控制。

## 7. 数据库备份

v0.1 使用 `/var/lib/gate/gate.db` 作为单机 SQLite 基线。最保守的备份是短暂停止控制面与
worker 后复制数据库及配置：

```powershell
ssh HK-Aliyun "set -eu; stamp=`$(date +%Y%m%d-%H%M%S); install -d -m 0700 /var/backups/gate; systemctl stop gate-api gate-worker; trap 'systemctl start gate-worker gate-api' EXIT; tar -C / -czf /var/backups/gate/gate-`$stamp.tar.gz var/lib/gate/gate.db etc/gate/config.yaml etc/gate/secrets.env; chmod 0600 /var/backups/gate/gate-`$stamp.tar.gz"
```

备份包含会话密钥和密码散列，必须按凭据处理。将备份下载到受控位置：

```powershell
scp HK-Aliyun:/var/backups/gate/gate-YYYYMMDD-HHMMSS.tar.gz .
```

不要只复制 `gate.db` 而忽略正在写入的 WAL 文件；停服务后打包可以避免不一致快照。

## 8. 数据库恢复

1. 确认备份来自兼容的 Gate 版本。
2. 先保留当前数据库，再恢复归档。
3. 修复属主与权限，删除过期的 WAL/SHM 文件。
4. 启动服务并检查就绪状态。

```powershell
scp .\gate-YYYYMMDD-HHMMSS.tar.gz HK-Aliyun:/tmp/gate-restore.tar.gz
ssh HK-Aliyun "set -eu; systemctl stop gate-api gate-worker; cp -a /var/lib/gate/gate.db /var/lib/gate/gate.db.before-restore; tar -xzf /tmp/gate-restore.tar.gz -C /; rm -f /var/lib/gate/gate.db-wal /var/lib/gate/gate.db-shm; chown gate:gate-worker /var/lib/gate/gate.db; chmod 0640 /var/lib/gate/gate.db; systemctl start gate-worker gate-api; curl --fail http://127.0.0.1:18080/api/v1/health/ready"
```

恢复失败时停止服务，把 `gate.db.before-restore` 放回原位，再启动并检查日志。

## 9. 数据库版本策略

v0.1 是唯一基线：空数据库由 SQLAlchemy `create_all` 创建，适合首次安装；它不是长期 schema
迁移机制。v0.1 内不得在已有生产数据库上做破坏性字段变更。

第一次需要改变表结构时，发布必须同时引入 Alembic，并遵循：

1. 从当前 v0.1 metadata 生成并人工核对 baseline revision。
2. 对已有数据库执行 `stamp` 到 baseline，不重建表。
3. 后续 release 在服务切换前运行显式 `alembic upgrade head`。
4. 每次迁移前创建第 7 节备份。
5. 只自动执行可向后兼容的 expand 迁移；contract 迁移在旧 release 不再回滚后单独执行。
6. 发布失败若 migration 不可逆，不能只回滚 symlink，必须恢复配套数据库备份。

在 Alembic 落地前，release 回滚只保证代码和静态资源回滚；schema 兼容性由“不改变 v0.1 表
结构”这一约束保证。

## 10. 凭据轮换

在 VPS 交互式 shell 中执行，避免管理员密码进入 PowerShell 历史或 SSH 命令行：

```powershell
ssh -t HK-Aliyun
```

然后在 VPS 上：

```sh
set -eu
cp -a /etc/gate/secrets.env /etc/gate/secrets.env.before-rotation
printf 'New Gate administrator password: ' >&2
stty -echo
IFS= read -r gate_new_password
stty echo
printf '\n' >&2
gate_new_hash="$(printf '%s\n' "$gate_new_password" | /opt/gate/current/.venv/bin/gate-password-hash)"
unset gate_new_password
gate_new_session_secret="$(openssl rand -hex 32)"
umask 077
{
  printf 'GATE_ADMIN_PASSWORD_HASH=%s\n' "$gate_new_hash"
  printf 'GATE_SESSION_SECRET=%s\n' "$gate_new_session_secret"
} >/etc/gate/secrets.env.new
chown root:gate-worker /etc/gate/secrets.env.new
chmod 0640 /etc/gate/secrets.env.new
mv -f /etc/gate/secrets.env.new /etc/gate/secrets.env
systemctl restart gate-api
```

同时轮换 session secret 会立即注销所有浏览器会话。验证新密码后删除
`/etc/gate/secrets.env.before-rotation`；验证失败则恢复该文件并重启 `gate-api`。

## 11. 发布回滚

先列出当前和可用 release：

```powershell
ssh HK-Aliyun "readlink -f /opt/gate/current; find /opt/gate/releases -mindepth 1 -maxdepth 1 -type d -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort -r"
```

选择明确的目标目录后执行原子切换：

```powershell
ssh HK-Aliyun "set -eu; target=/opt/gate/releases/REPLACE_WITH_RELEASE_ID; test -d `$target; ln -s `$target /opt/gate/.current-manual-rollback; mv -Tf /opt/gate/.current-manual-rollback /opt/gate/current; systemctl restart gate-firewall haproxy gate-worker gate-api; curl --fail http://127.0.0.1:18080/api/v1/health/ready"
```

如果旧 release 与当前数据库 schema 不兼容，按第 8 节恢复该 release 对应的备份。不要把一个
release 目录复制覆盖另一个目录。

## 12. Fail-closed 验证

这项测试会短暂中断日本出口。先确认有控制台访问和恢复窗口。

1. 在 WebUI 将日本设为锁定，记录当前活动 slot 和出口 IP。
2. 暂停控制循环，避免测试期间自动切换。
3. 停止活动 OpenVPN unit，但保留 HAProxy 和 SOCKS。
4. 通过 `11081` 请求应超时或失败，绝不能返回 VPS 原始公网 IP。
5. 恢复 API，通过 WebUI 重新连接并验证日本出口。

VPS 上：

```sh
vps_ip="$(curl -4fsS https://api.ipify.org)"
systemctl stop gate-api
systemctl list-units --state=active --plain --no-legend 'gate-openvpn-jp-*.service'
systemctl stop gate-openvpn-jp-a.service  # 使用上一步实际活动 unit
printf 'VPS public IP: %s\n' "$vps_ip"
```

Windows 上：

```powershell
curl.exe --fail --max-time 15 --proxy socks5h://127.0.0.1:11081 https://api.ipify.org
```

期望命令失败。完成后：

```powershell
ssh HK-Aliyun "systemctl start gate-api"
```

若代理请求成功且返回 `vps_ip`，立即停止 `haproxy`，保留现场日志和 namespace 规则，并将其
视为阻断发布的问题。

## 13. 资源与保留

建议为 5 个冷备用地区预留：

| 资源 | 预算 |
| --- | ---: |
| 常驻内存 | 300-600 MiB |
| 单条 A/B 切换临时增加 | 50-100 MiB |
| 空闲 CPU | 通常低于 5% |
| release 与 Python 依赖 | 0.5-1 GiB |
| SQLite 与保留日志 | 0.5 GiB 内 |
| 自动探测流量 | 约 5-15 GiB/月 |

查看实际资源：

```powershell
ssh HK-Aliyun "systemd-cgtop -b -n 1 | grep -E 'gate|haproxy' || true; systemctl show gate-api gate-worker haproxy -p MemoryCurrent -p CPUUsageNSec; du -sh /opt/gate /var/lib/gate; journalctl --disk-usage"
```

数据库按配置每日清理历史观测、探测、已完成任务和事件。发布目录自动保留 3 个。journald 的
全机上限由 VPS 运维策略管理；根盘低于 15% 可用空间时，应先清理旧系统日志和无关文件，不能
删除当前 release、数据库或 `/etc/gate`。

## 14. VPS 迁移

1. 在旧 VPS 按第 7 节备份。
2. 在新 VPS 配置 SSH 别名并执行首次 bootstrap/deploy。
3. 停止新实例，按第 8 节恢复数据库和配置。
4. 轮换 session secret 和管理员密码。
5. 验证监听、JP/KR 出口、远端 DNS 和 fail-closed。
6. 客户端 SSH 隧道切换到新别名后，再停用旧 VPS。

不要迁移 `/run`、network namespace 或 transient unit；它们由新实例 reconcile 重建。

## 15. 卸载

卸载会移除运行状态，执行前必须备份。推荐先通过 WebUI 停用全部地区，然后：

```powershell
ssh HK-Aliyun "systemctl disable --now gate-api gate-worker gate-firewall haproxy; systemctl daemon-reload"
```

数据和凭据目录不自动删除。确认备份有效后再由管理员明确处理 `/opt/gate`、`/var/lib/gate`、
`/etc/gate` 及 Gate 安装的 systemd 文件。
