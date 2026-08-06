# SOP-009 — Hardware and field debugging

For when it works under `MOCK_HW=1` and not on a Pi — or works on your desk and
not in a van.

## Before assuming the code is wrong

Check the cheap explanations first. In this system they are usually right.

| Symptom | Check first |
|---|---|
| Random instability, resets, USB dropouts | **Undervoltage.** `vcgencmd get_throttled` — anything non-zero. A marginal PSU explains a large share of "flaky appliance" reports. |
| Works, then degrades over days | SD card. `dmesg \| grep -i mmc`, filesystem errors. |
| Interface missing after replug | Re-enumeration — is something keying off `ifname`? ([ADR-002](../adr/ADR-002-atomic-identity.md)) |
| Throughput far below expectation | 2.4 GHz congestion. Are you in SHARED profile, splitting airtime? ([`../hardware.md`](../hardware.md)) |
| AP unreachable but device up | `systemctl status hostapd` — it is independent of `wifucked.service` by design, so check it independently. |

## The device tells you first

Do not start with `tcpdump`. Start with what the appliance already recorded.

```bash
# What does it think is going on?
curl -s localhost:8080/api/state | jq

# Why did it do that? — the decision journal
curl -s localhost:8080/api/decisions?limit=50 | jq

# Structured logs
journalctl -u wifucked -n 500 --no-pager
journalctl -u wifucked -n 500 -o json | jq 'select(.WORKFLOW=="backup_activation")'
```

The slow loop (`daemon._log_diagnostics_snapshot`, ~5 min cadence) logs a
`diagnostics_snapshot` DEBUG entry every pass: AP status and associated-client
count, every WAN atomic's kind/mode/health, and the raw `nft list ruleset` /
`tc -s qdisc show` / `ip rule show` / `ip route show table all` text —
`enforce/`'s own readback, not a second shell-out (`enforce` is the only
module permitted to invoke `tc`/`nft`/`ip`). DEBUG is off by default
(`WIFUCKED_DEBUG` gates the root logger level in `logging.py`); set it in the
unit's environment (`systemctl edit wifucked.service`, add
`Environment=WIFUCKED_DEBUG=1`) before you need it, since it can't retroactively
recover a window you didn't capture:

```bash
journalctl -u wifucked -n 500 -o json | jq 'select(.WORKFLOW=="diagnostics_snapshot")'
```

The same fields, structured, also come back from
`curl -s localhost:8080/api/diagnostics/bundle` as `radio_state.json`,
`nft_ruleset.txt`, `tc_qdisc.txt`, `ip_rule.txt`, and `ip_route.txt` — useful
when the dashboard is reachable but you want one file to attach rather than a
log query.

`journalctl` is RAM-only on this image (`Storage=volatile` in `setup_rpi.sh`, to
protect the SD card) — it is lost the moment the device is power-cycled, which is
often exactly when you'd want it, and it says nothing about a boot early enough
that the daemon or a console isn't up yet. `wifucked-firstboot` and
`wifucked-bootcount` write their own output to a persistent file for that reason:

```bash
cat /var/log/wifucked-boot.log
```

Because it is on disk, this also survives a device you cannot get a console on at
all: pull the SD card, mount its root partition on another machine, and read it
directly.

