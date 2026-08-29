"""Every way muster's answer can take this router off the network, refused.

On 2026-08-29 a deploy shipped `server_public_key = "<server-public-key>"` to
suzu. The placeholder is in the repo on purpose; nothing had ever substituted it
back. `wg setconf` refused it, the bond never came up, and because the agent
owns the router's only default route the box left the network. Recovery took
physical access.

The existing guard did not catch it and could not have: it validated that the
config was VALID TOML, and `"<server-public-key>"` is perfectly valid TOML. The
check proved the file parsed, not that it was usable.

So this file is a catalogue of things that parse and must not be applied. Every
test is a refusal, and every refusal has the same postcondition: the cached key
on disk is untouched and stays in force. A control plane that can hand a router
a bad key is a control plane that can island it by typo, and this router is
sometimes its own only uplink.
"""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from zippie import musterwrt

GOOD = "cMkhUCDaGTjInWtCEG8TpMRo40f5BimY1IWKZ18rmmc="
OTHER = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # base64 of 32 bytes, like every key here


def app_config(*lines: str) -> dict:
    return {"app-config": "\n".join(lines) + "\n"}


def datapath_line(key: str, value: str) -> str:
    return f"set {musterwrt.DATAPATH_SUBJECT} {key} {value}"


# --------------------------------------------------------------------------
# What a good answer looks like, so the refusals below mean something.
# --------------------------------------------------------------------------


def test_a_current_key_alone_is_accepted():
    keys = musterwrt.datapath_keys(app_config(datapath_line("key.current", GOOD)))
    assert keys == {"key.current": GOOD}


def test_a_rotation_carries_both_keys():
    """The overlap IS the rotation mechanism.

    The far end accepts both while the two ends cross over. A format that can
    only express one key forces every rotation to be a simultaneous swap, and a
    simultaneous swap on a link that carries the router's own management traffic
    is an outage with no way back in.
    """
    keys = musterwrt.datapath_keys(
        app_config(
            datapath_line("key.current", GOOD),
            datapath_line("key.previous", OTHER),
        )
    )
    assert keys == {"key.current": GOOD, "key.previous": OTHER}


def test_comments_and_blank_lines_are_ignored():
    keys = musterwrt.datapath_keys(
        app_config("# suzu datapath", "", datapath_line("key.current", GOOD))
    )
    assert keys == {"key.current": GOOD}


def test_a_hash_inside_a_value_is_not_a_comment():
    """muster's rule, for muster's reason: a value here may be a credential, and
    truncating one at an inner `#` produces a router that authenticates with
    something almost right - which looks like a problem at the far end for as
    long as anyone is willing to look."""
    weird = "aaaa#bbbb"
    parsed = musterwrt.parse_app_config(f"set subj key {weird}\n")
    assert parsed["subj"]["key"] == weird


# --------------------------------------------------------------------------
# The refusals. Each one is a router that stays on the network.
# --------------------------------------------------------------------------


def test_a_placeholder_is_refused():
    """THE 2026-08-29 OUTAGE. It is valid text, it parses, and it took the box
    off the network. Refused by SHAPE, not by name, so the next scrubbed value
    is caught too."""
    with pytest.raises(musterwrt.Refused, match="placeholder"):
        musterwrt.datapath_keys(
            app_config(datapath_line("key.current", "<server-public-key>"))
        )


def test_a_different_placeholder_is_refused_too():
    """Not special-cased to the one that bit us."""
    with pytest.raises(musterwrt.Refused, match="placeholder"):
        musterwrt.datapath_keys(
            app_config(datapath_line("key.current", "<datapath-key>"))
        )


@pytest.mark.parametrize(
    "bad",
    [
        "cMkhUCDaGTjInWtCEG8TpMRo40f5BimY1IWKZ18rmmc",  # 43 chars, no padding
        "cMkhUCDaGTjInWtCEG8TpMRo40f5BimY1IWKZ18rmmc==",  # over-padded
        "short=",
        "cMkhUCDaGTjInWtCEG8TpMRo40f5BimY1IWKZ18rmm!=",  # not base64
    ],
)
def test_a_key_of_the_wrong_shape_is_refused(bad):
    """32 bytes or nothing. A truncated key is the failure that looks like the
    far end being wrong."""
    with pytest.raises(musterwrt.Refused):
        musterwrt.datapath_keys(app_config(datapath_line("key.current", bad)))


def test_a_blank_value_is_refused_at_its_own_boundary():
    """Checked here as well as at the parser, on purpose.

    The line grammar cannot currently produce an empty value - a line with a
    missing field is refused for having three fields. This is the second door,
    and it is the one that matters, because the difference between "blank" and
    "absent" is a key that is silently cleared. muster refuses a blank server
    side for the same reason; a boundary check that rests on a function two
    layers away is not a boundary check.
    """
    with pytest.raises(musterwrt.Refused, match="blank"):
        musterwrt._refuse_bad_key("key.current", "")


