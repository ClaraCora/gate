# Gate 完整部署与卸载教程

本文适用于从 Windows 开发机通过 SSH，把 Gate 部署到一台新的 Debian VPS。默认方案只让
WebUI 和 SOCKS5 监听 `127.0.0.1`，通过 SSH 本地转发使用；Cloudflare Tunnel 仅用于 WebUI，
不能代替 SOCKS5 的 SSH 转发。

## 1. 支持范围与资源

新 VPS 应满足：

- Debian 13，`x86_64` 架构；当前 bootstrap 不支持 Debian 12 或 ARM。
- 可使用 `root` SSH 登录并运行 systemd。
- 至少 1 vCPU、1 GiB 内存和 2 GiB 可用磁盘；建议 2 vCPU、2 GiB 内存。
- 能访问 GitHub、PyPI、npm、VPN Gate 和 sing-box 发布地址。
- 云防火墙只需放行 SSH。不要公开 `11081-11109` 或 `18080`。

Gate 会安装 OpenVPN、HAProxy、nftables、iptables、Python、sing-box 等依赖。五个活动入口通常
占用 300-600 MiB 内存；每次 A/B 切换会临时增加约 50-100 MiB。

## 2. 准备 Windows 开发机

安装 Git、Node.js 20.19 或更新版本、Python 3.13 和 OpenSSH Client。克隆项目：

```powershell
git clone https://github.com/ClaraCora/gate.git
Set-Location .\gate
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm --prefix frontend ci
.\scripts\test.ps1
```

在 `C:\Users\<用户名>\.ssh\config` 中为新 VPS 添加别名：

```sshconfig
Host Gate-VPS
    HostName 203.0.113.10
    User root
    Port 22
    IdentityFile C:/Users/<用户名>/.ssh/id_ed25519
```

将 `HostName`、端口和密钥路径替换为实际值，然后检查系统：

```powershell
ssh Gate-VPS "id -u; . /etc/os-release; echo `$PRETTY_NAME; uname -m"
```

第一行必须是 `0`，架构必须是 `x86_64`。部署脚本不会把私钥、管理员密码或会话密钥写入仓库。

## 3. 首次部署

在项目根目录执行：

```powershell
.\scripts\deploy.ps1 -HostAlias Gate-VPS -Bootstrap
```

`-Bootstrap` 会安装系统依赖并创建 `gate` 服务用户；发布阶段会构建前端、创建 Python 虚拟环境、
安装 systemd 服务、生成 HAProxy 配置并启动 Gate。首次成功时终端只显示一次：

```text
Gate WebUI initial admin password: <随机密码>
```

立即把随机密码保存到密码管理器。VPS 上只保存 Argon2 散列，不保存这段明文。

检查服务和回环监听：

```powershell
ssh Gate-VPS "systemctl is-active gate-firewall haproxy gate-worker gate-api"
ssh Gate-VPS "curl --fail --silent http://127.0.0.1:18080/api/v1/health/ready"
ssh Gate-VPS "ss -lntp | grep -E ':(18080|11081|11082|11083|11084|11085)[[:space:]]'"
```

就绪接口应返回 `{"status":"ok"}`。所有 Gate 端口都应监听 `127.0.0.1`；发现 `0.0.0.0`
时应停止服务并检查 `/etc/gate/config.yaml` 和 HAProxy 配置。

## 4. 通过 SSH 使用 WebUI 与 SOCKS5

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
  Gate-VPS
```

浏览器打开 `http://127.0.0.1:18080`。代理客户端使用
`socks5h://127.0.0.1:<端口>`，其中 `socks5h` 会让 DNS 在远端解析。

```powershell
curl.exe --fail --max-time 20 `
  --proxy socks5h://127.0.0.1:11081 `
  https://www.cloudflare.com/cdn-cgi/trace
```

首次登录后，可点击右上角钥匙图标修改管理密码。修改会写入 Gate 数据库并轮换会话密钥，其他
浏览器中的旧会话会立即失效。

## 5. 可选：通过 Cloudflare Tunnel 访问 WebUI

Cloudflare Tunnel 只转发 WebUI HTTP 流量。SOCKS5 端口仍应使用 SSH 转发，不能配置为普通
Cloudflare HTTP Public Hostname。

先把公网来源加入 `/etc/gate/config.yaml`：

```yaml
security:
  enabled: true
  session_hours: 12
  cookie_secure: true
  allowed_origins:
    - https://gate.example.com
    - http://127.0.0.1:18080
```

重启 API：

```powershell
ssh Gate-VPS "systemctl restart gate-api && curl --fail http://127.0.0.1:18080/api/v1/health/ready"
```

Cloudflare Tunnel 的 ingress 应指向 HTTP 回环地址：

```yaml
ingress:
  - hostname: gate.example.com
    service: http://127.0.0.1:18080
  - service: http_status:404
```

建议同时启用 Cloudflare Access，只允许自己的账号访问。常见故障：

- `502`：先在 VPS 执行 `curl http://127.0.0.1:18080/api/v1/health/ready`；若本地正常，检查
  cloudflared 是否与 Gate 位于同一台机器，以及 service 是否误写成 HTTPS。
- `Request origin is not allowed`：把浏览器地址栏中的完整 origin 加入 `allowed_origins`，只写
  协议和域名，不包含路径，也不要保留末尾斜杠，然后重启 `gate-api`。
- 登录后仍回到登录页：公网 HTTPS 场景应使用 `cookie_secure: true`，并确认浏览器没有拦截 Cookie。

## 6. 地区入口配置

生产配置位于 `/etc/gate/config.yaml`。每个入口需要唯一的 `id`、`socks_port` 和
`network_index`；同一地区的多个入口使用相同 `group_id` 和 `countries`。新增入口建议先设置：

