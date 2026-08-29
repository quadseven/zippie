# Headwind to Fleet: what parity actually costs

Written 2026-08-17 after the Fleet Android MDM plane went live. Everything here
was read from the two running systems, not inferred from their documentation.

## The thing to understand before planning anything

**Headwind manages the phone the household depends on. Fleet manages the spare.**

| | serial | Android | enrolled | role |
|---|---|---|---|---|
| Headwind `h0001` | not recorded (no IMEI) | - | 2026-08-11 | the LIVE uplink, `pixel-6a-a554` at 10.99.0.174 |
| Fleet host 1 | `PLACEHOLDER0000` | 16 | 2026-08-17 22:38Z | the second Pixel, carrying nothing |

The live phone was last seen by Headwind at 22:51Z, so it is actively syncing -
Headwind is not a dead system to be switched off, it is the one holding the
config of the leg currently carrying every byte this house sends.

That matters because the ethernet leg does not work at home (#204 - the router is
plugged into the same house it tunnels to, dials home by that house's public
address, and has never carried a byte; the same address works fine from the
cellular side, so it is the path from inside the house that fails, by a
mechanism nobody has needed to pin down). What sits underneath the bond is plain netifd WAN routing, not a
second working leg. So the phone going quiet is survivable, but only because the
router falls back to the house's own uplink - the bond itself goes to zero.

## Parity, precisely

Headwind pushes six managed-configuration keys to the app. Names and lengths
only; the values live in Headwind's database and in SSM, and two of them are
credentials:

| key | length | what it is |
|---|---|---|
| `homeHost` | 15 | the endpoint the relay dials |
| `homePort` | 5 | its port |
| `consoleLanHost` | 14 | the router console on the LAN |
| `announceToken` | 32 | **secret** - authenticates a leg announcement |
| `ddClientToken` | 35 | **secret** - Datadog client token for the log shipper |
| `autoStartRelay` | 4 | whether the relay starts on boot |

