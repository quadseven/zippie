# Zippie Companion: design system

Recorded from the built app, 2026-08-05. Ground truth, not intention.

## The thesis

This app answers "is it working, and on what" in one glance from a car
dashboard. It refuses the hero-metric template every bonding app ships -
enormous throughput number, small unit label, a row of supporting stats -
because speed is not the question at 70mph, and a number cannot say whether
YOUR phone is one of the legs.

The hero is a sentence, because the answer is a sentence.

## Light-first, from the use scene

Read on a dashboard in daylight far more often than in the dark, so the light
rendition is the one that was designed. A dark UI in direct sun is a mirror.
Dark mode is fully supported because iOS demands it, and both were inspected.

## Color

**Restrained**: near-white ground, near-black text, ONE accent.

| Role | Light | Dark | Meaning |
|---|---|---|---|
| ground | white 0.985 | white 0.055 | the page |
| primary | white 0.08 | white 0.96 | text |
| secondary | tinted slate | tinted slate | supporting text, never neutral gray |
| rule | white 0.88 | white 0.20 | hairlines, the structural device |
| live | #1757D6 | #70A3FF | **carrying traffic right now** |
| degraded | amber | amber | holding on, not failing |
| down | red | red | not carrying |

The accent means exactly one thing: traffic is moving. Spending it on
decoration would cost the app its single unambiguous signal. Degraded is amber
rather than red because a leg holding on is not a failure, and crying wolf in a
car is worse than saying nothing.

Secondary text is tinted from the ground rather than neutral gray - gray on a
warm ground reads as dirty at low contrast.

## Type

System SF, used properly rather than replaced. A display face here would be
costume; a system stack is the right workhorse for a task surface. What earns
the craft is scale, tracking, and tabular numerals.

- **Display** 40pt semibold, tracking -0.8. The state sentence, and the only
  thing at this size.
- **Section** 15pt semibold. Not tracked-out micro caps, which is the eyebrow
  habit wearing a different hat.
- **Label** 17pt medium, **Body** 17pt regular, **Caption** 13pt.
- **Figures**: monospaced DIGITS on every changing number, so values do not
  jitter as they update. Not a mono face - that would be technical costume.

## Structure

**Hairlines and space. No cards.** A card around each row would add three edges
and a shadow to say what the whitespace already says; nested cards would be
worse. One horizontal margin (20pt) that everything aligns to, including the
rules, so the page has one left edge.

Spacing rhythm: 4 / 8 / 12 / 16 / 24 / 40 / 56, with more space above a heading
than below it.

Hairlines are `1 / UIScreen.main.scale`, not 1pt - a 1pt "hairline" on a 3x
screen is three device pixels and reads as a border.

## Motion

**One authored moment.** The traffic split settles over 0.55s with an
exponential ease-out, from an already-visible default. Nothing animates in from
nothing, because the data was always there. A glance at a moving bar reads as
live rather than as a redraw. There are no other animations.

## The rule that outranks the aesthetics

**Never state something that has not been measured.**

Every failure this system has had reads as "connected but carrying nothing" - a
leg UP on keepalives while delivering zero. So:

- The headline derives from DELIVERED traffic, not from whether a socket exists.
- A stale report says so rather than showing its last good value as current.
  Staleness is owned by `RelayStatus`, not redefined in the view layer.
- An unmeasured value is ABSENT, not a placeholder dash. The app has no RTT for
  the relay leg, so that column is empty rather than showing "--".
- The traffic bar shows sent-versus-received, which is measured. It replaced a
  share bar that was always 100% with one leg - a full-width slab that said
  nothing while being the loudest thing on screen.
- Section headers claim only what the app can see, and the heading FOLLOWS THE
  SOURCE. With the router answering it reads "Connections in the bond"; without
  it, "What this phone carried" over the single row this device can observe. One
  fixed heading would be wrong half the time, and wrong in the flattering
  direction.
- The freshness stamp follows the source too. It read "Updated 2661s ago" under
  router data five seconds old, because it always described the local relay
  report. Both numbers were individually correct; the pairing was a lie.
- The session total is suppressed while the relay report is stale. It is this
  phone's own byte count, which the router cannot see, and showing it beneath a
  live leg table presented a stopped relay's last count as current spending.
- Identity is evidence or nothing. A companion leg is marked "this phone" only
  when the endpoint the router dials matches this device's wifi address and
  listen port. Two rows read "iPhone (Verizon)" and "Co-operator iPhone (Verizon)";
  guessing would tell Co-operator her phone is helping while showing someone else's
  traffic.
- A leg that has carried nothing draws an EMPTY track. This was written down
  before it was true: the received capsule had no width of its own and expanded
  to fill, so a zero-traffic leg rendered as a full bar - the exact failure the
  component exists to expose, reintroduced by the layout meant to expose it.
  Only the rendered screen showed it.

## The three surfaces

All three are on this system; none is a stock `List`.

**Status** - the two-second question. A sentence at display size, the legs that
back it, what it cost. Shows the whole bond when the router answers and this
phone alone when it does not, and says which.

**Relay** - state, then the single control that changes it, then the numbers
backing it, then the endpoint, then the fallback. The previous version ordered
its five sections the way the features were BUILT: it opened on a control with
the state three sections down in grey `LabeledContent`. The foreground relay
and "remove VPN configuration" sit behind a deliberate tap - both are ways to
end up with a phone that looks configured and carries nothing, so they stay
reachable without being offered.

**Probe** - the verdict IS the page. It previously led with a button and buried
the finding under two rows of raw addresses, which is backwards for a tool whose
whole output is a verdict. The addresses stay underneath as the evidence: v1 of
this probe was confidently wrong (it read iCloud Private Relay exits as proof),
so showing the working is not decoration. A coloured rule carries the verdict's
tone rather than a seal or an octagon icon, which would restate the words in a
form that has to be learned.

## States rendered and inspected

Not reporting (no record), stale (record older than the threshold), no
cellular, standing by (connected, nothing carried), contributing, budget
exhausted, whole-bond-from-router, and console-unreachable. Light and dark.

Inspected by rendering in the simulator against the LIVE router's `/api/status`,
not against a fixture. Three defects survived code review and died on sight of
the screen: the full bar on a zero-traffic leg, the stale timestamp under fresh
data, and the stale session total. Reading the code found none of them.
