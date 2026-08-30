"""Fetch this router's own secrets from muster, and refuse to apply garbage.

WHY THIS EXISTS. On 2026-08-29 a deploy shipped `server_public_key =
"<server-public-key>"` to suzu. The placeholder is in the repo on purpose - the
real value must not be - and nothing had ever substituted it back. `wg setconf`
refused it, the bond never came up, and because the agent owns the router's only
default route the box left the network entirely. Recovery took physical access.

The lesson is not "add a placeholder check". It is that a device secret which
lives in a config file has to travel through every place that file travels: a
git repo, a CI runner's memory, a `tar` over a pipe. muster already solves this
for phones, and `policy.py`'s own docstring says why `app-config` is the file
that may not be shared - "a credential under the shared scope is a credential
handed to everyone". The router should be reading from that same channel.

NOT mTLS, AND THIS IS THE THING MOST PEOPLE GET WRONG ABOUT muster. Read
`server/muster/proof.py`: muster deliberately does NOT authenticate devices at
the transport layer, because Cloudflare will not carry a client certificate
through a Tunnel to the origin, and a header a proxy writes is not a client
certificate - it is whatever anything on the path says it is. Possession is
proven at the APPLICATION layer instead:

    router                                            muster
      |  POST /v1/auth/challenge                        |
      |<------------------------ nonce -----------------|
      |  sign the nonce with the enrolled key           |
      |  POST /v1/device/config                         |
      |    { nonce, signature_b64, certificate_pem }    |
      |<--------- { revision, files: {...} } -----------|

That is the entire reason this file can exist at all. A client-certificate
handshake through busybox is a project; signing 43 bytes with `openssl dgst` is
a subprocess call. Nothing here is Android-specific, because the wire protocol
never was - only muster's existing AGENT is.

WHAT IS ON THIS ROUTER, measured 2026-08-29 on suzu (GL-MT3000, OpenWrt 21.02,
aarch64):

    python3          3.9.15, with `ssl` (OpenSSL 1.1.1q) and urllib
    cryptography     ABSENT. Not in the feed; do not plan around it.
    pynacl           1.4.0 - Ed25519/Curve25519, which is the WRONG curve.
                     muster's CA refuses a non-EC key outright (ca.py) and
                     proof.py verifies ECDSA-P256-SHA256. pynacl cannot help.
    openssl          /usr/bin/openssl 1.1.1q, with ecparam, req, pkey, dgst,
                     base64 and asn1parse. Verified end to end on the box:
                     P-256 keygen, a 204-byte DER CSR, a 91-byte SPKI, and a
                     72-byte DER signature that verified.

So: openssl for the crypto, stdlib for everything else. No new packages.

THE USER-AGENT IS LOAD-BEARING AND THAT IS NOT A JOKE. Measured on suzu the
same day: `urllib` with its default `Python-urllib/3.9` header gets **403 from
Cloudflare** before the request ever reaches muster, while the identical request
with any other User-Agent gets 201. A client written the obvious way fails with
an error that looks like an authentication problem, from a service whose logs
show nothing at all, because muster never saw it.

WHAT THE ROUTER DOES WHEN muster CANNOT BE REACHED: nothing. The cached value in
`/etc/zippie/keys.json` is authoritative and this module is a REFRESHER, never a
precondition. The agent does not import it, does not wait for it, and starts
from the cache. Three reasons, and any one of them is sufficient:

  * muster is behind Cloudflare. A Cloudflare incident must not be able to stop
    a router in a hotel from bringing its bond up.
  * The router boots with no clock. `ca.py` backdates by 12 hours for exactly
    this reason, and a router that decided it had no valid identity because it
    believes it is 1970 would refuse to configure itself.
  * On this router today the only default route is the bond itself - measured
    2026-08-29: `wan` and `wwan` both down, no IPv4 on eth0, apclix0, apcli0 or
    wwan0, `default dev pbz0`. muster is a public host and reaching it does not
    REQUIRE the bond in general; it does on this box in this state. Whichever is
    true on any given day, a key that can only be obtained over the link it
    configures must never gate that link.

WHAT THE CACHE COSTS, said plainly: a router that is stolen keeps a working key
until the far end rotates. That is not a new exposure - it is exactly what the
config file already has - and it is the trade this module is here to make
better, not worse, by making rotation cheap enough to actually do.

A FILE ABSENT FROM A SUCCESSFUL ANSWER IS NOT AN INSTRUCTION TO DELETE, HERE.
That IS muster's contract, and the Android steward implements it correctly:
policy that only ever adds is a ratchet. But the file in question here holds the
key to this router's only uplink, so obeying "withdraw" would be a control plane
able to island a device by omission - one mistyped Secret key. This module
therefore reports an absent `app-config` and changes nothing. It is a deliberate
divergence from the Android agent, written down here so that a future reader
does not "fix" it.

ONE BAD LINE REFUSES THE WHOLE FILE. Skipping a line that will not parse and
applying the rest would apply half a rotation: the new key adopted, the old one
not retained, and a router that can no longer talk to the far end. There is no
useful partial state here, so there is no partial apply.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# ANYTHING BUT THE DEFAULT. See the module docstring: Cloudflare answers 403 to
# `Python-urllib/3.9` before muster is reached, so a client that does not set
# this fails with a message about authentication for a request the server never
# received. Measured on suzu 2026-08-29.
USER_AGENT = "musterwrt/1 (openwrt; zippie)"

# The router's own name for the thing muster's `app-config` grammar calls a
# package. muster does not translate, rename or invent keys (docs/policy.md);
# the vocabulary belongs to whatever reads the file, and this is ours.
DATAPATH_SUBJECT = "zippie.datapath"

# WHERE THE SECRET ITSELF GOES, and it is a file rather than a JSON field
# because that is what reads it.
#
# `zippie/auth.py` (zippie#7) takes `auth_key_file` - a PATH in `[policy]`, so
# the public repo carries no secret - and `load_bond_secret` reads raw bytes from
# it, strips trailing whitespace, requires at least MIN_BOND_SECRET bytes, and
# REFUSES a file that is readable by group or other. Its own docstring says
# "Distribution of that file is what muster is expected to take over", which is
# this module.
#
# Writing the key into keys.json instead would have been a section nothing reads
# - the shape this estate keeps rediscovering as "unit-tested, never wired".
#
# THE PREVIOUS KEY GOES BESIDE IT, under this suffix. Nothing verifies against it
# yet: auth.py holds ONE key, so the overlap a rotation needs cannot be expressed
# through its loader today. The file is written anyway so that delivery is not
# the thing blocking a rotation later, and the reason is recorded here rather
# than discovered by whoever tries.
PREVIOUS_SUFFIX = ".previous"

# What keys.json keeps: the RECORD, never the secret. Two homes for one credential
# is one more place to leak it from and one more to forget to rotate.
KEYS_SECTION = "datapath"

# A datapath key is 32 bytes. Base64 of 32 bytes is 44 characters ending in a
# single `=`, which is also the shape of a WireGuard key - the same regex the
# deploy script uses to refuse the placeholder that took the router down.
_KEY = re.compile(r"^[A-Za-z0-9+/]{43}=$")

# WHAT KILLED THE ROUTER, AS A PATTERN. `<server-public-key>` is valid TOML,
# valid base64-ish text, and a perfectly ordinary string to every check that
# existed. It is refused here by SHAPE rather than by name, so the next scrubbed
# value is caught too.
_PLACEHOLDER = re.compile(r"^<.*>$")

# The whole vocabulary this router will act on. A CLOSED SET, for the same
# reason `policy.MANAGED_FILES` is closed on the server: what is written here
# becomes key material, and an open set is a remote write primitive over
# whatever else lives in keys.json.
CURRENT = "key.current"
PREVIOUS = "key.previous"
_KNOWN_KEYS = (CURRENT, PREVIOUS)


class Refused(Exception):
    """muster answered, and what it said may not be applied.

    NOT an error about reaching muster - that is `Unreachable`. The distinction
    is the whole operational story: unreachable means try later and nothing is
    wrong with the estate, refused means somebody wrote something that would
    have taken a router off the network and it was caught in time.
    """


class Unreachable(Exception):
    """muster could not be asked. The cache stands and nothing changed."""


# ---------------------------------------------------------------- parsing


def parse_app_config(text: str) -> dict[str, dict[str, str]]:
    """muster's `app-config` grammar, as documented in muster/docs/policy.md.

        set       <subject> <key> <value>
        set-bool  <subject> <key> <value>

    A `#` STARTS A COMMENT ONLY AT THE START OF A LINE, and that rule is
    muster's, not ours. A value here may be a credential, and truncating one at
    an inner `#` produces a router that authenticates with something almost
    right - which looks like a problem at the far end for as long as anyone is
    willing to look.

    THE SUBJECT IS ON EVERY LINE and there are no section headers, also muster's
    rule: a mistyped `[header]` silently assigns every key beneath it to the
    wrong subject, and the key most likely to be under one is a credential. A
    wrong subject on one line is one wrong line.
    """
    out: dict[str, dict[str, str]] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("#") or not raw.strip():
            continue
        parts = raw.split(None, 3)
        if len(parts) < 4:
            raise Refused(
                f"line {number}: expected `set <subject> <key> <value>`, got "
                f"{len(parts)} field(s). One bad line refuses the whole file - "
                "half a key set is a router that cannot talk to the far end."
            )
        verb, subject, key, value = parts[0], parts[1], parts[2], parts[3].strip()
        if verb not in ("set", "set-bool"):
            raise Refused(f"line {number}: '{verb}' is not a verb this router knows")
        out.setdefault(subject, {})[key] = value
    return out


def _refuse_bad_key(name: str, value: str) -> None:
    """Everything a value has to survive before it may become key material."""
    if not value:
        # muster's own rule, for its own reason: the receiver cannot tell blank
        # from absent, so a blank line reads as "clear this" and does nothing.
        raise Refused(f"{name} is blank. Delete the line instead of blanking it.")
    if _PLACEHOLDER.match(value):
        raise Refused(
            f"{name} is a placeholder ({value!r}). This is the 2026-08-29 outage "
            "exactly: a placeholder is valid text and every check that only asks "
            "whether the file parses will pass it."
        )
    if not _KEY.match(value):
        raise Refused(
            f"{name} is not a 32-byte base64 key: {len(value)} characters. "
            "Refusing rather than applying - the cached key stays in force."
        )


def datapath_keys(files: dict[str, str]) -> dict[str, str]:
    """The validated key set in muster's answer, or Refused.

    `files` is the object muster returns from POST /v1/device/config. An ABSENT
    `app-config` is refused rather than treated as "withdraw" - see the module
    docstring for why this router does not implement muster's removal semantics
    for this one file.
    """
    if "app-config" not in files:
        raise Refused(
            "muster's answer carries no app-config for this device. That is a "
            "policy scope that does not exist, not an instruction to withdraw "
            "the key to this router's only uplink - so nothing changed."
        )
    parsed = parse_app_config(files["app-config"])
    mine = parsed.get(DATAPATH_SUBJECT)
    if not mine:
        raise Refused(
            f"app-config names no `{DATAPATH_SUBJECT}` settings, so there is "
            "nothing here for the datapath and the cached key stays in force."
        )

    unknown = sorted(k for k in mine if k not in _KNOWN_KEYS)
    if unknown:
        # LOUD, NOT IGNORED. A key this router does not act on is either a typo
        # in the one file that carries credentials, or a newer muster talking to
        # an older router - and silently dropping it would make a rotation that
        # did not happen look exactly like one that did.
        raise Refused(
            f"app-config sets {unknown} under {DATAPATH_SUBJECT}, which this "
            f"router does not know. Known keys: {list(_KNOWN_KEYS)}."
        )

    if CURRENT not in mine:
        raise Refused(f"app-config sets no {CURRENT}, so there is no key to adopt.")

    keys = {CURRENT: mine[CURRENT]}
    _refuse_bad_key(CURRENT, keys[CURRENT])

    if PREVIOUS in mine:
        _refuse_bad_key(PREVIOUS, mine[PREVIOUS])
        if mine[PREVIOUS] == keys[CURRENT]:
            # NOT HARMLESS. The overlap is the whole mechanism that makes a
            # rotation survivable: the far end accepts both while the two ends
            # cross over. Two identical values express no overlap at all, and
            # they look exactly like a rotation that is safely armed.
            raise Refused(
                f"{PREVIOUS} equals {CURRENT}, which is not an overlap. A "
                "rotation with no overlap takes the bond down at the moment the "
                "two ends disagree."
            )
        keys[PREVIOUS] = mine[PREVIOUS]
    return keys


# ---------------------------------------------------------------- the cache


def _digest(value: str) -> str:
    """A name for a key that is not the key.

    Nothing in this module ever logs key material. A short digest is enough to
    answer "did it change" and "do the two ends agree", which is every question
    a log is asked about a secret.
    """
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:12]


def merge_into_keys(
    existing: dict, keys: dict[str, str], revision: str, key_path: str
) -> dict:
    """What keys.json becomes: a RECORD of the delivery, not the secret.

    A WHOLESALE REWRITE WOULD BE A DISASTER and it is the obvious implementation.
    keys.json holds the per-path WireGuard private keys this router cannot
    regenerate - `store.py` makes the same argument about legs.json, that losing
    it means losing something nobody can recompute. This module owns exactly one
    top-level object in that file and must not be able to touch the rest.

    NO KEY MATERIAL IN HERE. The secret lives in one file, the one `auth.py`
    reads. What is kept is what answers an operator's questions without holding
    the credential: which revision, which key (by digest), and where it was put.
    """
    merged = dict(existing)
    record = {
        # The revision muster computed over the bytes it served. Stable across
        # pods and restarts (policy._revision), so it answers "is this router on
        # the policy the operator wrote" without anybody holding the key.
        "revision": revision,
        "key_file": key_path,
        "current_digest": _digest(keys[CURRENT]),
    }
    if PREVIOUS in keys:
        record["previous_digest"] = _digest(keys[PREVIOUS])
    merged[KEYS_SECTION] = record
    return merged


def write_secret(path: Path, secret: str) -> None:
    """One key, at 0600, atomically, with nothing appended.

    THE MODE IS NOT ADVISORY. `auth.load_bond_secret` refuses a key file that is
    readable by group or other - refuses rather than warns - so a key written at
    0644 is an agent that will not start rather than a quiet weakness. The mode
    is set on the temporary file before a byte of the secret is written to it.

    NO TRAILING NEWLINE. `load_bond_secret` strips trailing whitespace precisely
    because `wg genpsk >` adds one, so either would work - but a newline at one
    end and not the other derives two different keys and presents as "the MAC
    never verifies", and the cheapest way not to have that conversation is to
    write exactly the bytes that were delivered.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bondkey.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_keys(path: Path, document: dict) -> None:
    """Atomically, at 0600, or not at all.

    RENAMED INTO PLACE, and the temporary file is created in the SAME directory
    so the rename cannot cross a filesystem and silently become a copy. A
    truncated keys.json is a router that cannot bring any path up, and the agent
    reads this file at start - which is the one moment nobody is watching.

    The mode is set on the temp file BEFORE any content is written to it, so
    there is no window in which key material sits at the default mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".keys.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # The temp file must never be left behind holding key material at a
        # name nothing will clean up.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_keys(path: Path) -> dict:
    """keys.json as it is, or Refused. NEVER a silent empty document.

    An empty dict here would make a keys.json that failed to parse read exactly
    like a router that has never been configured, and the next write would then
    erase every per-path private key on the box. `policy._read` on the server
    makes the same argument for the same reason.
    """
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as bad:
        raise Refused(
            f"{path} could not be read as JSON ({bad}). Refusing to write over a "
            "file this router cannot understand - it holds per-path private keys "
            "that cannot be regenerated."
        ) from bad
    if not isinstance(loaded, dict):
        raise Refused(f"{path} is not a JSON object")
    return loaded


# ---------------------------------------------------------------- the client


def _openssl(args, stdin: bytes | None = None) -> bytes:
    """One openssl invocation, or Unreachable.

    THE KEY IS PASSED BY PATH, NEVER ON THE COMMAND LINE. `ps` is readable by
    anything on this box, and an argument list is the one place a secret cannot
    be taken back from.
    """
    try:
        done = subprocess.run(
            ["openssl", *args], input=stdin, capture_output=True, check=False,
        )
    except OSError as missing:
        raise Unreachable(f"openssl is not usable on this device: {missing}") from missing
    if done.returncode != 0:
        raise Unreachable(
            f"openssl {args[0]} failed: {done.stderr.decode('utf-8', 'replace').strip()}"
        )
    return done.stdout


def sign_nonce(nonce: str, key_path: Path) -> str:
    """The base64 DER ECDSA-P256-SHA256 signature muster's proof.py verifies.

    OVER THE NONCE'S BYTES AS SENT, with nothing appended. `proof.verify` calls
    `public_key.verify(signature, nonce.encode(), ec.ECDSA(SHA256))`, so a
    trailing newline - which every naive `echo | openssl dgst` adds - signs a
    different message and produces BAD_SIGNATURE, which reads like a wrong key.
    """
    der = _openssl(["dgst", "-sha256", "-sign", str(key_path)], stdin=nonce.encode())
    return base64.b64encode(der).decode("ascii")


# Where a device may be told to send its identity. HTTPS, or loopback.
#
# `urlopen` HAPPILY OPENS `file:` (ruff S310), and the URL it is given here comes
# from configuration - so a base_url of `file:///etc/shadow` would make this
# module a local file reader, and a plain `http://` one would put the device's
# certificate and a signed nonce on the wire in clear. Neither is a scheme this
# client should be able to reach by accident.
#
# LOOPBACK OVER http IS ALLOWED, AND ONLY LOOPBACK. It is what lets the
# User-Agent behaviour be tested against a real HTTP server without a
# certificate, and a request to 127.0.0.1 cannot be observed by anybody who is
# not already on the box.
_ALLOWED_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _checked_url(url: str) -> str:
    """The URL, or Refused. Never anything `urlopen` would treat as a file."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and parsed.hostname in _ALLOWED_LOOPBACK:
        return url
    raise Refused(
        f"muster must be reached over https; refusing to open {parsed.scheme or 'a schemeless'} "
        f"URL. This request carries this device's certificate and a signed nonce."
    )


