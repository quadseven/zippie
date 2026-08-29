# GL.iNet GL-MT3000 (Beryl AX)

The compact bond client. As of 2026-07-27 this is the **primary** travel target,
not an alternative to the UniFi UTR: the UTR cannot emit 2.4 GHz and 5 GHz
simultaneously, so it was dropped and the MT3000 now does double duty as both
the bond client AND the AP your devices join.

That double duty is the whole design constraint. Read the radio budget before
planning paths.

## The radio budget (read this first)

The MT3000 has **two** radios (2.4 GHz + 5 GHz). A radio acting as a station
(client) can still host an AP, but **the AP inherits the station's channel** -
you do not get free channel selection on a radio that is also a WAN.

| radio / port | role | yields |
|---|---|---|
| 2.4 GHz | STA -> hotspot A (+ AP on that channel) | Wi-Fi WAN 1 |
| 5 GHz | STA -> hotspot B (+ AP on that channel) | Wi-Fi WAN 2 |
| GbE WAN | Starlink dish / hotel ethernet | WAN 3 |
| USB 3.0 | phone tether (RNDIS/NCM) | WAN 4 |

**Two Wi-Fi WANs is the ceiling.** A third SSID does not become a third path;
it becomes one radio thrashing between two SSIDs. The agent enforces this
explicitly (`wifi_uci.assign_radios`) and logs what it skipped rather than
pretending. More paths must come from the ethernet WAN or USB tethering.

If you want three-plus Wi-Fi WANs, you need a Pi with USB Wi-Fi adapters, and
the MT3000 becomes AP-only. See `../../docs/hardware.md`.

## Stock firmware approach (fastest to a working trip)

GL firmware already ships repeater, tethering, multi-WAN failover, and a
WireGuard client. That gets you **failover, not aggregation** - good enough for
day one and worth having as the fallback you can reach from a phone.

1. **WAN1**: repeater to hotspot A
2. **WAN2**: tether or second-radio repeater to hotspot B
3. One WireGuard client to home (`pb-home0`) from a Zippie client bundle
4. WireGuard as the default route / full tunnel
5. GL multi-WAN handles uplink failover *underneath* the single tunnel

One tunnel over a failing-over uplink means every failover breaks existing
connections. Zippie's multi-tunnel mode exists to avoid exactly that.

## Full Zippie multipath on OpenWrt

### 1. Enable SSH, then install packages

This exact set was installed and verified on the live device 2026-07-27
(~2.9 MB of downloads, ~10 MB on `/overlay`):

```sh
opkg update
opkg install python3-light python3-logging python3-urllib python3-email python3-codecs
# wireguard-tools + kmod-wireguard are ALREADY present on GL 4.8.1 - check first
opkg list-installed | grep -E 'wireguard|iwinfo|ip-full'
```

Package notes, each learned by hitting it:

- `python3-light`, not full `python3`. But light alone is not enough:
  - `python3-logging`, `python3-urllib` - the agent imports both.
  - `python3-email` - `http.server` (the dashboard) pulls it in.
  - `python3-codecs` - **not optional.** Without it the dashboard dies with
    `LookupError: unknown encoding: idna` inside `socket.getfqdn()`. Easy to
    miss because the agent gets all the way to starting up first.
- `wireguard-tools` and `kmod-wireguard` ship with GL 4.8.1 already; so does
  `iwinfo`. Check before spending cellular data on them.
- `iwinfo` is what the agent reads SSIDs with. There is **no `iwgetid`, no
  `iw`, and no `nmcli`** on this platform - see the SSID note below.
- **No NetworkManager.** OpenWrt has no `nmcli`; Wi-Fi lives in UCI. The agent
  detects this and uses its UCI backend (`zippie/wifi_uci.py`). Before that
  backend existed, SSID auto-join silently did nothing on this hardware while
  the agent still reported healthy.

### 1b. Python is 3.9, and that is not a choice

