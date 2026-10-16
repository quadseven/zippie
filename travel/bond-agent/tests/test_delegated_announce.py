"""A phone announcing a VERIFIED PEER as a leg must have the peer dialled.

Same-LAN discovery (#270) lets one phone - the one holding the console token -
find another paired device over Bonjour/NSD, check its pairing proof, and
offer it as a leg without anyone editing zippie.toml. The announce for that
leg arrives from the ANNOUNCING phone, not from the peer.

#252 made the packet beat the claim: the router dials the address an announce
came from, because a phone on two networks guesses its own address wrong. For
a delegated announce that rule points the router at the wrong device - the
announcer's own address - and the leg would carry nothing while looking
configured. So an announce that says it is delegated keeps its claim. The
claim must still be a private address on this LAN; DynamicLegs.announce
refuses anything else either way.

These tests drive the claimed and observed addresses APART, because a test
where they agree cannot fail.
"""

from __future__ import annotations

from zippie.agent import _announce_host_for

ANNOUNCER = "10.99.0.241"
PEER = "10.99.0.57"


def test_a_delegated_announce_dials_the_peer_not_the_announcer():
    body = {"name": "spare-android", "host": PEER, "delegated": True}
    assert _announce_host_for(body, ANNOUNCER, {}, "spare-android") == PEER


def test_an_ordinary_announce_still_dials_where_it_came_from():
    """#252 is unchanged for a phone announcing itself."""
    body = {"name": "iphone", "host": PEER}
    assert _announce_host_for(body, ANNOUNCER, {}, "iphone") == ANNOUNCER


def test_only_a_literal_true_delegates():
    """A truthy string must not switch off #252. `"false"` is truthy in Python,
    and a lenient check would let any value flip which address is dialled."""
    for value in ("true", "false", 1, "yes", None):
        body = {"name": "iphone", "host": PEER, "delegated": value}
        assert _announce_host_for(body, ANNOUNCER, {}, "iphone") == ANNOUNCER, value


def test_a_delegated_announce_does_not_warn_about_the_disagreement(caplog):
    """The addresses disagree BY DESIGN here; the #252 warning would fire on
    every first renewal and teach the reader to ignore it."""
    body = {"name": "spare-android", "host": PEER, "delegated": True}
    with caplog.at_level("WARNING"):
        _announce_host_for(body, ANNOUNCER, {}, "spare-android")
    assert "reached us from" not in caplog.text