Plus one configuration, `Common - Minimal` ("Generic Android; zippie bond leg.
autoupdate on"), and app delivery.

Fleet reaches parity when it can deliver those six keys and the app. The keys are
the easy half - Android managed configuration is the same mechanism underneath
both MDMs, and `ManagedConfig.kt` already reads them.

## The hard half: the app is not upgradeable across this migration

Headwind serves:

    http://10.99.0.1/zippie-companion-0.1.0-78-109e9aa-TESTKEY.apk

Two facts in that one line.

**It is TESTKEY-signed.** The release APK built by CI is signed with the real
release key, certificate SHA-256 `ecaaf695e2ac5bee845edf075038437ab8ae668890c07012525640c652e477f7`.
Android refuses to upgrade an installed app with a package signed by a different
key. So moving the live phone to the Fleet-delivered build is **uninstall then
reinstall**, not an update - and an uninstall takes the relay, its managed
configuration and its announce state with it.

**It is served over plain HTTP from the router's own LAN address.** That works
only while the phone is on the travel router's wifi, and it is the reason
`network_security_config.xml` still pins `10.99.0.1` as a cleartext exemption
(#156). Fleet does not deliver APKs this way at all: a custom Android app reaches
a device as a **private app published to managed Google Play**, which needs a Play
Console account, and since 2026-07-13 new packages must be `.aab` rather than
`.apk`. `bundleRelease` is already wired in `app/build.gradle.kts:160`, so
producing the bundle is a CI job, not a rewrite - but publishing it is an account
and a human decision.

## What this means for sequencing

The migration cannot be "point the live phone at Fleet and let it converge".
The honest order is:

1. **Prove the whole path on the SPARE phone first.** It is already enrolled in
   Fleet, it carries nothing, and breaking it costs nobody their internet. Reach
   parity there: six keys delivered, app installed by Fleet, relay starts, leg
   announces to the router.
2. **Only then move the live phone** - and this step is far more destructive
   than "swap the app". See the hazard below before planning it.
3. **Retire `mdm.ts.example-home.invalid` last**, once no device points at it.

Doing 2 before 1 means discovering the Play Console requirement, or a managed
config that does not arrive, on the phone the house is using.

## THE HAZARD: moving the live phone means factory-resetting it

This document previously described step 2 as an uninstall and reinstall. That
was wrong, and the correction matters more than anything else written here.

**Device Owner can only be set on an unprovisioned device.** Once
`Settings.Secure.USER_SETUP_COMPLETE` has been set - which happens the first time
anyone finishes setup - the device counts as provisioned and cannot be enrolled
as fully managed. Google is explicit that the device must be factory reset first,
and that the restriction exists so malware cannot seize a device that is already
in use. There is no conversion path.

So enrolling the live phone into Fleet is not an app swap. It is: wipe the phone,
walk it through out-of-box setup, scan the enrolment QR, and rebuild everything
on it. The relay's on-device state goes with it - the DataBudget counters, the
boot log, the managed configuration.

**And now the trap, which is the reason this section exists.**

Managed Google Play DELETES apps that are not in the enterprise policy
(#224 - `Finsky: Uninstaller: ... status=UNINSTALLED`, proven on the spare with
every relevant security override applied). So after the reset, the companion
CANNOT be sideloaded back. The only way to install it is a private-app publish
through managed Google Play.

Put those together:

> Factory-reset the live phone before the private app is published, and you have
> a phone that is enrolled, managed, wiped, and with no way whatsoever to get the
> relay back onto it.

That is not a long outage. That is a leg that stays dead until a Play Console
step - one that needs an account, an `.aab`, and the PEPK key export - is
finished. Doing it in the wrong order converts a planned maintenance window into
an open-ended one.

**The order is therefore not a preference, it is a safety rule:**

1. Publish the companion as a private app and prove Fleet installs it, on the
   SPARE.
2. Only once a Fleet-managed device has been seen to receive the app, reset and
   re-enrol the live phone.
3. Retire `mdm.ts.example-home.invalid`.

There is no step 2 without step 1. The spare exists precisely so this is
discovered by reading rather than by wiping the phone the household depends on.

## Open questions that are decisions, not work

- **Play Console.** Publishing the companion as a private app needs an account
  and an `.aab`. Without it, Fleet cannot install the app at all, and parity is
  unreachable regardless of how good the policy story is. This is the gate.
- **Whether to re-sign, or to accept the reinstall.** The alternative to an
  uninstall is rebuilding the Fleet-delivered app with the TESTKEY, which trades
  a one-time outage for shipping a test-signed binary permanently. Not
  recommended, but it is the only other shape.
- **`announceToken` rotation.** It has to be replaced anyway (it was disclosed on
  2026-08-17), and rotating it during the migration means changing it in exactly
  one place instead of two. Worth folding in rather than doing twice.

## Traps inherited from the infra side, recorded so nobody re-derives them

- `FLEET_SERVER_URL` in the configmap only SEEDS Fleet on first setup; thereafter
  Fleet reads `server_url` from its database. It is `https://fleet.example-home.invalid` and
  must stay so - Google's Pub/Sub push URL is derived from it at
  enterprise-creation time and there is no patch path.
- An Android host is created by exactly one code path, driven by a Google Pub/Sub
  push. `ReconcileAndroidDevices` does not discover devices. If a device never
  appears, the question is always "did the push arrive", never "is Fleet
  polling".
- `pubsub_topic_id` in `android_enterprises` is always empty here. That is normal
  and is not a health signal.
- Cloudflare custom firewall rules on this zone are hand-made in the dashboard
  and are not in Pulumi. A "Known Good Bots" block silently 403'd ~300 Google
  pushes a minute at the edge, invisible in Fleet's logs. If Android traffic goes
  quiet, check Cloudflare edge analytics before reading Fleet.
- `sync.k8s-manifests` applies ConfigMaps but does not restart Deployments. A
  configmap change is not live until a rollout restart.
- Fleet logs the full request URI on error, including the pubsub token, and those
  logs ship to Datadog (infra#2522). Scrub before pasting Fleet errors anywhere.
