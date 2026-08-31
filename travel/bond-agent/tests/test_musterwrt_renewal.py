"""Renewing this router's own certificate, and every way that must not go wrong.

muster grew an unattended renewal route (muster#10), so the 45-day warning this
module shipped with is no longer the whole story: the router can now replace its
own certificate over the identity it already holds, with nobody present.

THE ASYMMETRY IS THE WHOLE DESIGN. Failing to renew costs this router its
refreshes and nothing else - the cached datapath key is authoritative and the
bond does not care. Installing a WRONG certificate costs it the ability to prove
itself to anything, and it cannot renew its way out of that, because renewing
requires the proof it just threw away. Recovery is then physical, which this
project has already paid for once.

So every test here is about the second failure. muster is a real HTTP server on
loopback, because the thing being tested is what this router does with an answer.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from zippie import musterwrt

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is the router's only crypto"
)


def _key(path: Path) -> Path:
    subprocess.run(
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
         "-out", str(path)],
        check=True, capture_output=True,
    )
    return path


def _certificate(key: Path, days: int = 90) -> str:
    return subprocess.run(
        ["openssl", "req", "-new", "-x509", "-key", str(key),
         "-subj", "/CN=travel-router", "-days", str(days)],
        check=True, capture_output=True,
    ).stdout.decode()


class _Muster:
    """A muster that answers however a test needs it to."""

    def __init__(self, answer, status=200):
        self.answer, self.status, self.seen = answer, status, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep pytest output readable
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.seen.append((self.path, json.loads(raw or b"{}")))
                if self.path.endswith("/challenge"):
                    body, code = {"nonce": "n" * 43}, 200
                else:
                    body, code = outer.answer, outer.status
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()


def test_a_renewal_replaces_the_certificate_and_keeps_the_key(tmp_path):
    """THE POINT. A new certificate for the SAME key, installed, with nobody
    present - and the private key untouched, because the private key is the only
    thing on this router that cannot be re-obtained."""
    key = _key(tmp_path / "device.key")
    before_key = key.read_bytes()
    crt = tmp_path / "device.crt"
    crt.write_text(_certificate(key))
    old = crt.read_text()

    fresh = _certificate(key)  # same key, different certificate
    with _Muster({"certificate_pem": fresh, "not_after": "2027-01-01T00:00:00+00:00",
                  "renew_after": "2026-12-01T00:00:00+00:00"}) as muster:
        summary = musterwrt.renew(muster.url, key, crt)

    assert "renewed" in summary
    assert crt.read_text() == fresh
    assert crt.read_text() != old
    assert key.read_bytes() == before_key, "renewal touched the private key"


def test_a_certificate_for_a_different_key_is_refused_and_not_installed(tmp_path):
    """THE TEST THIS FILE EXISTS FOR.

    A certificate that does not match the private key on this box leaves a router
    that cannot prove itself to anything - and it cannot renew its way out,
    because renewing needs the proof it just discarded. muster would have to be
    badly wrong to send one, and the cost of not checking is a drive.
    """
    key = _key(tmp_path / "device.key")
    crt = tmp_path / "device.crt"
    crt.write_text(_certificate(key))
    mine = crt.read_text()

    stranger = _certificate(_key(tmp_path / "stranger.key"))
    with _Muster({"certificate_pem": stranger}) as muster, pytest.raises(musterwrt.Refused, match="DIFFERENT key"):
        musterwrt.renew(muster.url, key, crt)

    assert crt.read_text() == mine, "a stranger's certificate was installed"


def test_an_answer_with_no_certificate_changes_nothing(tmp_path):
    key = _key(tmp_path / "device.key")
    crt = tmp_path / "device.crt"
    crt.write_text(_certificate(key))
    mine = crt.read_text()

    with _Muster({"revision": "r1"}) as muster, pytest.raises(musterwrt.Refused):
        musterwrt.renew(muster.url, key, crt)

    assert crt.read_text() == mine


def test_garbage_where_a_certificate_should_be_changes_nothing(tmp_path):
    """The shape a truncated response or a proxy error page arrives in. It has to
    be refused by the SAME path that refuses a wrong key, because "openssl could
    not read it" and "openssl read it and it is somebody else's" are one outcome
    here: do not install it."""
    key = _key(tmp_path / "device.key")
    crt = tmp_path / "device.crt"
    crt.write_text(_certificate(key))
    mine = crt.read_text()

    with _Muster({"certificate_pem": "-----BEGIN CERTIFICATE-----\nnope\n"
                                     "-----END CERTIFICATE-----\n"}) as muster, pytest.raises(musterwrt.Refused):
        musterwrt.renew(muster.url, key, crt)

    assert crt.read_text() == mine


def test_too_early_is_quiet_and_is_not_an_outage(tmp_path):
    """muster owns the schedule, so the router simply asks and is told no.

    `NotYet` subclasses `Unreachable` on purpose: every caller that already
    catches `Unreachable` keeps what it has and says nothing, which is the right
    response to "ask again tomorrow". A router that treated this as a fault
    would page somebody once an hour for thirty days.
    """
    key = _key(tmp_path / "device.key")
    crt = tmp_path / "device.crt"
    crt.write_text(_certificate(key))

    with _Muster({"detail": "too early; this certificate may be renewed from "
                            "2026-09-29T00:00:00+00:00"}, status=409) as muster, pytest.raises(musterwrt.NotYet):
        musterwrt.renew(muster.url, key, crt)
    assert issubclass(musterwrt.NotYet, musterwrt.Unreachable)


def test_a_revoked_device_is_told_so_loudly_and_keeps_its_key(tmp_path):
    """A 403 means an administrator deliberately cut this router off.

    Before `Revoked` existed that came back as `Unreachable` and was logged at
    the same volume as a hotel captive portal - the one answer somebody needs to
    see, in the one class nobody reads. It is LOUD now, and it still deletes
    nothing: a router that wiped its own key on a 403 would island itself the
    moment an administrator revoked the wrong key_id.
    """
    key = _key(tmp_path / "device.key")
    crt = tmp_path / "device.crt"
    crt.write_text(_certificate(key))
    mine = crt.read_text()

    with _Muster({"detail": "this device has been revoked"}, status=403) as muster, pytest.raises(musterwrt.Revoked):
        musterwrt.renew(muster.url, key, crt)

    assert crt.read_text() == mine
    assert issubclass(musterwrt.Revoked, musterwrt.Refused)


def test_the_csr_carries_the_key_this_router_already_has(tmp_path):
    """RENEWAL, NOT ROTATION, checked from this end too.

    muster refuses a swapped key with a 403, but by then a router that generated
    a new key would already have overwritten the only credential it can prove
    itself with. `openssl req -new -key` signs a request for an existing key and
    generates nothing; `-newkey` would generate one. The difference is four
    characters and a dead router, so it is pinned.
    """
    key = _key(tmp_path / "device.key")
    crt = tmp_path / "device.crt"
    crt.write_text(_certificate(key))

    with _Muster({"certificate_pem": _certificate(key)}) as muster:
        musterwrt.renew(muster.url, key, crt)
        sent = next(body for path, body in muster.seen if path.endswith("/renew"))

    ours = subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout"],
        check=True, capture_output=True,
    ).stdout.strip()
    theirs = subprocess.run(
        ["openssl", "req", "-noout", "-pubkey"],
        input=sent["csr_pem"].encode(), check=True, capture_output=True,
    ).stdout.strip()
    assert theirs == ours, "the CSR asked for a different key than this router holds"
