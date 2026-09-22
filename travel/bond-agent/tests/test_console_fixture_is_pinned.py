"""The console HTTPS fixture key is exempt from the detect-private-key hook
by exact path (infra#995). A path exemption would also exempt a REAL key
saved there later, so the fixture is pinned by content: replace either file
and this fails, which forces the exemption to be re-argued.

Both are a self-signed CN=127.0.0.1 pair used only by test_console_https.py.
"""

import hashlib
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PINNED = {
    "console-test.key": "41eb9fd17c6ea00243deb615a6c15e983ec24a1e87d4c5cbf2899eabdd4153a1",
    "console-test.crt": "12a6229a565b26211629ced0a037a80c9d648106cfbe76c28dd2486d27060969",
}


def test_console_fixture_pair_is_the_known_test_pair():
    for name, digest in PINNED.items():
        got = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert got == digest, (
            f"{name} changed. It is exempt from detect-private-key by path "
            "(infra#995); a changed file must be re-reviewed as a possible "
            "real key before this pin moves."
        )
