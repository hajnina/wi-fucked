#!/bin/busybox sh
# shellcheck shell=dash
# PID 1 inside the fabric QEMU guest. The fabric server also needs real
# kernel WireGuard support (this sandbox's own host kernel has none — see
# download_kernel.sh's header) — running it as a second QEMU guest using the
# exact same kernel/module set as the appliance guest is what makes this a
# genuine two-VM proof rather than "appliance in QEMU, fabric mocked".

/bin/busybox --install -s /bin
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || mount -t tmpfs tmpfs /dev
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts 2>/dev/null || true
# `nft -f -` (and anything else reading/writing the standard streams by
# path) needs these — most distros provide them via a tmpfiles rule this
# minimal initramfs doesn't run.
ln -sf /proc/self/fd/0 /dev/stdin
ln -sf /proc/self/fd/1 /dev/stdout
ln -sf /proc/self/fd/2 /dev/stderr

echo "wifucked-fabric-qemu-test: mounted proc/sys/dev"

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
    echo "wifucked-fabric-qemu-test: FATAL wireguard.ko did not load"
fi
if ! grep -q nf_tables /proc/modules; then
    echo "wifucked-fabric-qemu-test: FATAL nf_tables.ko did not load"
fi
echo "wifucked-fabric-qemu-test: modprobe.log follows"
cat /wifucked-modprobe.log
echo "wifucked-fabric-qemu-test: /proc/modules follows"
cat /proc/modules

ip link set lo up
ip link set eth0 up
# One NIC carries both the "WAN-facing" segment the appliance guest's
# eth1/eth2 reach it on, and (as a second address on the same bridge) the
# segment the "internet" target netns sits on — see topology.sh's header.
ip addr add 203.0.113.10/24 dev eth0
ip addr add 198.51.100.1/30 dev eth0

sysctl -w net.ipv4.ip_forward=1
# `ip_forward=1` (conf.all.forwarding) only synchronizes onto interfaces
# that already exist at the moment it's written — wg0 doesn't exist yet at
# this point in boot (FabricWireGuard creates it later, inside the first
# /register call), so it would otherwise inherit conf.default.forwarding's
# stock value instead. Setting `default` too makes wg0 forwarding-enabled
# from the moment it's created. Found by this proof: NAT and the RFC1918
# route were both already correct and packets still weren't forwarded
# until this was added.
sysctl -w net.ipv4.conf.default.forwarding=1
# rp_filter=0 (off), not 2 (loose, what the appliance side uses): a real
# fabric has a real default route out its real WAN, so loose mode (any
# route to the source, via any interface) passes fine there. This test
# topology's fabric guest has no default route at all — it only knows
# directly-connected subnets — so a tunnel-peer's forwarded LAN-client
# packet (source e.g. 192.168.60.2, arriving on wg0) has literally no
# matching route in any table, and even loose RPF drops it. Found by this
# proof, not by inspection: this is test-harness topology plumbing, not a
# statement about the real fabric's sysctl posture, which is unaffected —
# nothing here ships. Set on `default` (not just `all`) so wg0, created
# later by fabric.wireguard.FabricWireGuard, inherits it too.
sysctl -w net.ipv4.conf.all.rp_filter=0
sysctl -w net.ipv4.conf.default.rp_filter=0

echo "wifucked-fabric-qemu-test: network up, starting fabric.app"
export PYTHONPATH=/opt/fabric/src:/usr/lib/python3.11/site-packages
export FABRIC_ADDRESS="203.0.113.10:51820"
export FABRIC_USERNAME="qemutest"
export FABRIC_PASSWORD="qemutest"
export FABRIC_WG_PRIVATE_KEY_FILE="/var/lib/fabric/wg-privatekey"
export FABRIC_PEER_REGISTRY="/var/lib/fabric/peers.json"
export FABRIC_TUNNEL_POOL="10.99.0.0/24"
mkdir -p /var/lib/fabric

python3 /opt/fabric/fabric_server.py > /wifucked-fabric-app.log 2>&1 &
FABRIC_PID=$!
tail -f /wifucked-fabric-app.log &
TAIL_PID=$!

# Wait for the real Flask process to actually answer before declaring ready.
FABRIC_UP=0
for i in $(seq 1 30); do
    if python3 -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=2)
" > /dev/null 2>&1; then
        FABRIC_UP=1
        break
    fi
    sleep 1
done

if [ "${FABRIC_UP}" != "1" ]; then
    echo "wifucked-fabric-qemu-test: fabric.app did not come up, dumping its log:"
    cat /wifucked-fabric-app.log
fi

echo "WIFUCKED_FABRIC_QEMU_READY"

