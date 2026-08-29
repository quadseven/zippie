# AGENTS.md

For anyone changing this code, human or model. It is deliberately short: the
reasoning lives in `ARCHITECTURE.md` and `DECISIONS.md`, and this file's job is
to stop you walking into the things that have already stranded a router.

Read `CONTEXT.md` first for the vocabulary, then `ARCHITECTURE.md` for the
tour. `DECISIONS.md` records why things have the shape they have; each entry
names the failure that caused it.

## The one thing that makes this repo different

**The travel router's only uplink is the bond this code implements.** Its
default route is `dev pbz0`. There is no separate management path, no WAN
sitting there as a backstop, and no console you can reach from anywhere else.

So a change that breaks the agent removes the router's own route to the
internet, and with it every way of fixing it. That has happened. It is why the
deploy has the shape it does, and why the rules below are not ceremony.

## Before you touch anything under `travel/`

**Merging to `main` deploys to a live router.** `deploy.suzu.yml` triggers on
`travel/**` and `scripts/deploy-openwrt.sh`. There is no staging device. If the
operator is travelling, they are travelling on the thing you just changed.

**The rollback must fire before you rely on it.** `travel/gl-mt3000/deploy-rollback.sh`
is armed before the change and disarms itself on success. The discipline that
matters is not arming it - it is deliberately firing it while nothing is broken,
confirming the router comes back, then re-arming and making the real change. A
rollback path that has never executed is an assumption.

Three specific traps, each found the hard way:

- `setsid` and `nohup &` do not survive on this hardware, busybox has no
  `date -d "+N min"`, and backgrounded sleeps are unreliable over long windows.
  The only dependable mechanism is a self-removing cron one-shot at a fixed
  wall-clock minute - and **read the crontab line back**, because a malformed
  line is accepted silently and never fires.
- An ssh-driven restart cannot survive the link it tears down. Hand the restart
  to the router's own cron.
- "The new files are on disk" is not "the new agent is running". Fingerprints
  are computed from files, so an old process reports the new fingerprint. Gate
  on the process, not the digest.

**A scrubbed placeholder that reaches the router fails silently.** This is the
one that nearly ended a trip: a scrub replaced the home endpoint with
`dns-e.example-home.invalid`, that config reached the router, and RFC 2606
guarantees `.invalid` never resolves. At home it was invisible, because a leg
on the house network uses `lan_endpoints` instead. Away from home, nothing
would have matched and the bond would have had nothing to dial.

`scripts/deploy-openwrt.sh` now refuses to ship reserved-range values. Do not
work around it. If a value is dialed, resolved or compared at runtime,
parameterize it or read it from the router - never substitute a plausible fake.

## The datapath is shipped twice, and the copies must match

`travel/bond-agent/zippie/transport.py` and
`deploy/oke/zippie-home/zippie-pkg/transport.py` are the same file for the
travel end and the home end. `deploy.oke-manifests.yml` enforces that the
running pod matches the commit, and the file list is held *equal* to the
`configMapGenerator` in `deploy/oke/zippie-home/kustomization.yaml` rather than
merely overlapping.

**Adding a module means editing both**, plus `test_manifest_copy_in_sync.py`.
A module that ships without being listed is not drift-checked, so nothing will
tell you the pod is running something other than what you committed.

Merging anything under `deploy/oke/**` rolls the home pod - which is the far
end of the bond. Expect the tunnel to drop while it restarts. Measured
recovery has ranged from ~95 seconds to ~6 minutes.

## Datapath authentication

`travel/bond-agent/zippie/auth.py`. Four rungs: `OFF`, `OBSERVE`, `SIGN`,
`REQUIRE`.

**`OFF` is the zero value, deliberately.** An unconfigured transport emits
byte-identical v2 frames, and a test pins that - so the code can land without
changing anything on the wire, and the rollout is a config change rather than a
deploy. Both ends must never be more than one rung apart; home moves first.

Residual risks are documented in the module rather than glossed: there is no
replay protection (the MAC cannot cover a NAT-rewritten source address), an
attacker who knows the current epoch can hold the takeover window shut against
a genuine restart until `REQUIRE`, and there is no forward secrecy or automatic
rotation. Rotation has no key overlap yet, so rotating is an outage.

## Working on it

```bash
cd travel/bond-agent && uv run pytest      # agent tests, seconds, no hardware
```

Prove a guard fails without its fix. Run mutations with `python -B` and clear
`__pycache__` first: a same-second `.pyc` will otherwise serve stale bytecode
and report a mutant as killed when nothing ran.

## This repository is public

`scripts/check-no-operator-hosts.sh` and the "operator hosts ratchet" check
exist because a scrub removed the topology and nothing stopped it returning.
No real hostnames, addresses, SSIDs or personal identifiers - not in code, not
in comments, not in fixtures.

One filename still carries a host name deliberately: renaming
`deploy.suzu.yml` requires renaming two repository secrets and an SSM path in
the same change, and getting that wrong breaks the only remote deploy path to
a router that may be in another state. It waits for a window where someone can
watch a deploy round-trip.

## Related

[muster](https://github.com/quadseven/muster) exists because of a decision made
here: a device should fetch its own identity over its own credential rather
than have a deploy pipeline splice a static secret into its config. It does not
use mTLS, and its README explains why - if you are wiring this end to it, read
that before assuming a TLS handshake is available.
