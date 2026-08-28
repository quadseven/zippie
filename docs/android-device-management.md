# Which Android device management this project may actually use

Written 2026-08-18, after a night that walked into two separate Google gates
back to back. Both were discovered late, both were cheap to check first, and
neither is a bug or a paywall - they are eligibility rules. This file exists so
the next person to say "let's just build our own MDM" reads the answer in two
minutes instead of two days.

**Date-mark everything here.** Google moved both gates during 2025-2026. If you
are reading this months later, re-check the two support pages linked below
before trusting a CLOSED verdict - the whole point of writing it down is to
know what was true when, not to freeze it.

## The short version

The phone is managed by an app holding **Device Owner**. There are only a few
ways to become that app, and most of them are now gated by Google.

| Door | Status | Gate |
|---|---|---|
| Call the Android Management API ourselves | **CLOSED** | Commercial-EMM eligibility |
| Our own DPC, provisioned by QR / 6-tap | **CLOSED** | Play Protect DPC allowlist |
| Our own DPC, provisioned by `adb dpm set-device-owner` | **OPEN** | Cable + wiped device |
| An already-approved third-party DPC (Headwind) | **OPEN** | In use today |
| Fleet Premium | **OPEN** | Licence cost |

The one that survives is the third. It gives full Device Owner powers to code
we write, with no Google approval - it just costs the QR-scan experience and
replaces it with a USB cable, once per device.

## Door 1: calling AMAPI ourselves - CLOSED

Tried and retired in #125, 2026-08-10. Enterprise creation and policy push
both **succeeded**; enrolment is where it stops. The Pixel refused provisioning
with "your organization has reached its usage limits" **at zero devices
enrolled** - the eligibility rule seen from the handset, not a quota that had
been spent.

Google's [permissible usage policy](https://developers.google.com/android/management/permissible-usage),
re-read 2026-08-18 and unchanged, restricts the API to

> commercial Enterprise Mobility Management (EMM) developers, Device Trust from
> Android Enterprise solution providers, and Original Equipment Manufacturers

who "must offer solutions commercially to external customers", and explicitly
prohibits

> Solutions developed and used exclusively for first party in-house applications

A homelab managing its own two phones is the excluded case, exactly.

**This is also the whole explanation for Fleet.** Fleet's fully-managed Android
works because *Fleet* holds this eligibility and sells to external customers.
Their Premium tier is not a feature flag over an API we could call ourselves -
it is access to a door we cannot open. No amount of forking changes that.

## Door 2: our own DPC by QR / 6-tap - CLOSED

This is the flow that gets asked for by name: factory reset, tap the welcome
screen six times, scan a QR, walk away. It is standard AOSP provisioning and
for years it needed nothing from Google - the QR carries a download URL, a
SHA-256 and the admin component, and the setup wizard fetches and installs the
DPC.

Google closed it. An **approved-DPC allowlist** now gates enterprise
provisioning: only DPCs verified by Android Enterprise may be installed during
it, and an unapproved one fails with

> App blocked to protect your device

The [approval page](https://support.google.com/work/android/answer/16694822)
describes the criteria (Play Protect MUwS/PHA compliance) and an appeal form.
Two things make this a bad bet rather than a form to fill in:

- Google's own DPC docs now carry the caution that Android Enterprise **"is no
  longer accepting new registrations for custom device policy controllers"**.
- Public reports through 2025-2026 describe appeals taking months and being
  repeatedly rejected, including for real commercial products. Headwind's own
  agent was blocked at enrolment for a period before being allowlisted again.

An unapproved, self-signed DPC that exists only on this household's phones has
no realistic route onto that allowlist. Treat the QR flow as unavailable to us.

## Door 3: our own DPC by adb - OPEN, and this is the one

`adb shell dpm set-device-owner <pkg>/<receiver>` still works. It is documented
as a development and testing path rather than a fleet deployment method, which
is precisely why it is not allowlist-gated: it is a shell command on a device
someone is physically holding, not an enterprise provisioning flow.

Conditions, all of which this project already meets:

- the device must be freshly factory reset, with **no Google account added** and
  no secondary users - the same wiped state the QR flow needs anyway
- USB debugging on, cable attached, once per device
- the DPC APK is sideloaded first, which works because a device provisioned this
  way is not enrolled in managed Google Play and so has no enterprise app set
  for Play to reconcile against (contrast #125's finding, where a *managed*
  device had sideloads deleted by Finsky within seconds)

What we lose against the QR dream is the QR. What we keep is everything the
Device Owner can then do, which is the actual point:

- silent install and update of the companion, no prompts
- wallpaper set programmatically, and locked against change
- lock-task / kiosk, or fully managed with the stock Pixel launcher
- app configuration delivered directly, since we own both ends
- an mTLS control plane of our own design (see #158, #142)

**One thing no Device Owner can do**, and it is on the wish list, so it is worth
stating plainly: the 80% charge cap cannot be set by policy. Android allowlists
which secure settings a Device Owner may write and the charging-optimization
setting is not among them. That stays `companion-android/mdm/provision-device.sh`
over adb - which is fine, because door 3 already has a cable attached.

## Door 4: Headwind - OPEN, in use today

Headwind ships its own DPC and never calls AMAPI, so door 1 does not apply to
it, and it is currently allowlisted, so door 2 does not either. It manages the
live Pixel now and delivers managed configuration to the companion (#137).

Its cost is not technical: it replaces the launcher and its management model is
not what this household wants long term. It is the incumbent, not the
destination.

## Door 5: Fleet Premium - OPEN, costs money

Works, for the reason in door 1. Fleet free tier does everything except install
applications: policy profiles apply and converge in under a minute, but the
`applications` field is Premium, "Add software" is greyed out, and the two
issues that shipped Android app install (fleetdm/fleet#33061, #35666) landed in
`ee/`. [#36424](https://github.com/fleetdm/fleet/issues/36424) tracks a free-tier
ask; it is open, unmilestoned and has no comments.

Fleet's licence permits forking - core is MIT, and `ee/` may be modified for
development and testing without a subscription - but running unlocked `ee/` code
in production is exactly what it forbids. Reimplementing the feature in MIT core
is legally permitted and unlikely to be merged, since Fleet chose to sell it.

## What to do with this

For a two-phone appliance fleet the honest ranking is:

1. **Door 3**, if owning the stack is the goal. Real work, no gates, no fees,
   and it grows into whatever else needs managing later.
2. **Door 5**, if working today matters more than owning it.
3. **Door 4**, which is what is running while the other two are decided.

Do not spend another night on doors 1 and 2. They are closed by policy, they
were closed before this project started caring, and the evidence is above.

## References

- #125 - the AMAPI attempt and its live failure on the handset
- #158 - first-run pairing ceremony, mTLS identity for phones
- #142 - wayfinder map for device identity with no internet
- `companion-android/mdm/README.md` - the retired AMAPI integration
- `docs/headwind-to-fleet-migration.md` - parity work, written before doors 1
  and 2 were known to be closed
