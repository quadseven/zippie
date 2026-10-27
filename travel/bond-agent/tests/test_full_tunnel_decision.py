from pathlib import Path


def test_full_tunnel_decision_is_recorded():
    decisions = " ".join((Path(__file__).resolve().parents[3] / "DECISIONS.md").read_text().split())
    assert "Keep all travel LAN traffic in the full tunnel" in decisions
    assert "Local breakout would expose traffic to the local network" in decisions
    assert "114 ms" in decisions and "177 MB" in decisions and "9.5 Mbps" in decisions
