#!/bin/busybox sh
# shellcheck shell=dash
# PID 1 inside the QEMU guest. Brings up just enough of a Linux system to run
# the real wifucked.enforce / wifucked.tunnel code paths against real kernel
# networking, then hands off to driver.py.

/bin/busybox --install -s /bin
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || mount -t tmpfs tmpfs /dev
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts 2>/dev/null || true
# `nft -f -` needs these — most distros provide them via a tmpfiles rule
# this minimal initramfs doesn't run.
ln -sf /proc/self/fd/0 /dev/stdin
ln -sf /proc/self/fd/1 /dev/stdout
ln -sf /proc/self/fd/2 /dev/stderr

echo "wifucked-qemu-test: mounted proc/sys/dev"

KVER="6.6.110-0-lts"
MODDIR="/lib/modules/${KVER}"

# Dependency-first insmod order (see module_closure.txt at build time).
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
    echo "wifucked-qemu-test: FATAL wireguard.ko did not load, see /wifucked-modprobe.log"
    cat /wifucked-modprobe.log
fi
if ! grep -q sch_cake /proc/modules; then
    echo "wifucked-qemu-test: WARNING sch_cake.ko did not load (CAKE shaping argv untested)"
fi

ip link set lo up

# LAN trunk (no address of its own — hostapd's VLAN subinterfaces carry
# addresses, matching production's two_psk layout, ADR-014).
ip link set eth0 up
ip link add link eth0 name eth0.10 type vlan id 10
ip link add link eth0 name eth0.20 type vlan id 20
ip addr add 192.168.60.1/24 dev eth0.10
ip addr add 192.168.61.1/24 dev eth0.20
ip link set eth0.10 up
ip link set eth0.20 up

# Two WAN atomics, on a shared "internet" L2 segment with the fabric — the
# WAN swap the scenario test proves at the control-plane layer, proven here
# at the packet layer instead.
ip link set eth1 up
ip link set eth2 up
ip addr add 203.0.113.11/24 dev eth1
ip addr add 203.0.113.12/24 dev eth2

# Matches appliance/setup_rpi.sh's provisioning-time sysctls exactly — the
# daemon itself never sets these (confirmed by grep across appliance/), so
# this guest replicates what first-boot provisioning does, not what the
# code under test does.
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv4.conf.all.rp_filter=2

echo "wifucked-qemu-test: network interfaces up, running driver.py"
export PYTHONPATH=/opt/wifucked/src
python3 /opt/wifucked/driver.py 2>&1

echo "wifucked-qemu-test: post-driver diagnostics"
echo "-- ip addr show eth0.10 --"
ip addr show eth0.10
echo "-- ip route show table 888 --"
ip route show table 888
echo "-- nft list ruleset --"
nft list ruleset
echo "-- ip neigh --"
ip neigh show

echo "WIFUCKED_QEMU_READY"

# Give the host time to inject/observe packets. Print eth0's packet
# counters and wg0's transfer counters every few seconds so the host can
# correlate exactly when (and how far) an injected frame actually got:
# this Alpine kernel build has no CONFIG_PACKET (AF_PACKET raw sockets
# fail with EAFNOSUPPORT — confirmed while building this proof), so an
# in-guest packet sniffer isn't available; and this sandbox's own tcpdump
# does not capture real traffic even in the host's default namespace
# (confirmed independently of this test). Interface RX counters plus
# WireGuard's own transfer counters (which only advance for genuinely
# encrypted-and-sent bytes) are the verification path actually available
# here: rising `eth0.10` alongside rising `wg_sent` is real evidence the
# LAN packet was marked, routed, and handed to WireGuard for encryption.
for i in $(seq 1 20); do
    sleep 3
    wg_line="$(wg show wg0 transfer 2>/dev/null)"
    echo "wifucked-qemu-test: t=+$((i * 3))s eth0=$(cat /sys/class/net/eth0/statistics/rx_packets 2>/dev/null) eth0.10=$(cat /sys/class/net/eth0.10/statistics/rx_packets 2>/dev/null) wg_transfer=[${wg_line}]"
done

echo "wifucked-qemu-test: sleep elapsed, powering off"
poweroff -f
