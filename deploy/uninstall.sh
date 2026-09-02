#!/bin/sh
set -eu

purge_data=0
confirmed=0

usage() {
    cat <<'EOF'
Usage: uninstall.sh --yes [--purge-data]

  --yes         Confirm removal of Gate services and program files.
  --purge-data  Also remove /etc/gate and /var/lib/gate after creating a backup.
EOF
}

for argument in "$@"; do
    case "$argument" in
        --yes) confirmed=1 ;;
        --purge-data) purge_data=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $argument" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "uninstall.sh must run as root" >&2
    exit 1
fi
if [ "$confirmed" -ne 1 ]; then
    echo "Refusing to uninstall without --yes" >&2
    usage >&2
    exit 2
fi

stamp="$(date +%Y%m%d-%H%M%S)"
backup_root="/var/backups/gate-uninstall-$stamp"
install -d -o root -g root -m 0700 "$backup_root"

systemctl stop gate-api.service gate-worker.service 2>/dev/null || true

for unit in $(
    systemctl list-units --all --plain --no-legend \
        'gate-openvpn-*.service' 'gate-socks-*.service' 2>/dev/null \
        | awk '{print $1}'
); do
    case "$unit" in
        gate-openvpn-*.service|gate-socks-*.service) systemctl stop "$unit" || true ;;
    esac
done

systemctl disable --now gate-api.service gate-worker.service gate-firewall.service \
    >/dev/null 2>&1 || true

if [ -d /etc/gate ]; then
    cp -a /etc/gate "$backup_root/etc-gate"
fi
if [ -d /var/lib/gate ]; then
    cp -a /var/lib/gate "$backup_root/var-lib-gate"
fi
if [ -f /etc/haproxy/haproxy.cfg ] && \
    grep -q '^# Managed by Gate' /etc/haproxy/haproxy.cfg; then
    cp -a /etc/haproxy/haproxy.cfg "$backup_root/haproxy.cfg.gate"
    previous_haproxy="$(
        find /etc/haproxy -maxdepth 1 -type f -name 'haproxy.cfg.before-gate-*' \
            -printf '%T@ %p\n' 2>/dev/null | sort -nr | awk 'NR == 1 {$1=""; sub(/^ /, ""); print}'
    )"
    if [ -n "$previous_haproxy" ] && [ -f "$previous_haproxy" ]; then
        if haproxy -c -f "$previous_haproxy"; then
            cp -a "$previous_haproxy" /etc/haproxy/haproxy.cfg
            systemctl enable haproxy.service >/dev/null 2>&1 || true
            systemctl restart haproxy.service
        else
            systemctl disable --now haproxy.service >/dev/null 2>&1 || true
            rm -f /etc/haproxy/haproxy.cfg
        fi
    else
        systemctl disable --now haproxy.service >/dev/null 2>&1 || true
        rm -f /etc/haproxy/haproxy.cfg
    fi
fi

ip netns list 2>/dev/null | awk '{print $1}' | while IFS= read -r namespace; do
    case "$namespace" in
        gate-*-a|gate-*-b) ip netns delete "$namespace" || true ;;
    esac
done

find /etc/netns -mindepth 1 -maxdepth 1 -type d -name 'gate-*-?' \
    -exec rm -rf -- {} + 2>/dev/null || true

rm -f /etc/systemd/system/gate-api.service
rm -f /etc/systemd/system/gate-worker.service
rm -f /etc/systemd/system/gate-firewall.service
rm -f /etc/tmpfiles.d/gate.conf
rm -f /etc/sysctl.d/90-gate.conf
rm -f /usr/libexec/gate/gate-firewall.sh
rm -f /run/gate/worker.sock
rm -rf /opt/gate

if [ "$purge_data" -eq 1 ]; then
    rm -rf /etc/gate
    rm -rf /var/lib/gate
    if id gate >/dev/null 2>&1; then
        userdel gate || true
    fi
    if getent group gate-worker >/dev/null 2>&1; then
        groupdel gate-worker || true
    fi
fi

systemctl daemon-reload
sysctl --system >/dev/null 2>&1 || true

echo "Gate has been uninstalled. Backup: $backup_root"
if [ "$purge_data" -eq 0 ]; then
    echo "Configuration and database were kept in /etc/gate and /var/lib/gate."
fi
