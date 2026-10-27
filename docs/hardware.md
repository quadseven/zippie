# Hardware guide (your kit)

## Recommended travel topology

```
[Starlink dish/router] --Wi-Fi--> \
[T-Mobile phone hotspot] --Wi-Fi--> +--> [ Zippie client ] --LAN--> [ UniFi UTR ] --Wi-Fi--> laptops/phones
[Verizon phone hotspot] --Wi-Fi--> /         Pi 5 or MT3000
```

### Raspberry Pi 5 (best Zippie client)

- Pi 5 8GB + official PSU + good SD or NVMe
- Onboard Wi-Fi for one path (e.g. Starlink)
- USB Wi-Fi adapters for T-Mobile + Verizon hotspots (prefer chipsets with good Linux AP/client support: MT76, ATH9K/10K, RTL8821/8832 — avoid flaky RTL8152-class Wi-Fi)
- Optional: USB Ethernet for a wired modem/hotspot

### GL.iNet GL-MT3000 (Beryl AX)

- Pocketable, Wi-Fi 6, solid OpenWrt-based UI
- Great as travel router with multi-WAN (repeater + tether + ethernet)
- Run Zippie agent via OpenWrt packages / scripts in `travel/gl-mt3000/`
- If CPU-bound on WireGuard at high rates, prefer Pi 5 for the bond and use MT3000 as AP only

### UniFi UTR

- Acts as the **familiar UniFi network** while away
- WAN port → Zippie client LAN (DHCP from Pi/MT3000)
- Optional: site-to-site back to home UniFi for true "I'm on my LAN"

## Home

| Piece | Recommendation |
|---|---|
| Bond server | Mini PC / NUC / Pi 5 with Ethernet to UniFi LAN |
| NIC | 2.5G if you want headroom toward a multi-gigabit uplink |
| Gateway | Port-forward UDP 51820–51823 → bond server |
| DDNS | UniFi DDNS or `cloudflared` / Route53 / DuckDNS to track the home public IP |

## Cabling tips

- Starlink router in bypass/bypass-ish if you want less NAT hairpin; not required for Zippie
- Keep phone hotspots on **AC power** while traveling (hotspot sleep kills paths)
- Name phone hotspots stably (`PHONE-TMO`, `PHONE-VZ`) so configs never churn

## Powering phone legs

A phone contributing its cellular runs the modem hot, continuously, with the
screen off. It must be on mains or a battery station, and **not on the
router's USB port**.

The MT3000 has one USB port, and at USB 3.0's 900 mA / 5 V it can supply about
**4.5 W**. That slows a relaying phone's drain; it does not beat it. Powering
phones from the router also serialises them behind a single connector and, for
Android, requires toggling USB tethering by hand every time - the tethering API
is `TETHER_PRIVILEGED`, so no app can enable it.

So phones join over **wifi** and charge from their own supply. What the cable
needs, given it carries power only:

- **USB-C to USB-C, 3A rated.** A Pixel 6a peaks near 18 W and a Pixel 7/8 near
  30 W, so 3A/60 W has large margin. An e-marker chip is only required above
  3A (5A/100 W) and buys nothing here.
- **Short.** 0.5-1 m. Resistance, not the printed wattage, is what determines
  charge rate and how much of the power is dissipated as heat in a hot vehicle.
  A short cheap cable beats a long thin "100 W" one for this job.
- **Both CC lines wired.** Without them PD never negotiates and the phone
  trickles at 5 V / 0.5 A.
- For **A-to-C** cables only: the pull-up must be **56k**. 10k or 22k is the
  known-dangerous defect - bin those rather than test them twice.

A cable claiming 100 W with no e-marker is mislabelled; it will run at 3A or
misbehave. Buy one spec of short C-to-C in quantity, so a phone charging badly
is diagnosable as the phone or the port rather than the cable.
