# Zippie Companion

## What it is

An iOS app that makes a phone part of a bonded internet connection. Two
directions, and the app is the only place a person can see which one is
happening:

- **Contribute** - on the travel router's wifi, the phone lends its cellular
  to the router's bond. Other devices in the car get faster, more reliable
  internet because this phone is in it.
- **Client** - anywhere else, the phone bonds its OWN wifi and cellular back
  to the home lab (planned, #2247).

The bond itself is real infrastructure: a GL-MT3000 travel router running a
per-packet datapath that sprays traffic across every available link and
reassembles it at a home server. The phone is one leg among several.

## Who uses it

Two people, and that is the whole user base. The Operator, who built the
system and wants to see what it is doing. The Co-operator, who did not build
it and needs to know one thing: is it working, and is her phone helping or
not.

Neither is a network engineer while using it. Both are usually in a moving
car.

## The real scene

**A phone mounted on a car dashboard, in daylight, glanced at for two seconds
between other things.** Sometimes a passenger with more attention. Sometimes at
home on wifi, checked out of curiosity. Rarely at a desk.

This scene decides almost everything: legibility beats density, the answer must
survive a two-second glance, and the app is read in bright ambient light far
more often than in the dark.

## The questions it answers, in order

1. Is the bond working right now?
2. Which connections is it using, and is mine one of them?
3. Is my phone spending cellular data, and how much?
4. Why is something not working?

Question 4 matters more than its position suggests. Every failure this system
has had reads as "connected but carrying nothing" - a leg UP while delivering
zero. The app must never say a comfortable thing it cannot back.

## Product truths that constrain the design

- **A leg reading UP is not evidence it carries traffic.** Keepalives bypass
  the reassembler. Only delivered payloads prove a leg works, and the UI must
  reflect that distinction rather than flattening it into a green dot.
- **Cellular data is metered and someone else pays.** Budget state is
  first-class, not buried in settings.
- **Mode is chosen by the network, not the user** (SSID-based). The app's job
  is to make plain which direction traffic flows right now, never to offer a
  toggle that implies otherwise.
- **The relay is a dumb hop.** It never reads what it carries. That is a
  privacy property worth stating plainly in the UI, once.
- **Nothing is fabricated.** No placeholder numbers, no simulated graphs, no
  "connected" that has not been measured. An unknown value is shown as unknown.

## Constraints

- iOS 17+, SwiftUI, no third-party UI dependencies.
- Data comes from a Network Extension across a process boundary; it can be
  stale or absent, and both states must render honestly.
- Two-person internal TestFlight. No onboarding funnel, no marketing surface,
  no accounts.

## Explicitly not this product

- Not a VPN app. It does not sell privacy or location shifting; traffic exits
  at the user's own home.
- Not a speed-test app. Throughput is evidence, not the point.
- Not a dashboard for strangers. Two known users, deep context, no hand-holding
  copy.
