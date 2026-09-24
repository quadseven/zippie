"""serve_static must never hand out a TESTKEY-signed APK.

A TESTKEY build that reaches a handset can never be upgraded over, so the
router's web root refuses one by name. The name is judged AFTER URL
decoding: a check on the raw path lets ".ap%6b" through.
"""

from __future__ import annotations

import pytest

import hub


class _Recorder:
    def _send(self, code: int, body: bytes, ctype: str) -> int:
        return code


@pytest.mark.parametrize(
    "path",
    [
        "/zippie-testkey.apk",
        "/Zippie-TESTKEY-1.APK",
        "/zippie-testkey.ap%6b",
        "/zippie-%74estkey.apk",
    ],
)
def test_testkey_apk_is_refused(path: str) -> None:
    assert hub.serve_static(_Recorder(), path) == 403


def test_other_static_files_are_not_refused() -> None:
    assert hub.serve_static(_Recorder(), "/index.html") == 200
    assert hub.serve_static(_Recorder(), "/zippie-release.apk") == 404