GL 4.8.1 is built on OpenWrt 21.02, which ships **Python 3.9.15** - EOL since
October 2025. The feed has no newer build, and packages are ABI-matched to that
toolchain, so 3.11+ is not available without GL rebasing their firmware.

The agent therefore targets **>=3.9** (`pyproject.toml`), not 3.11. The only
real 3.11 dependency was stdlib `tomllib`; `config.py` already falls back to
`tomli`, which is NOT packaged for OpenWrt. Vendor the pure-Python files:

```sh
# on your laptop - ship ONLY the .py files, never the compiled .so wheels
uv pip install --target /tmp/tv tomli
tar czf - -C /tmp/tv tomli/*.py | ssh root@<router> 'tar xzf - -C /opt/zippie-agent'
```

### 2. Create the station network

Stations attach to a netifd network that must already exist:

```sh
uci set network.wwan=interface
uci set network.wwan.proto=dhcp
uci commit network
/etc/init.d/network reload
```

### 3. Install the agent

**Use the script. Do not copy files by hand.**

```sh
# from the repo root
scripts/deploy-openwrt.sh <router> --dry-run   # what would change
scripts/deploy-openwrt.sh <router>             # deploy, then prove it
```

Hand-copying is how the deployed agent drifted. On **2026-08-06** six of
nineteen modules on the live router differed from the repo, `telemetry.py` was
three days stale and owned by uid 501 rather than root, and five metrics that
shipped Datadog monitors already queried were not being emitted at all. One of
those monitors had been in Alert for days as a direct consequence. Throughout,
`/api/status` reported `"version": "0.1.0"`, because that string is a
hand-edited constant and therefore cannot be wrong.

The script exists to make that state impossible to reach quietly. It refuses to
deploy an uncommitted tree without `--allow-dirty`, verifies the bytes on the
router equal the bytes here **before** restarting anything, records what it
installed in `/etc/zippie/build.json`, and then re-reads `/api/status` to
confirm the RUNNING agent reports the fingerprint it just installed. A
mismatch at any of those points is a hard failure, not a warning.

**Why it pipes tar rather than using `scp`:** `scp` does not work against this
device at all. dropbear ships no SFTP server, and OpenSSH 9+ `scp` speaks SFTP
by default:

```
ash: /usr/libexec/sftp-server: not found
scp: Connection closed
```

A short write over that pipe is silent, which is exactly why the script hashes
the result instead of trusting the exit code.

**Checking what a router is running**, without deploying anything:

```sh
ssh root@<router> "PYTHONPATH=/opt/zippie-agent python3 -c \
  'from zippie import build; print(build.build_info())'"
```

`matches_deploy: false` means someone edited the running copy after it was
deployed. `null` means there is no deploy record to compare against.

<details>
<summary>The manual procedure this replaced (reference only)</summary>

```sh
# from zippie/travel/bond-agent
ssh root@<router> 'mkdir -p /opt/zippie-agent'
tar czf - zippie/*.py | ssh root@<router> 'tar xzf - -C /opt/zippie-agent'
```

This is what drifted. It verifies nothing, records nothing, and leaves no way
to tell afterwards what is running.
</details>

There is no `pip` in `python3-light`, and none is needed - the agent is stdlib
only, so run it straight off `PYTHONPATH`:

```sh
ssh root@<router> 'PYTHONPATH=/opt/zippie-agent python3 -m zippie.cli status'
```

Config goes to `/etc/zippie/zippie.toml` the same way (`cat | ssh`).

### 4. Import and start

```sh
zippie import /etc/zippie/client.json
/etc/init.d/zippie enable
/etc/init.d/zippie start
zippie status
```

### 5. Install the dead-man watchdog

**This step was undocumented until 2026-08-01** and the watchdog was hand-installed,
so the running copy could drift from git with nothing to catch it. Install it
from the repo, never by editing in place:

