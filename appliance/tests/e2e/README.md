# AP + dashboard E2E proof

Boots a **real** Debian guest under QEMU and runs the **actual**
`appliance/setup_rpi.sh` — the same script the real Pi image bake runs — live,
inside it. That script enables the real systemd units this repo ships:
`hostapd`, `dnsmasq`, `systemd-networkd`, `NetworkManager` (with the real
`unmanaged-devices` config), `wifucked-firstboot` (the real `firstboot.sh`),
and `wifucked.service` itself — **no `MOCK_HW` override**, so it drives the
real Linux HAL, issuing real `iw`/`hostapd_cli`/`nft`/`tc` calls, exactly as
it would on a Pi. `mac80211_hwsim` gives the guest a literal `wlan0` (real
kernel 802.11 stack, real association, no RF, no Pi). A second radio, moved
into its own network namespace inside the *same* guest kernel, plays a real
Wi-Fi client: real `iw connect` association, a real DHCP lease from the real
`dnsmasq`, a real `ping` at the gateway, and a real headless Chromium
(Playwright) at the real dashboard.

## What changed, and why

An earlier version of this test (see git history on this branch) used Linux
network namespaces on the CI runner directly, called `wifucked.lan`'s
config-generating Python functions instead of running `firstboot.sh`,
hand-assigned the gateway IP with `ip addr add` instead of letting
`systemd-networkd` apply it, and ran the dashboard daemon under `MOCK_HW=1`.
Every one of those was a shortcut around a real production integration point
— NetworkManager, systemd-networkd, hostapd's real service unit, real config
generation — and the entire real-world bug this test exists to catch ("I get
an IP but can't ping the gateway or open `:8080`") could plausibly live in
any of them. A test that routes around its own reason for existing isn't
worth trusting. This version doesn't route around anything it can avoid
routing around.

## Running it

```bash
sudo appliance/tests/e2e/run_e2e_ap_test.sh [results-dir]
```

Requires root, `qemu-system-x86_64`, `qemu-img`, `genisoimage`, `mkfs.vfat`
(`dosfstools`), and `mtools` on `PATH`. First run downloads and caches a
~300 MB Debian 12 cloud image (`download_base_image.sh`, cached under
`.work/`, or via `actions/cache` in CI). This is why it is **not** part of
`run_all_tests.sh` (SOP-003: "no test may require... root") and instead runs
as its own CI job (`.github/workflows/ci.yml`'s `e2e-ap-dashboard`).

`results-dir` defaults to `<repo-root>/e2e-artifacts/` and receives, on every
run (pass or fail):

- `report.json` / `report.md` — one row per stage, pass/fail, timing, detail
- `junit.xml` — same, as JUnit XML
- `screenshots/dashboard.png` — full-page screenshot of the live dashboard
- `console.log` — the guest's serial console (kernel boot, cloud-init, systemd)
- `logs/driver.log` — the E2E driver script's own narration
- `logs/diagnostics.txt` — `systemctl status`/`journalctl` for every relevant
  unit, `ip addr`, `nmcli device status`, `hostapd_cli status`, and every
  generated config file, captured unconditionally (not just on failure)

## What's actually running, and why it's shaped this way

- **`mac80211_hwsim radios=2`**, loaded inside the *guest's own kernel* (not
  the CI runner's), creates two real virtual 802.11 PHYs. This has to be
  inside the guest and not the host: the whole point is that `hostapd`,
  `wpa_supplicant`, `NetworkManager`, and `systemd-networkd` all run for
  real, as real systemd units, which needs a real systemd — the CI runner's
  own host OS is not going to be reconfigured as a wifucked appliance.
- **The interface is forced to be named `wlan0`** — down/renamed
  immediately after the module loads, before `NetworkManager` or `hostapd`
  can see it — rather than trusting whatever name `mac80211_hwsim`/udev
  happens to assign. The real device has exactly one onboard radio, always
  `wlan0` (`docs/hardware.md`); every hostapd config this repo generates and
  `setup_rpi.sh`'s `NetworkManager` `unmanaged-devices` glob hard-code that
  name. This is the harness enforcing a naming guarantee the real hardware
  gets "for free" from having only one radio — see ADR-002.
- **The second radio moves into its own network namespace before anything
  else touches the network** — before `NetworkManager`, before `hostapd` —
  so it never gets managed as if it were the AP radio. It plays the "phone."
- **`appliance/setup_rpi.sh` runs unmodified**, given the repo checkout via
  a read-only ISO9660 image (`/mnt/repo` in the guest) — real
  `apt-get install` of the real `appliance/apt_deps.txt` package list, real
  `systemctl enable` of every real unit, the real `NetworkManager`
  `unmanaged-devices` config, the real sysctls. If a future change to that
  script breaks on a real Debian system, this test fails; a step that
  reimplemented "what the script probably does" could never catch that.
- **`wifucked-firstboot.service` runs the real `firstboot.sh`**, which
  derives identity from `/proc/cpuinfo`'s `Serial` line — absent on this x86
  guest, so it falls through to the script's own documented fallback
  (`/etc/machine-id`), the same fallback path real firstboot code already
  has to have for the rare real device that fails to read its serial.
- **`systemd-networkd` applies the real generated `.network` unit** to
  `wlan0` for its gateway address — nothing in this harness assigns that
  address directly. If `systemd-networkd` and `NetworkManager` don't
  actually coexist cleanly on this interface split (the exact open question
  in `docs/active-tests.md`'s "AP bring-up" entry), this is the first place
  that would show up as a failure, not silently work anyway.
- **`wifucked.service` runs exactly as `setup_rpi.sh` enables it** — no
  `MOCK_HW`, real `Daemon`, real Linux HAL. `/var/lib/wifucked/api_token` is
  read directly off disk (the real, real-persisted token
  `load_or_create_api_token()` writes), not faked or fixed in advance.
- **A real headless Chromium (Playwright)** runs inside the client network
  namespace against the real dashboard URL, so the HTTP request genuinely
  traverses the same path a real associated client's browser would.

## Known deviations from a real device — read before trusting a PASS

- **Provisioning runs live at guest boot, not baked into the image
  beforehand.** On a real device, `setup_rpi.sh` runs once, at image-bake
  time, so `NetworkManager` never sees `wlan0` before the
  `unmanaged-devices` rule protecting it exists. Here, `setup_rpi.sh` runs
  *during* this guest's first (and only) boot, after `wlan0` already exists
  — a real, narrow race this harness does not have on real hardware,
  mitigated with an explicit `nmcli device set wlan0 managed no` immediately
  after the interface is renamed, but not eliminated by construction the way
  baking the config into the image does.
- **Real `brcmfmac`/CYW43438 firmware and driver behaviour, and real RF, are
  still untested.** `mac80211_hwsim` is a real 802.11 *software* MAC layer —
  real association state machine, real frame exchange — but it cannot
  reproduce a specific chip's firmware quirks, `rfkill` power state on real
  hardware, or AP+STA concurrency limits. See `docs/active-tests.md`'s "AP
  bring-up" and "AP+STA SHARED profile" entries, which this test narrows but
  does not close.
- **This is Debian on x86_64, not Raspberry Pi OS on ARM.** Package versions,
  default configs, and kernel build differ. Close enough to catch a broad
  class of "the generated config is wrong" or "the units don't coexist"
  bugs; not a substitute for the real image on real hardware.

## Files

| File | Role |
|---|---|
| `download_base_image.sh` | Fetches + caches the Debian 12 "generic" cloud qcow2 |
| `cloud-init/user-data`, `cloud-init/meta-data` | Minimal NoCloud seed — mounts the repo/results disks, hands off to `guest/e2e_driver.sh` |
| `guest/e2e_driver.sh` | Runs *inside* the guest as root: hwsim/wlan0 setup, the real `setup_rpi.sh`, real service startup, real client association/DHCP/ping/Playwright, writes the report |
| `playwright_check.py` | Real headless Chromium at the real dashboard URL; screenshot + assertions (called by `e2e_driver.sh` inside the guest) |
| `write_fragment.py` / `aggregate_report.py` | Turn each stage's pass/fail into `report.json`/`report.md`/`junit.xml` (pure stdlib — run both inside the guest and, for the "guest never finished" case, on the host) |
| `run_e2e_ap_test.sh` | Host orchestrator: builds the repo/seed/results disk images, boots QEMU, waits, extracts results |
