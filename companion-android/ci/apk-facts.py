"""Read an APK's identity and its signing certificate WITHOUT the Android SDK.

    apk-facts.py <apk> [<apk> ...]        one `key=value` line per fact
    apk-facts.py --json <apk>             the same as one JSON object per APK

WHY THIS EXISTS. The two questions that decide whether a build can be installed
over another one - "is the version code higher" and "is it the same signing
certificate" - are answered by `aapt2 dump badging` and `apksigner`, which live
in the Android SDK. The machine holding the handset is not necessarily the
machine with the SDK: the operator's laptop has `adb` from a package manager
and nothing else, and that is the machine that runs the install.

Without this, the preflight cannot run where it matters and the failure moves
to `adb install`, which reports INSTALL_FAILED_UPDATE_INCOMPATIBLE and leaves
the operator guessing which of the two facts was wrong.

TRUSTWORTHINESS IS NOT ASSUMED, IT IS CHECKED ON EVERY BUILD. build-signed-apk.sh
runs this against the APK it just produced and asserts the answers match aapt2
and apksigner exactly, so the SDK-free reader is differentially tested against
the SDK continuously, with no fixture APKs to go stale. If Android changes a
format, a build fails rather than an install.

Stdlib only, and deliberately read-only: it never writes, unpacks or executes.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
import zipfile

# --- APK Signing Block (v2) -------------------------------------------------
#
# It sits immediately before the central directory. The v2 block's signer
# carries the certificate chain; the certificate's SHA-256 is the identity
# Android compares on an update, and it is the ONLY thing that distinguishes a
# throwaway CI key from the real one - the certificate SUBJECT is identical on
# both, which is how a throwaway-signed build once reached the fleet.
SIG_BLOCK_MAGIC = b"APK Sig Block 42"
V2_BLOCK_ID = 0x7109871A

# Resource ids for the two `manifest` attributes worth reading. Attribute NAMES
# in a compiled manifest are usually empty strings - the name is carried by the
# resource map instead - so matching on the pool string alone finds nothing.
ATTR_BY_RES_ID = {0x0101021B: "versionCode", 0x0101021C: "versionName"}


class NotAnApk(Exception):
    pass


def _string_pool(chunk: bytes) -> list[str]:
    count, _style_count, flags, strings_start, _styles_start = struct.unpack_from(
        "<IIIII", chunk, 8
    )
    offsets = struct.unpack_from("<%dI" % count, chunk, 28)
    utf8 = bool(flags & (1 << 8))
    out = []
    for offset in offsets:
        p = strings_start + offset
        if utf8:
            # Two lengths, each 1 or 2 bytes: characters then bytes. Only the
            # second one measures the buffer.
            for _ in range(2):
                n = chunk[p]
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | chunk[p + 1]
                    p += 2
                else:
                    p += 1
            out.append(chunk[p:p + n].decode("utf-8", "replace"))
        else:
            n = struct.unpack_from("<H", chunk, p)[0]
            p += 2
            out.append(chunk[p:p + n * 2].decode("utf-16-le", "replace"))
    return out


def manifest_facts(axml: bytes) -> dict:
    """package / versionCode / versionName off the first `manifest` element."""
    pool: list[str] = []
    res_map: list[int] = []
    off = 8
    while off < len(axml):
        chunk_type, header_size, size = struct.unpack_from("<HHI", axml, off)
        if size == 0:
            break
        if chunk_type == 0x0001:  # RES_STRING_POOL_TYPE
            pool = _string_pool(axml[off:off + size])
        elif chunk_type == 0x0180:  # RES_XML_RESOURCE_MAP_TYPE
            n = (size - header_size) // 4
            res_map = list(struct.unpack_from("<%dI" % n, axml, off + header_size))
        elif chunk_type == 0x0102:  # RES_XML_START_ELEMENT_TYPE
            # ResXMLTree_attrExt, straight after the 16-byte node header:
            # ns, name, attributeStart, attributeSize, attributeCount, ...
            p = off + header_size
            element = pool[struct.unpack_from("<I", axml, p + 4)[0]]
            if element != "manifest":
                off += size
                continue
            attr_start = p + struct.unpack_from("<H", axml, p + 8)[0]
            attr_count = struct.unpack_from("<H", axml, p + 12)[0]
            facts = {}
            for i in range(attr_count):
                a = attr_start + i * 20
                name_idx = struct.unpack_from("<I", axml, a + 4)[0]
                raw_idx = struct.unpack_from("<i", axml, a + 8)[0]
                data_type = axml[a + 15]
                value = struct.unpack_from("<I", axml, a + 16)[0]
                key = ATTR_BY_RES_ID.get(res_map[name_idx]) if name_idx < len(res_map) else None
                if key is None:
                    key = pool[name_idx]
                # 0x03 is TYPE_STRING, whose real value is the raw pool entry.
                facts[key] = pool[raw_idx] if data_type == 0x03 and raw_idx >= 0 else value
            return {k: facts[k] for k in ("package", "versionCode", "versionName") if k in facts}
        off += size
    raise NotAnApk("no `manifest` element in AndroidManifest.xml")


def _signing_block(blob: bytes) -> bytes:
    end = len(blob) - 22
    floor = max(0, end - 65536)  # the comment field bounds how far back it can be
    for i in range(end, floor - 1, -1):
        if blob[i:i + 4] == b"PK\x05\x06":
            cd_offset = struct.unpack_from("<I", blob, i + 16)[0]
            break
    else:
        raise NotAnApk("no end-of-central-directory record")
    if blob[cd_offset - 16:cd_offset] != SIG_BLOCK_MAGIC:
        raise NotAnApk("no APK Signing Block - the APK is unsigned or v1 only")
    size_at_end = struct.unpack_from("<Q", blob, cd_offset - 24)[0]
    start = cd_offset - 8 - size_at_end
    size_at_start = struct.unpack_from("<Q", blob, start)[0]
    if size_at_start != size_at_end:
        raise NotAnApk("APK Signing Block size fields disagree - the file is damaged")
    return blob[start + 8:cd_offset - 24]


def _length_prefixed(buf: bytes):
    off = 0
    while off < len(buf):
        n = struct.unpack_from("<I", buf, off)[0]
        yield buf[off + 4:off + 4 + n]
        off += 4 + n


def signer_digests(path: str) -> list[str]:
    with open(path, "rb") as fh:
        blob = fh.read()
    block = _signing_block(blob)
    off = 0
    while off < len(block):
        pair_size = struct.unpack_from("<Q", block, off)[0]
        pair_id = struct.unpack_from("<I", block, off + 8)[0]
        value = block[off + 12:off + 8 + pair_size]
        if pair_id == V2_BLOCK_ID:
            out = []
            for signer in _length_prefixed(next(_length_prefixed(value))):
                signed_data = next(_length_prefixed(signer))
                # signed data is: digests, then certificates, then attributes.
                certificates = list(_length_prefixed(signed_data))[1]
                for der in _length_prefixed(certificates):
                    out.append(hashlib.sha256(der).hexdigest())
            return out
        off += 8 + pair_size
    raise NotAnApk("no v2 signature - this build cannot install on minSdk 29")


def facts(path: str) -> dict:
    with zipfile.ZipFile(path) as zf:
        out = manifest_facts(zf.read("AndroidManifest.xml"))
    digests = signer_digests(path)
    out["signerSha256"] = digests[0]
    if len(digests) > 1:
        out["extraSigners"] = ",".join(digests[1:])
    return out


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    paths = [a for a in argv if a != "--json"]
    if not paths:
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 2
    for path in paths:
        try:
            got = facts(path)
        except (NotAnApk, KeyError, zipfile.BadZipFile) as exc:
            sys.stderr.write("%s: %s\n" % (path, exc))
            return 1
        if as_json:
            print(json.dumps(got, sort_keys=True))
        else:
            for key, value in got.items():
                print("%s=%s" % (key, value))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