# Self-test: does this guest's own eth0 actually reach the "internet" netns
# target at all, independent of the tunnel/NAT path? Isolates a topology
# problem (ARP/bridging between this guest and that host netns) from an
# ADR-019 forwarding problem. Runs entirely inside this guest — both send
# and receive — since AF_PACKET raw sockets aren't usable for crafting from
# the host in this sandbox (see vlan_ping.py's docstring).
python3 -u -c "
import socket, struct, time

def checksum(data):
    if len(data) % 2:
        data += b'\x00'
    total = sum(struct.unpack(f'!{len(data)//2}H', data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF

try:
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    s.settimeout(5)
    hdr = struct.pack('!BBHHH', 8, 0, 0, 0xABCD, 1)
    csum = checksum(hdr)
    hdr = struct.pack('!BBHHH', 8, 0, csum, 0xABCD, 1)
    s.sendto(hdr, ('198.51.100.2', 0))
    print('wifucked-fabric-qemu-test: self-test ICMP sent to 198.51.100.2', flush=True)
    start = time.time()
    while time.time() - start < 5:
        data, addr = s.recvfrom(200)
        if addr[0] == '198.51.100.2' and len(data) >= 20 and data[20] == 0:
            print('wifucked-fabric-qemu-test: self-test PASS — got ICMP reply from 198.51.100.2', flush=True)
            break
    else:
        print('wifucked-fabric-qemu-test: self-test FAIL — no reply from 198.51.100.2 (topology/ARP issue, not ADR-019)', flush=True)
except Exception as exc:
    print('wifucked-fabric-qemu-test: self-test errored:', repr(exc), flush=True)
"

# Second self-test: source-spoof an RFC1918 address (IP_HDRINCL) and send
# it to 198.51.100.2 *without* going through wg0 at all. Isolates whether
# NAT+forward+ARP genuinely works for an RFC1918 source address on this
# guest, independent of anything WireGuard-specific about the real path.
python3 -u -c "
import socket, struct, time

def checksum(data):
    if len(data) % 2:
        data += b'\x00'
    total = sum(struct.unpack(f'!{len(data)//2}H', data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF

try:
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
    icmp = struct.pack('!BBHHH', 8, 0, 0, 0xBEEF, 1)
    icmp = struct.pack('!BBHHH', 8, 0, checksum(icmp), 0xBEEF, 1)
    ip_hdr = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(icmp), 0, 0, 64, 1, 0,
                          socket.inet_aton('192.168.60.99'), socket.inet_aton('198.51.100.2'))
    ip_hdr = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(icmp), 0, 0, 64, 1,
                          checksum(ip_hdr), socket.inet_aton('192.168.60.99'), socket.inet_aton('198.51.100.2'))
    s.sendto(ip_hdr + icmp, ('198.51.100.2', 0))
    print('wifucked-fabric-qemu-test: spoof-test sent (src=192.168.60.99, no wg0 involved)', flush=True)

    r = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    r.settimeout(5)
    start = time.time()
    while time.time() - start < 5:
        data, addr = r.recvfrom(200)
        if addr[0] == '198.51.100.2' and len(data) >= 28 and data[20] == 0:
            print('wifucked-fabric-qemu-test: spoof-test PASS — NAT+forward+ARP works for RFC1918 src outside wg0', flush=True)
            break
    else:
        print('wifucked-fabric-qemu-test: spoof-test FAIL — RFC1918-sourced traffic never reached 198.51.100.2 even without wg0', flush=True)
except Exception as exc:
    print('wifucked-fabric-qemu-test: spoof-test errored:', repr(exc), flush=True)
"

# Mirror the appliance guest's diagnostic loop: wg0 transfer counters show
# whether the fabric is decrypting anything from the appliance at all,
# eth0 counters show whether anything reaches the "internet" netns side.
for i in $(seq 1 20); do
    sleep 3
    wg_line="$(wg show wg0 transfer 2>/dev/null)"
    echo "wifucked-fabric-qemu-test: t=+$((i * 3))s eth0_rx=$(cat /sys/class/net/eth0/statistics/rx_packets 2>/dev/null) eth0_tx=$(cat /sys/class/net/eth0/statistics/tx_packets 2>/dev/null) wg_transfer=[${wg_line}]"
done

nft list ruleset
echo "-- wg show wg0 (full, including allowed-ips as actually configured) --"
wg show wg0
echo "-- ip route show (main table) --"
ip route show
echo "-- ip route show table all --"
ip route show table all
echo "-- ip route get 192.168.60.99 --"
ip route get 192.168.60.99

kill "${TAIL_PID}" 2>/dev/null || true
kill "${FABRIC_PID}" 2>/dev/null || true
poweroff -f
