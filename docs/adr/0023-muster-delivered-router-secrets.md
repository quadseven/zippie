# 23. muster-delivered secrets for the travel router

Date: 2026-08-29

## Status

Proposed. The client module (`travel/bond-agent/zippie/musterwrt.py`) is
shipped and unarmed; enrollment of the travel router has not happened and
needs an operator at the console.

## Context

On 2026-08-29 a deploy shipped `server_public_key = "<server-public-key>"` to
the travel router. The placeholder is in the repo deliberately - the real value
must not be - and nothing had ever substituted it back. `wg setconf` refused
it, the bond never came up, and because the agent owns the router's only default
route the box left the network. Recovery took physical access.

The immediate fix (#3) takes the key the router already has and splices it into
a rendered copy, so the secret never enters git, a GitHub secret, or a runner's
memory. That is a good stopgap and it is explicitly a stopgap: the router still
holds its secrets in files that a deploy pipeline writes.

muster is the estate's device enrollment and identity plane. It already serves
per-device policy over an authenticated channel, and `policy.py`'s own docstring
is the argument for why `app-config` is the file that may not be shared: "a
credential under the shared scope is a credential handed to everyone." That is
where a device secret belongs.

## What was measured, and what it changes

All on the travel router (GL-MT3000, OpenWrt 21.02-SNAPSHOT, aarch64, mediatek/mt7981),
2026-08-29.

### muster does not use mTLS, and must not start

`server/muster/proof.py` is explicit. Cloudflare accepts a custom CA for client
certificate validation only on Enterprise, and even there a Tunnel opens a NEW
connection to the origin - so a certificate presented at the edge never reaches
the pod, and the application would be trusting a header a proxy wrote. Anything
that can set that header becomes any device it names.

So possession is proven at the application layer: muster issues a nonce, the
device signs it, and muster verifies the signature against the certificate it
issued. `api._proven_device` is documented as "THE ONE PLACE A DEVICE IS
AUTHENTICATED, and it stays the one place."

**This is the finding that shrinks the job.** A client-certificate handshake
through busybox is a project. Signing 43 bytes is a subprocess call.

### Nothing on the wire is Android-specific

Only muster's existing *agent* is. `POST /v1/auth/challenge`, `POST
/v1/device/config` and the enrollment pair take and return plain JSON; the
`app-config` file is opaque text that muster does not reorder, rename, translate
or invent keys for (`docs/policy.md`). **No muster server change is required to
serve a router.** The one thing that is genuinely Android-shaped is the
*semantics* the Android steward applies to the answer, and that is where this
design deliberately diverges - see "Absent is not withdraw".

### What the router can actually do

| | |
|---|---|
| `python3` | 3.9.15, with `ssl` (OpenSSL 1.1.1q) and `urllib` |
| `cryptography` | **absent**, not in the feed - do not plan around it |
| `python3-pynacl` | 1.4.0, already a required package - and the **wrong curve**. muster's CA refuses a non-EC key outright (`ca.py`) and `proof.py` verifies ECDSA-P256-SHA256. pynacl is Ed25519/Curve25519 and cannot help |
| `openssl` | `/usr/bin/openssl` 1.1.1q with `ecparam`, `req`, `pkey`, `dgst`, `base64`, `asn1parse` |

Exercised end to end on the box: P-256 keygen, a 204-byte DER CSR, a 91-byte
SPKI (which is what `key_id` is the SHA-256 of), and a 72-byte DER signature
that verified. **openssl for the crypto, stdlib for everything else, no new
packages.**

### The User-Agent is load-bearing

`urllib` with its default `Python-urllib/3.9` header gets **403 from Cloudflare**
before the request reaches muster. The identical request with any other
User-Agent gets 201. A client written the obvious way fails with something that
reads like an authentication problem, against a service whose logs show nothing,
because muster never saw it.

## Decisions

### Enrollment: `openssl` + the existing unauthenticated routes

`openssl ecparam -genkey` then `openssl req -new`, `POST /v1/enroll/requests`
with the CSR and a pairing code, then poll
`GET /v1/enroll/requests/{id}/identity`.

**A typed six-digit code, not a scanned one.** `enroll.py` explains that a
scanned code is self-vouching because there is nobody holding the device to
compare a fingerprint with, so the code has to be the thing that cannot be
guessed. A router is different: the operator is in an ssh session on the device
*and* at the console, so the fingerprint the router prints is a genuine second
copy and the vouch is a real check rather than a ritual. Take the stronger path
where it is available.

### Scope: per-device for the datapath key, `role-` for fleet credentials

`ROLE_FILES` includes `app-config`, so a role scope *could* carry this. It
should not. `policy.py` states the trade plainly: "a role is a statement that
these devices are interchangeable, and interchangeable devices share what they
run." A datapath key is shared between one router and one far end; putting it in
a role means one router's compromise is every router's, and there is exactly
one travel router.

`role-` earns its keep for credentials that genuinely are fleet-wide - a GitHub
runner registration token, a Datadog client token. The client does not care
which scope produced a file: `policy.for_device` resolves device, then role, then
kith, per file.

### Rotation: a key SET, with an overlap, from day one

The datapath key is symmetric and shared with the far end, so a single-value
swap has no make-before-break. Worse, on this router the management path rides
the very link the key configures - so a rotation that breaks the bond also
removes the route to the fix.

The delivered file therefore carries two values:

```
set  zippie.datapath  key.current   <44-char base64>
set  zippie.datapath  key.previous  <44-char base64>
```

Rotation is: the far end adds the new key to its accepted set, muster serves the
router `current=new, previous=old`, the router adopts, the far end drops the old.
`previous` equal to `current` is **refused**, because that expresses no overlap
at all while looking exactly like a rotation that is safely armed.

This costs nothing to support now and cannot be added later without a format
change. A key that cannot be rotated without an outage is a key that never gets
rotated.

### Where the key goes, and what reads it

Settled against zippie#7 rather than guessed. `zippie/auth.py` takes
`auth_key_file` - a **path** in `[policy]`, so the public repo carries no secret
- and `load_bond_secret` reads raw bytes from that file, strips trailing
whitespace, requires at least 16 bytes, and **refuses** a file readable by group
or other. Its own docstring says "Distribution of that file is what muster is
expected to take over."

So the client writes that file, at 0600, atomically, with nothing appended -
`load_bond_secret` strips a trailing newline, but a newline at one end and not
the other derives two different keys and presents as "the MAC never verifies",
which is a miserable thing to debug from either end.

`keys.json` keeps only the **record**: the revision, the key's digest, and where
it was put. Two homes for one credential is one more place to leak it from, and
the second is always the one nobody remembers to rotate.

Writing it into a JSON section instead would have been the "unit-tested, never
wired" shape this estate keeps rediscovering: a field nothing opens.

### The rotation overlap has a delivery half and a verifier half, and only one exists

zippie#7's four-rung ladder (`off` -> `observe` -> `sign` -> `require`, ends never
more than one rung apart, home first) solves **turning authentication on** without
an outage. It does not solve **changing the key afterwards**: `load_bond_secret`
returns one secret and `Identity` holds one derived key, so an authenticated bond
has no window in which both the old and the new key verify.

That is the same class of problem the ladder was built for, one layer down, and
on this router it is worse: the management path rides the link the key protects,
so a rotation that breaks the bond also removes the route to the fix.

This design delivers `key.current` **and** `key.previous` from day one, and writes
the second beside the first, because a format that cannot express an overlap
forces an outage at every rotation and cannot be widened later without changing
both ends at once. Nothing verifies against the previous key yet; the missing
half is a verifier that accepts either, and it is filed separately. Delivery is
deliberately not the thing blocking it.

### The cache is authoritative; muster is a refresher

`/etc/zippie/keys.json` (0600, already the home of the per-path WireGuard private
keys, already read by `config.load_config`) is what the agent starts from. The
agent does not import `musterwrt`, does not wait for it, and a test enforces
that. Three independent reasons:

- muster is behind Cloudflare, and a Cloudflare incident must not stop a router
  in a hotel bringing its bond up;
- the router has no RTC and often no NTP - `ca.py` backdates 12 hours for exactly
  this reason - and a router that decided its identity had lapsed because it
  believes it is 1970 could not fix itself;
- muster is a public host and reaching it does not require the bond *in general*,
  but on this box in this state it does. Measured 2026-08-29: `wan` and `wwan`
  both down, no IPv4 on `eth0`, `apclix0`, `apcli0` or `wwan0`, and the only
  default route in any table is `dev pbz0` - the bond. Its uplink today is a
  phone relay leg. Whichever is true on a given day, a key that can only be
  obtained over the link it configures must never gate that link.

**What the cache costs, plainly:** a stolen router keeps a working key until the
far end rotates. That is not a new exposure - it is exactly what the config file
already has - and the answer to it is the overlap format above, which makes
rotation cheap enough to actually perform.

### Absent is not withdraw, for this one file

muster's contract is that a file absent from a *successful* answer is removed
from the device, and for restrictions that is right: policy that only ever adds
is a ratchet, and the only way to undo a ratchet on a Device Owner is a factory
reset.

This file holds the key to the router's only uplink. Obeying "withdraw" would be
a control plane able to island a device by omission - one mistyped Secret key.
The client reports an absent `app-config` and changes nothing. It is a deliberate
divergence from the Android steward, recorded here and in the module so nobody
"fixes" it.

### Malformed or empty is refused, and refused whole

This is the 2026-08-29 outage as a class of bug. Every value must survive: not
blank, not `<placeholder>` shaped, and exactly 32 bytes of base64. A single
unparseable line refuses the **whole file** - skipping it and applying the rest
would adopt a new key without retaining the old one, which is half a rotation and
a router that can no longer talk to the far end. An unknown key under the subject
is refused rather than ignored, because silently dropping it makes a rotation
that did not happen look exactly like one that did.

Every refusal has the same postcondition, which is the contract worth stating on
its own: **`keys.json` is byte-identical to what it was.** A 503 from muster gets
the same treatment - muster refuses rather than answering empty when it cannot
say what a device should be, precisely because an empty answer is an instruction,
and the client mirrors that.

## Consequences

- No muster server change. The channel already exists.
- No new OpenWrt packages.
- The `server_public_key` splice in `deploy-openwrt.sh` (#3) stays until the
  travel router is enrolled and the datapath key is actually delivered; it is
  the same shape and does not have to be unwound.
- Enrollment is a one-time operator act and is not automated. A deploy that could
  enroll a device would need a credential that mints pairing codes, and that is a
  credential in CI - the thing this whole ADR exists to remove.

## Staged

1. **Shipped here.** The client, unarmed: parse, validate, refuse, atomically
   cache. Nothing imports it; nothing schedules it.
2. Enroll the travel router by hand, with the operator at the console and the
   fingerprint compared. Certificate to `/etc/zippie/muster/`.
3. Serve `<key_id>.app-config` from the `muster-policy` Secret with the router's
   current key as `key.current`, and confirm a fetch is a no-op.
4. Wire the refresh into cron. Only then does anything on the router depend on
   muster.

   **This stage was written as "refresh into cron and RENEWAL into the agent's
   own lifecycle", and half of it turned out not to exist.** Checked against the
   server on 2026-08-30: muster's only route to a certificate is
   `POST /v1/enroll/requests`, which requires a pairing code an administrator
   minted - vouched at the console, or self-vouched by QR. A device holding a
   valid certificate cannot trade it for a fresh one. There is no renewal to
   wire in, and a job written to attempt one would have failed forever against
   an endpoint that was never there.

   So renewal is a NOTIFICATION, not a mechanism: `muster-refresh.sh` reads the
   stored certificate every hour and tells the operator - log, and once a day
   Datadog - that a person must enroll the router again, starting 45 days out.
   That warning is not a prelude to an automatic retry. It IS the mechanism, and
   if it fails to reach somebody the refresh channel simply stops one day.

   The thresholds are the router's own and are deliberately not muster's
   `renew_after`. That value answers "when may a machine start trying", which
   here has no answer; theirs answers "how long does a PERSON have", and a
   person needs more warning than a retry loop. Nothing recomputes muster's
   fraction - `api.py` is right that a second copy of that arithmetic is a
   second definition of when a device renews.

   Unattended renewal is worth building and is tracked upstream as
   [muster#10](https://github.com/quadseven/muster/issues/10): a device that
   can already sign a nonce with an enrolled key has proved exactly what a
   pairing code proves, so possession of the current key should be sufficient
   vouch for the next certificate. That is the property `key_id` was chosen for
   ("identity survives renewal") and it is not yet redeemable.
5. Move `server_public_key` off the splice and onto this channel, and perform one
   real rotation with the overlap - which is the first time the design is proven
   rather than argued.
