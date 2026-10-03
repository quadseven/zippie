"""The install path refuses an APK the fleet certificate did not sign (#185).

A TESTKEY-signed APK once reached a production handset and could never be
upgraded, because the key that signed it was destroyed by the CI job that
created it. Three redundant name markers (artifact name, version name, file
name) told the truth to anyone who read them and stopped nobody. Naming is
not a control.

The guard is a digest comparison in install-to-handsets.sh: the candidate
APK's signing certificate SHA-256 must equal the pinned fleet digest, or the
install is refused before the script touches the network. The digest is
compared, never the certificate subject - every key this project has used
carries subject "zippie", including the throwaway ones.

These tests feed crafted APKs to the real script. The APKs are built by the
helper below: a minimal compiled manifest plus a v2 signing block carrying a
chosen certificate. apk-facts.py reads them exactly as it reads real builds
(it is differentially tested against aapt2/apksigner on every build, so a
fixture it cannot read is a broken fixture, not a broken test).
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

CI_DIR = Path(__file__).resolve().parent.parent
SCRIPT = CI_DIR / "install-to-handsets.sh"

PACKAGE = "app.zippie.companion"

# Test-only fleet identity. NOT the real fleet key: the test must not depend
# on the operator's key, and what matters is the comparison, not the value.
TEST_FLEET_CERT = b"test-fleet-certificate"
TEST_FLEET_DIGEST = hashlib.sha256(TEST_FLEET_CERT).hexdigest()

THROWAWAY_CERT = b"test-throwaway-certificate"


# --- minimal APK builder ----------------------------------------------------
#
# Enough of the APK format for apk-facts.py to read package/versionCode/
# versionName/signerSha256: a zip containing a compiled AndroidManifest.xml
# and an APK Signing Block with one v2 signer. The "certificate" is arbitrary
# bytes; apk-facts.py hashes them without validating, which is exactly what
# the install path compares.


def _u8s(s: str) -> bytes:
    b = s.encode("utf-8")
    assert len(s) < 128 and len(b) < 128
    return bytes([len(s), len(b)]) + b


def _build_axml(package: str, version_code: int, version_name: str) -> bytes:
    strings = ["manifest", "unused", "package", package, version_name]
    enc = [_u8s(s) for s in strings]
    offsets = []
    off = 0
    for e in enc:
        offsets.append(off)
        off += len(e)
    strings_start = 28 + 4 * len(strings)
    body = struct.pack("<IIIII", len(strings), 0, 1 << 8, strings_start, 0)
    body += struct.pack("<%dI" % len(strings), *offsets)
    body += b"".join(enc)
    pool = struct.pack("<HHI", 0x0001, 28, 8 + len(body)) + body

    resmap_body = struct.pack("<II", 0x0101021B, 0x0101021C)
    resmap = struct.pack("<HHI", 0x0180, 8, 8 + len(resmap_body)) + resmap_body

    def attr(name_idx: int, raw_idx: int, data_type: int, data: int) -> bytes:
        # ns(u32) name(u32) rawValue(i32) + typed value: size(u16) res0(u8)
        # dataType(u8) data(u32)
        return struct.pack("<IIi", 0, name_idx, raw_idx) + struct.pack(
            "<HBBI", 8, 0, data_type, data
        )

    attrs = b"".join(
        [
            # package: name from the string pool (index past the resource map)
            attr(2, 3, 0x03, 0),
            # versionCode: name from the resource map (an int, not a string)
            attr(0, -1, 0x10, version_code),
            # versionName: name from the resource map, value from the pool
            attr(1, 4, 0x03, 0),
        ]
    )
    node = struct.pack("<IIHHHHHH", 0, 0, 20, 20, 3, 0xFFFF, 0xFFFF, 0xFFFF) + attrs
    element = struct.pack("<HHI", 0x0102, 8, 8 + len(node)) + node
    return (
        struct.pack("<HHI", 0x0003, 8, 8 + len(pool) + len(resmap) + len(element))
        + pool
        + resmap
        + element
    )


def _lp(b: bytes) -> bytes:
    return struct.pack("<I", len(b)) + b


def _add_v2_block(apk: bytes, cert_der: bytes) -> bytes:
    eocd = apk.rfind(b"PK\x05\x06")
    assert eocd != -1
    cd_offset = struct.unpack_from("<I", apk, eocd + 16)[0]
    signed_data = _lp(b"") + _lp(_lp(cert_der)) + _lp(b"")
    # value = LP(sequence of signers); each signer is length-prefixed and its
    # first item is the signed data. Three LP layers total.
    value = _lp(_lp(_lp(signed_data)))
    pair = struct.pack("<Q", 4 + len(value)) + struct.pack("<I", 0x7109871A) + value
    size = len(pair) + 24
    block = (
        struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"
    )
    new_eocd = bytearray(apk[eocd:])
    struct.pack_into("<I", new_eocd, 16, cd_offset + len(block))
    return apk[:cd_offset] + block + apk[cd_offset:eocd] + bytes(new_eocd)


def make_apk(path: Path, cert: bytes, version_code: int = 200) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(
            "AndroidManifest.xml",
            _build_axml(PACKAGE, version_code, "0.1.0-test"),
        )
    path.write_bytes(_add_v2_block(buf.getvalue(), cert))


# --- the script under test --------------------------------------------------


@pytest.fixture()
def stub_bin(tmp_path: Path) -> Path:
    """A PATH entry with stub adb/ssh: the gate fires before either is used."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("adb", "ssh"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def run_install(
    apk: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> "subprocess.CompletedProcess[str]":
    monkeypatch.setenv("ZIPPIE_FLEET_SIGNER_SHA256", TEST_FLEET_DIGEST)
    monkeypatch.setenv("PATH", str(stub_bin) + os.pathsep + os.environ["PATH"])
    return subprocess.run(
        [str(SCRIPT), str(apk), "--router", "dummy.invalid"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_fixture_apk_reads_back(tmp_path: Path) -> None:
    """The crafted APK is one apk-facts.py reads like a real build."""
    apk = tmp_path / "fixture.apk"
    make_apk(apk, THROWAWAY_CERT)
    out = subprocess.run(
        [sys.executable, str(CI_DIR / "apk-facts.py"), str(apk)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    facts = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    assert facts["package"] == PACKAGE
    assert facts["signerSha256"] == hashlib.sha256(THROWAWAY_CERT).hexdigest()


def test_throwaway_signed_apk_is_refused(
    tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: a throwaway-signed APK is rejected, and the message names the digest."""
    apk = tmp_path / "throwaway.apk"
    make_apk(apk, THROWAWAY_CERT)
    result = run_install(apk, stub_bin, monkeypatch)
    assert result.returncode != 0
    found = hashlib.sha256(THROWAWAY_CERT).hexdigest()
    assert "refusing" in result.stderr.lower(), result.stderr
    assert found in result.stderr, "the refusal must name the digest it found"
    assert TEST_FLEET_DIGEST in result.stderr, (
        "the refusal must name the fleet digest it expected"
    )
    # The gate fires before any network access: no router probe was attempted.
    assert "asking" not in result.stdout.lower(), result.stdout


def test_fleet_signed_apk_passes_the_gate(
    tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must not block a correctly signed build.

    It cannot install here (no router), but it must get PAST the signer check
    and fail at the network step instead - proving the gate is a comparison,
    not a blanket refusal.
    """
    apk = tmp_path / "fleet.apk"
    make_apk(apk, TEST_FLEET_CERT)
    result = run_install(apk, stub_bin, monkeypatch)
    assert result.returncode != 0  # no router in the test
    assert "could not probe the router" in result.stderr, result.stderr
    assert "refusing" not in result.stderr.lower(), result.stderr
