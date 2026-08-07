# AP + dashboard E2E proof

Boots the **real** `hostapd`, the **real** `dnsmasq`, and the **real** dashboard
(`wifucked.daemon.Daemon` + `wifucked.api.create_app`, exactly what
`python3 -m wifucked` runs — see `e2e_daemon.py`), wired to a real kernel
802.11 stack via `mac80211_hwsim`: two virtual radios that exchange genuine
802.11 management and data frames over a simulated medium (real
`hostapd`/`wpa_supplicant`/`nl80211` code paths, real association, real
`iw`/`dhclient`, no RF, no Raspberry Pi).

This exists because [`appliance/tests/qemu/`](../qemu/)'s proofs — while real
in their own right — never start hostapd or dnsmasq at all; they call
`wifucked.enforce`/`wifucked.tunnel` Python code directly against a driver
script. That leaves exactly the failure mode this test was built to catch
uncovered: **a client gets a DHCP lease, but cannot ping the gateway and
cannot open the setup dashboard at `:8080`** — a real bring-up bug, not a
control-loop bug, in the boundary those tests never touch.

## Running it

```bash
sudo appliance/tests/e2e/run_e2e_ap_test.sh [results-dir]
```

Requires root (network namespaces, `mac80211_hwsim`, binding `hostapd` to a
real interface) and `hostapd`, `dnsmasq`, `iw`, and a DHCP client
(`dhclient` or `udhcpc`) on `PATH`, plus Python with `flask` and `playwright`
installed (`pip install -r appliance/tests/e2e/requirements.txt && playwright
install --with-deps chromium`). This is why it is **not** part of
`run_all_tests.sh` (SOP-003: "no test may require... root") and instead runs
as its own CI job (`.github/workflows/ci.yml`'s `e2e-ap-dashboard`).

`results-dir` defaults to `<repo-root>/e2e-artifacts/` and receives, on every
run (pass or fail):

- `report.json` / `report.md` — one row per stage, pass/fail, timing, detail
- `junit.xml` — same, as JUnit XML
- `screenshots/dashboard.png` — full-page screenshot of the live dashboard
- `*.log` — hostapd, dnsmasq, the daemon, dhclient, and Playwright logs

## What's actually running, and why it's shaped this way

- **`mac80211_hwsim radios=2`** creates two independent virtual PHYs that can
  hear each other over a simulated medium — this is the same mechanism
  `hostapd`'s and `wpa_supplicant`'s own upstream test suites use, and it
  exercises the genuine `nl80211` driver binding, not a fake. It is *not* a
  Raspberry Pi's real Broadcom (`brcmfmac`/CYW43438) radio — see "What this
  does not prove" below.
- **One radio moves into the `wifucked-e2e-ap` network namespace** and runs
  real `hostapd` against a config built by `render_configs.py`, which calls
  the *exact same* `wifucked.lan.hostapd_config()`/`dnsmasq_config()`
  functions `appliance/stage-custom/opt/wifucked/firstboot.sh` calls on a
  real first boot — including the real derived SSID and the real
  open-network default (ADR-021). If a future change to those functions
  produces a config `hostapd` rejects, this test fails; a hand-rolled config
  living only in this test could never catch that.
- **The gateway address** (`10.44.0.1/24`) is assigned directly with `ip
  addr add` rather than through `systemd-networkd`, because this harness is
  a pair of bare network namespaces, not a full init system. This is a
  deliberate simplification from what `networkd_config()` generates for a
  real device — the *address value* is real and taken from
  `LanConfig.address`, but the *mechanism* that applies it on a real Pi
  (`systemd-networkd` matching by interface name) is not exercised here.
- **The real dashboard daemon** (`e2e_daemon.py`) runs `Daemon` and
  `create_app()` unmodified, under `MOCK_HW=1` — the same required HAL seam
  every other test in this repo uses (SOP-003) — so the control loops never
  attempt real `tc`/`nft`/`hostapd_cli` calls. hostapd and dnsmasq are
  started independently by the orchestrator, exactly mirroring ADR-011 ("the
  AP is the anchor," never owned by the daemon) on a real device. The only
  deviation from `python3 -m wifucked`: the dashboard's bearer token is fixed
  instead of randomly generated, so the client-side check can authenticate.
- **The other radio moves into `wifucked-e2e-client`** and stands in for a
  phone or laptop: `iw connect` (real association, matching the open-network
  default), then `dhclient`/`udhcpc` (a real DHCP transaction against the
  real dnsmasq), then `ping` at the gateway, then a real headless Chromium
  (Playwright) loading `http://10.44.0.1:8080/` — all run inside that
  namespace via `ip netns exec`, so the HTTP request genuinely traverses the
  same simulated L2/L3 path a real client's browser would.

## What this proves, and what it doesn't

**Confirmed by a passing run:** the real hostapd config this code generates
is accepted by real hostapd and reaches `state=ENABLED`; a real client can
associate to the real derived SSID; the real dnsmasq hands out a real lease
on the real subnet; the gateway answers ICMP; the real Flask app binds
`10.44.0.1:8080` and serves the real dashboard template with a 200 and the
real `/api/health` unauthenticated liveness path also answers.

**Not proven — still needs a real Pi** (see
[`docs/active-tests.md`](../../../docs/active-tests.md)'s "AP bring-up"
entry, which this test narrows but does not close):

- Real `brcmfmac`/CYW43438 firmware and driver behaviour. `mac80211_hwsim` is
  a real 802.11 *software* MAC layer; it cannot reproduce a specific chip's
  firmware quirks, `rfkill` state, or AP+STA concurrency limits.
- Real RF — channel contention, range, interference. Everything here happens
  over a lossless simulated medium.
- Whether `systemd-networkd` actually applies `networkd_config()`'s generated
  unit to a real `hostapd`-created VLAN subinterface on boot (this harness
  assigns the address directly instead — see above).
- Whether `NetworkManager`'s `unmanaged-devices` configuration actually keeps
  its hands off `wlan0` on a real image (this harness has no `NetworkManager`
  in the loop at all).

## Files

| File | Role |
|---|---|
| `render_configs.py` | Calls the real `wifucked.lan` functions to write `hostapd.conf`/`dnsmasq.conf`, same as `firstboot.sh` |
| `e2e_daemon.py` | Runs the real `Daemon` + dashboard `Flask` app with a known token |
| `playwright_check.py` | Real headless Chromium at the real dashboard URL; screenshot + assertions |
| `write_fragment.py` / `aggregate_report.py` | Turn each stage's pass/fail into `report.json`/`report.md`/`junit.xml` |
| `run_e2e_ap_test.sh` | Orchestrates all of the above: `mac80211_hwsim`, netns, hostapd, dnsmasq, the daemon, the client, and the report |
