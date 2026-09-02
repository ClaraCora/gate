#!/bin/sh
set -eu

REPOSITORY="ClaraCora/gate"
VERSION="latest"
ASSET_NAME="gate-linux-amd64.tar.gz"

usage() {
    cat <<'EOF'
Usage: install-from-github.sh [--version VERSION]

Download a verified Gate GitHub Release, install host runtime dependencies,
and activate the release. VERSION defaults to latest and may be a tag such as
v0.1.1.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            [ "$#" -ge 2 ] || { echo "--version requires a value" >&2; exit 2; }
            VERSION="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "install-from-github.sh must run as root" >&2
    exit 1
fi
for command in curl grep sha256sum tar; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is missing: $command" >&2
        exit 2
    fi
done
if [ "$VERSION" != "latest" ] && \
    ! printf '%s\n' "$VERSION" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "Invalid version: $VERSION" >&2
    exit 2
fi

if [ "$VERSION" = "latest" ]; then
    download_root="https://github.com/$REPOSITORY/releases/latest/download"
else
    download_root="https://github.com/$REPOSITORY/releases/download/$VERSION"
fi

temp_dir="$(mktemp -d /tmp/gate-github-install.XXXXXX)"
cleanup() {
    rm -rf -- "$temp_dir"
}
trap cleanup EXIT HUP INT TERM

archive="$temp_dir/$ASSET_NAME"
checksum="$temp_dir/$ASSET_NAME.sha256"
curl --fail --location --retry 3 --retry-all-errors \
    --output "$archive" "$download_root/$ASSET_NAME"
curl --fail --location --retry 3 --retry-all-errors \
    --output "$checksum" "$download_root/$ASSET_NAME.sha256"
if ! (
    cd "$temp_dir"
    sha256sum --check --status "$ASSET_NAME.sha256"
); then
    echo "Gate release archive checksum verification failed" >&2
    exit 3
fi

bundle_root="$temp_dir/bundle"
mkdir "$bundle_root"
tar -xzf "$archive" -C "$bundle_root"

if [ ! -f "$bundle_root/deploy/bootstrap.sh" ] || \
    [ ! -f "$bundle_root/deploy/install-release.sh" ] || \
    [ ! -f "$bundle_root/RELEASE_VERSION" ] || \
    [ ! -f "$bundle_root/SHA256SUMS" ]; then
    echo "Downloaded asset is not a valid Gate release package" >&2
    exit 3
fi
if ! (
    cd "$bundle_root"
    sha256sum --check --status SHA256SUMS
); then
    echo "Gate release contents checksum verification failed" >&2
    exit 3
fi

release_id="$(sed -n '1p' "$bundle_root/RELEASE_VERSION")"
case "$release_id" in
    ''|*[!A-Za-z0-9._-]*) echo "Invalid release ID in package" >&2; exit 3 ;;
esac
if [ "$VERSION" != "latest" ] && [ "$release_id" != "$VERSION" ]; then
    echo "Release package version mismatch: expected $VERSION, got $release_id" >&2
    exit 3
fi

sh "$bundle_root/deploy/bootstrap.sh"
sh "$bundle_root/deploy/install-release.sh" "$archive" "$release_id"
