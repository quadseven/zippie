# Managing the Pixel from the sky: closed

This directory used to hold a working-looking integration against Google's
Android Management API (AMAPI): a `policy.json` and an `mdm.py` to create an
enterprise, push a Device Owner policy, and enrol the Pixel. That code is
deleted. The route is not usable, and the docs that were here recommended a
path Google's terms forbid.

## What was tried

Direct AMAPI, not an MDM product, on the theory that Fleet's Android support -
and most of the market's - is a wrapper over the same API, so a fleet of one
appliance phone could call it directly. Device Owner mode specifically, because
a work profile is credential-encrypted and does not exist until the phone is
first unlocked, which does not fit a phone that must come up unattended after a
power cut.

## Why it does not work: eligibility, not a bug

Google's [permissible usage policy](https://developers.google.com/android/management/permissible-usage)
restricts AMAPI

> solely to commercial Enterprise Mobility Management (EMM) developers, Device
> Trust from Android Enterprise solution providers, and Original Equipment
> Manufacturers of Android devices

and explicitly prohibits

> Solutions developed and used exclusively for first party in-house
> applications

A homelab managing its own phone is exactly the excluded case. Device quota is
zero until approved as a commercial EMM, and there is no business to present
for that approval. This is worth keeping on record: Fleet being a thin wrapper
over the same API means Fleet's fully-managed Android mode is gated by this
same eligibility rule, not a smaller version of it.

## How it failed, on the device

Enterprise creation and policy push succeeded against the API - the account
holds no device quota to check at that step. Enrolment is where the gate sits.
The Pixel refused provisioning with "your organization has reached its usage
limits", at zero devices enrolled. That is the eligibility restriction, seen
from the handset, not a quota that was merely used up.

## Where device management lives now

Headwind, which ships its own device policy controller and never calls AMAPI,
so none of the above applies to it.

**Do not read that as "write our own DPC and the gate goes away".** It does not,
and this was checked on 2026-08-18 rather than assumed. Google now runs a Play
Protect **approved-DPC allowlist** over enterprise provisioning: an unapproved
DPC fails the QR / 6-tap flow with "App blocked to protect your device", and the
DPC docs state Android Enterprise is no longer accepting new custom-DPC
registrations. Headwind is unaffected because it is allowlisted, not because
being a DPC exempts anything. The route that survives for a DPC of our own is
`adb dpm set-device-owner` on a wiped device - see
[`docs/android-device-management.md`](../../docs/android-device-management.md),
which lays out every door and which are shut.

It moved out of this repo entirely:
tracked in quadseven/infra, in progress as of this writing
(quadseven/infra#2428, with the removal of the zippie-side copy in
quadseven/zippie#123). See that PR and its README for the current manifest and
namespace.

## What Headwind still cannot do: device settings

Headwind delivers managed configuration to APPLICATIONS, and that part works -
the announce token reaches the companion with no hands (#137). It cannot write
arbitrary `Settings.Secure` values, and this is not a Headwind limitation to
work around: `DevicePolicyManager` restricts secure-setting writes to a short
allowlist for every Device Owner, and the settings below are not on it.

So they have no policy path at all. Left alone they are a tap sequence somebody
has to remember per device, which is the thing that gets forgotten on device
two. `provision-device.sh` is that sequence, made repeatable and checkable:

```sh
./provision-device.sh              # apply, then read back to confirm
./provision-device.sh --check      # verify only; non-zero exit on drift
ADB_SERIAL=<serial> ./provision-device.sh   # pick one of several devices
```

Run it once per device at enrolment, after Headwind has taken ownership.
`--check` is safe to run any time and is the honest way to answer "is that
second Pixel actually configured" without opening Settings on it.

What it enforces today, and why:

- **Charging optimization = Limit to 80%.** A bond leg is a phone that sits on a
  charger permanently, and holding a lithium cell full is the worst case for
  ageing - the case a dedicated leg is in every hour of its life. Capping the
  charge is the largest single lever on how long these phones last, and the
  headroom costs nothing on a device that never leaves the mains. Android still
  tops up to 100% occasionally on its own, so the capacity estimate stays honest.

Every write is read back, because `settings put` prints nothing on success and
nothing when it silently fails to stick - a phone that ignored the setting would
otherwise look exactly like one that took it.

## Left behind, on purpose

The SSM parameters created for this attempt
(`/infra/android/mdm/gcp_project_id`, `/infra/android/mdm/enterprise_name`,
`/infra/android/mdm/service_account`, plus the two `*_wifi_ssid` /
`*_wifi_passphrase` parameters holding the router's own wifi credentials)
were not deleted here. They are orphaned by this
retirement; whether to remove or reuse them is a separate decision.

Refs #120
