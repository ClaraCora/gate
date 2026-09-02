# Gate 完整部署与卸载教程

本文适用于把 GitHub 已编译的 Gate Release 直接安装到 Debian VPS。构建、测试、前端打包和
Python 依赖下载均由 GitHub Actions 完成；VPS 不需要安装 Git、Node.js、npm、编译器，也不需要
访问 PyPI。默认方案只让 WebUI 和 SOCKS5 监听 `127.0.0.1`，通过 SSH 本地转发使用；SOCKS
可在 WebUI 改为监听全部网卡，但必须同时启用统一认证和来源 IP 防火墙；
Cloudflare Tunnel 仅用于 WebUI，不能代替 SOCKS5 的 SSH 转发。

## 1. 支持范围与资源

新 VPS 应满足：

- Debian 13，`x86_64` 架构；当前 bootstrap 不支持 Debian 12 或 ARM。
- 可使用 `root` SSH 登录并运行 systemd。
- 至少 1 vCPU、1 GiB 内存和 2 GiB 可用磁盘；建议 2 vCPU、2 GiB 内存。
- 能访问 Debian 软件源、GitHub 和 VPN Gate。
- 云防火墙只需放行 SSH。不要公开 `11081-11109` 或 `18080`。

Gate 安装器会自动安装 OpenVPN、HAProxy、nftables、iptables、Python 3.13、sing-box 等运行时
依赖。五个活动入口通常占用 300-600 MiB 内存；每次 A/B 切换会临时增加约 50-100 MiB。

## 2. VPS 前置检查

通过 SSH 登录 VPS：

```powershell
ssh Gate-VPS
```

在 VPS 中检查系统；以下命令都以 root 执行：

```sh
id -u
. /etc/os-release
printf '%s\n' "$PRETTY_NAME"
uname -m
command -v curl
```

第一行必须是 `0`，系统必须是 Debian 13，架构必须是 `x86_64`。若最小系统没有 `curl`，先执行
`apt-get update && apt-get install -y ca-certificates curl`；这是下载安装包所需的唯一前置工具。

## 3. 首次部署

在 VPS 中直接下载 GitHub Release 自带的安装器并执行：

```sh
curl -fL https://github.com/ClaraCora/gate/releases/latest/download/gate-install.sh \
  -o /tmp/gate-install.sh
sh /tmp/gate-install.sh
```

安装器依次完成以下工作：下载 `gate-linux-amd64.tar.gz` 及校验文件、验证 SHA-256、安装系统运行
依赖、从包内 wheelhouse 离线创建 Python 虚拟环境、安装 systemd 服务、生成 HAProxy 配置并启动
Gate。首次成功时终端只显示一次：

```text
Gate WebUI initial admin password: <随机密码>
```

立即把随机密码保存到密码管理器。VPS 上只保存 Argon2 散列，不保存这段明文。

仍在 VPS 中检查服务和回环监听：

```sh
systemctl is-active gate-firewall haproxy gate-worker gate-api
curl --fail --silent http://127.0.0.1:18080/api/v1/health/ready
ss -lntp | grep -E ':(18080|11081|11082|11083|11084|11085)[[:space:]]'
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

SOCKS 认证默认关闭。点击右上角盾牌用户图标，可启用所有 SOCKS 入口共用的用户名和密码，
也可以把监听范围从“仅本机”切换为“全部网卡”。
首次启用必须设置密码；之后密码留空表示保留当前密码。保存会重载活动 sing-box 服务，现有
代理连接可能中断。启用后用以下命令测试，curl 会交互提示代理密码，密码不会写入命令历史：

```powershell
curl.exe --fail --max-time 20 `
  --proxy socks5h://127.0.0.1:11081 `
  --proxy-user gate_user `
  https://www.cloudflare.com/cdn-cgi/trace
```

用户名限制为 3-32 位 ASCII 字母、数字、点、下划线或连字符；密码限制为 8-128 位可见
ASCII 字符。Gate 不向 API 或 WebUI 回显密码。由于 sing-box 运行和内部探测需要可恢复凭据，
密码由 root worker 保存到 `/etc/gate/socks-auth.json`，权限为 `0640 root:gate-worker`，不得提交
到 Git 或发送到日志。

