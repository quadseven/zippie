"""The home address must survive a power cycle, or a cold boot deadlocks (#182).

WHY THIS IS THE WHOLE PROJECT IN ONE FILE. zippie exists so an unattended router
and a locked phone can bring each other up with nobody touching either. That
requires the router to form a bond with NO internet available, because the phone
IS the internet it is trying to reach.

Resolving the home endpoint needs DNS. DNS needs internet. Internet needs a
carrying leg. A leg only earns weight once the transport's keepalives are
answered by the home end - which cannot happen until something knows where to
send them. A process that starts with an empty cache and no uplink can never
break that circle, and every earlier success had merely inherited a warm cache
from a previous run.

Measured on the travel router 2026-08-16 with a phone as the only uplink:

    17:46:35  zippie starts
    17:48:26  leg announced pixel-6a-a554 endpoint=10.99.0.174:51999
    17:48:45  link up: pixel-6a-a554 via br-lan
    17:52:00  watchdog: no leg is carrying          <- 3m15s at weight 0

`logread | grep -i "home endpoint"` was EMPTY for the entire boot. That line
prints only on a successful resolve, so its absence is the proof: the address was
never obtained, the transport had nowhere to send keepalives, and the phone's
twelve announcements over four minutes had nothing to answer them.

THESE TESTS DO NOT NEED A ROUTER, and that is deliberate. The bug is only
reachable in the first seconds of a process that has never resolved, which is the
one state a long-running test harness never reproduces by accident.
"""

from __future__ import annotations

import pytest

from zippie.store import HomeAddressStore


# --------------------------------------------------------------- the store


def test_a_missing_file_is_not_an_error(tmp_path):
    """First boot ever. No file, no address, no crash."""
    assert HomeAddressStore(tmp_path).load() is None


def test_a_saved_address_reads_back(tmp_path):
    s = HomeAddressStore(tmp_path)
    assert s.save("203.0.113.33") is True
    assert s.load() == "203.0.113.33"


def test_saving_the_same_address_does_not_rewrite_flash(tmp_path):
    """This is flash on a router that boots from it. The address changes at the
    pace of a dynamic-DNS update; rewriting it on a timer is wear for nothing."""
    s = HomeAddressStore(tmp_path)
    s.save("203.0.113.33")
    before = (tmp_path / "home-ip").stat().st_mtime_ns
    assert s.save("203.0.113.33") is False, "rewrote an unchanged address"
    assert (tmp_path / "home-ip").stat().st_mtime_ns == before


def test_a_changed_address_is_written(tmp_path):
    """Dynamic DNS moves. When it does, the persisted copy has to follow."""
    s = HomeAddressStore(tmp_path)
    s.save("203.0.113.33")
    assert s.save("203.0.113.9") is True
    assert s.load() == "203.0.113.9"


@pytest.mark.parametrize("junk", [
    "",                     # truncated write
    "   \n",                # whitespace only
    "dns-e.example.com",    # a hostname somebody pasted in
    "not an address",
    "999.1.1.1",            # out of range
    "10",                   # inet_aton accepts this; the datapath cannot use it
    "1.2.3",                # ditto - short forms are not dotted quads
])
def test_an_unusable_file_reads_as_no_address(tmp_path, junk):
    """A bad file must read as "no address", never propagate.

    This value is handed to the datapath as a send target. A router in a car
    loses power mid-write, and somebody will eventually hand-edit this file
    because it is one line of plain text sitting in /etc.
    """
    (tmp_path / "home-ip").write_text(junk)
    assert HomeAddressStore(tmp_path).load() is None


def test_trailing_whitespace_is_tolerated(tmp_path):
    """`echo 1.2.3.4 > home-ip` by hand is a legitimate way to seed this."""
    (tmp_path / "home-ip").write_text("203.0.113.33\n")
    assert HomeAddressStore(tmp_path).load() == "203.0.113.33"


# --------------------------------------------------------------- the agent


def _agent(tmp_path, endpoint="dns-e.example.com:51900"):
    from zippie.agent import BondAgent
    from zippie.config import parse_config
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": endpoint, "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet"},
        "paths": [{"name": "ethernet", "interface": "eth0"}],
    }))


