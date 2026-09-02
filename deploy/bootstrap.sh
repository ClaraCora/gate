#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "bootstrap.sh must run as root" >&2
    exit 1
fi
if [ ! -r /etc/os-release ]; then
    echo "Unable to identify the operating system" >&2
    exit 2
fi
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    debian:13*) ;;
    *) echo "Gate bootstrap currently requires Debian 13" >&2; exit 2 ;;
esac
if [ "$(dpkg --print-architecture)" != "amd64" ]; then
    echo "Gate bootstrap currently requires the amd64 architecture" >&2
    exit 2
fi

SING_BOX_VERSION=1.14.0
SING_BOX_SHA256=84035ea7eb85570830af77801e8e949d3769dd23bbcacce08df3bfde1945f299
SING_BOX_DEB="sing-box_${SING_BOX_VERSION}_linux_amd64.deb"
SING_BOX_URL="https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/${SING_BOX_DEB}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl haproxy iproute2 iptables nftables openvpn openssl \
    python3 python3-pip python3-venv tar

if ! command -v sing-box >/dev/null 2>&1 || \
    ! sing-box version 2>/dev/null | grep -q "${SING_BOX_VERSION}"; then
    temp_dir="$(mktemp -d /tmp/gate-bootstrap.XXXXXX)"
    trap 'rm -rf "$temp_dir"' EXIT INT TERM
    curl --fail --location --retry 3 --output "$temp_dir/$SING_BOX_DEB" "$SING_BOX_URL"
    printf '%s  %s\n' "$SING_BOX_SHA256" "$temp_dir/$SING_BOX_DEB" | sha256sum --check --status
    apt-get install -y "$temp_dir/$SING_BOX_DEB"
    rm -rf "$temp_dir"
    trap - EXIT INT TERM
fi

systemctl disable --now sing-box.service >/dev/null 2>&1 || true

getent group gate-worker >/dev/null || groupadd --system gate-worker
if ! id gate >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/gate --shell /usr/sbin/nologin \
        --gid gate-worker gate
fi

install -d -o root -g root -m 0755 /opt/gate /opt/gate/releases
install -d -o root -g gate-worker -m 0750 /etc/gate
install -d -o root -g gate-worker -m 0750 /etc/netns
install -d -o root -g root -m 0755 /run/netns
install -d -o gate -g gate-worker -m 0750 /var/lib/gate
install -d -o root -g gate-worker -m 0750 /var/lib/gate/slots
install -d -o root -g root -m 0755 /usr/libexec/gate

cat >/etc/sysctl.d/90-gate.conf <<'EOF'
net.ipv4.ip_forward = 1
EOF
sysctl --system >/dev/null

echo "Gate host prerequisites are installed. Run install-release.sh next."
