# Freeing `srv-rpi4-01` — use **homedns**

Implemented as a full project: **[`/Users/operator/dev/homedns`](../../homedns)**  
Private repo target: `quadseven/homedns`.

## What you get

- UniFi DHCP DNS → **VIPs** `10.0.0.53` / `10.0.0.54` (never the Pi)
- Two **dnsfront** nodes: keepalived VRRP + **dnsdist** + **homednsctl**
- Backends: Unraid NextDNS VMs (+ optional rpi4 drain)
- Public last resort (Quad9 / CF) if every NextDNS dies
- Discord + Prometheus when the pool changes

## Cutover order

1. Deploy two dnsfronts (`homedns/scripts/install-dnsfront.sh`)
2. Point UniFi DNS at VIPs (`homedns/docs/unifi-cutover.md`)
3. Set `rpi4.enabled: false` in `homedns.yaml`
4. Re-role Pi as Zippie travel compute

## Relation to Zippie

Travel multipath ≠ home DNS. Zippie clients can keep tunnel DNS as `1.1.1.1` or home exit; the house stays up via **homedns** while the Pi is in a backpack.