def _post(url: str, payload: dict, timeout: float = 20.0) -> dict:
    """A JSON POST that Cloudflare will let through. See USER_AGENT."""
    # S310 is suppressed on this call and on the urlopen below. `_checked_url`
    # has already refused every scheme but https - and http on loopback - which
    # is the property the rule asks about; it cannot see through a function call.
    # (A comment must not START with the word noqa here: ruff reads that as a
    # blanket directive on the line, which RUF100 then reports as unused.)
    request = urllib.request.Request(  # noqa: S310
        _checked_url(url),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as status:
        detail = status.read()[:400].decode("utf-8", "replace")
        if status.code == 503:
            # muster REFUSES rather than answering empty when it cannot say what
            # a device should be (policy.NoSource). Mirroring that here is the
            # point: an empty answer would be an instruction, and muster went to
            # some trouble not to send one.
            raise Unreachable(
                f"muster cannot say what this device should be yet ({detail}). "
                "The cached key is unaffected and stays in force."
            ) from status
        raise Unreachable(f"muster answered {status.code}: {detail}") from status
    except (urllib.error.URLError, OSError, ValueError) as down:
        raise Unreachable(f"muster could not be reached: {down}") from down


def fetch_configuration(
    base_url: str, key_path: Path, certificate_pem: str
) -> tuple[dict[str, str], str]:
    """muster's answer for this device: (files, revision).

    TWO ROUND TRIPS, AND THE FIRST ONE IS THE POINT. The nonce is server-issued,
    single use and short lived (proof.py), so a signature observed on the wire is
    worthless by the time anyone could replay it.
    """
    base = base_url.rstrip("/")
    challenge = _post(f"{base}/v1/auth/challenge", {})
    nonce = challenge.get("nonce", "")
    if not nonce:
        raise Unreachable("muster issued no nonce")
    answer = _post(
        f"{base}/v1/device/config",
        {
            "nonce": nonce,
            "signature_b64": sign_nonce(nonce, key_path),
            "certificate_pem": certificate_pem,
        },
    )
    files = answer.get("files")
    if not isinstance(files, dict):
        raise Refused("muster's answer carried no files object")
    return files, str(answer.get("revision", ""))


def refresh(
    base_url: str,
    identity_key: Path,
    certificate_pem: str,
    keys_path: Path,
    bond_key_path: Path,
) -> str:
    """Ask muster, validate, and write only if all of that worked.

    `identity_key` is this device's own P-256 key, used to sign muster's nonce.
    `bond_key_path` is the file `auth.py` reads - `auth_key_file` in `[policy]`.
    They are different credentials with different jobs and are deliberately not
    the same argument.

    Returns a one-line human summary. Raises Refused or Unreachable, and in
    EVERY failing case both files are exactly as they were - which is the whole
    contract this module exists to keep.
    """
    files, revision = fetch_configuration(base_url, identity_key, certificate_pem)
    keys = datapath_keys(files)
    existing = read_keys(keys_path)
    held = existing.get(KEYS_SECTION, {})

    # COMPARED BY DIGEST AGAINST THE RECORD, and then confirmed against the FILE.
    # The record alone would say "unchanged" for a key file somebody deleted or
    # truncated by hand, which is the state where re-writing it matters most.
    on_disk = ""
    if bond_key_path.is_file():
        try:
            on_disk = bond_key_path.read_text(encoding="utf-8").strip()
        except OSError:
            on_disk = ""
    if held.get("current_digest") == _digest(keys[CURRENT]) and on_disk == keys[CURRENT]:
        # NOT REWRITTEN WHEN NOTHING CHANGED, for the same reason the deploy does
        # not rewrite an unchanged zippie.toml: a file whose mtime moves on every
        # poll teaches an operator reading `ls -l` that the key rotated when it
        # did not.
        return f"unchanged (revision {revision}, current {_digest(keys[CURRENT])})"

    # THE SECRET FIRST, THE RECORD SECOND. If the record were written first and
    # the key write then failed, the next poll would read "unchanged" and never
    # retry - a router left without the key, reported as up to date.
    write_secret(bond_key_path, keys[CURRENT])
    note = ""
    if PREVIOUS in keys:
        write_secret(Path(str(bond_key_path) + PREVIOUS_SUFFIX), keys[PREVIOUS])
    elif Path(str(bond_key_path) + PREVIOUS_SUFFIX).exists():
        # NOT DELETED HERE. An absent `key.previous` is how a completed rotation
        # looks, and it is also how a policy file somebody is midway through
        # editing looks. Deleting key material is the irreversible direction, so
        # it is said out loud and left for a person - the same posture as the
        # absent `app-config` above.
        note = "; a previous-key file is still on disk and muster no longer names one"
    write_keys(keys_path, merge_into_keys(existing, keys, revision, str(bond_key_path)))
    return (
        f"updated (revision {revision}, current {_digest(keys[CURRENT])}"
        + (f", previous {_digest(keys[PREVIOUS])}" if PREVIOUS in keys else "")
        + f") -> {bond_key_path}{note}"
    )
