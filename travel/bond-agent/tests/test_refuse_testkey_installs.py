"""Refuse TESTKEY-signed installs via signer digest check in install path (#241).

A TESTKEY-signed APK reached a production handset and could never be upgraded,
because the CI job that created it destroyed the throwaway signing key.
Naming alone is not an enforcement mechanism: the APK's certificate SHA-256
digest must be checked against the pinned release certificate digest before any
installation is attempted, both for candidate builds and for on-device state.
Furthermore, TESTKEY builds must not be servable from the router web root.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import subprocess
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = REPO / "companion-android" / "ci" / "install-to-handsets.sh"
DEPLOY_SCRIPT = REPO / "scripts" / "deploy-openwrt.sh"
DRIFT_SCRIPT = REPO / "travel" / "gl-mt3000" / "drift-check.sh"
RELEASE_SIGNER = "ecaaf695e2ac5bee845edf075038437ab8ae668890c07012525640c652e477f7"


def make_test_apk(
    path: Path,
    cert_der: bytes,
    version_code: int = 200,
    version_name: str = "0.1.0-TESTKEY",
    package: str = "app.zippie.companion",
) -> str:
    """Construct a minimal valid APK with AndroidManifest.xml and v2 signing block.

    Returns the expected SHA-256 digest of the certificate.
    """
    cert_digest = hashlib.sha256(cert_der).hexdigest()

    # 1. Build UTF-8 string pool for binary AndroidManifest.xml
    strings = [
        "manifest",
        "package",
        "versionCode",
        "versionName",
        package,
        version_name,
    ]
    str_data = bytearray()
    offsets = []
    for s in strings:
        enc = s.encode("utf-8")
        offsets.append(len(str_data))
        str_data.extend(bytes([len(s), len(enc)]))
        str_data.extend(enc)
        str_data.append(0)
    while len(str_data) % 4 != 0:
        str_data.append(0)

    header_size = 28
    count = len(strings)
    strings_start = header_size + count * 4
    pool_chunk_size = strings_start + len(str_data)
    flags = 1 << 8  # UTF-8
    offsets_bytes = struct.pack(f"<{count}I", *offsets)
    pool_chunk = (
        struct.pack(
            "<HHI IIIII",
            0x0001,
            header_size,
            pool_chunk_size,
            count,
            0,
            flags,
            strings_start,
            0,
        )
        + offsets_bytes
        + bytes(str_data)
    )

    # 2. Build start element chunk (0x0102)
    attr_header = struct.pack("<II HHH HHH", 0xFFFFFFFF, 0, 20, 20, 3, 0, 0, 0)
    attr1 = struct.pack("<IIi HBB I", 0xFFFFFFFF, 1, 4, 8, 0, 0x03, 4)
    attr2 = struct.pack("<IIi HBB I", 0xFFFFFFFF, 2, -1, 8, 0, 0x10, version_code)
    attr3 = struct.pack("<IIi HBB I", 0xFFFFFFFF, 3, 5, 8, 0, 0x03, 5)

    start_elem_size = 16 + len(attr_header) + len(attr1) + len(attr2) + len(attr3)
    start_elem_chunk = (
        struct.pack("<HHI II", 0x0102, 16, start_elem_size, 1, 0xFFFFFFFF)
        + attr_header
        + attr1
        + attr2
        + attr3
    )

    axml_size = 8 + len(pool_chunk) + len(start_elem_chunk)
    axml = struct.pack("<HHI", 0x0003, 8, axml_size) + pool_chunk + start_elem_chunk

    # 3. Create initial zip file containing AndroidManifest.xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("AndroidManifest.xml", axml)
    zip_bytes = buf.getvalue()

    # 4. Locate central directory offset in EOCD
    eocd_idx = zip_bytes.rfind(b"PK\x05\x06")
    cd_offset = struct.unpack_from("<I", zip_bytes, eocd_idx + 16)[0]

    # 5. Build APK Signing Block v2
    cert_entry = struct.pack("<I", len(cert_der)) + cert_der
    certs_list = struct.pack("<I", len(cert_entry)) + cert_entry
    digests_list = struct.pack("<I", 0)
    signed_data = digests_list + certs_list
    signer = struct.pack("<I", len(signed_data)) + signed_data
    signers = struct.pack("<I", len(signer)) + signer
    v2_value = struct.pack("<I", len(signers)) + signers
    pair_size = 4 + len(v2_value)
    pair = struct.pack("<QI", pair_size, 0x7109871A) + v2_value
    block_content = pair
    total_size = len(block_content) + 24
    sig_block = (
        struct.pack("<Q", total_size)
        + block_content
        + struct.pack("<Q", total_size)
        + b"APK Sig Block 42"
    )

    # 6. Assemble signed APK with updated EOCD offset
    before_cd = zip_bytes[:cd_offset]
    cd_and_eocd = zip_bytes[cd_offset:]
    new_cd_offset = cd_offset + len(sig_block)
    rel_eocd = eocd_idx - cd_offset
    new_eocd = (
        cd_and_eocd[: rel_eocd + 16]
        + struct.pack("<I", new_cd_offset)
        + cd_and_eocd[rel_eocd + 20 :]
    )
    final_apk = before_cd + sig_block + new_eocd
    path.write_bytes(final_apk)
    return cert_digest


def test_feed_throwaway_signed_apk_asserts_rejection(tmp_path: Path):
    """A throwaway-signed APK must be refused loudly in candidate preflight."""
    apk_file = tmp_path / "zippie-companion-throwaway.apk"
    throwaway_cert = b"throwaway-ci-keystore-certificate-bytes-12345"
    digest = make_test_apk(apk_file, throwaway_cert)

    proc = subprocess.run(
        [
            "bash",
            str(INSTALL_SCRIPT),
            str(apk_file),
            "--dry-run",
            "--router",
            "fake-router.example-home.internal",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0, "install path accepted a throwaway-signed APK!"
    combined = proc.stdout + proc.stderr
    assert digest in combined, "error output must name the candidate throwaway signer found"
    assert RELEASE_SIGNER in combined, "error output must name the expected pinned release signer"
    assert "refusing to install TESTKEY or throwaway-signed build" in combined
    assert "asking fake-router" not in combined, "preflight should halt before probing the router"


def test_feed_matching_signer_apk_passes_candidate_preflight(tmp_path: Path):
    """An APK matching the pinned release signer passes candidate verification."""
    apk_file = tmp_path / "zippie-companion-release.apk"
    test_release_cert = b"matching-release-certificate-bytes-54321"
    digest = make_test_apk(apk_file, test_release_cert)

    env = os.environ.copy()
    env["ZIPPIE_FLEET_SIGNER"] = digest

    # We expect candidate check to pass and proceed to router probe (which fails connection)
    proc = subprocess.run(
        [
            "bash",
            str(INSTALL_SCRIPT),
            str(apk_file),
            "--dry-run",
            "--router",
            "127.0.0.1",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    combined = proc.stdout + proc.stderr
    assert f"signer       {digest}" in combined
    assert "refusing to install TESTKEY" not in combined
    assert "asking 127.0.0.1 what is advertising adb..." in combined


def test_router_web_root_guards_in_scripts():
    """Verify router deploy and drift scripts guard /www against TESTKEY APKs."""
    deploy_content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "/www/*TESTKEY*.apk" in deploy_content
    assert "zippie-block-testkey.conf" in deploy_content

    drift_content = DRIFT_SCRIPT.read_text(encoding="utf-8")
    assert "/www/*TESTKEY*.apk" in drift_content
    assert "FOUND TESTKEY/throwaway APK IN /www" in drift_content


def test_agent_dashboard_forbids_serving_apks(monkeypatch):
    """The agent dashboard HTTP handler must refuse requests for APK files."""
    from zippie.agent import BondAgent
    from zippie.models import AgentConfig, HomeConfig, PolicyConfig

    captured_handler = None

    def fake_install(_self, handler_cls):
        nonlocal captured_handler
        captured_handler = handler_cls

    monkeypatch.setattr("zippie.agent._install_dashboard_listeners", fake_install)

    cfg = AgentConfig(
        home=HomeConfig(endpoint="127.0.0.1"),
        policy=PolicyConfig(),
        paths=[],
    )
    agent = BondAgent(cfg)
    agent.start_dashboard()

    assert captured_handler is not None, "start_dashboard did not register a Handler"

    handler = captured_handler.__new__(captured_handler)
    errors = []
    handler.send_error = lambda code, msg="": errors.append((code, msg))

    handler.path = "/zippie-companion-0.1.0-TESTKEY.apk"
    handler.do_GET()
    assert errors == [(403, "serving APKs from agent dashboard is forbidden")]

    errors.clear()
    handler.path = "/release.apk"
    handler.do_GET()
    assert errors == [(403, "serving APKs from agent dashboard is forbidden")]

    # The suffix match lowercases the path, so an uppercase extension
    # must be refused too, not fall through to a 404.
    errors.clear()
    handler.path = "/zippie-companion-0.1.0-TESTKEY.APK"
    handler.do_GET()
    assert errors == [(403, "serving APKs from agent dashboard is forbidden")]

    errors.clear()
    handler.path = "/api/unknown"
    handler.do_GET()
    assert errors == [(404, "")]
