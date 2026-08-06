#!/bin/busybox sh
# shellcheck shell=dash
# PID 1 inside the "appliance" guest for the WAN-chaos download proof.
#
# Differs from guest_init.sh (the ADR-019 packet-routing proof) in two ways:
#  - LAN is flat (ADR-020's "single" mode, the current production default) —
#    eth0 itself carries the gateway address, no VLAN subinterfaces — so a
#    real TCP/IP client in the host's lanclient netns can do a genuine HTTP
#    download without hand-crafted 802.1Q frames.
#  - runs chaos_driver.py, which loops the real control-loop steps for the
#    whole test duration instead of a one-shot render()/reconcile().
set -eu 2>/dev/null || true

/bin/busybox --install -s /bin
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || mount -t tmpfs tmpfs /dev
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts 2>/dev/null || true
ln -sf /proc/self/fd/0 /dev/stdin
ln -sf /proc/self/fd/1 /dev/stdout
ln -sf /proc/self/fd/2 /dev/stderr

echo "wifucked-chaos-test: mounted proc/sys/dev"

KVER="6.6.110-0-lts"
MODDIR="/lib/modules/${KVER}"

for m in \
    drivers/virtio/virtio_ring.ko \
    drivers/virtio/virtio.ko \
    drivers/virtio/virtio_pci_legacy_dev.ko \
    drivers/virtio/virtio_pci_modern_dev.ko \
    drivers/virtio/virtio_pci.ko \
    net/core/failover.ko \
    drivers/net/net_failover.ko \
    drivers/net/virtio_net.ko \
    net/802/mrp.ko \
    net/8021q/8021q.ko \
    lib/crypto/libcurve25519-generic.ko \
    arch/x86/crypto/curve25519-x86_64.ko \
    lib/crypto/libchacha.ko \
    arch/x86/crypto/chacha-x86_64.ko \
    arch/x86/crypto/poly1305-x86_64.ko \
    lib/crypto/libchacha20poly1305.ko \
    net/ipv6/ip6_udp_tunnel.ko \
    net/ipv4/udp_tunnel.ko \
    net/ipv6/ipv6.ko \
    drivers/net/wireguard/wireguard.ko \
    net/sched/sch_cake.ko \
    crypto/crc32c_generic.ko \
    net/netfilter/nfnetlink.ko \
    lib/libcrc32c.ko \
    net/netfilter/nf_tables.ko \
    net/ipv6/netfilter/nf_defrag_ipv6.ko \
    net/ipv4/netfilter/nf_defrag_ipv4.ko \
    net/netfilter/nf_conntrack.ko \
    net/netfilter/nf_nat.ko \
    net/netfilter/nft_masq.ko \
    net/netfilter/nft_chain_nat.ko \
    ; do
    insmod "${MODDIR}/kernel/${m}" 2>>/wifucked-modprobe.log
done

if ! grep -q wireguard /proc/modules; then
    echo "wifucked-chaos-test: FATAL wireguard.ko did not load, see /wifucked-modprobe.log"
    cat /wifucked-modprobe.log
fi
if ! grep -q sch_cake /proc/modules; then
    echo "wifucked-chaos-test: WARNING sch_cake.ko did not load (CAKE shaping argv untested)"
fi

ip link set lo up

# Flat LAN — ADR-020 "single" mode, matching the guest's driver, which
# constructs LinuxEnforcer(lan_mode="single", base_interface="eth0").
ip link set eth0 up
ip addr add 192.168.60.1/24 dev eth0

# Two WAN atomics, shared L2 with the fabric guest — same topology shape as
# guest_init.sh's, taps are shaped by the host's chaos_netem.sh.
ip link set eth1 up
ip link set eth2 up
ip addr add 203.0.113.11/24 dev eth1
ip addr add 203.0.113.12/24 dev eth2
# LinuxProber's active probe pings its target over the WAN interface
# directly (`ping -I ethN <target>`, real production behaviour — it relies
# on that interface's own default route, e.g. the ISP's DHCP-assigned
# gateway). This synthetic topology has no such gateway, so each WAN gets an
# explicit route to the probe target instead — still crosses tap-wan1/
# tap-wan2, so chaos_wan.sh's shaping still applies to it, which is the
# point: the probe is meant to see exactly what the WAN link is doing.
ip route add 198.51.100.0/30 dev eth1 metric 100
ip route add 198.51.100.0/30 dev eth2 metric 200

sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv4.conf.all.rp_filter=2

# The orchestrator controls run duration via the kernel command line (no env
# var passthrough into a QEMU -kernel/-initrd boot), e.g.
# "wifucked_duration_s=90 wifucked_hold_s=30".
CMDLINE="$(cat /proc/cmdline)"
for kv in ${CMDLINE}; do
    case "${kv}" in
        wifucked_duration_s=*) export WIFUCKED_CHAOS_DURATION_S="${kv#wifucked_duration_s=}" ;;
        wifucked_hold_s=*) export WIFUCKED_CHAOS_HOLD_S="${kv#wifucked_hold_s=}" ;;
    esac
done

echo "wifucked-chaos-test: network interfaces up, running chaos_driver.py (duration=${WIFUCKED_CHAOS_DURATION_S:-90}s)"
export PYTHONPATH=/opt/wifucked/src
python3 /opt/wifucked/chaos_driver.py 2>&1

echo "wifucked-chaos-test: post-driver diagnostics"
echo "-- ip addr show eth0 --"
ip addr show eth0
echo "-- ip route show table 888 --"
ip route show table 888
echo "-- nft list ruleset --"
nft list ruleset

echo "WIFUCKED_QEMU_READY"

# Held open so the host can run the LAN-side download while this guest's
# driver has already finished its own timed loop above — download_client.py
# runs concurrently with chaos_driver.py's loop, not after it, so this sleep
# is just keeping the guest (and therefore wg0/enforced routing) alive long
# enough for the host to observe the download's outcome and collect logs.
sleep "${WIFUCKED_CHAOS_HOLD_S:-30}"

echo "wifucked-chaos-test: hold elapsed, powering off"
poweroff -f
