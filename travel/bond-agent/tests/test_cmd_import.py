"""cmd_import regression tests (#2048).

Three bugs, all on the SAME command, and all three only appear on the SECOND
import - which is why a fresh-device test would have passed:

1. Re-importing `/etc/zippie/client.json` (the canonical copy the previous
   import made) crashed with shutil.SameFileError.
2. `[home]` was only written when zippie.toml did NOT already exist, so a
   re-import silently wrote nothing at all - including server_public_key. The
   agent then failed at `up` with "missing home server_public_key; import a
   client bundle first", and following that advice re-ran the import, which
   again wrote nothing. An unbreakable loop whose error message blamed the
   bundle.
3. Consequently a server rekey could never propagate to a provisioned device.

The fix splits SERVER IDENTITY (always refreshed - it comes from the bundle and
is never hand-edited) from [[paths]] (preserved - SSIDs are hand-edited on the
device), so re-import is idempotent and safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from zippie import cli  # noqa: E402


def _bundle(server_key: str = "SERVERKEY_V1=", ssid: str = "OriginalSSID") -> dict:
    return {
        "home": {
            "endpoint": "dns-e.example-home.invalid",
            "ports": [51900, 51901],
            "server_public_key": server_key,
        },
        "config": {
            "paths": [
                {
                    "name": "wifi",
                    "weight": 100,
                    "priority": 10,
                    "match": {"type": "ssid", "ssid": ssid},
                    "private_key": "k1",
                    "public_key": "p1",
                    "address_cidr": "10.9.0.2/32",
                    "port": 51900,
                }
            ]
        },
    }


@pytest.fixture
def dest(tmp_path):
    return tmp_path


def _write_bundle(path, doc) -> str:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _import(bundle_path: str, dest_dir, *, force: bool = False) -> int:
    return cli.cmd_import(
        argparse.Namespace(bundle=bundle_path, dest=str(dest_dir), force=force)
    )


class TestReimportIsIdempotent:
    def test_reimporting_the_canonical_copy_does_not_crash(self, dest):
        """Bug 1: `zippie import /etc/zippie/client.json` raised SameFileError.

        This is the most obvious command to run after a rekey, and it crashed.
        """
        src = _write_bundle(dest / "client.json", _bundle())
        assert _import(src, dest) == 0
        # Second import of the SAME file that is now also the canonical copy.
        assert _import(str(dest / "client.json"), dest) == 0

    def test_server_rekey_propagates_on_reimport(self, dest):
        """Bug 2/3: [home] was skipped whenever zippie.toml already existed."""
        bundle_file = dest / "client.json"
        _import(_write_bundle(bundle_file, _bundle("SERVERKEY_V1=")), dest)

        _write_bundle(bundle_file, _bundle("SERVERKEY_V2="))
        _import(str(bundle_file), dest)

        toml = (dest / "zippie.toml").read_text(encoding="utf-8")
        assert "SERVERKEY_V2=" in toml, "rekey did not propagate"
        assert "SERVERKEY_V1=" not in toml, "stale key left behind"

    def test_endpoint_change_propagates_on_reimport(self, dest):
        bundle_file = dest / "client.json"
        doc = _bundle()
        _import(_write_bundle(bundle_file, doc), dest)

        doc["home"]["endpoint"] = "dns-new.example-home.invalid"
        _write_bundle(bundle_file, doc)
        _import(str(bundle_file), dest)

        toml = (dest / "zippie.toml").read_text(encoding="utf-8")
        assert "dns-new.example-home.invalid" in toml
        assert "dns-e.example-home.invalid" not in toml


class TestHandEditsArePreserved:
    def test_reimport_keeps_hand_edited_ssids(self, dest):
        """The guard being replaced DID protect something real.

        SSIDs are edited on the device (`edit SSIDs in zippie.toml` is what
        import itself prints). Refreshing [home] must not cost the operator
        those edits, or the fix trades one bug for a worse one.
        """
        bundle_file = dest / "client.json"
        _import(_write_bundle(bundle_file, _bundle()), dest)

        toml_path = dest / "zippie.toml"
        toml_path.write_text(
            toml_path.read_text(encoding="utf-8").replace("OriginalSSID", "HotelWifi"),
            encoding="utf-8",
        )

        _write_bundle(bundle_file, _bundle("SERVERKEY_V2="))
        _import(str(bundle_file), dest)

        toml = toml_path.read_text(encoding="utf-8")
        assert "HotelWifi" in toml, "hand-edited SSID was clobbered"
        assert "OriginalSSID" not in toml
        assert "SERVERKEY_V2=" in toml, "server identity must still refresh"

    def test_force_resets_paths_from_the_bundle(self, dest):
        bundle_file = dest / "client.json"
        _import(_write_bundle(bundle_file, _bundle()), dest)

        toml_path = dest / "zippie.toml"
        toml_path.write_text(
            toml_path.read_text(encoding="utf-8").replace("OriginalSSID", "HotelWifi"),
            encoding="utf-8",
        )

        _import(str(bundle_file), dest, force=True)

        toml = toml_path.read_text(encoding="utf-8")
        assert "OriginalSSID" in toml
        assert "HotelWifi" not in toml

    def test_existing_toml_without_paths_gets_paths_generated(self, dest):
        """A half-written config must not survive as a server block with no paths.

        Splicing "everything from the first [[paths]] header" yields nothing
        when there is no such header. Leaving it there would produce a config
        that parses fine and binds no interfaces.
        """
        bundle_file = dest / "client.json"
        toml_path = dest / "zippie.toml"
        toml_path.write_text("[home]\nendpoint = \"stale\"\n", encoding="utf-8")

        _import(_write_bundle(bundle_file, _bundle()), dest)

        toml = toml_path.read_text(encoding="utf-8")
        assert "[[paths]]" in toml, "no paths generated for a half-written config"
        assert "OriginalSSID" in toml


class TestKeysAreStillWritten:
    def test_keys_json_written_and_locked_down(self, dest):
        src = _write_bundle(dest / "client.json", _bundle())
        _import(src, dest)
        keys_path = dest / "keys.json"
        assert keys_path.exists()
        assert json.loads(keys_path.read_text())["paths"]["wifi"]["private_key"] == "k1"
        assert oct(keys_path.stat().st_mode)[-3:] == "600"