选择“全部网卡”后，SOCKS 端口监听 `0.0.0.0`，WebUI 会强制开启认证，后端和特权 worker
也会再次校验。还必须在云安全组或 VPS 防火墙中仅允许可信来源 IP，不能把密码当作唯一的公网
防护。关闭认证前必须先切回“仅本机”。WebUI 本身仍只监听 `127.0.0.1:18080`。

页面顶部“自动检查”关闭后，定时发现、健康检查和自动优化会暂停，手动测试与切换仍可用。
锁定单个入口只禁止自动换节点，并不会停止该入口的健康检查。

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

## 7. 更新、固定版本与回滚

更新到 GitHub 上的最新稳定 Release：

```sh
curl -fL https://github.com/ClaraCora/gate/releases/latest/download/gate-install.sh \
  -o /tmp/gate-install.sh
sh /tmp/gate-install.sh
```

安装指定版本时，安装器和目标资产使用同一个 tag：

```sh
curl -fL https://github.com/ClaraCora/gate/releases/download/v0.1.6/gate-install.sh \
  -o /tmp/gate-install.sh
sh /tmp/gate-install.sh --version v0.1.6
```

重复安装当前版本是幂等操作；新版本会原子切换 `/opt/gate/current`，就绪检查失败时自动恢复上一
版本。安装期间保留现有 `/etc/gate`、数据库和管理密码。查看可回滚版本：

```powershell
ssh Gate-VPS "readlink -f /opt/gate/current; find /opt/gate/releases -mindepth 1 -maxdepth 1 -type d -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort -r"
```

需要手动回滚时，将下列 `target` 替换为上一步显示的明确目录：

```powershell
ssh Gate-VPS "set -eu; target=/opt/gate/releases/RELEASE_ID; test -d `$target; ln -s `$target /opt/gate/.rollback; mv -Tf /opt/gate/.rollback /opt/gate/current; systemctl restart gate-firewall haproxy gate-worker gate-api; curl --fail http://127.0.0.1:18080/api/v1/health/ready"
```

## 8. 备份与恢复

备份包含数据库、配置、管理员密码散列、会话密钥和 SOCKS 明文凭据，应按高敏感凭据保管：

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

如果采用保留数据模式，重新运行 GitHub 安装器即可复用原配置、数据库和 WebUI 密码：

```sh
curl -fL https://github.com/ClaraCora/gate/releases/latest/download/gate-install.sh \
  -o /tmp/gate-install.sh
sh /tmp/gate-install.sh
```

## 11. GitHub 发布机制

仓库创建 `v*` tag 后，`.github/workflows/release.yml` 会先执行后端检查、前端测试和生产构建，
再生成仅面向 Debian 13 amd64 / Python 3.13 的自包含包。Release 固定发布三个资产：

- `gate-linux-amd64.tar.gz`：应用 wheel、全部运行依赖 wheel、WebUI、配置模板和部署脚本。
- `gate-linux-amd64.tar.gz.sha256`：安装前强制验证的包校验值。
- `gate-install.sh`：VPS 一键安装器。

维护者发布新版本时，应先同步 `pyproject.toml`、`gate.__version__` 和前端版本，再推送同名 tag。
例如版本为 `0.1.6` 时，tag 必须为 `v0.1.6`；版本不一致会使工作流立即失败。

`scripts/deploy.ps1` 仍可用于开发或 GitHub Releases 故障时的应急发布，但它需要 Windows 本机
具备开发环境，并可能在 VPS 从 PyPI 安装依赖，不是常规生产路径。

## 12. 最小验收清单

部署或迁移后至少确认：

1. 四个服务为 active，就绪接口返回 `ok`。
2. WebUI 能登录、修改管理密码和 SOCKS 接入设置，并能看到出口、任务、事件三个导航入口。
3. 点击右侧出口能打开候选弹窗，IP 搜索和各排序方式正常。
4. 地区入口表逐行显示当前出口 IP；全局“自动检查”开关关闭后刷新页面仍保持关闭。
5. 已启用入口的 SOCKS 请求携带当前凭据后返回目标国家，且不等于 VPS 原始公网 IP。
6. 停止活动 OpenVPN 后，SOCKS 请求失败而不是回落到 VPS 公网出口。
7. WebUI 始终只监听回环地址；SOCKS 若监听全部网卡，认证已开启且防火墙只放行可信来源。

更细的日志、网络 namespace、HAProxy、防火墙和 fail-closed 诊断见
[运维手册](OPERATIONS.md)。