def test_a_cold_start_with_no_dns_still_has_somewhere_to_dial(tmp_path, monkeypatch):
    """THE DEADLOCK, pinned.

    A fresh process, a persisted address, and a resolver that cannot answer -
    which is precisely the router 90 seconds after a power cut with the phone as
    its only uplink. The agent must come up knowing where home is.
    """
    (tmp_path / "home-ip").write_text("203.0.113.33\n")

    from zippie import net
    monkeypatch.setattr(net, "resolve_host",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no DNS")))

    a = _agent(tmp_path)
    assert a._home_ip == "203.0.113.33", "started with no idea where home is"
    assert a._resolve_home_ip() == "203.0.113.33", (
        "a failed lookup discarded the last known good address - the transport "
        "then has no remote, so no keepalive is sent, so no leg can earn weight"
    )


def test_the_persisted_address_reaches_a_links_remote(tmp_path, monkeypatch):
    """WIRED, not merely stored.

    A field on the agent proves nothing: the address only matters if it reaches
    the endpoint a link actually sends to. `sync_transport` resolves home once
    per pass and hands it down as each leg's default remote, so that is the
    value asserted here rather than `_home_ip`.

    A leg with a `relay_endpoint` - a companion phone - deliberately does NOT
    use this: it dials the phone on the LAN, because the phone is the hop that
    owns the cellular. This test therefore uses an ordinary leg, which is
    exactly the kind that cannot reach home at all without an address.
    """
    (tmp_path / "home-ip").write_text("203.0.113.33\n")

    from zippie import net
    monkeypatch.setattr(net, "resolve_host",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no DNS")))

    a = _agent(tmp_path)
    seen = {}

    def _capture(path, pid, *, usable, carrying, remote):
        seen[path.name] = remote

    # sync_transport returns immediately without a transport, and keepalives it
    # sends at the end are not what this asserts. Only the remote handed down.
    class _Stub:
        def send_keepalives(self):
            pass

    a._transport = _Stub()  # type: ignore[assignment]
    monkeypatch.setattr(a, "_reconcile_link", _capture)
    a.sync_transport()

    assert seen, "no link was reconciled at all"
    host, _port = seen["ethernet"]
    assert host == "203.0.113.33", (
        f"link remote is {host!r}. With no DNS and no persisted address this "
        "falls back to the raw endpoint STRING, which cannot be sent to - so "
        "no keepalive leaves, no leg earns weight, and the bond never forms"
    )


def test_a_cold_start_without_a_persisted_address_is_unchanged(tmp_path, monkeypatch):
    """No file, no DNS: still None. The fix must not invent an address."""
    from zippie import net
    monkeypatch.setattr(net, "resolve_host", lambda *a, **k: "")

    a = _agent(tmp_path)
    assert a._home_ip is None
    assert a._resolve_home_ip() is None


def test_the_seeded_address_does_not_suppress_the_first_real_lookup(tmp_path, monkeypatch):
    """Stale-on-arrival, by design.

    time.monotonic() reads seconds-since-boot on Linux, so stamping the seeded
    value with a real timestamp would put it INSIDE the TTL and the agent would
    trust a possibly-stale address for five minutes without ever asking. It must
    be good enough to dial immediately and never trusted in place of a resolve.
    """
    (tmp_path / "home-ip").write_text("203.0.113.33\n")
    calls = []

    from zippie import net

    def _resolve(host, **kw):
        calls.append(host)
        return "203.0.113.9"

    monkeypatch.setattr(net, "resolve_host", _resolve)

    a = _agent(tmp_path)
    assert a._resolve_home_ip() == "203.0.113.9", "used the stale seed instead of DNS"
    assert calls, "never attempted a lookup - the seed was treated as fresh"


def test_a_successful_resolve_is_persisted_for_the_next_boot(tmp_path, monkeypatch):
    from zippie import net
    monkeypatch.setattr(net, "resolve_host", lambda *a, **k: "203.0.113.33")
    monkeypatch.setattr(net, "dry_run", lambda: False)

    a = _agent(tmp_path)
    a._resolve_home_ip()

    assert HomeAddressStore(tmp_path).load() == "203.0.113.33", (
        "resolved and then forgot it - the next cold boot deadlocks again"
    )


def test_a_private_address_is_dialed_but_never_persisted(tmp_path, monkeypatch):
    """A HIJACKED LOOKUP MUST NOT BECOME PERMANENT.

    net.is_private_v4 records why a home endpoint resolving private means the
    LOOKUP was hijacked - a captive portal, or the dead Fi dongle on 2026-08-02
    that answered every query with a sequential fake address. Dialing it for this
    run is existing behaviour and unchanged. Writing it to flash is not: it would
    become the router's permanent idea of home and be dialled FIRST on every cold
    boot from then on. A bad lookup should cost one run, not every future one.
    """
    from zippie import net
    monkeypatch.setattr(net, "resolve_host", lambda *a, **k: "192.168.3.95")
    monkeypatch.setattr(net, "dry_run", lambda: False)

    a = _agent(tmp_path)
    assert a._resolve_home_ip() == "192.168.3.95", "stopped dialing it - behaviour changed"
    assert not (tmp_path / "home-ip").exists(), "persisted a hijacked lookup"


def test_a_good_address_already_on_disk_survives_a_later_hijack(tmp_path, monkeypatch):
    """The case that matters: a captive portal must not erase a working address."""
    (tmp_path / "home-ip").write_text("203.0.113.33\n")

    from zippie import net
    monkeypatch.setattr(net, "resolve_host", lambda *a, **k: "192.168.3.95")
    monkeypatch.setattr(net, "dry_run", lambda: False)

    a = _agent(tmp_path)
    a._resolve_home_ip()

    assert HomeAddressStore(tmp_path).load() == "203.0.113.33", (
        "a hijacked lookup overwrote the last address known to work"
    )


def test_an_unwritable_state_dir_does_not_kill_the_bond(tmp_path, monkeypatch):
    """Failing to persist costs a slow cold boot later. Raising costs the bond now."""
    from zippie import net
    monkeypatch.setattr(net, "resolve_host", lambda *a, **k: "203.0.113.33")
    monkeypatch.setattr(net, "dry_run", lambda: False)

    a = _agent(tmp_path)

    def _boom(self, addr):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(HomeAddressStore, "save", _boom)
    assert a._resolve_home_ip() == "203.0.113.33"
