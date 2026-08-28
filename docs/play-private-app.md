# Publishing the companion as a private app

Fleet installs a custom Android app only as a **private app through managed
Google Play**. This is the runbook for getting there. Written 2026-08-17, when
the bundle first existed.

## What is already done

CI produces a signed Android App Bundle from the same invocation as the APK
(#218), so both carry one `versionCode`. The most recent one:

    file          zippie-companion-<version>.aab
    package       app.zippie.companion
    signer        SHA-256 ecaaf695e2ac5bee845edf075038437ab8ae668890c07012525640c652e477f7
    verified      jarsigner: jar verified, no unsigned entries

Rebuild it any time with the manual workflow
`app.companion-android.release.yml`, `keystore: release`; the bundle uploads as
the `zippie-companion-release-aab` artifact.

The Android app carries **no compiled-in personal infrastructure**. Its defaults
are empty strings and a CI ratchet (#208) fails the build if a host reappears.
Everything it needs arrives by managed configuration.

## Where this actually got to, 2026-08-17

The console work is done up to the one step that needs the signing key.

    developer account   quadseven (personal), account 0000000000000000000
    app                 Zippie Companion, app.zippie.companion
    app id              0000000000000000000
    managed Google Play ON
    restricted to       LC00000000 - "zippie (Fleet MDM)"
    state               private, PERMANENTLY - Play warns this cannot be undone
    signing key         still Google-managed; NOT yet changed

TWO THINGS WORTH KNOWING FROM DOING IT.

**The Play account already listed a different enterprise.** `LC00000000`, named
after the old personal domain, was there before this app existed. It is NOT the enterprise Fleet
manages - Fleet's own database gives `LC00000000`, created 2026-08-17 21:13:40.
Attaching the app to the wrong one would have produced an app that publishes
cleanly and reaches no device, with nothing to explain why. The old entry was
left unticked.

**Android developer verification is already satisfied.** The console reports all
apps registered against the new requirement, so it is not a blocker here.

## The step that is left, and why it is Operator's

Play defaults to managing the app signing key itself. That was changed
deliberately, because the live phone runs a build signed with OUR release key -
verified on the device rather than assumed:

    adb shell dumpsys package app.zippie.companion
      versionName=0.1.0-107-6f97848
      firstInstallTime=2026-08-11 22:48:16

versionCode 107, certificate `ecaaf695...`. If Play signs with its own key, a
Play-delivered build cannot upgrade that install and the phone needs an
uninstall/reinstall, discarding its on-device DataBudget counters. If Play signs
with ours, it upgrades cleanly.

Note that Headwind's database DISAGREES with the device - it records
`0.1.0-78-109e9aa-TESTKEY` served over plain HTTP, which was never installed.
The MDM row records an intention; `dumpsys` records the state. Derive parity from
the phone, not from Headwind's app table.

Uploading our key means running Google's PEPK tool:

    java -jar pepk.jar --keystore=<the release keystore> --alias=<alias> \
        --output=output.zip --include-cert --rsa-aes-encryption \
        --encryption-key-path=<encryption_public_key.pem>

It needs the keystore FILE and prompts for the keystore and key PASSWORDS. That
is why this step is not automated here and not done by an agent: it means
materialising the release private key onto disk and handling its passwords.
Console path is **App signing -> Change key -> "Export and upload a key from
Java keystore"**.

After that, the remaining work is ordinary: upload the `.aab`, create the
production release, send for review.

## What only Operator can do

Account creation and anything behind a Google sign-in. Listed precisely so none
of it has to be worked out at the keyboard.

### 1. A Play Console developer account

RESOLVED 2026-08-17: the account already existed (`quadseven`, a personal
developer account), so the $25 registration was never needed. The paragraph
below is kept because the reasoning about WHICH route applies is still the
reasoning, and a future second enterprise would face the same fork.

There IS a free route - the Google Workspace admin console can publish a private
app without a developer account, creating a Play Console account on the
organization's behalf - but it applies to Workspace-managed enterprises. This
enterprise (`LC00000000`) was created through fleetdm.com's proxy, and Fleet's
own guide directs you to the Play Console and the Enterprise ID. If a Workspace
domain is ever in play, revisit this: it removes the fee and a signup.

### 2. Publish the bundle as private, to this enterprise

In the Play Console, create the app, upload the `.aab`, then attach the
organization: paste the Android Enterprise ID

    LC00000000

and mark the app private. Private apps are not visible in the public Play store
and are **automatically approved for the organization, typically ready within
about ten minutes** - unlike a public app, which is reviewed over days. It can
still take a few hours to appear to devices.

### 3. Expect the declaration forms

Play asks for app-content declarations regardless of how narrowly the app is
distributed. The one worth knowing about in advance:

**This app declares a VpnService.** `AndroidManifest.xml` registers
`ZippieVpnService` with `android.permission.BIND_VPN_SERVICE` and the
`android.net.VpnService` intent filter, because client mode is the product. Play
has a specific policy and declaration for VPN apps, and this will have to be
answered honestly: the app routes the device's traffic through the household's
own bond, for the account holder's own devices.

Also expect forms for foreground service types (the app uses
`connectedDevice`, and `FOREGROUND_SERVICE_CONNECTED_DEVICE`) and data safety.

## What happens after

Once the app is private-published to the enterprise, Fleet can install it, and
the remaining migration steps are ordinary work rather than blocked:

1. Fleet delivers the app plus the managed-configuration keys to the SPARE
   phone, and its relay starts and announces. Nothing about this touches the
   phone the household depends on.
2. Only then the live phone moves, with Operator present. That step is an
   uninstall/reinstall, not an upgrade - the currently installed build is
   TESTKEY-signed and the release key is different, so Android will refuse to
   upgrade over it. The leg drops for the duration; the router falls back to the
   house WAN, so the household keeps internet while the bond carries nothing.
3. `mdm.ts.example-home.invalid` retires last, once nothing points at it.

See `docs/headwind-to-fleet-migration.md` for why that order and not the obvious
one.

## The managed-configuration surface

Eight keys, already implemented and already delivered by Headwind today:

    homeHost        homePort        consoleLanHost    announceToken
    listenPort      autoStartRelay  ddClientToken     ddSite

Fleet uses the same Android mechanism, so this is a re-point rather than new
work.

**One thing managed configuration cannot reach**, and it is worth knowing before
somebody loses an afternoon to it: `res/xml/network_security_config.xml` pins
`10.20.0.1` as a cleartext exemption, and it is a compiled resource. Push a
console at any other private address and the app stores it, displays it, and
then cannot talk to it - failing with "Cleartext HTTP traffic not permitted",
which reads like a console bug and is not. That is #156, and the clean fix is
the console speaking HTTPS so the exemption disappears.
