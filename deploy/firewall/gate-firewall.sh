#!/bin/sh
set -eu

ACTION="${1:-start}"
PUBLIC_INTERFACE="${GATE_PUBLIC_INTERFACE:-$(ip -4 route show default | awk 'NR == 1 { print $5 }')}"

case "$PUBLIC_INTERFACE" in
    ''|*[!A-Za-z0-9_.:-]*)
        echo "Invalid public interface: $PUBLIC_INTERFACE" >&2
        exit 2
        ;;
esac

choose_hook_chain() {
    if iptables -n -L DOCKER-USER >/dev/null 2>&1; then
        echo DOCKER-USER
    else
        echo FORWARD
    fi
}

start_firewall() {
    nft list table ip gate_nat >/dev/null 2>&1 && nft delete table ip gate_nat
    nft add table ip gate_nat
    nft 'add chain ip gate_nat postrouting { type nat hook postrouting priority srcnat; policy accept; }'
    nft add rule ip gate_nat postrouting ip saddr 10.253.0.0/16 oifname "$PUBLIC_INTERFACE" masquerade

    iptables -N GATE-FORWARD 2>/dev/null || true
    iptables -F GATE-FORWARD
    iptables -A GATE-FORWARD -s 10.253.0.0/16 -o "$PUBLIC_INTERFACE" -j ACCEPT
    iptables -A GATE-FORWARD -d 10.253.0.0/16 -i "$PUBLIC_INTERFACE" \
        -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A GATE-FORWARD -j RETURN

    hook_chain="$(choose_hook_chain)"
    if ! iptables -C "$hook_chain" -j GATE-FORWARD >/dev/null 2>&1; then
        iptables -I "$hook_chain" 1 -j GATE-FORWARD
    fi
}

stop_firewall() {
    for hook_chain in DOCKER-USER FORWARD; do
        if iptables -n -L "$hook_chain" >/dev/null 2>&1; then
            while iptables -C "$hook_chain" -j GATE-FORWARD >/dev/null 2>&1; do
                iptables -D "$hook_chain" -j GATE-FORWARD
            done
        fi
    done
    iptables -F GATE-FORWARD >/dev/null 2>&1 || true
    iptables -X GATE-FORWARD >/dev/null 2>&1 || true
    nft delete table ip gate_nat >/dev/null 2>&1 || true
}

case "$ACTION" in
    start|restart)
        stop_firewall
        start_firewall
        ;;
    stop)
        stop_firewall
        ;;
    *)
        echo "Usage: $0 {start|stop|restart}" >&2
        exit 2
        ;;
esac
