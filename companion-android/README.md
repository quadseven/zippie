# Zippie Companion for Android

Same two modes as the iOS app, same bond.

## Status: BUILDS AND TESTS GREEN, NOT YET RUN ON A DEVICE (2026-08-05)

Built on the self-hosted macOS runner (JDK 17 Temurin, SDK platform 34, build-tools 34,
Gradle 8.9 via the checked-in wrapper):

```
JAVA_HOME=/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home \
  ./gradlew clean assembleDebug testDebugUnitTest
```

`BUILD SUCCESSFUL`, 41 unit tests, 0 failures, `app-debug.apk` 20.6 MB, signed
with the debug key (v2 scheme; there is no v1 signature because `minSdk 29` does
not need one). It has **never run on a phone** - compiling and passing unit
tests proves the manifest, resources, decoding and budget arithmetic are
coherent, and proves nothing about whether a packet crosses this relay.

Nothing here builds on the laptop: it has no Android SDK, and its JDK is 26,
which AGP 8.5 does not accept. The mini is the build host - and since
`check.android.yml` runs `testDebugUnitTest` plus `assembleDebug` on that same
mini for every PR, CI is now the thing that proves a change compiles. Work
added after the date above (announcing, 2026-08-08) has been proven that way and
not by a local build.

## What it does

**CONTRIBUTE (`RelayService`)** - on the router's own network, lend this phone's
cellular to the bond. A foreground service, because Android stops background
network access minutes after the screen goes off and a relay that dies in a
pocket is a leg the bond counts on and does not have.

**ANNOUNCE (`LegAnnouncer`)** - tell the router this phone is a leg, and keep
saying so. Without it the relay carries perfectly and is invisible to the bond,
which is indistinguishable from a broken relay when you are looking at the leg
list. See "Announcing" below.

**STATUS (`StatusScreen`)** - the whole bond, read from the router console. This
is the part that ships working today.

