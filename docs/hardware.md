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
