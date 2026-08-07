"""Render real hostapd/dnsmasq config for the AP E2E proof.

Deliberately calls the exact same ``wifucked.lan`` functions
``appliance/stage-custom/opt/wifucked/firstboot.sh`` calls on real first boot —
this script exists so the E2E harness never hand-rolls a config that could drift
from what a real device actually generates. The only difference from firstboot
is the fixed test serial (so CI runs are reproducible) and the interface name
(a mac80211_hwsim radio instead of ``wlan0``).

Writes ``hostapd.conf`` and ``dnsmasq.conf`` into ``--out-dir``, and prints the
derived identity (SSID, BSSID, passphrase) as JSON on stdout so the orchestrator
and the client-side association step can consume it without re-deriving it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wifucked.config import LanConfig
from wifucked.lan import derive_identity, dnsmasq_config, hostapd_config
from wifucked.policy import profiles_for_lan_mode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="e2e-test-0001", help="fixed test device serial")
    parser.add_argument("--interface", required=True, help="hwsim wlan interface for hostapd")
    parser.add_argument("--channel", type=int, default=6)
    parser.add_argument("--lan-mode", default="single", choices=["single", "two_bss", "two_psk"])
    parser.add_argument(
        "--open-network",
        action="store_true",
        help="match production's real first-boot default (ADR-021): no WPA",
    )
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    config = LanConfig(lan_mode=args.lan_mode, open_on_first_boot=args.open_network)
    identity = derive_identity(args.serial, config)
    profiles = profiles_for_lan_mode(args.lan_mode)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "hostapd.conf").write_text(
        hostapd_config(
            identity,
            args.channel,
            args.lan_mode,
            interface=args.interface,
            open_network=args.open_network,
        )
    )
    (args.out_dir / "dnsmasq.conf").write_text(dnsmasq_config(config, profiles))

    identity_json = {
        "ssid": identity.ssid,
        "bssid": identity.bssid,
        "passphrase": identity.passphrase,
        "open_network": args.open_network,
        "gateway": config.address,
        "prefix": config.prefix,
    }
    (args.out_dir / "identity.json").write_text(json.dumps(identity_json, indent=2))
    json.dump(identity_json, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