```yaml
enabled: false
```

修改前备份配置：

```powershell
ssh Gate-VPS "cp -a /etc/gate/config.yaml /etc/gate/config.yaml.before-edit"
```

配置更新后重新生成并校验 HAProxy，再重启服务：

```powershell
ssh Gate-VPS "set -eu; temp=/etc/haproxy/.gate-manual.cfg; /opt/gate/current/.venv/bin/gate-render-haproxy --config /etc/gate/config.yaml --output `$temp; haproxy -c -f `$temp; install -o root -g root -m 0644 `$temp /etc/haproxy/haproxy.cfg; rm -f `$temp; systemctl reload-or-restart haproxy; systemctl restart gate-worker gate-api"
```

## 7. 更新与回滚

拉取代码并发布新版本：

```powershell
git pull --ff-only
.\scripts\deploy.ps1 -HostAlias Gate-VPS
```

脚本会先运行检查，原子切换 `/opt/gate/current`，失败时恢复上一 release。查看可回滚版本：

```powershell
ssh Gate-VPS "readlink -f /opt/gate/current; find /opt/gate/releases -mindepth 1 -maxdepth 1 -type d -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort -r"
```

需要手动回滚时，将下列 `target` 替换为上一步显示的明确目录：

```powershell
ssh Gate-VPS "set -eu; target=/opt/gate/releases/RELEASE_ID; test -d `$target; ln -s `$target /opt/gate/.rollback; mv -Tf /opt/gate/.rollback /opt/gate/current; systemctl restart gate-firewall haproxy gate-worker gate-api; curl --fail http://127.0.0.1:18080/api/v1/health/ready"
```

## 8. 备份与恢复

备份包含数据库、配置、管理员密码散列和会话密钥，应按凭据保管：

```powershell
ssh Gate-VPS "set -eu; stamp=`$(date +%Y%m%d-%H%M%S); install -d -m 0700 /var/backups/gate; systemctl stop gate-api gate-worker; trap 'systemctl start gate-worker gate-api' EXIT; tar -C / -czf /var/backups/gate/gate-`$stamp.tar.gz var/lib/gate etc/gate; chmod 0600 /var/backups/gate/gate-`$stamp.tar.gz"
```

下载备份：

```powershell
scp Gate-VPS:/var/backups/gate/gate-YYYYMMDD-HHMMSS.tar.gz .
```

恢复前停止服务，并先保留当前数据：

```powershell
scp .\gate-YYYYMMDD-HHMMSS.tar.gz Gate-VPS:/tmp/gate-restore.tar.gz
ssh Gate-VPS "set -eu; systemctl stop gate-api gate-worker; cp -a /var/lib/gate /var/lib/gate.before-restore; cp -a /etc/gate /etc/gate.before-restore; tar -xzf /tmp/gate-restore.tar.gz -C /; chown -R gate:gate-worker /var/lib/gate; systemctl start gate-worker gate-api; curl --fail http://127.0.0.1:18080/api/v1/health/ready"
```

## 9. 忘记管理密码

正常情况下应从 WebUI 修改。无法登录时，通过交互式 SSH 终端重置，密码不会进入命令历史：

```powershell
ssh -t Gate-VPS
```

在 VPS 中执行：

```sh
systemctl stop gate-api
runuser -u gate -- sh -c 'cd /var/lib/gate && GATE_CONFIG=/etc/gate/config.yaml /opt/gate/current/.venv/bin/gate-password-reset'
systemctl start gate-api
curl --fail http://127.0.0.1:18080/api/v1/health/ready
```

输入两次新密码。命令会同时轮换会话密钥，因此所有旧会话都会失效。

## 10. 卸载

卸载脚本默认停止服务、清理 namespace 和 Gate 防火墙规则、恢复安装前的 HAProxy 配置、删除
程序及 systemd 文件，但保留 `/etc/gate` 和 `/var/lib/gate`。脚本始终先备份到
`/var/backups/gate-uninstall-<时间>`。

保留配置和数据库卸载：

```powershell
ssh Gate-VPS "sh /opt/gate/current/deploy/uninstall.sh --yes"
```

同时删除配置、数据库、凭据和服务账号：

```powershell
ssh Gate-VPS "sh /opt/gate/current/deploy/uninstall.sh --yes --purge-data"
```

确认卸载结果：

```powershell
ssh Gate-VPS "systemctl status gate-api gate-worker gate-firewall --no-pager || true; ip netns list; nft list table ip gate_nat 2>&1 || true; test ! -e /opt/gate && echo Gate-program-removed"
```

卸载脚本不会删除 OpenVPN、HAProxy、nftables、Python 或 sing-box 软件包，因为它们可能被 VPS
上的其他服务使用。确定这台 VPS 没有其他用途时，再由管理员单独审查并卸载系统依赖。

如果采用保留数据模式，重新运行首次部署命令即可复用原配置、数据库和 WebUI 密码：

```powershell
.\scripts\deploy.ps1 -HostAlias Gate-VPS -Bootstrap
```

## 11. 最小验收清单

部署或迁移后至少确认：

1. 四个服务为 active，就绪接口返回 `ok`。
2. WebUI 能登录、修改密码，并能看到出口、任务、事件三个导航入口。
3. 点击右侧出口能打开候选弹窗，IP 搜索和各排序方式正常。
4. 已启用入口的 SOCKS 请求返回目标国家，且不等于 VPS 原始公网 IP。
5. 停止活动 OpenVPN 后，SOCKS 请求失败而不是回落到 VPS 公网出口。
6. WebUI 和 SOCKS 仅监听回环地址，Cloudflare Access 或 SSH 负责访问控制。

更细的日志、网络 namespace、HAProxy、防火墙和 fail-closed 诊断见
[运维手册](OPERATIONS.md)。
