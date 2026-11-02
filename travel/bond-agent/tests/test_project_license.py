import hashlib
import unittest
from pathlib import Path


EXPECTED_AGPL_V3_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"


def matches_agpl_v3(text: str) -> bool:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() == EXPECTED_AGPL_V3_SHA256


class ProjectLicenseTests(unittest.TestCase):
    def test_repository_license_is_the_complete_gnu_agpl_v3_text(self) -> None:
        repository_root = Path(__file__).resolve().parents[3]
        license_text = (repository_root / "LICENSE").read_text(encoding="utf-8")

        self.assertTrue(
            license_text.startswith("                    GNU AFFERO GENERAL PUBLIC LICENSE\n")
        )
        self.assertIn("                       Version 3, 19 November 2007\n", license_text)
        self.assertIn("How to Apply These Terms to Your New Programs", license_text)
        self.assertTrue(license_text.rstrip().endswith("<https://www.gnu.org/licenses/>."))
        self.assertTrue(matches_agpl_v3(license_text))

    def test_previous_mit_license_does_not_satisfy_the_agpl_check(self) -> None:
        self.assertFalse(matches_agpl_v3("MIT License\n"))


if __name__ == "__main__":
    unittest.main()
