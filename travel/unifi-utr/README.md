# UniFi UTR — "be at home" while bonded

The UTR is the **user-facing LAN**. Zippie is the **WAN engine**.

## Topology

```
Internet WANs (Starlink / T-Mo / VZ)
        |
   Zippie client (Pi / MT3000)
   LAN: 10.50.0.1/24
        |
   UTR WAN port (DHCP client)
        |
   UTR LAN / Wi-Fi (your travel UniFi SSID)
        |
   laptops, phones, work gear
```

## Why this split

- UTR keeps UniFi adoption, guest policies, VLAN habits
- Zippie can be rebooted/replaced without redoing Wi-Fi for humans
- Optional site-to-site: UTR (or a home UniFi WG server) bridges LAN resources

## Site-to-site options

### A) Full-tunnel only (simplest)

Zippie `AllowedIPs = 0.0.0.0/0` → all internet appears to come from the home uplink.  
LAN resources at home (10.0.0.0/8 etc.) work if the home bond server routes/NATs them — by default NAT exit only hits the public internet. For home LAN access, add:

- On home bond server: routes to home LAN + no NAT for those destinations, **or**
- Separate site-to-site WG from UTR to home UniFi gateway

### B) UTR site-to-site into home UniFi (recommended for NAS/printers)

1. On home UniFi: create WireGuard/Teleport/site-to-site server
2. On UTR: configure client to home, allowed IPs = home LAN CIDRs only
3. Keep Zippie as default internet exit

Result: internet via the bonded home exit, LAN like you're on the couch.

## Checklist

- [ ] Pi/MT3000 LAN shares DHCP to UTR WAN
- [ ] UTR WAN gets DHCP from Zippie LAN
- [ ] UTR clients reach internet (`1.1.1.1`)
- [ ] `curl ifconfig.me` from a UTR client shows **home public IP**
- [ ] (optional) ping home NAS over site-to-site
