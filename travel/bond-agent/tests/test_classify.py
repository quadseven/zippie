"""Which packets get duplicated.

The classifier decides where the bandwidth budget goes, so the tests are
mostly about NOT spending it stupidly: duplicating bulk transfer would halve
the bond's usable throughput for no benefit.
"""

from __future__ import annotations

import pytest

from zippie.classify import Classifier, ClassifierConfig
from zippie.datapath import SendMode


def _c(**kw):
    return Classifier(ClassifierConfig(**kw))


class TestSizeSplit:
    @pytest.mark.parametrize("size,label", [
        (40, "bare TCP ACK"),
        (72, "DNS query"),
        (100, "SSH keystroke"),
        (200, "G.711 voice frame"),
    ])
    def test_interactive_traffic_is_duplicated(self, size, label):
        """These are what a dropped packet is actually FELT on -- and they are
        cheap enough that duplicating them costs almost nothing."""
        assert _c().mode_for(size, paths_available=2) is SendMode.DUPLICATE, label

    @pytest.mark.parametrize("size,label", [
        (900, "video payload"),
        (1420, "full-MTU bulk transfer"),
    ])
    def test_bulk_traffic_is_sprayed_not_duplicated(self, size, label):
        """Duplicating bulk would halve usable bandwidth to protect data TCP
        would have retransmitted anyway."""
        assert _c().mode_for(size, paths_available=2) is SendMode.SPRAY, label

    def test_threshold_is_inclusive_and_configurable(self):
        c = _c(duplicate_max_bytes=250)
        assert c.mode_for(250, paths_available=2) is SendMode.DUPLICATE
        assert c.mode_for(251, paths_available=2) is SendMode.SPRAY


class TestPathCountMatters:
    def test_one_path_is_never_reported_as_duplicated(self):
        """Duplicating onto a single path is SINGLE with extra bookkeeping.
        Calling it DUPLICATE would make the stats claim redundancy that does
        not exist -- exactly the kind of number that gets trusted later."""
        assert _c().mode_for(40, paths_available=1) is SendMode.SINGLE
        assert _c(duplicate_all=True).mode_for(40, paths_available=1) is SendMode.SINGLE

    def test_zero_paths_still_returns_a_mode_rather_than_raising(self):
        """Total outage must not crash the classifier; the scheduler is the
        thing that decides there is nowhere to send."""
        assert _c().mode_for(40, paths_available=0) is SendMode.SINGLE


class TestBudgetControls:
    def test_bond_overhead_is_never_duplicated(self):
        """Insurance on a keepalive buys nothing, even when it is small."""
        c = _c(duplicate_all=True)
        assert c.mode_for(32, paths_available=3, overhead=True) is SendMode.SINGLE
        assert c.stats()["overhead"] == 1

    def test_duplication_can_be_turned_off_entirely(self):
        """For a month where every path is metered and the budget matters more
        than the call."""
        c = _c(duplicate_enabled=False)
        assert c.mode_for(40, paths_available=2) is SendMode.SPRAY
        assert c.mode_for(1400, paths_available=2) is SendMode.SPRAY

    def test_duplicate_all_turns_the_bond_into_pure_redundancy(self):
        """Legitimate on an unmetered pair: trade all aggregate bandwidth for
        immunity to losing a path."""
        c = _c(duplicate_all=True)
        assert c.mode_for(1420, paths_available=2) is SendMode.DUPLICATE

    def test_disabled_beats_duplicate_all(self):
        """A contradictory config must fail SAFE for the data budget, not
        against it."""
        c = _c(duplicate_enabled=False, duplicate_all=True)
        assert c.mode_for(40, paths_available=2) is SendMode.SPRAY


class TestStats:
    def test_reports_how_much_is_being_duplicated(self):
        """The operator needs to see what the bandwidth is being spent on --
        a misconfigured threshold is otherwise invisible until the data cap."""
        c = _c()
        for _ in range(9):
            c.mode_for(1400, paths_available=2)   # bulk -> spray
        c.mode_for(40, paths_available=2)         # ack  -> duplicate
        s = c.stats()
        assert s["duplicate"] == 1 and s["spray"] == 9
        assert s["duplicate_pct"] == 10
