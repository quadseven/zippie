"""Authenticating the BOND: a keyed MAC over the frame header (#2172).

A PORT, NOT A NEW SCHEME. travel/datapath-go/zippie/auth.go and identity.go
already implement this, they were reviewed, and the Go end and this one have to
interoperate byte-for-byte. Everything here mirrors those two files: the same
four rungs, the same labels, the same key derivation, the same 29-byte v3
header. Where this file departs from them it says so and says why.

THE ATTACK THIS CLOSES. The datapath listens on a public UDP port and a v2
frame is accepted on the strength of two magic bytes, a version byte and a
32-bit epoch. None of that is a secret, so a stranger who guesses or observes
the epoch can (1) point every reply at himself, because roaming follows
whoever spoke last, (2) send a 17-byte NACK and have up to 1400 bytes fired at
a victim of his choosing, and (3) reset the stream by claiming a restart.
WireGuard inside the tunnel makes none of those go away: they are attacks on
availability and on being a useful reflector, not on confidentiality.

WHY THERE IS A LADDER AND NOT A SWITCH. This is a live wire protocol between a
travelling router and a home exit, and BOTH ENDS MUST AGREE. The router is
deployed by hand and has drifted from git before, so "flip it on and deploy
both ends together" is not a thing that can be arranged for someone who may be
driving. Instead an endpoint stands on one of four rungs:

    off      emit v2, accept v2.                 Byte-identical to before this
                                                 file existed. THE DEFAULT.
    observe  emit v2, accept v2 and verified v3. Nothing changes on the wire;
                                                 the key is loaded and its id
                                                 logged, so both ends can be
                                                 proved to hold the same one.
    sign     emit v3, accept v2 and verified v3. The mixed-version rung.
    require  emit v3, accept verified v3 only.   Forgery is now impossible.

THE ONE RULE: the two ends may never be more than one rung apart. Every
adjacent pair interoperates - off/observe both speak v2, observe/sign works
because an observing receiver accepts both, sign/require works because a
requiring receiver is talking to an end that signs. Skipping a rung (off ->
sign against an off peer, or sign -> require against an observing peer) is what
breaks the bond, and it is the only thing that does.

ROLLING BACK is moving down a rung, in the reverse order. There is no state to
unwind: the rung is read from configuration at startup and nothing persists.

THE ORDER, for whoever is doing this rather than reading about it. Home first
at every step, because home is reachable and the router may be in a moving car:

    0. Deploy this build at both ends with auth off. Nothing changes. Verify:
       the stats line carries no "auth" section and the tunnel still carries.
    1. Put the same secret at both ends, mode 0600, then home to observe, then
       the router. Nothing changes on the wire. Verify: both logs print the
       same `header MAC observe (key XXXXXXXX)` id. If the two ids differ,
       STOP - the ends hold different key material and moving up would take
       the bond down.
    2. Home to sign. The far end still accepts v2, so this is safe with the
       router at observe. Verify: the router's auth.verified climbs and
       auth.rejected stays at zero.
    3. Router to sign, AND lower the tunnel MTU by 12 bytes first - a signed
       frame carries a 29-byte header, not 17, and a tunnel left at the old
       size drops full-length packets only, which looks like a routing fault.
       Verify: home's auth.legacy stops climbing and a large transfer still
       completes.
    4. Both ends to require, home first, once auth.legacy has been flat at zero
       at both ends for a soak period. Only now is forgery impossible.

AT ANY STEP, roll back by returning that end to the previous rung and
restarting it. A rung is a flag, not a migration.

ROTATING THE KEY AFTERWARDS (#13). The ladder covers turning authentication
on; it says nothing about changing the secret once both ends sign. With one
key per end there is no moment at which the old and the new key both verify,
so a rotation is both ends changing at the same instant - and on the travel
router the management path rides the very link the key protects, so a
rotation that breaks the bond also removes the route to the fix. Measured
while doing something else: an agent restart takes the router off the network
for 30-60 seconds, and a failed one took it off for eleven and a half minutes
until an armed rollback rescued it. A key that cannot be rotated without that
is a key that never gets rotated.

So an end may hold TWO keys: the one it signs with (`<auth_key_file>`) and a
retired one it still accepts (`<auth_key_file>.previous`, same mode, same
format, written beside the first by the router's own key refresh). The
receive side accepts a MAC under either; the send side only ever uses
current. That is the same asymmetry the ladder already uses between what a
rung emits and what it accepts, and it is what lets the two ends move one at
a time. The retired key stops being accepted when its file is removed, which
is the operator's explicit act - the same shape as the ladder's last rung.

THE ORDER, home first at every step for the reason the ladder gives. Each
step is one file change at one end followed by rebuilding that end's
identity, which means restarting it: the router's agent reads the key files
once in `start_transport` and the home pod reads them once in
`build_home_transport`, and neither re-reads on its own.

    1. Home: write the NEW secret to `.previous`, current stays OLD. Restart
       home. It now accepts both and still signs OLD, so the router, which
       knows only OLD, notices nothing. Verify: home logs the retired key's
       id beside the current one, and the router's auth.rejected stays flat.
    2. Router: NEW to current, OLD to `.previous`. Restart the agent. The
       router signs NEW, which home accepts under its `.previous`; home still
       signs OLD, which the router accepts under its `.previous`. Verify:
       auth.verified climbs at both ends and auth.rejected stays flat.
    3. Home: NEW to current, remove `.previous`. Restart home. Both ends now
       sign NEW and verify it as current. Verify as in step 2.
    4. Router: remove `.previous`. Restart the agent. Only now is OLD refused
       everywhere.

The new key goes into `.previous` at home FIRST rather than into current,
because home moving its signing key first would have it signing with a key
the router cannot yet verify - which is the outage this exists to avoid. At
any step, roll back by restoring that end's files and restarting it.

WHAT THIS DOES NOT DO, stated plainly so nobody reads more into it than is
there:

  - NO REPLAY PROTECTION. The MAC covers the header and payload with a static
    key, and it cannot cover the UDP source address because NAT rewrites it.
    So an attacker who has OBSERVED a valid frame can resend it from his own
    address and still roam the link to himself. That is a real residual, and
    it is a deliberate one: the bar moves from "anybody on the internet with a
    17-byte packet" to "somebody already on the path", which is the attacker
    who can do worse things anyway. Closing it needs the roam to additionally
    require a sequence the stream has not seen; that is a separate change.
  - NO FORWARD SECRECY and no automatic rotation. Rotating the secret is an
    operator action at both ends, in the order above. The overlap makes it
    performable without an outage; nothing here decides WHEN to rotate, and
    a retired key stays accepted for exactly as long as its file exists.
  - NO CONFIDENTIALITY, on purpose. See new_bond_identity.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import struct
from dataclasses import dataclass
from enum import IntEnum

# The v2 magic and version, imported rather than repeated. A v3 header is the
# v2 header plus two fields, so the two formats MUST agree on those bytes or a
# v3 frame would not even be recognisable as belonging to this protocol. They
# are private names in datapath.py; importing them is deliberate, because the
# alternative is a second copy of the same two constants that can drift.
from zippie.datapath import _MAGIC, _VERSION, DatapathError, Frame

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire v3
# ---------------------------------------------------------------------------
#
# LAYOUT, identical to identity.go:
#
#   v2, 17 bytes: magic(2) ver(1) flags(1) pathID(1) seq(8) epoch(4)
#   v3, 29 bytes: ................ same 17 ................ client(4) mac(8)
#
# The MAC covers the whole header INCLUDING the client id and the version byte,
# plus the payload. Covering the version is what stops a downgrade: an attacker
# cannot re-label a v3 frame as v2 without invalidating it, and an
# authenticated reader refuses v2 outright at the top rung.
_VERSION_V3 = 3

# The signed prefix: everything up to but not including the MAC field.
_HEADER_V3_SIGNED = struct.Struct("!2sBBBQII")

MAC_LEN = 8
# 29. THE TUNNEL MTU DEPENDS ON THIS. The travel router's pbz0 is sized as
# (smallest leg MTU minus header), so moving to v3 costs 12 bytes of payload
# per packet and the router config must follow. Getting it wrong does not fail
# loudly - it fragments, or silently drops the large packets only.
HEADER_LEN_V3 = _HEADER_V3_SIGNED.size + MAC_LEN


class UnauthenticatedError(DatapathError):
    """Every failure to prove a frame's identity: wrong key, wrong peer id,
    tampering, or a v2 frame offered to a reader that requires authentication.

    Deliberately ONE error - telling an attacker which of those it was is free
    information, and every caller does the same thing with it anyway (drop,
    count, carry on).

    A SUBCLASS OF DatapathError so that it is fail-safe. Every existing receive
    path in this package already catches DatapathError and drops the datagram;
    subclassing means a path this change did not think to update still drops an
    unauthenticated frame rather than raising through the loop. Callers that
    want to COUNT the two apart catch this one first, which transport.py does.
    """


class AuthLevel(IntEnum):
    """One rung of the ladder.

    OFF IS THE ZERO VALUE, deliberately. Everything that constructs a Transport
    today gets the behaviour it has always had, and no partial deployment of
    this change can alter a single byte on the wire.
    """

    OFF = 0
    OBSERVE = 1
    SIGN = 2
    REQUIRE = 3

    def __str__(self) -> str:
        return self.name.lower()

    @property
    def signs(self) -> bool:
        """Whether this rung puts v3 bytes on the wire."""
        return self in (AuthLevel.SIGN, AuthLevel.REQUIRE)

    @property
    def verifies(self) -> bool:
        """Whether this rung checks the MAC on an arriving v3 frame."""
        return self is not AuthLevel.OFF

    @property
    def accepts_legacy(self) -> bool:
        """Whether an unauthenticated v2 frame is still accepted.

        True everywhere except the top rung: that is what makes a mixed-version
        bond work, and it is also exactly why the top rung is a separate step.
        """
        return self is not AuthLevel.REQUIRE


def parse_auth_level(s: str) -> AuthLevel:
    """Turn operator input into a rung.

    Refuses anything it does not recognise rather than defaulting, because a
    typo that silently meant "off" would look exactly like a working rollout.
    """
    key = (s or "").strip().lower()
    if key in ("", "off"):
        return AuthLevel.OFF
    for level in (AuthLevel.OBSERVE, AuthLevel.SIGN, AuthLevel.REQUIRE):
        if key == level.name.lower():
            return level
    raise ValueError(
        f"{s!r} is not an auth level; want off, observe, sign or require")


# Domain-separates the bond MAC key from every other use of the same secret.
# One SHA-256 costs nothing and removes the question of whether reusing the raw
# secret in two primitives is safe.
_BOND_KEY_LABEL = b"zippie/bond-mac/v1\x00"
# Derives the short PUBLIC name of a key. See Identity.key_id.
_KEY_ID_LABEL = b"zippie/bond-mac-keyid/v1\x00"

# The smallest secret worth calling one. A WireGuard preshared key is 32 raw
# bytes (44 base64 characters), so the intended source clears this comfortably;
# the floor exists to catch an empty or truncated file, which is the realistic
# failure.
MIN_BOND_SECRET = 16


def derive_bond_key(secret: bytes) -> bytes:
    """Turn a shared secret into the HMAC key both ends use.

    WHERE THE SECRET COMES FROM. auth.go names the WireGuard preshared key as
    the intended source, because both ends already hold it. THIS DEPLOYMENT HAS
    NO PSK - the home server provisions peers without one (home/bond-server/
    zippie_home.py), so there is nothing already shared to key this from, and
    the secret is a separate one carried in its own file. auth.go anticipated
    exactly that: "this function takes bytes and does not care where they came
    from, and the deployment decides".

    The cost of a separate secret is one more thing to provision and one more
    thing to get out of step; the benefit is that rotating WireGuard and
    rotating the bond MAC stay independent failure domains. Distribution of
    that file is what muster is expected to take over - see the Phase 2 issue.

    The secret is never stored, never logged and never returned; only the
    derived key leaves this function.
    """
    if len(secret) < MIN_BOND_SECRET:
        raise ValueError(
            f"auth level needs a key: secret is {len(secret)} bytes, "
            f"want at least {MIN_BOND_SECRET}")
    return hashlib.sha256(_BOND_KEY_LABEL + secret).digest()


def load_bond_secret(path: str) -> bytes:
    """Read the shared secret from a file.

    A FILE AND NOT A FLAG OR AN ENVIRONMENT VARIABLE. A flag lands in
    /proc/<pid>/cmdline and in the output of `ps` for every user on the box; an
    environment variable lands in /proc/<pid>/environ and in any crash dump. A
    file can be mode 0600 and owned by the service user, and this function
    REFUSES to read it if it is not - refuses rather than warns, because a
    secret readable by every process on the router is not a secret, and a
    warning in a log nobody reads is how it stays that way.

    Trailing whitespace is trimmed because the realistic way this file gets
    written is `wg genpsk > /etc/zippie/bond.key`, which appends a newline. A
    newline at one end and not the other would derive two different keys and
    present as "the MAC never verifies", which is a miserable thing to debug.
    """
    st = os.stat(path)
    perm = st.st_mode & 0o777
    if perm & 0o077:
        raise PermissionError(
            f"key file is readable by others: {path} is mode {perm:#o}; "
            f"run chmod 600 {path}")
    with open(path, "rb") as fh:
        secret = fh.read().strip()
    if len(secret) < MIN_BOND_SECRET:
        raise ValueError(
            f"auth level needs a key: {path} holds {len(secret)} bytes after "
            f"trimming, want at least {MIN_BOND_SECRET}")
    return secret


# Where the retired secret lives: beside the current one, under this suffix.
# The router's key refresh (the module that talks to muster, which this one
# must never import) writes it there at 0600 with nothing appended; a test runs
# this reader against what that writer produces, because the two live in
# different modules and only this one ships to the home end. If they ever
# disagreed the retired key would be delivered and never read, which looks
# exactly like a rotation that broke the bond.
PREVIOUS_KEY_SUFFIX = ".previous"


def load_previous_bond_secret(key_file: str) -> bytes | None:
    """Read the RETIRED secret from `<key_file>.previous`, or None if there is
    no such file.

    ABSENT IS THE NORMAL STATE, not an error and not a warning. A previous key
    exists only for the duration of a rotation, and a deployment that has
    never rotated - which is every deployment before the first time - has no
    such file and must start exactly as it always did. The one thing an absent
    file must never be is quietly confused with a present one that cannot be
    read: a file that exists but is readable by others, or is too short, is
    refused with the same errors as the current key, because a retired key is
    still a key that verifies frames and gets the same protection.

    Takes the CURRENT key file's path rather than the previous one's, so the
    naming convention lives here and in the writer and nowhere else; a caller
    that could spell the suffix itself could also misspell it, and a misspelled
    previous key reads as absent, which is the silent failure above.
    """
    try:
        return load_bond_secret(key_file + PREVIOUS_KEY_SUFFIX)
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class Identity:
    """The wire credential: who this end says it is, and the key that proves it.

    `key` is the DERIVED key (derive_bond_key), never the raw secret. It is
    the ONLY key this end signs with.

    `previous_key` is a retired key, derived the same way, that this end still
    ACCEPTS on a frame it receives. None means there is none, which is the
    steady state; it is not None only while a rotation is in progress (see the
    module docstring). Verification tries `key` first and falls back to this;
    signing never looks at it. Defaults to None so that every existing way of
    constructing an Identity, and every existing frame on the wire, is
    unchanged by the field's existence.
    """

    client_id: int
    key: bytes
    previous_key: bytes | None = None

    def key_id(self) -> str:
        """A short, one-way name for the key, safe to log and to report.

        WHY IT EXISTS: the single most common way a rollout like this fails is
        the two ends holding different key material, and the only way to notice
        is that every frame fails to verify - which looks identical to a bug in
        the MAC itself. Comparing key ids across the two ends distinguishes
        those in one step, without either operator ever seeing a key.

        SAFE TO PUBLISH because it is 32 bits of SHA-256 output over a labelled
        preimage: recovering the key needs a preimage attack, and CONFIRMING a
        guessed key needs the key to be guessable in the first place, which a
        32-byte secret is not. It would be an unsafe thing to publish for a
        low-entropy secret, which is the other reason derive_bond_key has a
        floor.
        """
        return hashlib.sha256(_KEY_ID_LABEL + self.key).hexdigest()[:8]

    def previous_key_id(self) -> str | None:
        """The retired key's id, or None when there is no retired key.

        Derived exactly as key_id is, so the id home prints for its retired
        key can be compared with the id the router prints for its current one
        - which is the check that step 2 of a rotation rests on. None rather
        than an empty string, and the callers that print it print nothing at
        all when it is None, so a deployment that has never rotated reports
        exactly the ids it always did and a log parser sees no new field.
        """
        if self.previous_key is None:
            return None
        return hashlib.sha256(_KEY_ID_LABEL + self.previous_key).hexdigest()[:8]


def new_bond_identity(
    peer_id: int, secret: bytes, previous_secret: bytes | None = None,
) -> Identity:
    """The credential for the router-to-home bond: one shared symmetric key,
    used by both ends to sign and to verify.

    `previous_secret`, when given, is a retired secret this end will still
    accept; it is derived with the same label as the current one, because the
    far end derived it that way when it was current. A previous secret that
    equals the current one is NOT refused here: it verifies nothing extra and
    is visible as two equal key ids in the log, and refusing it would stop the
    agent on a router whose only route to the fix is the agent. The router's
    key refresh refuses it at delivery time instead, where refusing is cheap.

    NOT SEALED, unlike the Go client-mode identity. The bond carries WireGuard
    ciphertext produced by the router, so a second encryption layer would spend
    CPU and 28 bytes per frame to encrypt something already encrypted.
    Authentication is what is missing here; confidentiality is not.

    peer_id is the v3 client id both ends put on the wire. For a two-party bond
    it identifies the bond rather than a client, and both ends must be given
    the same one - a mismatch fails verification with the same single error as
    a bad MAC, on purpose.
    """
    if peer_id == 0:
        # Zero cannot be told apart from a field nobody set, and the receiver
        # compares it, so a zero at one end only would be a silent mismatch.
        raise ValueError("auth peer id must not be zero")
    if not 0 < peer_id <= 0xFFFFFFFF:
        raise ValueError(f"auth peer id out of range: {peer_id}")
    return Identity(
        client_id=peer_id,
        key=derive_bond_key(secret),
        previous_key=(None if previous_secret is None
                      else derive_bond_key(previous_secret)),
    )


def compute_mac(key: bytes, signed_header: bytes, payload: bytes) -> bytes:
    """HMAC-SHA256 truncated to MAC_LEN, over header-without-mac then payload.

    HMAC-SHA256 rather than something faster because it is stdlib, and both
    this module and the Go one are stdlib-only by design - the GL-MT3000's
    Python 3.9 has no `cryptography` module at all, so anything else would not
    run on the device this agent exists to run on.

    Truncating to 8 bytes is standard practice and leaves 2^64 forgery work per
    packet, far beyond what a UDP flood can search, while saving 8 bytes of MTU
    that this project measures in single digits.
    """
    return hmac.new(key, signed_header + payload, hashlib.sha256).digest()[:MAC_LEN]


def pack_as(frame: Frame, identity: Identity) -> bytes:
    """Serialise `frame` as an authenticated v3 datagram.

    SIGNED WITH THE CURRENT KEY ONLY. `identity.previous_key` is never
    consulted here, whatever it holds: an end that signed with its retired key
    would be moving the rotation backwards, and a far end that has already
    dropped that key would refuse every frame - which is the outage the
    overlap exists to prevent, arriving from the other direction.
    """
    signed = _HEADER_V3_SIGNED.pack(
        _MAGIC, _VERSION_V3, frame.flags, frame.path_id, frame.seq,
        frame.epoch, identity.client_id,
    )
    return signed + compute_mac(identity.key, signed, frame.payload) + frame.payload


def unpack_as(raw: bytes, identity: Identity) -> Frame:
    """Parse and VERIFY a v3 datagram.

    An authenticated reader refuses v2 outright. That is the downgrade guard:
    if presenting an old-format frame were enough to skip the check, the MAC
    would protect nothing. `unpack_auth` is what decides whether a v2 frame is
    offered to this function at all.

    EITHER KEY VERIFIES while the identity holds two. The current key is tried
    first, because outside a rotation it is the only one there is and during
    one it is the key most frames arrive under; the retired key is tried only
    when the current one fails. Both comparisons are constant-time for the
    reason given inline. What this does leak is WHICH key a frame verified
    under - a frame under the retired key costs one more HMAC - and that is
    accepted: it tells an observer a rotation is in progress, which the two
    ends' restarts already told him, and nothing about either key.

    The retired key is accepted for as long as `Identity.previous_key` is
    set, which is for as long as `<auth_key_file>.previous` existed when the
    identity was built. Removing the file is the operator's explicit act, and
    it takes effect when the identity is next rebuilt - a restart of that end
    - and not before. A frame under the retired key is then refused with the
    same single error as any other bad MAC.

    NOTE ON FLAG 0x20. Go's UnpackAs additionally refuses a frame whose flags
    carry FlagEncrypted (0x20) when the reader holds no sealer. That check is
    deliberately NOT ported: 0x20 is FLAG_RETRANSMIT in this implementation
    (transport.py, and frame.go says so explicitly - "NOT 0x20, EVEN THOUGH
    THAT IS WHAT PYTHON USES FOR THE SAME MEANING"). Porting it would make this
    end reject every retransmit it was sent. Python has no sealing, so there is
    no sealed frame for it to guard against.
    """
    if len(raw) < HEADER_LEN_V3:
        raise DatapathError(f"short frame: {len(raw)} < {HEADER_LEN_V3}")
    magic, version, flags, path_id, seq, epoch, claimed = (
        _HEADER_V3_SIGNED.unpack(raw[:_HEADER_V3_SIGNED.size]))
    if magic != _MAGIC:
        raise DatapathError(f"bad magic: {magic!r}")
    if version != _VERSION_V3:
        # Includes v2. See the docstring: refusing this is the point.
        raise UnauthenticatedError(
            f"version {version} offered to an authenticated reader")
    if claimed != identity.client_id:
        raise UnauthenticatedError("client id mismatch")

    signed = raw[:_HEADER_V3_SIGNED.size]
    payload = raw[HEADER_LEN_V3:]
    got = raw[_HEADER_V3_SIGNED.size:HEADER_LEN_V3]
    # Constant time, for BOTH keys: a byte-at-a-time comparison leaks the MAC
    # one byte per forgery attempt, which is a practical attack on an open UDP
    # port, and a retired key that is still accepted is still a key that a
    # forged MAC under it would steer the tunnel with.
    verified = hmac.compare_digest(compute_mac(identity.key, signed, payload), got)
    if not verified and identity.previous_key is not None:
        verified = hmac.compare_digest(
            compute_mac(identity.previous_key, signed, payload), got)
    if not verified:
        raise UnauthenticatedError("frame failed authentication")

    return Frame(seq=seq, path_id=path_id, payload=payload, flags=flags,
                 epoch=epoch, client_id=claimed)


def pack_auth(frame: Frame, identity: Identity | None, level: AuthLevel) -> bytes:
    """Serialise at the rung this endpoint stands on: v3 and authenticated once
    the rung signs, v2 bytes otherwise.

    A None identity always means v2, whatever the rung says, so there is
    exactly one way to be unauthenticated and it is the absence of a credential.
    """
    if identity is None or not level.signs:
        return frame.pack()
    return pack_as(frame, identity)


def unpack_auth(
    raw: bytes, identity: Identity | None, level: AuthLevel,
) -> tuple[Frame, bool]:
    """Parse one datagram under this endpoint's rung.

    Returns (frame, authenticated), so a caller can count the two apart and
    watch a rollout finish.

    THE VERSION BYTE SELECTS THE CHECK, NOT THE CONFIGURATION: a v3 frame is
    always verified (a rung that verifies at all refuses to take v3 on trust),
    and a v2 frame is accepted only while the rung still tolerates legacy. That
    ordering is what makes "accept both" safe - the presence of a MAC is never
    optional for a frame that claims to have one.
    """
    if identity is None or not level.verifies:
        return Frame.unpack(raw), False
    if len(raw) >= 3 and raw[:2] == _MAGIC and raw[2] == _VERSION_V3:
        return unpack_as(raw, identity), True
    # Not v3. Let Frame.unpack produce its own specific error for genuine
    # garbage - a short datagram is a short datagram, and calling it an
    # authentication failure would hide malformed input inside the security
    # counter.
    frame = Frame.unpack(raw)
    if not level.accepts_legacy:
        raise UnauthenticatedError(
            f"unauthenticated v{_VERSION} frame at auth level {level}")
    return frame, False


def build_identity(
    level: AuthLevel, key_file: str, peer_id: int,
) -> Identity | None:
    """Load the credential a rung needs, or refuse the configuration.

    ONE PLACE where "which rungs need a key" is decided, so the home transport,
    the travel agent and the tests cannot each answer it differently. Both
    inconsistent combinations are refused rather than silently resolved,
    because each of them looks like a working rollout from the outside:

      - a key with the rung left at off is a rollout that will never start
      - a rung above off with no key is a rung that cannot verify anything

    Returns None only for the off rung, which is the one configuration that
    needs no credential.

    THE RETIRED KEY IS LOADED HERE TOO, from `<key_file>.previous`, so that
    both ends consume it through the one call they already make and neither
    end can be the one that forgot. Its id is logged beside the current one
    ONLY when it exists: a deployment that has never rotated prints the line
    it always printed and nothing else. That log line is what step 1 of a
    rotation is verified by (module docstring), and it is the only place the
    retired key's presence is visible from outside the process - a rotation
    whose overlap silently failed to load would look identical to one that
    was never started, right up until the router moved and the bond dropped.

    The files are read ONCE, here, when the identity is built. Neither the
    agent nor the home pod rebuilds its identity while running, so a file
    added or removed afterwards takes effect at that end's next restart. The
    docstrings on unpack_as and load_previous_bond_secret say the same thing
    from the other side.
    """
    if level is AuthLevel.OFF:
        if key_file:
            raise ValueError(
                "an auth key file was configured with auth level off: set the "
                "level to observe, sign or require, or remove the key file")
        return None
    if not key_file:
        raise ValueError(f"auth level {level} needs a key file")
    identity = new_bond_identity(
        peer_id, load_bond_secret(key_file), load_previous_bond_secret(key_file))
    if identity.previous_key is not None:
        log.info("header MAC also accepts retired key %s (current %s)",
                 identity.previous_key_id(), identity.key_id())
    return identity
