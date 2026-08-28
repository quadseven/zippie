# Branding: what to take from the sheet, and what to refuse

Proposal, 2026-08-19. **Nothing here is implemented.** It exists to be chosen
from. The one thing already shipped is the mark itself (#243).

## The tension worth naming first

The brand sheet is a good identity for a *product website*. `companion/DESIGN.md`
is a deliberate argument for something narrower: an instrument you read at a
glance from a car dashboard at 70mph. Those two want different things, and most
of the disagreement below is that one sentence.

The resolution is not a compromise. It is a **boundary**: the brand owns
everything somebody looks at while deciding, and the design system owns
everything somebody looks at while driving. A brand that crosses that line costs
the app its only unambiguous signal; a design system that crosses it back leaves
the product looking like a terminal.

    BRAND SURFACES          website, README, store listings, the hub landing,
                            the app icon, email, business cards
                            -> full palette, wordmark, Sora, the dot field

    INSTRUMENT SURFACES     the app's status screens, the widgets, diagnostics
                            -> tokens.json, ONE accent, system font, hairlines

## Take: the wordmark and lockup

There is no wordmark today, which is a real gap - the README, the hub and both
store listings currently have nothing to put at the top.

The sheet's lockup (mark + "zippie" in a rounded lowercase) works. Lowercase is
right: this is a household utility, not an enterprise product.

**Cost:** an SVG and a licence check on the face. **Risk:** none - no instrument
surface renders it.

## Take: the dot field

The strongest thing on the sheet after the mark itself. It is *texture*, so it
carries no meaning and therefore cannot collide with anything the UI needs to
say - which is exactly why it is safe where the palette is not.

Uses that earn it: the hub's landing background, empty states, the top of the
README, store screenshots. It should never appear behind live status.

## Take: purpose and pillars, as words

"Bonds cellular and internet connections together across iPhone, Android,
Starlink, and routers" is a better one-line description than anything currently
in the repo. Fast / Reliable / Unified / Everywhere are fine store-listing
pillars.

**Do not put the pillar icons in the app.** A shield that means "reliable" next
to a status light that means "carrying" is two visual languages arguing.

## Take: the ecosystem diagram

Section 10 explains the product better than the README does. Worth redrawing as
an SVG and putting at the top of the README, where somebody arriving cold
currently has to read three paragraphs to learn what bonding is.

## Refuse: Sora in the app

`DESIGN.md` uses the system face on purpose. Three costs, none of them
aesthetic:

- On iOS a custom face fights Dynamic Type unless every style is wired through
  a scaled font, and the app is read in sunlight by someone who may have text
  size turned up.
- It is a font payload in an app whose whole job is to work on a bad link.
- The system face already looks right on both platforms, which a single face
  cannot do.

Sora on the website and the store listing: yes. In the instrument: no.

## Refuse: the four-colour palette in the app

This is the one to defend hardest.

The app has ONE accent and it means exactly one thing - `live`, traffic is
moving right now. `tokens.json` spends the rest of its palette on *state*:
`degraded` amber, `down` red, and three greys for text. Adding Violet and
Magenta as UI colours does not add expression, it **removes the signal**,
because an accent that appears in decoration stops meaning anything where it
appears in status.

Zippie Blue `#2563FF` is close to the existing `live` `#1757D6`. If the brand
blue is wanted, the honest move is to change `live` in `tokens.json` and let it
flow to every surface - not to add a second blue beside it.

The full palette belongs on brand surfaces, where nothing has to mean anything.

## Refuse: the throughput hero

Section 9's "248 Mbps ↑12%" is precisely the template `DESIGN.md` rejects by
name:

> It refuses the hero-metric template every bonding app ships - enormous
> throughput number, small unit label, a row of supporting stats - because
> speed is not the question at 70mph, and a number cannot say whether YOUR
> phone is one of the legs.

That judgement is right and the sheet quietly reverses it. Throughput is worth
showing; it is not worth being the answer.

## Refuse: cards with shadows

The app uses hairlines as its structural device, deliberately. Cards add a
second grouping mechanism and, at a glance in sunlight, shadows are the first
thing to disappear.

## The open question: which blue

The only real palette decision. Current `live` is `#1757D6`; brand Zippie Blue
is `#2563FF` - brighter and slightly more violet, which sits better beside the
mark's gradient.

Changing it is one line in `tokens.json` and a regenerate. Worth doing as its
own change, with both rendered side by side in daylight, because it moves the
one colour in the product that carries meaning.

## What this leaves

    SHIP NOW      the mark (done, #243)
    NEXT          wordmark SVG, dot field, README ecosystem diagram
    DECIDE        whether `live` becomes Zippie Blue
    NEVER         Sora, the 4-colour palette, the throughput hero, and cards
                  inside the instrument
