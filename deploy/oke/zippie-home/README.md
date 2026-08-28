# zippie-home - the bond server, as a k8s workload

The home exit for Zippie (#1986): terminates one WireGuard tunnel per travel
WAN and NATs the bonded traffic out the house.

**How a change here reaches the cluster:** push to `main` runs
`.github/workflows/deploy.oke-manifests.yml`, which diffs and then applies this
directory, and finishes by byte-comparing the ConfigMaps the running pod mounts
against the checkout. Read [`../README.md`](../README.md) before editing the
workflow or adding a manifest - it has the prerequisites and the reason this
directory was applied by nothing for a fortnight after the repo split
(infra#2266).

Runs as a **hostNetwork pod pinned to `k8s-oke-lan-srv-unraid-worker-01`**,
chosen over the alternatives for concrete reasons:

| candidate | why not |
|---|---|
| srv-unraid HOST | no `apt`, no `systemd`, no `python3` - `install-home.sh` cannot run |
| srv-rpi4-01 | the only host that satisfies the installer, and it is being retired (#1987 Phase 2) |
| the Macs | macOS WireGuard is userspace; installer is apt-based |
| **k8s hostNetwork pod** | **kernel WireGuard confirmed on the node; reuses the deploy path already in git** |

Kernel WireGuard verified on that node 2026-07-27: kernel
`6.12.0-202.76.4.4.el9uek.x86_64`, module loaded, and `ip link add wg-probe0
type wireguard` succeeds.

## Why hostNetwork (not a Service)

Three things all require it:

1. **wg must bind the node's real addresses.** A pod-network address is not
   reachable from the internet no matter what Service wraps it.
2. **MASQUERADE must happen on the node's WAN interface.** The wg-quick
   `PostUp` runs `iptables -t nat -A POSTROUTING -o <wan> -j MASQUERADE`; inside
   a pod netns that NATs the wrong interface.
3. **Return traffic to the LAN.** Clients behind the travel router should reach
   home LAN hosts, which means the node's own routing table, not the CNI's.

## YOU STILL NEED INBOUND UDP. Here is why, and the alternatives

WireGuard has no coordination server. The car is on cellular CGNAT and can never
accept inbound, so the HOME side has to be the reachable one - hence
UDP 51820-51823 forwarded to this node. That is not a limitation of this
manifest; it is how a bare WireGuard peering works.

The alternatives, honestly:

- **Tailscale instead of WireGuard.** Hole-punches and falls back to DERP, so no
  port-forward at all - but Tailscale picks ONE path per connection and will
  never aggregate two links. You get a home exit with no bonding, which the
  existing exit nodes already give you. It does not replace this.
- **Put the bond server somewhere with a public IP** (an OCI node). No home
  port-forward, bonding still works - but the exit is then OCI, not your FiOS,
  so you lose the low-latency-to-home property that motivated the design.
- **Hole-punch ourselves.** Doable, but it means building a rendezvous service
  to exchange observed endpoints - i.e. reimplementing a chunk of Tailscale.

If the port-forward is unacceptable, the OCI variant is the realistic one. This
manifest takes `ZIPPIE_HOME_ENDPOINT` as config precisely so the exit can move
without a rewrite.

## State is persistent ON PURPOSE

The server's private key and the peer list live in `/var/lib/zippie`. If that
is lost, **every already-provisioned travel client stops working** and has to be
re-bundled. It is a PVC, not an emptyDir, and the pod is node-pinned so a
local-path volume stays with it.

## Changing the wg config WITHOUT a rekey

`server.json` is the source of truth; `pb-home0.conf` is a rendering of it.
`up` re-renders the `[Interface]` stanza from `server.json` on every start and
carries the existing `[Peer]` blocks over verbatim (infra#2048), so a changed
`ListenPort`, `PostUp` line or MASQUERADE interface reaches an initialised
install on the next pod restart.

Before that it did not. `init` writes the conf only on a NEW install and then
returns early forever, so the only way to regenerate it was `init --force` -
which **rekeys the server and invalidates every provisioned client bundle**.
That is re-provisioning every travel device to change a port number.

What this does and does not cover:

- The keys are READ from `server.json`, never regenerated. `--force` is still
  the only thing that rekeys, and it still starts the peer list clean.
- Hand edits to the `[Interface]` stanza are discarded on the next `up`. Change
  the template in `zippie_home.py`, or the values in `server.json`.
- `up` does NOT bounce a running interface. The refreshed conf applies at the
  next bring-up; the log says so when it finds `pb-home0` already up.
- **The ConfigMap IS the config surface for `endpoint`, `ports` and
  `wan_iface`** (#36, 2026-08-09). `up` reconciles those three from the
  environment into `server.json` on every start, before the conf re-render, so
  a ConfigMap edit reaches the wire in ONE restart. It says which key moved and
  to what.

  Until then they only reached `server.json` on a FIRST init and `init` returns
  early forever after, so editing the ConfigMap and restarting changed nothing,
  silently - a surface that looked authoritative and was not.

  Two rules make this safe to run against a live exit:

  - **Absent or empty means "leave the stored value alone", never "use the
    default".** `init` takes `--ports` with an argparse default, so a pod
    started without the variable would otherwise reset live ports and drop
    every tunnel. A ConfigMap key present with no value is a common shape, so
    empty matters as much as missing.
  - **A malformed `ZIPPIE_HOME_PORTS` is refused whole, not in part.**
    Accepting the parseable half of `51910,notaport,51911` would silently
    shrink the port set, dropping the tunnels on the removed ports.

  Reconciling happens in `up` and NEVER in `init`, so no path was added that
  can touch key material - `--force` remains the only rekey.

## Bootstrap

`init` is idempotent - it refuses to rekey unless `--force`, which is what makes
it safe to run on every pod start:

```sh
kubectl -n zippie exec deploy/zippie-home -- \
  zippie-home add-client travel-glinet
```

That prints the client bundle. Get it onto the router (over the tailnet, since
the MT3000 has no sftp - pipe it, do not scp):

```sh
kubectl -n zippie exec deploy/zippie-home -- \
  zippie-home add-client travel-glinet \
  | ssh root@192.0.2.30 'cat > /etc/zippie/client.json'
ssh root@192.0.2.30 'PYTHONPATH=/opt/zippie-agent python3 -m zippie.cli import /etc/zippie/client.json'
```

## Known rough edge: the image

The container `apk add`s `wireguard-tools iptables bash python3` at start, so a
pod restart needs working internet on the node. That is a deliberate shortcut to
avoid blocking on a custom image build - the right fix is to bake one through
the existing `build.container` workflow and push it to
`registry.ts.example-home.invalid`. Tracked as a follow-up, not a permanent design.
