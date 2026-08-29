# zippie - the words this project uses

Read this before you read anything else. The names below are used exactly, in
code, in logs, in issues and in conversation. When a word here appears, it means
this and nothing else.

## What zippie is for

A router travels. It must keep a household online using whatever uplinks exist -
a phone's cellular, a hotel wifi, an ethernet cable - and it must do so with
nobody watching it. The point is not speed. The point is that it recovers by
itself.

## The nouns

**home** - the endpoint at the house that every path terminates at. One address,
reached over the internet. `dns-e.example-home.invalid`.

**bond** - all the paths in use, treated as one connection. The bond is what the
household actually browses through.

**leg** - one path from the router to home. A leg can be an ethernet cable, a
station radio, or a phone. Legs come and go; the bond survives them.

**agent** - the zippie program on the router. It owns the legs, decides which
carry traffic, and installs the routes.

**relay** - the companion app on a phone. It is a DUMB HOP: it forwards frames
between the router and home over cellular. It never parses what it carries, so
the wire format can change without an app release.

**console** - the agent's status page and write API, on port 8787. Reads are
open. Writes need a token.

**announce** - how a phone tells the router it exists. The relay POSTs to the
console every 15s with a 45s lease. No announce, no leg.

**uplink** - anything the router can reach the internet through. The bond is one
uplink; an ethernet cable is another.

**watchdog** - a script that runs every minute on the router. It can stop the
agent when the router loses the internet. It has hurt more than it has helped
and is now heavily guarded - see below.

**datapath** - the per-packet machinery. `packet mode` sends each frame over a
chosen leg; `route mode` uses kernel routes.

**home hub** - the server side of the transport, in Kubernetes. It answers
keepalives and reassembles the stream.

## The verbs that matter

**carrying** - a leg is carrying when it is in the transport's link table AND
has weight above zero AND bytes are arriving. All three. A leg with weight and
no bytes is NOT carrying; that distinction has produced false passes twice.

**announcing** - a phone is announcing when the router logs
`leg announced name=...`. Announcing is not carrying. A phone can announce
perfectly while forwarding nothing.

**tearing down** - the watchdog stopping the agent and withdrawing its routes.

**re-arming** - the watchdog starting the agent again after a teardown.

## The rules learned the hard way

**A teardown needs two conditions, not one.** The agent's route sits at metric 1,
above other uplinks at metric 10+. Removing the agent reveals the route beneath.
That only helps if a route exists beneath. With a phone as the only uplink there
is nothing beneath, so a teardown removes the last path. Ask both: is anything
carrying, and is there anything to fall back to.

**Whoever cuts a link cannot restore it.** Any remedy or test that interrupts a
link must carry its recovery on the far side of that link. The router restores
its own ethernet from cron. The phone restores its own wifi from a detached
timer. A command sent across the broken link never arrives.

**Timers differ by platform, and it matters.** On the GL-MT3000 a backgrounded
`nohup ... &` DIES with the ssh session, so router-side timers use cron. On
Android the same pattern SURVIVES an adb disconnect - measured 2026-08-17, a
detached command ran 44s after adb and the tunnel were killed.

**Absence of a log line is not absence of the event.** logcat on the Pixel resets
to 256 KiB on every boot and is filled by unrelated system chatter within
minutes. `logcat -G` flushes the buffer it resizes. Read first, resize second.

**"Degraded" hides two different problems.** A leg that worked yesterday and is
struggling today, and a leg that has never once been answered, both read
`degraded`. They want opposite fixes: the first is the network, the second is
the endpoint the leg dials. Legs therefore carry `never_handshaked` beside
`state`, set when bytes have gone out, none have come back, and no keepalive has
ever been answered. Watch that flag, not the word.

**At home, the ethernet leg is EXPECTED to be dead.** the travel router is a travel router.
Plugged in at home its WAN sits on the house LAN, while the leg dials home by the
house's own public address - and that address is reachable from the internet but
not from inside the house. Both legs dial 203.0.113.33:51902: the phone's path,
arriving from the cellular side, has carried 392 MB at 2.5% loss, while eth0 to
the same address and port sits at 0 bytes and 100% loss. ICMP still answers in
under a millisecond, so a naive reachability check passes and tells you nothing.

The MECHANISM is not established. A NAT hairpin the edge router does not
implement, and an edge rule refusing LAN traffic addressed to its own WAN IP,
produce identical symptoms from where zippie can see. The fix is the same either
way - reach home by something other than the house's public address - so the
distinction has not been worth an outage to settle. This means the bond at home is single-legged on the phone, and that
"ethernet is plugged in" is a plain netifd default route underneath, not a second
working leg. Do not read a passing cold-boot test as proof the bond had two legs.

**Instruments sit behind the link under test.** adb reaches the phone through the
router; the console is served by the agent; Datadog needs the uplink. Any
diagnosis of a broken bond must come from something that survives the break -
which in practice means the router's own files and counters.

## Where things live

    travel/bond-agent/          the agent (python)
    travel/gl-mt3000/           router scripts: watchdog, guards, test harness
    companion-android/          the relay app
    companion-ios/              the iOS relay
    hub/                        home-side services
    deploy/oke/                 home hub manifests
    docs/coldboot-testing.md    how to test recovery without stranding a device

## The one sentence

If a leg can carry the household, the agent must be allowed to try, and nothing
may remove the last path on the assumption that something else is underneath.
