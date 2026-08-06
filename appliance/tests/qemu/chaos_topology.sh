#!/bin/bash
# Host-side network topology for the WAN-chaos download proof.
#
# Same bridge/tap/netns shape as topology.sh (the ADR-019 packet proof), with
# one deliberate difference: the LAN side is a flat, addressed network
# instead of a VLAN-tagged one. The appliance guest runs ADR-020's "single"
# LAN mode here (current production default — no VLAN split), so a real IP
# stack in lanclient netns can do a genuine HTTP download over a real TCP
# connection; topology.sh's hand-crafted-802.1Q approach existed only because
# that test's two_psk VLAN layout requires it and this sandbox's host kernel
# has no CONFIG_VLAN_8021Q to run a real subinterface (see vlan_ping.py).
#
#   netns lanclient --veth-lc (192.168.60.2/24)---+
#                                                    |
#                                                br-lan --- tap-lan --- [appliance guest eth0, 192.168.60.1/24]
#
#                                                br-wan --- tap-wan1 --- [appliance guest eth1] (chaos_wan.sh shapes this)
#                                                    |    \- tap-wan2 --- [appliance guest eth2] (chaos_wan.sh shapes this)
#                                                    |    \- tap-fabric -- [fabric guest eth0]
#                                                    |         (203.0.113.10/24 *and* 198.51.100.1/30)
#   netns internet --veth-inet (198.51.100.2/30)-----+
#     (serves the download payload; reachable only via the fabric's real
#     forwarding+NAT once the appliance's tunnel/enforce state is correct)
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

    echo "--- lanclient <-> br-lan (flat, addressed — real TCP/IP, no VLAN craft)"
    ip link add veth-lc type veth peer name veth-lc-br
    ip link set veth-lc netns lanclient
    ip link set veth-lc-br master br-lan
    ip link set veth-lc-br up
    ip netns exec lanclient ip link set lo up
    ip netns exec lanclient ip addr add 192.168.60.2/24 dev veth-lc
    ip netns exec lanclient ip link set veth-lc up
    ip netns exec lanclient ip route add default via 192.168.60.1

    echo "--- internet <-> br-wan (the download payload's server)"
    ip link add veth-inet type veth peer name veth-inet-br
    ip link set veth-inet netns internet
    ip link set veth-inet-br master br-wan
    ip link set veth-inet-br up
    ip netns exec internet ip link set lo up
    ip netns exec internet ip addr add 198.51.100.2/30 dev veth-inet
    ip netns exec internet ip link set veth-inet up
    ip netns exec internet ip route add default via 198.51.100.1

    echo "topology up (fabric + appliance guests boot separately)."
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
