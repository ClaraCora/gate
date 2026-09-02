#!/bin/sh
set -eu

ARCHIVE="${1:-}"
RELEASE_ID="${2:-}"

if [ "$(id -u)" -ne 0 ]; then
    echo "install-release.sh must run as root" >&2
    exit 1
fi
case "$RELEASE_ID" in
    ''|*[!A-Za-z0-9._-]*)
        echo "Invalid release ID" >&2
        exit 2
        ;;
esac
if [ ! -f "$ARCHIVE" ]; then
    echo "Release archive not found: $ARCHIVE" >&2
    exit 2
fi

RELEASE_ROOT=/opt/gate/releases
TARGET="$RELEASE_ROOT/$RELEASE_ID"
CURRENT=/opt/gate/current
PREVIOUS=""
ACTIVATED=0
TARGET_CREATED=0

resolved_current="$(readlink -f "$CURRENT" 2>/dev/null || true)"
case "$resolved_current" in
    "$RELEASE_ROOT"/*)
        if [ -d "$resolved_current" ]; then
            PREVIOUS="$resolved_current"
        fi
        ;;
esac

switch_current() {
    destination="$1"
    temp_link="/opt/gate/.current-$RELEASE_ID-$$"
    rm -f -- "$temp_link"
    ln -s "$destination" "$temp_link"
    mv -Tf "$temp_link" "$CURRENT"
}

rollback() {
    if [ "$ACTIVATED" -eq 1 ]; then
        if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ]; then
            switch_current "$PREVIOUS"
            systemctl restart gate-worker.service gate-api.service >/dev/null 2>&1 || true
        else
            active_target="$(readlink -f "$CURRENT" 2>/dev/null || true)"
            if [ "$active_target" = "$TARGET" ]; then
                rm -f -- "$CURRENT"
            fi
            systemctl stop gate-worker.service gate-api.service >/dev/null 2>&1 || true
        fi
    fi
    if [ "$TARGET_CREATED" -eq 1 ]; then
        case "$TARGET" in
            "$RELEASE_ROOT"/*) rm -rf -- "$TARGET" ;;
        esac
    fi
}

on_exit() {
    status="$?"
    trap - EXIT HUP INT TERM
    if [ "$status" -ne 0 ]; then
        rollback
    fi
    exit "$status"
}

trap on_exit EXIT
trap 'exit 130' HUP INT TERM

if [ -e "$TARGET" ]; then
    active_target="$(readlink -f "$CURRENT" 2>/dev/null || true)"
    if [ "$active_target" = "$TARGET" ] && \
        curl --fail --silent http://127.0.0.1:18080/api/v1/health/ready >/dev/null; then
        rm -f -- "$ARCHIVE"
        trap - EXIT HUP INT TERM
        echo "Gate release $RELEASE_ID is already installed and ready."
        exit 0
    fi
    echo "Release directory already exists but is not the ready active release: $TARGET" >&2
    exit 2
fi
install -d -o root -g root -m 0755 "$TARGET"
TARGET_CREATED=1
tar -xzf "$ARCHIVE" -C "$TARGET"
chown -R root:root "$TARGET"

python3 -m venv "$TARGET/.venv"
app_wheels="$(find "$TARGET/dist" -maxdepth 1 -type f -name 'gate_control-*.whl' -print 2>/dev/null || true)"
if [ -n "$app_wheels" ] && [ -d "$TARGET/wheelhouse" ]; then
    if [ "$(printf '%s\n' "$app_wheels" | wc -l)" -ne 1 ]; then
        echo "Release package must contain exactly one gate-control wheel" >&2
        exit 3
    fi
    "$TARGET/.venv/bin/python" -m pip install \
        --no-index \
        --find-links "$TARGET/wheelhouse" \
        "$app_wheels"
else
    echo "Legacy source package detected; Python dependencies may be downloaded from PyPI." >&2
    "$TARGET/.venv/bin/python" -m pip install "$TARGET"
fi

if [ ! -f /etc/gate/secrets.env ]; then
    initial_password="$(openssl rand -base64 24 | tr -d '\n')"
    password_hash="$(printf '%s\n' "$initial_password" | "$TARGET/.venv/bin/gate-password-hash")"
    session_secret="$(openssl rand -hex 32)"
    secrets_temp="/etc/gate/.secrets-$RELEASE_ID-$$"
    umask 077
    {
        printf 'GATE_ADMIN_PASSWORD_HASH=%s\n' "$password_hash"
        printf 'GATE_SESSION_SECRET=%s\n' "$session_secret"
    } >"$secrets_temp"
    chown root:gate-worker "$secrets_temp"
    chmod 0640 "$secrets_temp"
    mv -f "$secrets_temp" /etc/gate/secrets.env
    printf 'Gate WebUI initial admin password: %s\n' "$initial_password"
fi

if [ ! -f /etc/gate/config.yaml ]; then
    install -o root -g gate-worker -m 0640 "$TARGET/config/gate.example.yaml" /etc/gate/config.yaml
fi

install -o root -g root -m 0755 \
    "$TARGET/deploy/firewall/gate-firewall.sh" /usr/libexec/gate/gate-firewall.sh
install -o root -g root -m 0644 "$TARGET/deploy/systemd/gate-firewall.service" \
    /etc/systemd/system/gate-firewall.service
install -o root -g root -m 0644 "$TARGET/deploy/systemd/gate-worker.service" \
    /etc/systemd/system/gate-worker.service
install -o root -g root -m 0644 "$TARGET/deploy/systemd/gate-api.service" \
    /etc/systemd/system/gate-api.service
install -o root -g root -m 0644 "$TARGET/deploy/tmpfiles/gate.conf" \
    /etc/tmpfiles.d/gate.conf
systemd-tmpfiles --create /etc/tmpfiles.d/gate.conf

public_interface="$(ip -4 route show default | awk 'NR == 1 { print $5 }')"
case "$public_interface" in
    ''|*[!A-Za-z0-9_.:-]*)
        echo "Unable to determine a safe public interface" >&2
        exit 3
        ;;
esac
printf 'GATE_PUBLIC_INTERFACE=%s\n' "$public_interface" >/etc/gate/firewall.env
chown root:gate-worker /etc/gate/firewall.env
chmod 0640 /etc/gate/firewall.env

haproxy_temp="/etc/haproxy/.gate-$RELEASE_ID-$$.cfg"
GATE_CONFIG=/etc/gate/config.yaml "$TARGET/.venv/bin/gate-render-haproxy" \
    --config /etc/gate/config.yaml --output "$haproxy_temp"
haproxy -c -f "$haproxy_temp"
if [ -f /etc/haproxy/haproxy.cfg ] && \
    ! grep -q '^# Managed by Gate' /etc/haproxy/haproxy.cfg; then
    cp -a /etc/haproxy/haproxy.cfg "/etc/haproxy/haproxy.cfg.before-gate-$RELEASE_ID"
fi
install -o root -g root -m 0644 "$haproxy_temp" /etc/haproxy/haproxy.cfg
rm -f -- "$haproxy_temp"

switch_current "$TARGET"
ACTIVATED=1

systemctl daemon-reload
systemctl enable gate-firewall.service haproxy.service gate-worker.service gate-api.service
systemctl restart gate-firewall.service
systemctl reload-or-restart haproxy.service
systemctl restart gate-worker.service
systemctl restart gate-api.service

attempt=0
until curl --fail --silent http://127.0.0.1:18080/api/v1/health/ready >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 90 ]; then
        echo "Gate API readiness check failed" >&2
        exit 4
    fi
    sleep 1
done

trap - EXIT HUP INT TERM
find "$RELEASE_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr | awk 'NR > 3 { print $2 }' \
    | while IFS= read -r old_release; do
        case "$old_release" in
            "$RELEASE_ROOT"/*) rm -rf -- "$old_release" ;;
        esac
    done
rm -f -- "$ARCHIVE"
echo "Gate release $RELEASE_ID is ready."
