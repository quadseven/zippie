# CarPlay wired-Ethernet validation

The device-validation procedure #66 asked for. It exists because #64 and #65
fixed what could be fixed without a car: #64 proved the extension notices a
wired Ethernet adapter appearing or disappearing, #65 proved the client
tunnel rebuilds the affected legs when it does. Neither could prove the
actual reported failure - Maps freezing in CarPlay - goes away, because that
needs the real iPhone, the real adapter, the real head unit, and a real
Starlink connection degrading. A simulator run is not evidence here; see
"Out of scope" on #66.

## What you need on hand

Record the exact values below in the results - "an iPhone" and "an adapter"
are not evidence, the specific ones are.

- The iPhone under test: model, iOS version, Zippie build (`Build \(commit)`
  on the Bond screen's footer - see `BuildInfo.swift`)
- The USB-C Ethernet adapter: make/model, and how it is powered (passthrough
  charging changes whether the adapter stays up if the phone's own port is
  the only supply)
- The head unit CarPlay is running through (wired or wireless CarPlay - they
  exercise different iOS network paths)
- Starlink or whatever WAN the travel router is riding, and roughly how
  degraded it is at the moment of each cycle (a clear sky and a tree-blocked
  parking spot are different tests)

## The cycles to run

Run each cycle at least twice: Zippie **enabled** (client mode running) and
Zippie **disabled** (adapter alone, iOS's own routing). The comparison is the
point - #65 only matters if the enabled case now recovers where the disabled
case either recovers on its own or never had the problem to begin with.

1. **Ethernet inserted before the tunnel starts.** Plug in, start Zippie,
   confirm the wifi/cellular legs come up, THEN start CarPlay navigation.
2. **Ethernet inserted after the tunnel is already carrying.** Start Zippie
   first, start navigation, THEN plug in the adapter mid-drive - this is the
   scenario in #65's own issue text.
3. **Ethernet removed while carrying.** From either of the above, unplug
   without stopping Zippie or Maps.
4. **Repeat 2 and 3 with the WAN degraded** - the original report was
   specifically "Starlink signal is strong" and it still failed, so also
   capture a run where it is not strong, since a fix that only works on a
   good link is not the fix that was needed.

## What to capture during each cycle

- A timestamp for: adapter inserted/removed, Zippie relay verdict change (if
  visible in the app), and whether Maps re-drew or froze.
- Whether Maps recovers **on its own**, or only after the driver unplugs the
  adapter - that second case is the exact failure #65 exists to end, so it
  is the one result that actually falsifies the fix if it still happens.
- The extension's path-observation log line from #64
  (`path changed: ... status=... interfaces=...`) if reachable via Console -
  this is what proves the OS-level transition was seen at all, independent
  of whether the rebuild then worked.
- Whether the relay/tunnel needed a manual stop/start to recover, or came
  back by itself.

## Recording a result

Post the filled-in template below as a comment on #66 (or reopen it if
closed without one) - a verdict with no attached evidence is a verbal
report wearing a checkbox, which is exactly what #66's own acceptance
criteria refuse to accept.

```
## CarPlay Ethernet validation - <date>

Phone: <model, iOS version, build>
Adapter: <make/model, power source>
Head unit: <model, wired/wireless CarPlay>
WAN condition: <clear / degraded, how>

| Cycle | Zippie | Maps recovered? | Unplug required? | Notes |
|---|---|---|---|---|
| 1 - insert before start | on |  |  |  |
| 1 - insert before start | off |  |  |  |
| 2 - insert while carrying | on |  |  |  |
| 2 - insert while carrying | off |  |  |  |
| 3 - remove while carrying | on |  |  |  |
| 3 - remove while carrying | off |  |  |  |
| 4 - degraded WAN, insert | on |  |  |  |
| 4 - degraded WAN, remove | on |  |  |  |

Verdict: PASS / FAIL - <one sentence>
```

## Out of scope, restated from #66

No hardware changes as part of this procedure, and a simulator or unit-test
run does not satisfy any row above - the whole reason this document exists
is that #64/#65 already proved what could be proven without the car.
