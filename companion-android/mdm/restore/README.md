# Rebuilding the Pixel after a wipe

Everything needed to put the phone back the way it was, captured 2026-08-15 from
the live Headwind install BEFORE any wipe. Written because a factory reset is the
only way onto Fleet (Device Owner is set at provisioning and only a wipe clears
it), and the settings below exist nowhere else.

This is deliberately MDM-agnostic. It records what the device should look like,
not how Headwind expressed it, because the whole point is that Headwind is going
away.

## Apps

What was actually installed, read off the device rather than from the MDM's app
list - that list was EMPTY, which is the defect in zippie#171.

Reinstall:

| package | what | where from |
|---|---|---|
| `app.zippie.companion` | the bond leg | built from this repo, signed release APK |
| `com.tailscale.ipn` | tailnet | Play Store |

Do NOT reinstall, these were Headwind's own and are what is being retired:

    com.hmdm.launcher
    com.hmdm.pager
    com.hmdm.emuilauncherrestarter

The rest of what was present (`com.google.android.*`) is stock and returns with
the OS.

## Managed configuration

Five keys, delivered to `app.zippie.companion`. **The values are not written
here.** Four are environment-specific and one is a credential; all of them live
in SSM, which is the source of truth.

| key | source |
|---|---|
| `consoleLanHost` | the router's LAN console, `<lan-ip>:8787` |
| `homeHost` | the home endpoint |
| `homePort` | the home endpoint's port |
| `announceToken` | SSM, and it must match the router's `/etc/zippie/state/console_token` |
| `autoStartRelay` | `true` |

`announceToken` is the one that will silently ruin a rebuild: a stale value does
not fail loudly, it just 401s on every announce and the phone never joins the
bond. Verify it matches rather than assuming, by comparing fingerprints and never
by printing either value:

```
# on the router
python3 -c "import hashlib;print(hashlib.sha256(open('/etc/zippie/state/console_token').read().strip().encode()).hexdigest()[:12])"
```

Confirmed matching on 2026-08-15 (`0620daf138ac`).

## Look and feel

The launcher was themed to match the app. `zippie-wall.png` in this directory is
the wallpaper, recovered from the router at `/www/zippie-wall.png`, 1080x2400,
sha256 `6674eddb4205579b5ee0a63b...`, byte-identical to what the device was
serving.

| setting | value | why |
|---|---|---|
| background | `#080b14` | `Ink.ground`, the app's own background |
| text | `#f2f4f8` | `Ink.primary` |
| wallpaper | `zippie-wall.png` | in this directory |
| brightness | `180` fixed | a relay phone sitting in a car should not be adjusting itself |
| icon size | small | |
| header | none | |
| screen timeout | 60s | |
| kiosk exit | enabled | |

**The kiosk exit password was `12345678`.** Do not carry that over. It is the
only thing standing between anyone holding the phone and a normal Android
desktop, and it is the weakest possible value. Generate one and put it in SSM
alongside the rest.

## Battery

Not an MDM setting and no MDM can replace it - `provision-device.sh` in the
parent directory sets the 80 percent charge cap, and that has to be re-run after
a wipe:

    secure.charge_optimization_mode=1
    adaptive_charging_enabled=0

Verify by reading them back. An adb setting that did not apply looks exactly like
one that did.

## Enabling adb again afterwards

Wireless debugging does NOT survive a reboot, let alone a wipe, and its port
changes every time it is enabled. Do not read the port off the screen and expect
it to keep working. Ask the network instead - Android advertises it over mDNS,
so anything on the LAN can discover the current port:

    _adb-tls-connect._tcp.local     the port to `adb connect`
    _adb-tls-pairing._tcp.local     the port `adb pair` uses, only while the
                                    pairing dialog is open

The router can be asked directly; see `adb-mdns.py` notes in zippie#168's
history. Pairing is also lost on a wipe, so the first connection after one needs
`adb pair` with the six-digit code, not just `adb connect`.
