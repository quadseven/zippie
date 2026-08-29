# Zippie + Tailscale + k8s-oke

Zippie's **data plane** and your **tailnet** solve different problems. Use both.

## What rides where

| Plane | Mechanism | Why |
|---|---|---|
| Travel multipath data | WireGuard UDP → home public IP or a cloud public worker | Must work when you are *off* the tailnet (airplane Starlink, hotel Wi-Fi, phone hotspots) |
| Ops / SSH / dashboard | Tailscale | Manage the home exit and travel kit without opening SSH to the world |
| Cluster services | `*.ts.example-home.invalid` via Caddy on k8s-oke workers | Unrelated to bonding; already how you expose apps |

Tailscale alone is **not** a substitute for Zippie while traveling: MagicDNS and peer-to-peer assume the travel kit can reach coordination and peers. Bonding is for "I have three bad last-miles and need one good exit."

## Recommended home exit hosts (from your tailnet)

Good `zippie-home` candidates (always-on, home LAN or public):

| Host | Why |
|---|---|
| A LAN worker node | Home LAN, fat NIC, always on - **best default** for the residential exit |
| A small always-on box (Raspberry Pi or similar) | Light always-on; fine if CPU/NAT load is modest |
| A cloud Kubernetes worker | Public path - optional **cloud failover exit** when home power dies (different public IP, not the residential one) |

Port-forward on the home gateway:

```
UDP 51820-51823 → <zippie-home LAN IP>
```

Dynamic DNS → that public IP. Travel clients only need the DDNS name; they never need Tailscale for the bond itself.

## Ops over Tailscale

Once home is up:

```bash
# from your MBP on the tailnet
ssh you@your-home-host
sudo zippie-home show
sudo wg show pb-home0

# travel kit dashboard - loopback-only, so the tailscale IP does NOT serve it.
# A tailnet address is a real interface address; a 127.0.0.1 bind excludes it
# the same way it excludes the LAN. Tunnel instead:
ssh -N -L 8787:127.0.0.1:8787 pi@<travel-pi-tailscale-ip>
# then open http://127.0.0.1:8787 locally
```

Optional later: advertise a Tailscale **subnet router** on the travel bond LAN so home can reach UTR clients — orthogonal to multipath exit.

## k8s-oke interaction

Zippie does **not** need to run inside the cluster for v1.

- Home exit = host network process (systemd) on Unraid/Pi/OCI VM
- If you later containerize `zippie-home`, it needs `NET_ADMIN`, `/dev/net/tun`, host ports UDP 51820+, and must not fight flannel/cilium for default routes on workers

Avoid running the full-tunnel **travel agent** on a k8s-oke worker: changing default routes on a cluster node will break CNI.

## Failover story (future slice)

```
primary exit:   the home uplink (zippie-home on the LAN worker)
backup exit:    OCI worker public IP (second zippie-home)
client config:  two endpoint candidates; agent fails over if home probes die
```

That is the natural bridge to k8s-oke without putting bonding inside the mesh.