def test_an_absent_app_config_does_not_withdraw_the_key():
    """THE DELIBERATE DIVERGENCE FROM THE ANDROID AGENT.

    muster's contract is that a file absent from a successful answer is removed
    from the device, and for restrictions that is right - policy that only ever
    adds is a ratchet. But this file holds the key to the router's only uplink,
    so obeying "withdraw" would let one mistyped Secret key island a device by
    omission. Refused, and written down so nobody "fixes" it.
    """
    with pytest.raises(musterwrt.Refused, match="withdraw"):
        musterwrt.datapath_keys({"restrictions": "", "visible-apps": ""})


def test_an_app_config_for_somebody_else_changes_nothing():
    """A policy file that configures only the companion app is not an
    instruction to clear the datapath key."""
    with pytest.raises(musterwrt.Refused, match="nothing here for the datapath"):
        musterwrt.datapath_keys(
            app_config("set app.zippie.companion announceToken abc123")
        )


def test_a_key_this_router_does_not_know_is_refused_not_ignored():
    """Loud, not skipped.

    An unknown key is either a typo in the one file that carries credentials, or
    a newer muster talking to an older router. Silently dropping it would make a
    rotation that did not happen look exactly like one that did.
    """
    with pytest.raises(musterwrt.Refused, match="key.next"):
        musterwrt.datapath_keys(
            app_config(
                datapath_line("key.current", GOOD),
                datapath_line("key.next", OTHER),
            )
        )


def test_no_current_key_is_refused():
    with pytest.raises(musterwrt.Refused, match="no key.current"):
        musterwrt.datapath_keys(app_config(datapath_line("key.previous", GOOD)))


def test_previous_equal_to_current_is_not_an_overlap():
    """It looks exactly like a rotation that is safely armed, and it is a
    rotation with no overlap at all - which takes the bond down at the moment
    the two ends disagree."""
    with pytest.raises(musterwrt.Refused, match="not an overlap"):
        musterwrt.datapath_keys(
            app_config(
                datapath_line("key.current", GOOD),
                datapath_line("key.previous", GOOD),
            )
        )


def test_one_bad_line_refuses_the_whole_file():
    """No partial apply, because there is no useful partial state.

    Skipping the unparseable line and applying the rest would adopt the new key
    without retaining the old one - half a rotation, and a router that can no
    longer talk to the far end.
    """
    with pytest.raises(musterwrt.Refused, match="line 2"):
        musterwrt.datapath_keys(
            app_config(
                datapath_line("key.current", GOOD),
                "set only-three-fields",  # three fields, not four
            )
        )


def test_an_unknown_verb_is_refused():
    with pytest.raises(musterwrt.Refused, match="not a verb"):
        musterwrt.parse_app_config("delete subj key value\n")


# --------------------------------------------------------------------------
# The cache. Nothing here may lose a key nobody can regenerate.
# --------------------------------------------------------------------------


def test_the_merge_preserves_every_other_key(tmp_path):
    """keys.json holds the per-path WireGuard private keys this router cannot
    regenerate. A wholesale rewrite is the obvious implementation and it would
    be a disaster."""
    existing = {
        "private_key": "legacy",
        "paths": {"hotspot": {"private_key": "p1", "port": 51830}},
    }
    merged = musterwrt.merge_into_keys(existing, {"key.current": GOOD}, "rev1")
    assert merged["private_key"] == "legacy"
    assert merged["paths"]["hotspot"]["private_key"] == "p1"
    assert merged[musterwrt.KEYS_SECTION]["current"] == GOOD
    # And the caller's dict is not mutated underneath them.
    assert musterwrt.KEYS_SECTION not in existing


def test_the_written_file_is_not_world_readable(tmp_path):
    target = tmp_path / "keys.json"
    musterwrt.write_keys(target, {"datapath": {"current": GOOD}})
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"key material written at mode {oct(mode)}"


def test_a_write_leaves_no_temporary_file_behind(tmp_path):
    target = tmp_path / "keys.json"
    musterwrt.write_keys(target, {"datapath": {"current": GOOD}})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "keys.json"]
    assert leftovers == [], f"key material left at {leftovers}"


def test_an_unreadable_keys_file_is_refused_not_treated_as_empty(tmp_path):
    """An empty dict here would make a keys.json that failed to parse read
    exactly like a router that has never been configured - and the next write
    would erase every per-path private key on the box."""
    target = tmp_path / "keys.json"
    target.write_text("{not json")
    with pytest.raises(musterwrt.Refused):
        musterwrt.read_keys(target)


def test_a_missing_keys_file_is_simply_empty(tmp_path):
    assert musterwrt.read_keys(tmp_path / "nothing.json") == {}


