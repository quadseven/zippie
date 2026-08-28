# Cold-boot testing

How to prove, unattended, that the router keeps the house online with the phone
as its only uplink - and how to do it without stranding a router you cannot
reach.

## What it tests

The ethernet is taken away and the phone has to carry the bond alone. Two
shapes:

- **phone cold** - reboot the Pixel, drop the ethernet while it boots
- **both cold** - reboot the Pixel AND the router, ethernet down across both

Both-cold is the scenario that failed on 2026-08-16: the watchdog tore down a
bond that was carrying perfectly, because it never asked whether anything was
underneath to fall back to (#188).

## The failsafe, and why it lives on the router

Whoever starts the test loses the ability to end it. With the ethernet down,
ssh to the router only works if the thing under test is working - so the
rollback cannot be a command someone sends. `autotest.sh` runs from cron every
minute and, before anything else, restores the ethernet if it is down with no
valid running test to justify it. A crashed script, a reboot mid-test, a corrupt
state file, a deadline nobody was watching: every one of them ends with the
ethernet back.

Verified 2026-08-17: `ifdown wan` with no test state, restored 43s later with no
human involved.

    04:29:20  ifdown wan
    04:30:00  FAILSAFE: wan down with no valid running test - restoring
    04:30:03  wan RESTORED

## Arming it

A DEPLOY DOES NOT ARM ANYTHING. The scripts are installed by
`scripts/deploy-openwrt.sh` and do nothing until cron entries exist, because a
deploy that started taking the ethernet away would turn shipping a change into
an outage.

    # the failsafe + state machine (required before any test)
    * * * * * /etc/zippie/autotest.sh >/dev/null 2>&1 # autotest-tick

    # optional: arm a test every 30 minutes, until the campaign deadline
    0,30 * * * * /etc/zippie/autotest-arm.sh >/dev/null 2>&1 # autotest-arm
    echo $(( $(date +%s) + 8*3600 )) > /etc/zippie/state/autotest-until

Read the crontab back after writing it. A backgrounded command over ssh dies
with the session on this router, so cron is the only reliable way to run
anything that outlives the connection.

## Driving a cycle

    scripts/coldboot-cycle.sh <n>            # phone cold
    scripts/coldboot-cycle-bothcold.sh       # phone AND router cold

Both refuse to start unless the phone leg is already carrying and the failsafe
cron is installed. Rebooting the phone when the bond is already dead is an
outage, not a test.

## What counts as a PASS

Bytes delivered, not weight. `effective_weight > 0` can be true while the phone
forwards nothing - measured 2026-08-17, the router read `w=64` seconds after a
reboot while the phone's own log said `dropped upstream: cellular not ready`
twelve times. A PASS requires `link_rx_bytes` to ADVANCE across three
consecutive checks.

## Reading a result

    /etc/zippie/state/autotest.log     verdicts, survives reboot
    /tmp/coldboot-trace.log            per-15s counters for the current boot

The trace answers the one question the router alone can settle: whether the
phone is sending anything back.

    ka_out>0, replies_in=0    the phone is silent; fault is phone-side
    ka_out>0, replies_in>0    the phone answers; fault is router-side
    ka_out=0                  the agent never transmitted

## Never cut the channel you need to recover over

The rule this cost the most to learn. On 2026-08-17 a walk-away test disabled
the phone's wifi with `adb` - and `adb` reaches that phone THROUGH its wifi. The
re-enable command had to travel over the link it had just severed, so it could
not land. Headwind could not help (`devices: 0`, nothing enrolled), Tailscale is
installed but never signed in, and the Pixel needed a physical tap - on the one
project whose entire purpose is never touching the phone.

The script even carried a comment saying "adb is gone with the wifi". Noticing
is not solving.

So: any test that interrupts a link must carry its recovery on the FAR SIDE of
that link, running independently of whatever started it.

    # WRONG - the enable cannot reach a phone whose wifi is off
    adb shell "svc wifi disable"
    sleep 120
    adb shell "svc wifi enable"

    # RIGHT - one detached command, executed on the phone, surviving the
    # adb disconnection it causes
    adb shell "nohup sh -c 'svc wifi disable; sleep 120; svc wifi enable' \
      >/dev/null 2>&1 &"

The router-side failsafe is the same principle applied to the ethernet: cron on
the device restores it, because whoever cut it cannot.

## Traps found the hard way

- `adb reboot` BLOCKS when the device disconnects under it. Background it.
- A reboot must be PROVEN: adb must answer first, and the phone must then stop
  responding to ping FROM THE ROUTER - this machine has no route to the phone's
  LAN address, so a ping here is meaningless.
- `logcat -G` FLUSHES the buffer. Read first, resize second.
- `ifdown wan` does not survive a router reboot; netifd brings it back. The tick
  re-asserts the cut, which is what makes both-cold testable.
- adb reports `failed to install` when the install SUCCEEDED, because it tunnels
  through the app that restarts. Believe `dumpsys package`, not adb's exit line.

## Drift: is the router running what main says?

`travel/gl-mt3000/drift-check.sh` answers this from the router, on demand or
from cron. It fetches the package as `main` has it, fingerprints both copies
with the same code the agent uses, and raises a Datadog event only on a real
mismatch.

    /etc/zippie/drift-check.sh          # exit 0 = same, 2 = drift, 1 = could not tell

Three decisions are load-bearing, all of them learned the same day:

- **The ROUTER asks, not CI.** Nothing in `.github` reaches the tailnet, so a
  workflow that curls the console would ship on an unverified assumption about
  runner networking. The router can reach github and already holds the Datadog
  credentials.
- **Fingerprint, not commit.** A commit comparison alarms on every docs-only
  merge - the router sat on `08d4368` while main was `a2f6c47` with a
  byte-identical agent; the difference was `CONTEXT.md`.
- **It fetches its own copy.** A fingerprint computed from a stale working
  checkout reported drift that did not exist. A checker that trusts a tree it
  did not just fetch will page about its own staleness.

A fetch failure is NOT drift. This router is often on a metered or absent
uplink, and reporting drift because github was unreachable would make the check
untrustworthy exactly when the bond is unhealthy.

NOT ARMED BY A DEPLOY, like the test harness. Add cron deliberately:

    17 6 * * * /etc/zippie/drift-check.sh >/dev/null 2>&1 # zippie-drift

## "Ethernet is plugged in" is not a second leg

At home suzu's WAN sits on the house LAN, while its ethernet leg dials home by
the house's own public address - which is reachable from the internet and not
from inside the house. Measured 2026-08-17 after nine hours of uptime:

    name            ethernet
    state           degraded
    link_tx_bytes   403618
    link_rx_bytes   0
    loss_pct        100.0
    last_error      no reply yet - nothing is answering at this leg's address

The leg has never carried a byte in its life. ICMP to the same address still
answers in 0.5 ms at one hop, so a ping-based reachability check passes and tells
you nothing.

The control is the phone. BOTH legs dial 203.0.113.33:51902 - the same host, the
same port. The phone's path reaches it from the cellular side and has carried
392 MB at 2.5% loss, and home has delivered 377 MB back through it. So home is
emphatically alive; what fails is specifically the path from inside this house.

The MECHANISM is not established, and this document should not pretend
otherwise. A hairpin the edge router does not implement, and an edge rule
refusing LAN traffic addressed to its own WAN IP, look identical from here. The
remedy is the same either way.

What this means for every test in this document:

- **The bond at home is single-legged, on the phone.** Not by configuration, by
  topology. Assume one leg unless the console proves otherwise.
- **Plugging the cable back in is a netifd fallback, not a bond repair.** What
  recovers is `default via <house-gw> dev eth0 metric 10`, underneath zippie's
  metric-1 route. Internet returns; the bond does not gain a leg.
- **A cold-boot PASS with the cable in did not test two legs.** It tested one leg
  plus a fallback route. Read results accordingly.

Watch `never_handshaked` rather than `state`: `degraded` covers both a leg having
a bad hour and a leg that has never worked, and only the second is a
configuration mistake. Making the leg actually work at home needs a route to
home that is not the house's own public address - split-horizon DNS or a
LAN-side home endpoint - which is a separate decision, not a test-harness fix.
