#!/usr/bin/env bash
# One-shot Z.OS guest provisioning. Headless Ubuntu cloud image.
#
# ISOLATION IS LOAD-BEARING: no virtfs/9p/virtiofs, no shared host directory, and
# user-mode networking with only an SSH forward. Every vm_* tool is Safe in the
# permission gate BECAUSE the guest cannot touch the host. Do not add a mount.
set -euo pipefail

VMDIR="$(cd "$(dirname "$0")" && pwd)"
IMG="$VMDIR/zos-guest.qcow2"
SEED="$VMDIR/seed.iso"
KEYDIR="$HOME/.local/share/zos"
KEY="$KEYDIR/vm-key"
BASE_URL="https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img"
BASE="$VMDIR/base.img"

mkdir -p "$KEYDIR"
[ -f "$KEY" ] || ssh-keygen -t ed25519 -N '' -f "$KEY" -C zos-vm

if [ ! -f "$BASE" ]; then
  echo "fetching Ubuntu cloud image (~700MB)..."
  curl -fL --progress-bar -o "$BASE.part" "$BASE_URL"
  mv "$BASE.part" "$BASE"          # never leave a truncated base image behind
fi

if [ ! -f "$IMG" ]; then
  qemu-img create -f qcow2 -F qcow2 -b "$BASE" "$IMG" 8G
fi

# cloud-init: one user, key-only SSH, no password login, qemu-guest-agent.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/user-data" <<EOF
#cloud-config
hostname: zos-guest
users:
  - name: zos
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat "$KEY.pub")
ssh_pwauth: false
package_update: true
packages: [qemu-guest-agent, python3]
runcmd:
  - systemctl enable --now qemu-guest-agent
EOF
echo 'instance-id: zos-1' > "$WORK/meta-data"

# xorriso, not cloud-localds: cloud-localds is a wrapper around mkisofs and is not
# installed here, while xorriso is. cloud-init's NoCloud datasource only requires a
# filesystem labelled cidata holding user-data and meta-data at its root, so this is
# the same artefact without adding a system package.
xorriso -as mkisofs -quiet -output "$SEED" -volid cidata -joliet -rock \
        "$WORK/user-data" "$WORK/meta-data"

echo "provisioning files ready: $IMG, $SEED"
echo "key: $KEY  (guest user 'zos', NOPASSWD sudo INSIDE THE GUEST ONLY)"
