"""When this router must be enrolled again, and by whom.

muster has no unattended renewal route - checked against the server on
2026-08-30, the only way to a certificate is a pairing code an administrator
minted. A device holding a valid certificate cannot trade it for a fresh one.

That makes the expiry warning the ONLY mechanism standing between "the operator
enrolls the router in five quiet minutes" and "the refresh channel is dead and
nobody noticed for six weeks". There is no retry loop behind it to catch a
miss, so the arithmetic is tested against real certificates at real dates
rather than trusted.

Nothing here reaches the network or the router. Certificates are minted locally
by the same `openssl` the router uses, at chosen validity, so a test can stand
at any point in a certificate's life.
"""
from __future__ import annotations

import calendar
import shutil
import subprocess
import time

import pytest

from zippie import musterwrt

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is the router's only crypto"
)


def certificate(days: int) -> str:
    """A self-signed P-256 certificate valid for `days` from now.

    THE SHAPE THE ROUTER ACTUALLY HOLDS. muster signs P-256 and `expires_in_days`
    shells to the same `openssl x509` the router has, so a fixture minted any
    other way would be testing a parser against input it will never see.
    """
    key = subprocess.run(
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout"],
        check=True, capture_output=True,
    ).stdout
    return subprocess.run(
        ["openssl", "req", "-new", "-x509", "-key", "/dev/stdin",
         "-subj", "/CN=travel-router", "-days", str(days)],
        input=key, check=True, capture_output=True,
    ).stdout.decode()


def test_a_ninety_day_certificate_reads_as_ninety_days():
    # Floor rounding costs at most a day, and the seconds spent minting the
    # certificate can cost the other one.
    assert musterwrt.expires_in_days(certificate(90)) in (89, 90)


def test_the_verdict_walks_ok_then_attention_then_urgent():
    """The whole ladder, on ONE certificate, by moving the clock instead of it.

    A test that minted a short-lived certificate per rung would prove three
    unrelated things. Walking one certificate forward proves the thresholds are
    ordered and that no gap between them reports `ok` on a dying certificate -
    which is the only failure of this function that matters.
    """
    pem = certificate(90)
    ends = subprocess.run(
        ["openssl", "x509", "-noout", "-enddate"], input=pem.encode(),
        check=True, capture_output=True,
    ).stdout.decode().strip().partition("=")[2]
    expiry = calendar.timegm(time.strptime(ends, "%b %d %H:%M:%S %Y %Z"))

    day = 86400
    assert musterwrt.enrollment_verdict(pem, expiry - 60 * day)[0] == "ok"
    assert musterwrt.enrollment_verdict(pem, expiry - 46 * day)[0] == "ok"
    assert musterwrt.enrollment_verdict(pem, expiry - 44 * day)[0] == "attention"
    assert musterwrt.enrollment_verdict(pem, expiry - 15 * day)[0] == "attention"
    assert musterwrt.enrollment_verdict(pem, expiry - 13 * day)[0] == "urgent"
    assert musterwrt.enrollment_verdict(pem, expiry - 1 * day)[0] == "urgent"


def test_an_expired_certificate_is_urgent_and_says_the_bond_is_unaffected():
    """The message is the test.

    A router whose certificate lapsed has lost its refreshes and NOTHING else -
    the cached key is authoritative and the bond does not care. An alarm that
    reads like an outage sends somebody to a hotel car park to fix a link that
    was never down, and this router has form for costing a person a physical
    trip.
    """
    pem = certificate(90)
    long_after = time.time() + 200 * 86400
    severity, message = musterwrt.enrollment_verdict(pem, long_after)
    assert severity == "urgent"
    assert "EXPIRED" in message
    assert "bond is unaffected" in message
    assert musterwrt.expires_in_days(pem, long_after) < 0


def test_a_certificate_openssl_cannot_read_is_refused_not_assumed_healthy():
    """The direction of the failure is the point.

    Anything that returns a number for garbage returns a LARGE number - a
    truncated file reads as "good for 89 days" and the warning never fires. So
    an unreadable certificate raises, and the caller logs a problem, rather than
    silently reporting the one answer that means "do nothing".
    """
    with pytest.raises(musterwrt.Refused):
        musterwrt.expires_in_days("-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----\n")