```sh
# from zippie/travel/gl-mt3000
cat watchdog.sh | ssh root@<router> 'cat > /etc/zippie/watchdog.sh; chmod 0700 /etc/zippie/watchdog.sh'
# verify the transfer - dropbear has no SFTP and a truncated copy is silent
wc -l watchdog.sh
ssh root@<router> 'wc -l < /etc/zippie/watchdog.sh; sh -n /etc/zippie/watchdog.sh && echo SYNTAX_OK'

# cron entry (idempotent)
ssh root@<router> 'grep -q zippie-watchdog /etc/crontabs/root || \
  echo "* * * * * /etc/zippie/watchdog.sh >/dev/null 2>&1 # zippie-watchdog" >> /etc/crontabs/root
  /etc/init.d/cron reload'
```

What it does, in one line each:

- Pings two NextDNS anycast addresses once a minute. Three consecutive failures
  tear zippie down, because netifd's per-WAN routes underneath ours restore
  service by themselves.
- After a trip it re-arms **automatically** once the internet has been
  reachable for 10 consecutive checks - but at most twice per 24h. Exhausting
  that budget is the genuine flapping case and stays down for a human (#2137).
- Trip, re-arm, re-arm failure, and budget exhaustion all emit Datadog events
  (`aggregation_key: zippie-watchdog`). Verified live: the events API returns
  HTTP 202. A bond that is down must never be silent - both 2026-08-01 outages
  were invisible except as a 502 on the console.

State lives in two places on purpose: the re-arm budget in `/etc/zippie`
(overlayfs, survives reboot, or a reboot loop would reset the cap and re-arm
forever) and the fail/stable counters in `/tmp` (tmpfs, cleared on reboot,
which is correct because zippie starts clean after a boot anyway).

The state machine is covered by `tests/test_watchdog_rearm.py`, which runs the
real script against stubbed `ping`/`pgrep`/init.d. It is tested that way
because proving the re-arm by hand means killing the agent, and the agent
carries the bond that the SSH session driving the test rides on - the test
kills its own harness.

## Who owns the WANs: mwan3 or the agent?

**Pick one. Do not run both.** They both write default routes and both believe
they are authoritative; run together, the symptom is a default route that flaps
on every health check.

- **Agent owns routing (recommended for Zippie):** do NOT install `mwan3`.
  The agent builds the weighted multipath default route itself
  (`ip route replace default nexthop ... weight N`), which is what makes
  `aggregate` mode work at all. mwan3 cannot express per-path WireGuard tunnels.
- **mwan3 owns routing:** Zippie degrades to the stock single-tunnel failover
  above. Choose this only if you want GL's UI to stay in charge.

GL firmware may enable mwan3 for its own multi-WAN UI. Check
`/etc/init.d/mwan3 status` before starting the agent.

## Sections the agent owns

The UCI backend only writes sections it named itself, prefixed `pb_`
(e.g. `wireless.pb_STARLINK`). A GL.iNet factory repeater profile on the same
radio is never disabled by us - pinned by
`tests/test_wifi_uci.py::test_try_join_never_disables_sections_we_do_not_own`.

To see what the agent created:

```sh
uci show wireless | grep '\.pb_'
```

## Interface naming

GL interface names vary by mode (`wwan0`, `wlan-sta`, `apcli0`, `eth0`, `usb0`).
Prefer **SSID match** in `zippie.toml` over hard-coded interface names.

## CPU / throughput

WireGuard on the MT3000 handles typical travel rates (tens to a few hundred Mbps
aggregate). If you routinely push multi-hundred Mbps bonded, move the bond engine
to a Pi 5 and use the MT3000 as AP only.

## Surveyed on the real device (2026-07-27)

Read-only survey of the live unit ("TravelRouter", GL-MT3000, firmware 4.8.1, OpenWrt
21.02-SNAPSHOT, target mediatek/mt7981, kernel 5.4.211), reached over GoodCloud
remote SSH. These supersede the assumptions this file previously carried.

| check | result | so what |
|---|---|---|
| `mwan3` | **not enabled** | the agent can own routing uncontested |
| `ip -j addr` | **works** | the agent's JSON parsing is fine as-is |
| WireGuard | `kmod-wireguard` + `wireguard-tools` **already installed** | nothing to add |
| `/overlay` free | **138.9 MB of 159.8 MB** | ample room for python3 |
| python3 | **NOT installed**, and `/var/opkg-lists/` empty | needs `opkg update` first, over metered cellular |

Live interface map (`iwinfo`), showing AP and Client concurrent on BOTH radios:

```
apcli0    ESSID: unknown          Mode: Client  ch 3    PHY ra0     <- idle, free slot
apclix0   ESSID: "_17"            Mode: Client  ch 149  PHY rax0    <- phone hotspot
ra0       ESSID: "TravelRouter"           Mode: Master  ch 3    PHY ra0
ra1       ESSID: "TravelRouter-iot"           Mode: Master  ch 3    PHY ra0
rax0      ESSID: "GL-MT3000-000"  Mode: Master  ch 149  PHY rax0
rax1      ESSID: "HT_AP3"         Mode: Master  ch 149  PHY rax0
```

**The AP-inherits-the-station-channel rule above is confirmed, not theoretical:**
`ra0` (AP) and `apcli0` (client) both sit on ch 3; `rax0` (AP) and `apclix0`
(client) both on ch 149.

Live routing table - a working two-path setup already exists:

```
default via 172.20.10.1  dev apclix0 metric 20   <- phone hotspot (5 GHz station)
default via 26.98.216.66 dev eth2    metric 30   <- USB 4G dongle
```

With `apcli0` idle, that is **three** usable paths today: 5 GHz station, 2.4 GHz
station, and the USB dongle - without touching the ethernet WAN.

## IMPORTANT: the UCI station backend does NOT drive this firmware

`zippie/wifi_uci.py` creates stations as `wifi-iface` sections with
`mode='sta'`. That is correct for **stock mac80211 OpenWrt** (and a Pi), and
wrong here. GL's build is MediaTek proprietary (`iwinfo` reports `Type: mtk`)
and its stations are dedicated `apcli0` / `apclix0` interfaces owned by GL's own
repeater manager - `uci show wireless` on this device contains **no sta section
at all**.

So `auto_join_configured()` now **detects the MediaTek stack and refuses**,
rather than writing config that would look successful and join nothing. Until an
apcli-native joiner exists, join upstream SSIDs through the GL UI (or its
repeater config) and match them by SSID in `zippie.toml`.

Two bugs this survey caught, both of which would have shipped silently:

1. `scan_ssids()` was passing UCI device names (`mt798111`) to `iwinfo`, which
   takes **interface** names (`ra0`, `apcli0`). It returned an empty set, the
   caller read that as "nothing visible", and every join was skipped.
2. The `mode='sta'` model above, which does not exist on this driver.

Both are pinned by tests using output captured verbatim from this device.

## Status: the agent RUNS here (2026-07-27)

Deployed and executed on the live unit. It loads the config, recognises both
real WANs, and starts its dashboard:

```
zippie.agent: dashboard on http://127.0.0.1:8787
zippie.agent: zippie agent starting mode=aggregate paths=['hotspot', 'dongle4g'] home=...
zippie.agent: loop error: missing home server_public_key; import a client bundle first
```

That final line is the correct stopping point, not a bug: there is no home bond
server yet, so there is no client bundle to import and no peer key to build
tunnels against. **The client half is proven; the home exit is what remains.**

Live path set on the test bed (phone hotspot + USB 4G dongle, both cellular):

```
apclix0  172.20.10.2   ssid=_17   <- matched by SSID
eth2     26.98.216.65             <- matched by interface
apcli0   (idle)                   <- a third path slot, free
```

SSID matching only works because `net.wifi_ssid()` now understands `iwinfo`.
Before that it tried `iwgetid` / `iw` / `nmcli`, **none of which exist here**, so
every path reported `ssid=None` and SSID-matched paths could never bind.

Still open: `wifi_uci.py`'s UCI station backend cannot drive this firmware (see
above) - joining upstream SSIDs is still manual via the GL UI.
