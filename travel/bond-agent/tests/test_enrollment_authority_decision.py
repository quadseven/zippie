from pathlib import Path


def test_enrollment_authority_is_recorded():
    decisions = (Path(__file__).resolve().parents[3] / "DECISIONS.md").read_text()
    assert "The travel router must not hold a CA key or sign device certificates." in decisions
    assert "The home enrollment endpoint issues certificates after operator approval." in decisions
