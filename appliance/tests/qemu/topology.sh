#!/bin/bash
# Host-side network topology for the QEMU packet-routing proof.
#
#   netns lanclient --veth-lc (VLAN 10 crafted by vlan_ping.py, see its
#                              module docstring for why not a real VLAN
#                              netdevice on this host)---+
#                                                          |
#                                                      br-lan --- tap-lan --- [appliance guest eth0]
#
#                                                      br-wan --- tap-wan1 --- [appliance guest eth1]
#                                                          |    \- tap-wan2 --- [appliance guest eth2]
#                                                          |    \- tap-fabric -- [fabric guest eth0]
#                                                          |         (203.0.113.10/24 *and*
#                                                          |          198.51.100.1/30 on one NIC)
#   netns internet --veth-inet (198.51.100.2/30)-----------+
#     (the "rest of the Internet" ping target, reachable only via
#     the fabric's real forwarding+NAT once ADR-019 is correctly wired)
#
# Both the appliance and the fabric are QEMU guests here (see
# fabric_guest_init.sh's header for why the fabric isn't a host netns): this
# sandbox's own host kernel has neither CONFIG_WIREGUARD nor
# CONFIG_VLAN_8021Q (confirmed via /proc/config.gz), so anything needing
# real kernel WireGuard or real 802.1Q de-encapsulation has to run inside a
# guest kernel that does.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${QEMU_TEST_WORKDIR:-${HERE}/.work}"
RUNTIME="${WORK}/runtime"

up() {
    mkdir -p "${RUNTIME}"

    echo "--- namespaces"
    ip netns add lanclient
    ip netns add internet

    echo "--- bridges"
    ip link add br-lan type bridge
    ip link add br-wan type bridge
    ip link set br-lan up
    ip link set br-wan up

    echo "--- taps for QEMU"
    ip tuntap add dev tap-lan mode tap
    ip tuntap add dev tap-wan1 mode tap
    ip tuntap add dev tap-wan2 mode tap
    ip tuntap add dev tap-fabric mode tap
    ip link set tap-lan master br-lan
    ip link set tap-wan1 master br-wan
    ip link set tap-wan2 master br-wan
    ip link set tap-fabric master br-wan
    ip link set tap-lan up
    ip link set tap-wan1 up
    ip link set tap-wan2 up
    ip link set tap-fabric up

    echo "--- lanclient <-> br-lan"
    ip link add veth-lc type veth peer name veth-lc-br
    ip link set veth-lc netns lanclient
    ip link set veth-lc-br master br-lan
    ip link set veth-lc-br up
    ip netns exec lanclient ip link set lo up
    ip netns exec lanclient ip link set veth-lc up

    echo "--- internet <-> br-wan"
    ip link add veth-inet type veth peer name veth-inet-br
    ip link set veth-inet netns internet
    ip link set veth-inet-br master br-wan
    ip link set veth-inet-br up
    ip netns exec internet ip link set lo up
    ip netns exec internet ip addr add 198.51.100.2/30 dev veth-inet
    ip netns exec internet ip link set veth-inet up
    ip netns exec internet ip route add default via 198.51.100.1

    echo "topology up (fabric guest boots separately, see run_packet_routing_test.sh)."
}

down() {
    ip netns del lanclient 2>/dev/null || true
    ip netns del internet 2>/dev/null || true
    ip link del br-lan 2>/dev/null || true
    ip link del br-wan 2>/dev/null || true
    ip link del tap-lan 2>/dev/null || true
    ip link del tap-wan1 2>/dev/null || true
    ip link del tap-wan2 2>/dev/null || true
    ip link del tap-fabric 2>/dev/null || true
    echo "topology torn down."
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    *) echo "usage: $0 {up|down}" >&2; exit 2 ;;
esac