# --------------------------------------------------------------------------
# The contract that matters most: a refusal changes nothing on disk.
# --------------------------------------------------------------------------


def _fake_muster(monkeypatch, files, revision="rev1"):
    monkeypatch.setattr(
        musterwrt, "fetch_configuration", lambda *a, **k: (files, revision)
    )


def test_a_refused_answer_leaves_keys_json_byte_identical(tmp_path, monkeypatch):
    """THE WHOLE POINT. Every refusal above is worthless if the file has already
    been touched by the time the refusal is raised."""
    target = tmp_path / "keys.json"
    original = json.dumps({"paths": {"hotspot": {"private_key": "irreplaceable"}}})
    target.write_text(original)
    before = target.read_bytes()

    _fake_muster(monkeypatch, app_config(datapath_line("key.current", "<placeholder>")))
    with pytest.raises(musterwrt.Refused):
        musterwrt.refresh("https://muster.invalid", tmp_path / "k.pem", "cert", target)
    assert target.read_bytes() == before


def test_an_unreachable_muster_leaves_keys_json_byte_identical(tmp_path, monkeypatch):
    """muster being down must never be able to change what a router is running.
    The cached key is authoritative; this module is a refresher."""
    target = tmp_path / "keys.json"
    target.write_text(json.dumps({"paths": {"hotspot": {"private_key": "x"}}}))
    before = target.read_bytes()

    def down(*_a, **_k):
        raise musterwrt.Unreachable("no route to host")

    monkeypatch.setattr(musterwrt, "fetch_configuration", down)
    with pytest.raises(musterwrt.Unreachable):
        musterwrt.refresh("https://muster.invalid", tmp_path / "k.pem", "cert", target)
    assert target.read_bytes() == before


def test_an_unchanged_key_is_not_rewritten(tmp_path, monkeypatch):
    """A file whose mtime moves on every poll teaches an operator reading
    `ls -l` that the key rotated when it did not - the same argument the deploy
    makes for not rewriting an unchanged zippie.toml."""
    target = tmp_path / "keys.json"
    _fake_muster(monkeypatch, app_config(datapath_line("key.current", GOOD)))
    musterwrt.refresh("https://m.invalid", tmp_path / "k.pem", "cert", target)
    stamp = target.stat().st_mtime_ns
    before = target.read_bytes()

    outcome = musterwrt.refresh("https://m.invalid", tmp_path / "k.pem", "cert", target)
    assert "unchanged" in outcome
    assert target.stat().st_mtime_ns == stamp
    assert target.read_bytes() == before


def test_a_good_answer_does_update_the_cache(tmp_path, monkeypatch):
    """The refusals are only trustworthy if the accept path demonstrably works;
    a module that refused everything would pass every test above."""
    target = tmp_path / "keys.json"
    target.write_text(json.dumps({"paths": {"hotspot": {"private_key": "keepme"}}}))
    _fake_muster(monkeypatch, app_config(datapath_line("key.current", GOOD)), "rev9")
    outcome = musterwrt.refresh("https://m.invalid", tmp_path / "k.pem", "cert", target)
    assert "updated" in outcome
    written = json.loads(target.read_text())
    assert written[musterwrt.KEYS_SECTION]["current"] == GOOD
    assert written[musterwrt.KEYS_SECTION]["revision"] == "rev9"
    assert written["paths"]["hotspot"]["private_key"] == "keepme"


def test_no_key_material_reaches_the_summary_line(tmp_path, monkeypatch):
    """The return value goes into a log. `policy.Configuration` overrides
    __str__ on the server for exactly this reason: the generated one prints
    every field and one of them is a credential."""
    target = tmp_path / "keys.json"
    _fake_muster(
        monkeypatch,
        app_config(
            datapath_line("key.current", GOOD), datapath_line("key.previous", OTHER)
        ),
    )
    outcome = musterwrt.refresh("https://m.invalid", tmp_path / "k.pem", "cert", target)
    assert GOOD not in outcome and OTHER not in outcome