**CLIENT (`ZippieVpnService`)** - a deliberate skeleton. The tunnel establishes;
there is no datapath behind it until the gomobile build of the Go core exists
(#2246), and the screen says so rather than hiding it behind a disabled button.

## The three things that are easy to get wrong

1. **`requestNetwork(TRANSPORT_CELLULAR)` + `Network.bindSocket` is the whole
   design.** Without it the forward leaves by whichever interface holds the
   default route - on a phone joined to the router's wifi, that is the same wifi,
   and the "second path" is a loop into the first. It also needs
   `CHANGE_NETWORK_STATE`, without which it throws `SecurityException` at
   runtime: a relay that installs, starts, shows a notification and never
   carries a byte.

2. **In the bond and having weight are different facts.** The live router
   reports `ethernet` with `effective_weight: 40` and `in_bond: false`. Deciding
   "carrying" from weight showed four legs carrying while the transport held
   exactly one. `BondStatus.isCarrying` requires both, and
   `BondStatusTest` fails if that is relaxed.

3. **A reserve leg is doing its job.** A leg on a tier above the one currently
   carrying is held back on purpose; drawing it like a failure trains the reader
   to ignore the one signal that matters. It is rendered quietly, with the
   router's reason. One divergence from iOS: a leg that is DOWN or has no
   interface at all is NOT called "held in reserve", because it could not be
   called on if everything above it failed.

## What the packet datapath actually reports

In `datapath: "packet"` the per-leg `tx_bytes` / `rx_bytes` are hard 0 on every
leg while traffic flows; the real counters are `link_tx_bytes` /
`link_rx_bytes`. Verified live on 2026-08-05: the carrying leg showed
`tx_bytes: 0` with `link_tx_bytes: 11018643` and `link_rx_bytes: 31703754`, and
the transport's own `reassembly.delivered_bytes` agreed with the latter. This
app reads the link counters and falls back to the old pair. **The iOS app reads
only the old pair**, so it draws a leg that has carried 30 MB as having carried
nothing - worth fixing there.

## Android specifics that are decisions, not defaults

- **No `ACCESS_FINE_LOCATION`.** It would be needed to read the SSID, which is
  how iOS arms its on-demand rule. This app decides contribute vs client by
  asking the router's LOCAL console (`RouterProximity`) - evidence, rather than a
  name any cafe could also use - so the scary prompt buys nothing.
- **Cleartext is permitted to the router's LAN address only**
  (`res/xml/network_security_config.xml`), not globally.
  `usesCleartextTraffic="true"` would also permit plain HTTP to a captive portal
  answering every DNS query with its own address. Move the console and this file
  must move with `RelayConfiguration.consoleLanHost`.
- **The carrier name is surfaced** (`TelephonyManager`, no permission needed).
  iOS cannot: Apple removed `CTCarrier`'s name in iOS 16, which is why the two
  iPhone legs are labelled by hand in the router config and keep saying
  "Verizon" after a SIM swap.
- **The `NetworkCallback` gets its own `HandlerThread`.** By default it runs on
  the main thread, and opening a socket or resolving a host there is exactly what
  `NetworkOnMainThreadException` punishes.
- **The home host is resolved on the cellular `Network`**, not with the default
  resolver, for the captive-portal reason above.

## Announcing

A phone is not a fixed address, so it does not belong in the router's
`zippie.toml`. Static `companion-*` entries were deleted (#34) because they were
worse than stale: `match_interfaces` excludes companion legs on their relay
ENDPOINT rather than on the shared `br-lan`, so a static entry that claims a
phone's address takes the endpoint and the announced leg is then refused the
bridge and left DOWN with no interface. Announcing is the only route in.

- `POST /api/legs/announce` every 15 seconds against a 45 second lease
  (`DEFAULT_LEASE_S`), so two missed renewals do not drop the leg.
- The address is re-read on every pass. An announcement is also a RENEWAL of the
  address: a phone that moves on DHCP must not leave the router dialling the
  endpoint it used to have.
- `POST /api/legs/withdraw` on stop, so the leg goes at once instead of waiting
  out the lease. It runs on the announce thread, because `onDestroy` is the main
  thread and any network call there is `NetworkOnMainThreadException`.
- The leg name is `<model>-<4 hex>`, minted once and persisted (`LegName`). It
  satisfies the router's `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` exactly - a name
  that fails it is a 400 and a phone that can never join. The hex is what keeps
  two phones of the same model apart: `Build.MODEL` is a model name, not a
  device name.
- The label carries the REAL carrier (`LegLabel` + `CarrierInfo`). This is the
  thing iOS cannot do, and it is why the two iPhone legs are hand-labelled in
  the router config and keep saying "Verizon" after a SIM swap.

### The token

Announce is authenticated - unauthenticated is a 401, and a public (non-RFC1918)
host is a 400. The token is the router's own
`/var/lib/zippie/console_token`, which it generates on first use. Paste it into
the field at the bottom of "This phone"; it is masked, never read back onto the
screen, and never logged. Saving an empty field clears it. Without a token the
phone still relays and simply does not announce, which the screen says in as
many words.

## Data budget

`DataBudget` / `BudgetLedger` count BOTH directions - the carrier bills for the
download too, and on a phone that is the larger half. Zero means unlimited,
because inventing a cap would silently throttle a working relay. Counters are
persisted (`BudgetLedgerCodec`), so a process kill cannot reset a monthly cap
into a per-launch one, and they saturate rather than wrap: wrapping is the one
arithmetic outcome that switches a cap off silently.

There is no settings screen yet. The budget is whatever is in `SharedPreferences`
under `zippie`, which means unlimited on a fresh install.

## Building

1. Android SDK with platform 34 + build-tools 34, and JDK 17.
2. `local.properties` with `sdk.dir=...` (gitignored, per machine).
3. `./gradlew assembleDebug`

The gomobile `.aar` is NOT needed to build; it is needed before client mode can
carry anything (#2246).

## Release builds and signing

### What CI produces today

| workflow | trigger | key | artifact |
|---|---|---|---|
| `check.android.yml` | every PR | debug (SDK's shared key) | test reports |
| `check.android-signing.yml` | every PR | **throwaway**, made and destroyed in the job | `zippie-companion-release-apk-THROWAWAY-KEY` |
| `app.companion-android.release.yml` | **manual dispatch only** | the real one, from repo secrets | `zippie-companion-release-apk-signed` |

Both signed builds go through `ci/build-signed-apk.sh`, which is the single
place the version numbering and the post-build checks live. It refuses to
finish unless the APK it produced carries a v2 signature, is 4-byte aligned, and
reports the exact `versionCode`, `versionName` and package it was asked for -
reading them back out of the file with `aapt2` rather than trusting that the
environment reached gradle.

Nothing is published to Google Play or anywhere else. The APK is a workflow
artifact you download and `adb install`. The Play internal-testing route is a
separate decision (a developer account, a review loop) and is not wired here.

### THE REAL SIGNING KEY DOES NOT EXIST YET

DONE 2026-08-11. The ceremony below was performed and
`/infra/android/release_keystore/{keystore_base64,store_password,key_password,key_alias}`
now exist on `alias/pulumi-secrets`, with the four repo secrets seeded from them.
`keystore: release` produces a signed build.

The certificate this app is now identified by, for as long as it is installed
anywhere:

    SHA-256 EC:AA:F6:95:E2:AC:5B:EE:84:5E:DF:07:50:38:43:7A:B8:AE:66:88:90:C0:70:12:52:56:40:C6:52:E4:77:F7

A build whose certificate does not match that line was signed with the wrong key.
Verified on run 31557751650 by parsing the APK Signing Block v2 certificate
directly out of the artifact and comparing, rather than trusting the workflow's
own report of what it signed with.

(The line this replaces said "nothing under `/infra/android/`", which was already
stale before the ceremony: five `/infra/android/mdm/*` parameters survive from
the retired AMAPI attempt - see `mdm/README.md`, which says so deliberately.)

This is deliberate and it is not a task an agent should do. An Android app is
identified by its signing certificate for as long as it is installed. Lose the
key and every phone that has the app can never receive an update again - only
uninstall and reinstall, discarding its data. Leak it and anyone can build
something the phone will install straight over this app. It gets generated once,
by a human, and backed up before it signs anything.

### The ceremony (Operator, once)

1. Generate it. `keytool` prompts for the password twice; do NOT pass
   `-storepass` on the command line, where `ps` can read it. PKCS12 uses one
   password for the store and the key.

   ```
   keytool -genkeypair -v \
     -keystore zippie-companion-release.jks \
     -storetype PKCS12 \
     -alias zippie-companion \
     -keyalg RSA -keysize 2048 \
     -validity 10000 \
     -dname "CN=Zippie Companion, O=zippie, C=US"
   ```

   `-keysize 2048`, not 4096, for two reasons: it is what Google's own upload
   key guidance says, and a base64 4096-bit PKCS12 is about 5900 characters,
   over the 4096-character ceiling on a standard-tier SSM parameter. Measured:
   2048 gives 2748 bytes raw, 3664 base64. `-validity 10000` is ~27 years; a
   certificate that expires while the app is installed is a dead upgrade path.

2. **Back it up before it signs anything.** The password goes in 1Password, the
   `.jks` goes somewhere that survives this laptop. This is the step that cannot
   be redone later.

3. Put it in SSM on the SHARED CMK, `alias/pulumi-secrets`.

   `--key-id alias/pulumi-secrets`, deliberately, and this reverses what this
   file said before. The old advice was "never pass `--key-id`, it creates a
   billed CMK" - true of a NEW key, and the reason the estate avoided them. It
   does not apply here: that key already exists and is already paid for, and
   AWS bills KMS **per key**, not per parameter. The only new charge is
   requests at $0.03 per 10,000, which for a keystore read a handful of times a
   month is indistinguishable from zero.

   What it buys, and why a SIGNING key in particular is worth it: one
   revocation lever and per-key CloudTrail attribution. Disabling that key locks
   every consumer out of the keystore in one action. The free `alias/aws/ssm`
   cannot be disabled, cannot be audited per-key, and cannot be revoked at all.

   **Nothing in CI reads these.** `app.companion-android.release.yml` has no
   AWS step at all - it consumes the four GitHub repo secrets that step 4 seeds
   from SSM by hand. So the identity that needs `kms:Decrypt` on this key is
   the HUMAN running the ceremony, not a workflow role. Checked rather than
   assumed: grepping that workflow for `aws-actions`, `role-to-assume` and
   `ssm` returns nothing.

   For completeness, `zippie-oke-deploy` does hold `kms:Decrypt` on
   `Resource: *`, so a future job that did read these would work - but do not
   read that as the reason this is safe today.

   ```
   KSB64=$(mktemp); chmod 600 "$KSB64"
   # `| tr -d '\n'` is NOT optional. base64 ends its output with a newline, and
   # file:// stores the bytes verbatim, so without it the parameter carries a
   # trailing newline - the exact hazard the warning below this block describes.
   # It bit the 2026-08-11 ceremony and had to be corrected with a second put.
   base64 -i zippie-companion-release.jks | tr -d '\n' > "$KSB64"
   aws ssm put-parameter --type SecureString --key-id alias/pulumi-secrets \
     --name /infra/android/release_keystore/keystore_base64 --value "file://$KSB64"
   rm -f "$KSB64"

   aws ssm put-parameter --type String \
     --name /infra/android/release_keystore/key_alias --value zippie-companion

   # Passwords go through a 0600 temp file, NOT --value "$KSPW". On
   # put-parameter the value is an ARGUMENT, and argv is readable by any local
   # process via ps for as long as the call runs - the same reason step 1
   # refuses -storepass on the command line.
   #
   # PROMPT WITH printf, THEN A BARE `read -rs`. This ran as
   # `read -rs -p 'keystore password: '` until 2026-08-11, which works in bash
   # and silently does not in zsh - there `-p` means READ FROM THE COPROCESS,
   # not "prompt". The operator's shell is zsh, so nothing was captured, the
   # temp file was empty, and both put-parameter calls failed with a
   # ValidationException about a zero-length value. `read -rs` alone behaves
   # identically in both shells.
   PWF=$(mktemp); chmod 600 "$PWF"
   printf 'keystore password: '
   read -rs KSPW; echo
   printf '%s' "$KSPW" > "$PWF"; unset KSPW

   # Prove the password opens the keystore BEFORE storing it. A typo here is
   # otherwise invisible until a release build fails, long after the terminal
   # that knew the real password is gone. `-storepass:file` keeps it out of argv.
   if [ ! -s "$PWF" ]; then
     echo "password was empty - nothing written"; rm -f "$PWF"
   elif ! keytool -list -keystore zippie-companion-release.jks \
           -storepass:file "$PWF" >/dev/null 2>&1; then
     echo "that password does not open the keystore - nothing written"; rm -f "$PWF"
   else
     aws ssm put-parameter --type SecureString --key-id alias/pulumi-secrets \
       --name /infra/android/release_keystore/store_password --value "file://$PWF"
     aws ssm put-parameter --type SecureString --key-id alias/pulumi-secrets \
       --name /infra/android/release_keystore/key_password --value "file://$PWF"
     rm -f "$PWF"
   fi
   ```

   **Read values back with `--output json`, never `--output text`.** Text output
   APPENDS A TRAILING NEWLINE. Round-tripping a parameter through it grows the
   stored value by one byte per pass; that corrupted
   `/infra/android/mdm/service_account` during the migration onto this key and
   was caught only by a digest comparison. For a base64 keystore a stray
   newline is silently tolerated by most decoders and then is not, which is the
   worst kind of bug to find on a release build.

4. Seed the four repo secrets from SSM, piped so no value touches disk or an
   argument list:

   ```
   for pair in \
     "keystore_base64:ANDROID_KEYSTORE_BASE64" \
     "store_password:ANDROID_KEYSTORE_PASSWORD" \
     "key_alias:ANDROID_KEY_ALIAS" \
     "key_password:ANDROID_KEY_PASSWORD"; do
     aws ssm get-parameter --with-decryption --output text \
       --name "/infra/android/release_keystore/${pair%%:*}" \
       --query Parameter.Value \
       | tr -d '\n' | gh secret set "${pair##*:}" -R quadseven/zippie
   done
   ```

5. Build one:

   ```
   gh workflow run app.companion-android.release.yml -R quadseven/zippie -f keystore=release
   ```

6. Record the certificate SHA-256 the run prints in its job summary, here in
   this file. From then on a build whose digest does not match that line was
   signed with a different key, which is the failure worth catching early.

### The upgrade path

Android decides an upgrade on two facts, and both are handled here:

- **Same signing certificate.** Then the install is an update and the app keeps
  its data - `SharedPreferences` under `zippie`, which is where the `DataBudget`
  counters live. A different certificate is refused
  (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`); the only way through is to uninstall,
  which discards those counters. **A THROWAWAY-signed build and a real-key build
  are different certificates**, and so are two throwaway builds from two
  different CI runs.
- **A strictly higher `versionCode`.** `versionCode` is the commit count of the
  ref being built (`git rev-list --count HEAD`), so it only moves forward along
  main and can be traced back to a commit. A build from an older branch has a
  LOWER count and the phone refuses it (`INSTALL_FAILED_VERSION_DOWNGRADE`) -
  which is the correct answer, said out loud, rather than an older app silently
  replacing a newer one.

`versionName` carries the same numbers plus the short sha, so
Settings > Apps names the exact commit: `0.1.0-341-0ce0f7f`, and
`0.1.0-341-0ce0f7f-TESTKEY` when a throwaway key signed it.

**None of this has been tested on hardware.** The version arithmetic and the
signature are verified in CI against the built file; whether two consecutive
installs on a Pixel 6a preserve the budget counters has not been observed, and
cannot be until an APK is on a phone.

### Building a release APK by hand

You need a keystore outside this checkout - gradle refuses one inside it, on the
grounds that a signing key committed once is committed forever.

```
ZIPPIE_KEYSTORE_PATH=/absolute/path/outside/the/repo.jks \
ZIPPIE_KEYSTORE_PASSWORD=... ZIPPIE_KEY_ALIAS=... ZIPPIE_KEY_PASSWORD=... \
  ci/build-signed-apk.sh
```

Without those, `assembleRelease` fails on purpose. AGP's default behaviour is to
write `app-release-unsigned.apk` and exit 0 - a green build producing a file no
phone will install.

## Installing on a Pixel

Debug build, straight from a local build:

```
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Release build, from a CI artifact. This is the delivery route - there is no
store and no update channel, so getting a build onto the two Pixels is: download
the artifact, `adb install`, once per phone.

```
# the newest signing run, whatever branch it came from
RUN=$(gh run list -R quadseven/zippie -w 'Check - android release signing (throwaway key)' \
        -s success -L 1 --json databaseId -q '.[0].databaseId')
gh run download "$RUN" -R quadseven/zippie -D /tmp/zippie-apk
adb install -r /tmp/zippie-apk/*/zippie-companion-*.apk
```

For a real-key build, swap the workflow name for
`App - zippie companion android - signed release APK (manual)`. The file names
itself, so the phone and the filename agree about which commit and which key:
`zippie-companion-0.1.0-341-0ce0f7f-TESTKEY.apk`.

Then, on the phone: grant the notification permission when asked (the relay runs
without it, but its notification - the only visible sign it is spending cellular,
and the only way to stop it from the shade - is hidden), and confirm the router
console address if it is not `10.20.0.1:8787`.

## Not verified

Everything that needs a phone: whether the cellular socket really pins to the
radio, whether the router's frames arrive at the listen port, whether the
foreground service survives Doze on a Pixel, and whether the status screen reads
well on a real display. The first of these is the one that matters; the rest are
cosmetic by comparison.

Announcing (added 2026-08-08) is in the same position. Its logic is unit tested
- the request the router receives, the refusals, the renewal loop, the withdraw
on stop, and that every name it can produce satisfies the router's regex - and
none of that is a phone appearing in `/api/status`. Still to prove on hardware:
a Pixel showing up as a leg with `dynamic: true` and no `zippie.toml` entry,
stopping the relay removing it within the lease, and two Android phones
announcing at once without either being dropped.
