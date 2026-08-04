#!/usr/bin/env python3
"""Sends one real, 802.1Q VLAN-tagged ICMP echo request out a raw L2 socket
and waits for the reply.

Why this exists instead of the kernel's own `ip link ... type vlan` +
`ping`: the *host* this test runs in has no `CONFIG_VLAN_8021Q` (confirmed
via `/proc/config.gz` — it's a minimal container-oriented kernel with no
matching `/lib/modules` to load one either), so the host side of this test
cannot create a real VLAN netdevice. The QEMU *guest* boots a normal Alpine
kernel that has full 802.1Q support (`appliance/tests/qemu/module_closure.txt`
plus `modules.builtin`), so the guest's `eth0.10` VLAN subinterface — the
thing actually under test — still does real, kernel-level 802.1Q
de-encapsulation on a real frame. This script only needs to get a correctly
tagged frame onto the wire and read one back; it is test-harness plumbing,
not part of what ADR-019 changed.

Handles ARP itself (both directions) since there is no kernel IP stack
backing this raw socket to do it automatically.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

ETH_P_ALL = 0x0003
ETH_P_ARP = 0x0806
ETH_P_8021Q = 0x8100
ETH_P_IP = 0x0800

BROADCAST = b"\xff\xff\xff\xff\xff\xff"


def get_mac(ifname: str) -> bytes:
    with open(f"/sys/class/net/{ifname}/address") as f:
        text = f.read().strip()
    return bytes(int(b, 16) for b in text.split(":"))


def ip_to_bytes(ip: str) -> bytes:
    return bytes(int(o) for o in ip.split("."))


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def build_vlan_eth(
    dst_mac: bytes, src_mac: bytes, vlan_id: int, ethertype: int, payload: bytes
) -> bytes:
    tci = vlan_id & 0x0FFF
    return dst_mac + src_mac + struct.pack("!HHH", ETH_P_8021Q, tci, ethertype) + payload


def build_ipv4_icmp_echo(src_ip: str, dst_ip: str, ident: int, seq: int, payload: bytes) -> bytes:
    icmp_header = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
    icmp = icmp_header + payload
    icmp_csum = checksum(icmp)
    icmp = struct.pack("!BBHHH", 8, 0, icmp_csum, ident, seq) + payload

    total_len = 20 + len(icmp)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_len,
        0,
        0,
        64,
        1,
        0,
        ip_to_bytes(src_ip),
        ip_to_bytes(dst_ip),
    )
    ip_csum = checksum(ip_header)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_len,
        0,
        0,
        64,
        1,
        ip_csum,
        ip_to_bytes(src_ip),
        ip_to_bytes(dst_ip),
    )
    return ip_header + icmp


def build_arp_reply(src_mac: bytes, src_ip: str, dst_mac: bytes, dst_ip: str) -> bytes:
    return struct.pack(
        "!HHBBH6s4s6s4s",
        1,
        0x0800,
        6,
        4,
        2,
        src_mac,
        ip_to_bytes(src_ip),
        dst_mac,
        ip_to_bytes(dst_ip),
    )


def build_arp_request(src_mac: bytes, src_ip: str, dst_ip: str) -> bytes:
    return struct.pack(
        "!HHBBH6s4s6s4s",
        1,
        0x0800,
        6,
        4,
        1,
        src_mac,
        ip_to_bytes(src_ip),
        b"\x00" * 6,
        ip_to_bytes(dst_ip),
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("ifname")
    p.add_argument("--vlan", type=int, required=True)
    p.add_argument("--src-ip", required=True)
    p.add_argument("--dst-ip", required=True)
    p.add_argument("--gateway-ip", required=True, help="the guest's VLAN subinterface address")
    p.add_argument("--timeout", type=float, default=8.0)
    args = p.parse_args()

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((args.ifname, 0))
    sock.settimeout(args.timeout)

    my_mac = get_mac(args.ifname)
    ident = 0xBEEF & 0xFFFF
    seq = 1
    payload = b"wifucked-adr019-qemu-proof"

    icmp_pkt = build_ipv4_icmp_echo(args.src_ip, args.dst_ip, ident, seq, payload)
    frame = build_vlan_eth(BROADCAST, my_mac, args.vlan, ETH_P_IP, icmp_pkt)
    sock.send(frame)
    print(
        f"vlan_ping: sent tagged ICMP echo request vlan={args.vlan} {args.src_ip} -> {args.dst_ip}"
    )

    deadline = time.time() + args.timeout
    got_reply = False
    while time.time() < deadline:
        try:
            data = sock.recv(2048)
        except TimeoutError:
            break
        if len(data) < 18:
            continue
        tpid = struct.unpack("!H", data[12:14])[0]
        if tpid != ETH_P_8021Q:
            continue
        tci, ethertype = struct.unpack("!HH", data[14:18])
        vlan_id = tci & 0x0FFF
        if vlan_id != args.vlan:
            continue
        body = data[18:]

        if ethertype == ETH_P_ARP and len(body) >= 28:
            _, _, _, _, op = struct.unpack("!HHBBH", body[0:8])
            sender_mac, sender_ip_b = body[8:14], body[14:18]
            _, target_ip_b = body[18:24], body[24:28]
            target_ip = ".".join(str(b) for b in target_ip_b)
            if op == 1 and target_ip == args.src_ip:
                # The guest is ARPing for us — answer so its reply can route back.
                reply = build_arp_reply(
                    my_mac, args.src_ip, sender_mac, ".".join(str(b) for b in sender_ip_b)
                )
                sock.send(build_vlan_eth(sender_mac, my_mac, args.vlan, ETH_P_ARP, reply))
                print("vlan_ping: answered ARP request from guest")
            continue

        if ethertype == ETH_P_IP and len(body) >= 20:
            proto = body[9]
            if proto != 1:
                continue
            icmp = body[20:]
            if len(icmp) < 8:
                continue
            icmp_type, _, _, r_ident, r_seq = struct.unpack("!BBHHH", icmp[0:8])
            if icmp_type == 0 and r_ident == ident:
                print(f"vlan_ping: got ICMP echo reply seq={r_seq} from {args.dst_ip}")
                got_reply = True
                break

    if not got_reply:
        # One more nudge: make sure the guest actually has us in its ARP
        # table before giving up — some kernels ARP lazily on first send
        # failure rather than pre-resolving.
        req = build_arp_request(my_mac, args.src_ip, args.gateway_ip)
        sock.send(build_vlan_eth(BROADCAST, my_mac, args.vlan, ETH_P_ARP, req))

    return 0 if got_reply else 1


if __name__ == "__main__":
    sys.exit(main())