# --------------------------------------------------------------------------
# The wire. Two findings measured on the router, pinned here.
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not on PATH")
def test_the_signature_verifies_and_signs_the_nonce_with_nothing_appended(tmp_path):
    """muster verifies `public_key.verify(sig, nonce.encode(), ECDSA(SHA256))`.

    Every naive `echo "$nonce" | openssl dgst -sign` appends a newline and signs
    a different message. muster answers BAD_SIGNATURE, which reads exactly like
    the wrong key - and would be debugged as an enrollment problem.
    """
    key = tmp_path / "device.key"
    subprocess.run(
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
         "-out", str(key)],
        check=True, capture_output=True,
    )
    pub = tmp_path / "device.pub"
    subprocess.run(
        ["openssl", "ec", "-in", str(key), "-pubout", "-out", str(pub)],
        check=True, capture_output=True,
    )
    nonce = "zvAyPY5ejVd_sifTDPBMi_kxKi0LJsjdIuZRO3Li1hw"
    signature = tmp_path / "sig.bin"
    import base64 as b64
    signature.write_bytes(b64.b64decode(musterwrt.sign_nonce(nonce, key)))

    # Verified against the EXACT nonce bytes - no trailing newline anywhere.
    message = tmp_path / "nonce.bin"
    message.write_bytes(nonce.encode())
    done = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub),
         "-signature", str(signature), str(message)],
        capture_output=True, check=False,
    )
    assert done.returncode == 0, done.stderr

    # And it does NOT verify against the nonce plus a newline, which is what
    # proves the assertion above is actually load-bearing.
    with_newline = tmp_path / "nonce-nl.bin"
    with_newline.write_bytes(nonce.encode() + b"\n")
    assert subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub),
         "-signature", str(signature), str(with_newline)],
        capture_output=True, check=False,
    ).returncode != 0


def test_the_request_never_carries_the_default_user_agent():
    """MEASURED ON THE ROUTER, 2026-08-29. Cloudflare answers 403 to
    `Python-urllib/3.9` before the request reaches muster; the identical request
    with any other User-Agent gets 201.

    A client written the obvious way therefore fails with what looks like an
    authentication problem against a service whose logs show nothing, because
    muster never saw the request. This is a real HTTP server rather than a
    monkeypatch, so it catches a header set on the wrong object.
    """
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # BaseHTTPRequestHandler's own spelling
            seen["ua"] = self.headers.get("User-Agent")
            body = b'{"nonce":"n","ttl_s":120}'
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        musterwrt._post(f"http://127.0.0.1:{server.server_port}/v1/auth/challenge", {})
    finally:
        thread.join(timeout=5)
        server.server_close()

    assert seen["ua"] == musterwrt.USER_AGENT
    assert "Python-urllib" not in seen["ua"], (
        "the default User-Agent is refused by Cloudflare before muster sees it"
    )


def test_a_503_says_the_cached_key_stays_in_force():
    """muster REFUSES rather than answering empty when it cannot say what a
    device should be (policy.NoSource), because an empty answer is an
    instruction. Mirroring that distinction here is the point of the message.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # BaseHTTPRequestHandler's own spelling
            body = b"the policy directory holds nothing muster manages"
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        with pytest.raises(musterwrt.Unreachable, match="stays in force"):
            musterwrt._post(f"http://127.0.0.1:{server.server_port}/x", {})
    finally:
        thread.join(timeout=5)
        server.server_close()


def test_the_module_is_not_imported_by_the_agent():
    """UNARMED, AND THAT IS THE SHIPPING DECISION.

    The agent must never wait on muster to start: the cached key is
    authoritative and this is a refresher. An import is how "refresher" quietly
    becomes "precondition" - and on a box whose only default route is the bond
    it configures, a precondition is an outage waiting for a Cloudflare
    incident.

    The same shape as autotest*.sh, which the deploy installs and never arms.
    """
    package = Path(musterwrt.__file__).parent
    importers = [
        source.name
        for source in package.glob("*.py")
        if source.name != "musterwrt.py" and "musterwrt" in source.read_text()
    ]
    assert importers == [], f"{importers} import musterwrt; it must stay unarmed"


# --------------------------------------------------------------------------
# Where this device is willing to send its identity.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/shadow",
        "file:///etc/zippie/keys.json",
        "ftp://example.invalid/x",
        "gopher://example.invalid/x",
        "//example.invalid/x",
        "/etc/passwd",
    ],
)
def test_only_https_may_carry_this_device_s_identity(url):
    """`urlopen` opens `file:` as happily as it opens https (ruff S310).

    The URL comes from configuration, and this request carries the device's
    certificate and a signed nonce. A `file:` base_url would turn this module
    into a local file reader; a plain `http:` one would put the proof on the
    wire in clear for anybody on the path.
    """
    with pytest.raises(musterwrt.Refused, match="https"):
        musterwrt._checked_url(url)


def test_plain_http_to_a_remote_host_is_refused():
    """The one that would look like it works."""
    with pytest.raises(musterwrt.Refused, match="https"):
        musterwrt._checked_url("http://enroll.muster.casa/v1/auth/challenge")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_over_http_is_allowed_and_only_loopback(host):
    """The exception exists so the User-Agent behaviour can be proven against a
    real HTTP server without a certificate, and it cannot help anybody: a
    request to loopback is not observable by someone who is not already on the
    box."""
    assert musterwrt._checked_url(f"http://{host}:8787/x")


def test_https_is_always_allowed():
    assert musterwrt._checked_url("https://enroll.muster.casa/v1/device/config")