There is also, for now, `wifucked-console.service` — a live status snapshot plus
a streaming `journalctl -f` pushed to the HDMI output on `tty1`, for a device
whose network/AP path can't yet be trusted enough to debug any other way. This is
**temporary bring-up scaffolding, not a supported diagnostic surface** — it
trades away SD card survival (ADR-010) and the "ACT LED is the only status
channel" design in the same way the persistent boot log above does, just more so
(continuous writes, a monitor a shipped device won't have). See `TODO.md` item 2
for what removes it and when.

The decision journal exists precisely so that "why did it activate BACKUP?" is a
lookup rather than an investigation ([ADR-009](../adr/ADR-009-decision-records.md)).
If it does not answer the question, that is a logging defect worth fixing in the
same session — file it.

## Diagnostics bundle

For anything you cannot resolve at the console, or anything a user reports:

```bash
curl -s localhost:8080/api/diagnostics/bundle -o bundle.tar.gz
```

Contains logs, decision journal, telemetry rollups, kernel network state
(`tc qdisc show`, `nft list ruleset`, `ip rule`, `ip route show table all`),
radio state, and `/etc/wifucked-release`. **No credentials and no payload data** —
it is safe to attach to an issue. Verify that stays true if you extend it.

## Inspecting kernel state

The daemon's model and the kernel's reality can disagree — that disagreement is
usually the bug. `enforce/` reconciles them, so a persistent divergence means
reconciliation is not seeing something.

```bash
tc qdisc show                      # is CAKE where you expect, at what rate?
tc -s qdisc show dev wlan0         # drops, backlog — bufferbloat evidence
nft list ruleset                   # marks and classification
ip rule show                       # policy routing
ip route show table all            # one table per atomic
iw dev                             # actual radio state: interfaces, channels
wg show                            # tunnel: handshakes, transfer, endpoint
```

Compare against `/api/state`. Where they differ, you have found something.

## Radio problems

The most driver-dependent part of the system, and the least predictable.

```bash
iw dev                             # interfaces and their channels
iw dev wlan0 info
dmesg | grep -i brcmfmac           # firmware and driver complaints
hostapd_cli status                 # AP state, channel, connected stations
hostapd_cli list_sta               # who is associated
```

**Check the spike findings before theorising.** [`../radio-spike.md`](../radio-spike.md)
records what this chip actually does — multi-BSS, AP+STA concurrency, CSA — as
measured, not assumed. A behaviour contradicting the findings means either a
firmware difference worth recording, or a bug.

If you learn something new about the radio, **add it to the spike findings in the
same session.** That document is the team's shared model of the hardware, and it
is only worth anything if it stays accurate.

**Also check [`../active-tests.md`](../active-tests.md).** Some radio behaviour
(SHARED-profile CSA, notably) is live in the real control loop today even though
nobody has confirmed it works — the spike hasn't run, but the code doesn't wait
for it. If you're debugging something in that file's entries, you may be the
first person to actually observe the result: update the entry with what
happened before you move on, whether it worked or not.

## Reproducing field conditions on a desk

Most control-loop bugs are reproducible without a van, and reproducing beats
speculating.

```bash
# Degrade a link realistically: latency, jitter, loss
sudo tc qdisc add dev wlan0 root netem delay 700ms 200ms loss 17%

# Constrain capacity
sudo tc qdisc add dev wlan0 root tbf rate 1400kbit burst 32kbit latency 400ms

# Remove a WAN abruptly, the way a van does
sudo ip link set wlan0 down
```

Then watch the decision journal and confirm the system reacted the way the
scenario tests say it should. **If you can reproduce it, write it as a scenario
test** ([SOP-003](SOP-003-testing.md)) — a hardware bug that becomes a mock-level
regression test will never come back.

## Spikes

When behaviour genuinely cannot be determined from documentation or existing
findings, do a spike rather than guessing in a PR.

A spike is: timeboxed (default one week), on real hardware, with a written
findings document as the deliverable. It answers specific questions decided
in advance. Its code is throwaway and is not merged.

Spike output amends the relevant ADRs
([SOP-007](SOP-007-architectural-decisions.md)) — this is one of the main legitimate
reasons to supersede one. [`../radio-spike.md`](../radio-spike.md) is the worked
example and the template.

## Safety on real hardware

- **Never test OTA from a clean image only.** Upgrade from the previous release is
  the path users take, and the one that breaks
  ([SOP-008](SOP-008-release-and-ota.md)).
- Keep a known-good SD card. Recovery beats debugging a bricked device.
- If you change the AP configuration on a device you are connected to over its own
  Wi-Fi, you will disconnect yourself. Use serial or Ethernet-over-USB for
  radio work.
- The appliance sees all user traffic. When capturing packets, capture what you
  need, and delete it when done. Never attach a capture to an issue.
