"""Durable per-leg state: what we measured, and what the operator told us.

WHY THIS EXISTS. `usage.json` was READ at startup and never written - the file
did not exist on the live router and /var/lib/zippie was empty. So usage_gb
reset to zero on every restart, which means monthly_cap_gb and over_soft_limit
could never fire: the counter never survived long enough to approach a cap.
Data caps were decorative.

TWO STORES, DELIBERATELY SEPARATE.

`usage.json` is MEASURED - bytes this agent counted. It is rewritten
constantly and is safe to lose: worst case a month's accounting restarts.

It also carries the PERIOD each counter belongs to. Without that the counter
only ever grew, so "monthly usage" meant "usage since the file was first
written" - and because `over_soft_limit` feeds the policy's cost ranking, a leg
that crossed its cap was demoted permanently, by an accounting artifact rather
than by any real plan state. The period lives beside the counter (rather than
in legs.json, or in a running timer) because the router is powered off far more
than it is on: the boundary has to be noticed by the next START, not by a loop
that was not running when it passed.

`legs.json` is TOLD - what a human typed. Carrier, plan name, the cap the
provider CLAIMS as opposed to the bytes we counted, and any deliberate
override of tier, weight or throughput. Losing it means losing something
nobody can recompute, so it is never written by the same code path that
writes counters, and never overwritten wholesale by the agent.

JSON RATHER THAN SQLITE because the router's python3 has no sqlite3 module -
verified on the GL-MT3000, where `import sqlite3` raises. JSON is also
inspectable over ssh with cat, which on a device in a car matters more than
query power.

NOT THE CONFIG FILE. zippie.toml stays the source of truth for IDENTITY -
names, interface matches, keys - because those are the things that must not be
silently rewritten by a program. This store holds what CHANGES.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Usage is flushed at most this often. A router runs from flash, and rewriting
# a file every control tick (500ms) would be a wear problem for no benefit -
# the counter is an estimate either way.
FLUSH_INTERVAL_S = 60.0


def _atomic_write(path: Path, payload: str) -> None:
    """Write via a temp file and rename.

    A router in a car loses power without warning. A partial write to the file
    holding the operator's plan metadata would be worse than no file at all,
    because the agent would parse whatever survived and carry on with it.
    rename() within one filesystem is atomic, so a reader sees either the old
    file or the new one and never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        # Leave no debris a later run would mistake for real state.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: The cycle day used when nobody has said otherwise. 1 makes the period a
#: plain calendar month, which is what an unconfigured leg gets.
DEFAULT_BILLING_DAY = 1

#: A wall clock reading earlier than this is BROKEN, not history.
#:
#: Rollover is the one piece of this file that trusts the wall clock, and a
#: router is the worst place to do that: it has no reason to know the date
#: until NTP answers, and NTP needs the very uplink this agent is still
#: bringing up. A boot that reads 1970 (or a firmware build date) would compute
#: "the current period" as something decades before the stored one.
#:
#: Two guards, and this is the second one. `roll` only ever moves FORWARD, so a
#: backwards clock cannot roll anything on its own. This floor covers the case
#: the forward-only rule cannot: a counter file with no period recorded yet,
#: where a 1970 reading would be ADOPTED as the period start and would then
#: roll - discarding a real month - the moment the clock corrected itself.
#:
#: Dated to before this code shipped, so it can only ever reject a clock that
#: is obviously wrong rather than a date that is merely older than expected.
CLOCK_SANITY_FLOOR = date(2025, 1, 1)


def _today() -> date:
    """Local calendar date.

    LOCAL, not UTC, and spelled out via localtime() rather than left to a
    naive datetime so the choice is visible: a billing day is a thing a human
    read off a carrier bill, and "the 14th" means the 14th where they are. The
    few hours of skew this puts on a boundary do not matter to a counter that
    is an estimate of bytes in the first place.
    """
    tm = time.localtime()
    return date(tm.tm_year, tm.tm_mon, tm.tm_mday)


def sane_billing_day(value: Any) -> int:
    """A hand-typed cycle day, clamped into a day-of-month.

    legs.json is hand-edited and also written by the phone app, so this takes
    anything: "14", 14, None, "the 14th". Unreadable becomes the default rather
    than an exception, because the alternative is an accounting field taking
    the bond down.

    A number OUT OF RANGE is clamped rather than discarded, so there is no
    cliff where 31 means the end of the month and 32 jumps to the start of it.
    The clamp to the month's own length happens later, in period_start.
    """
    try:
        day = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BILLING_DAY
    return min(31, max(1, day))


def period_start(today: date, billing_day: Any = DEFAULT_BILLING_DAY) -> date:
    """First day of the billing period containing `today`.

    THE PERIOD MODEL, AND WHY IT IS THIS ONE.

    A cap is not a property of the calendar, it is a property of a PLAN: the
    carrier zeroes the allowance on a cycle day, and every one of these caps
    was typed in from a carrier's own bill. Reset on the 1st when the plan
    resets on the 14th and the counter feeding `over_soft_limit` describes a
    window the carrier has never heard of - up to half a month of usage
    attributed to the wrong period, in the direction that demotes a leg that
    actually has allowance left.

    This is ONE model, not two. A calendar month is the billing cycle whose day
    is 1, which is what a leg gets when no `billing_day` is set - so the simple
    case needs no configuration and costs no extra code path.

    The day is CLAMPED to the length of the month rather than skipped: a cycle
    day of 31 lands on the 28th in February. Skipping would leave February with
    no boundary at all, which is a month of usage silently accruing into
    January's total.
    """
    day = sane_billing_day(billing_day)
    start_here = min(day, calendar.monthrange(today.year, today.month)[1])
    if today.day >= start_here:
        return date(today.year, today.month, start_here)
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _parse_date(value: str) -> date | None:
    """An ISO date from the state file, or None if it is not one.

    None means "no usable anchor", which the caller treats as adopt-do-not-
    zero. A file someone hand-edited into nonsense must not read as a boundary
    crossing.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class LegUsage:
    """One leg's counter plus the boundary it is being counted against.

    `period_start` is the ANCHOR, and it is persisted for one reason: the
    router is off more than it is on. A rollover that only fires while the
    process is running never fires at all on a device that is powered down
    across the 1st of the month - which is every month. Storing the period the
    counter belongs to means the next START can see it is stale, whether that
    start is a second later or a season later.

    Empty `period_start` means "not established yet" - a counter written before
    this existed, or one flushed by a caller that never rolled. It is adopted
    into the current period rather than zeroed: the bytes were really measured,
    and there is no evidence of which period they belong to.
    """

    usage_gb: float = 0.0
    period_start: str = ""
    previous_usage_gb: float = 0.0
    previous_period_start: str = ""


class UsageStore:
    """Measured bytes per leg, surviving restarts, counted per billing period."""

    def __init__(self, state_dir: str | Path, clock=time.monotonic, today=_today) -> None:
        self.path = Path(state_dir) / "usage.json"
        self._clock = clock
        # TWO CLOCKS, DELIBERATELY. `clock` is monotonic and paces the flush -
        # it must not jump when NTP steps the time. `today` is the wall
        # calendar and is the only thing that can answer "which billing period
        # is this". Injected so a boundary test can freeze it: a test that
        # pinned the boundary against the real clock would flake on the one
        # edge it exists to cover.
        self._today = today
        # None, not 0.0. A zero would compare as "flushed at time zero", which
        # on a monotonic clock that also starts near zero means the FIRST write
        # is suppressed for a full interval - and on a router that reboots more
        # often than that, the first write is the only one that ever matters.
        self._last_flush: float | None = None
        self._dirty = False
        #: Period bookkeeping per leg, kept beside the counter the caller owns.
        self.periods: dict[str, LegUsage] = {}
        self._clock_warned = False

    def load(self, billing_days: dict[str, Any] | None = None) -> dict[str, float]:
        """Usage by leg name, FOR THE CURRENT PERIOD. Missing means zero.

        Rolls on the way out, which is what makes a router that was switched
        off across the boundary come back with a fresh month rather than last
        month's total. This is the only reset that ever fires in practice.

        A corrupt file is NOT fatal. Refusing to start because a counter file
        went bad would take the bond down over accounting, which is the wrong
        trade on a device someone is relying on for connectivity.
        """
        self.periods = {}
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("usage.json unreadable (%s); starting from zero", exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for name, v in (raw.get("legs") or {}).items():
            try:
                gb = float(v.get("usage_gb", 0.0) if isinstance(v, dict) else v)
            except (TypeError, ValueError):
                continue
            key = str(name)
            rec = LegUsage(usage_gb=gb)
            if isinstance(v, dict):
                rec.period_start = str(v.get("period_start") or "")
                rec.previous_period_start = str(v.get("previous_period_start") or "")
                try:
                    rec.previous_usage_gb = float(v.get("previous_usage_gb", 0.0) or 0.0)
                except (TypeError, ValueError):
                    rec.previous_usage_gb = 0.0
            self.periods[key] = rec
            out[key] = gb
        return self.roll(out, billing_days=billing_days)

    def roll(
        self,
        usage: dict[str, float],
        billing_days: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        """Bring counters into the CURRENT period. Returns what they should be.

        THE SINGLE BOUNDARY. Startup calls it through `load`, and the control
        loop calls it every tick, so a process that happens to be running
        across midnight on the cycle day rolls too. Both are the same function
        because two implementations of a month boundary is two chances to get
        one of them wrong, and the one that fires rarely is the one nobody
        notices is broken.

        Values come from the CALLER's live counters, never from the file. That
        distinction matters: re-reading the file mid-run is exactly how a
        30 MB transfer once got recorded as 100 KB, and the only value this
        can hand back that the caller did not already hold is a zero at a real
        boundary.

        FORWARD ONLY. A clock that reads earlier than the recorded period start
        is a wrong clock, not a new period, and rolling on it would discard a
        real month.
        """
        today = self._today()
        if today < CLOCK_SANITY_FLOOR:
            if not self._clock_warned:
                # Once. This is called twice a second.
                log.warning(
                    "wall clock reads %s, before this agent existed - usage periods "
                    "are frozen until it is corrected (NTP has probably not answered yet)",
                    today.isoformat(),
                )
                self._clock_warned = True
            return dict(usage)
        self._clock_warned = False

        days = billing_days or {}
        out = dict(usage)
        for name, gb in usage.items():
            rec = self.periods.get(name)
            if rec is None:
                rec = LegUsage()
                self.periods[name] = rec
            rec.usage_gb = gb
            start = period_start(today, days.get(name, DEFAULT_BILLING_DAY))
            stamp = start.isoformat()
            stored = _parse_date(rec.period_start)
            if stored is None:
                # ADOPTED, NOT ZEROED. These bytes were really measured; what
                # is missing is which period they belong to, and guessing
                # "this one" keeps a real number while guessing "none" throws
                # away the accounting the caps are about to be judged on.
                # Loud, because on the live router this is the one-time
                # migration of a counter that has been running since it was
                # first written.
                if rec.period_start:
                    log.warning("leg %s: unreadable period_start %r; adopting %s",
                                name, rec.period_start, stamp)
                else:
                    log.info("leg %s: %.3f GB has no period recorded; adopting it into "
                             "the period starting %s rather than discarding it",
                             name, gb, stamp)
                rec.period_start = stamp
                continue
            if start <= stored:
                continue
            rec.previous_usage_gb = rec.usage_gb
            rec.previous_period_start = rec.period_start
            rec.usage_gb = 0.0
            rec.period_start = stamp
            out[name] = 0.0
            log.info("leg %s: usage period rolled %s -> %s; %.3f GB kept as the previous period",
                     name, rec.previous_period_start, stamp, rec.previous_usage_gb)
        return out

    def mark_dirty(self) -> None:
        self._dirty = True

    def maybe_flush(self, usage: dict[str, float], *, force: bool = False) -> bool:
        """Write if enough time has passed, or if forced (shutdown).

        Returns whether it wrote, so a caller can log or count it.
        """
        now = self._clock()
        if not force:
            if not self._dirty:
                return False
            if self._last_flush is not None and (now - self._last_flush) < FLUSH_INTERVAL_S:
                return False
        legs: dict[str, dict[str, Any]] = {}
        for k, v in usage.items():
            entry: dict[str, Any] = {"usage_gb": round(v, 4)}
            rec = self.periods.get(k)
            if rec is not None and rec.period_start:
                # The anchor the next start compares against. Omitted when
                # unknown rather than invented, so a caller that never rolled
                # writes a file that says so instead of one asserting a
                # boundary nothing checked.
                entry["period_start"] = rec.period_start
                if rec.previous_period_start:
                    # LAST PERIOD'S TOTAL, kept because "why was this leg
                    # demoted last month" has no answer once the counter is
                    # zeroed, and a cap alert nobody can explain afterwards
                    # gets ignored.
                    entry["previous_usage_gb"] = round(rec.previous_usage_gb, 4)
                    entry["previous_period_start"] = rec.previous_period_start
            legs[k] = entry
        payload = json.dumps({"version": 2, "legs": legs}, indent=2)
        try:
            _atomic_write(self.path, payload)
        except OSError as exc:
            # Losing a counter write must not kill the agent.
            log.warning("could not write usage.json: %s", exc)
            return False
        self._last_flush = now
        self._dirty = False
        return True


class LegStore:
    """Operator-entered metadata and overrides, per leg.

    EVERYTHING HERE IS OPTIONAL AND EVERYTHING HERE WINS. A value present in
    this file overrides the same field in zippie.toml, because it is the more
    recent human decision - "the provider says the cap is 15 GB, not the 5 you
    configured" is exactly the case this exists for.

    Absent keys change nothing. That is what makes the file safe to hand-edit:
    you write only what you mean to change.
    """

    #: Fields an operator may override. Deliberately a whitelist - an arbitrary
    #: key would let a typo silently shadow config, and there is no schema
    #: check on a hand-edited file.
    OVERRIDABLE = {"tier", "priority", "weight", "max_kbps", "monthly_cap_gb",
                   "cost_class", "label", "enabled"}
    #: The carrier's cycle day. NOT descriptive and not overridable: it names
    #: no field of PathConfig, so nothing copies it onto a leg's config, but it
    #: decides which billing period usage is counted against and therefore when
    #: a cap - and the demotion behind it - resets. It was already accepted
    #: here and already sent by the phone app, and until now nothing read it.
    CYCLE = frozenset({"billing_day"})
    #: Descriptive only. Never affects routing; exists so the dashboard can be
    #: accurate about what a leg actually IS.
    DESCRIPTIVE = {"carrier", "plan_name", "plan_type", "notes"}

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / "legs.json"

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Loud, because unlike usage this is unrecoverable human input and
            # silently ignoring it would apply the wrong caps.
            log.error("legs.json unreadable (%s); OVERRIDES ARE NOT APPLIED", exc)
            return {}
        legs = raw.get("legs") if isinstance(raw, dict) else None
        return legs if isinstance(legs, dict) else {}

    def save(self, legs: dict[str, dict[str, Any]]) -> None:
        _atomic_write(self.path, json.dumps({"version": 1, "legs": legs}, indent=2))

    def update(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Merge fields into one leg's entry and persist. Returns the entry.

        Read-modify-write rather than replace, so editing the cap does not drop
        the carrier someone typed last week.
        """
        allowed = self.OVERRIDABLE | self.CYCLE | self.DESCRIPTIVE
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown field(s): {sorted(unknown)}")
        legs = self.load()
        entry = dict(legs.get(name) or {})
        for k, v in fields.items():
            if v is None:
                entry.pop(k, None)   # explicit null clears an override
            else:
                entry[k] = v
        legs[name] = entry
        self.save(legs)
        return entry


class HomeAddressStore:
    """The last home address that resolved, kept across reboots.

    WHY THIS IS ON DISK AND NOT JUST IN MEMORY. Resolving the home endpoint
    needs DNS; DNS needs internet; internet needs a carrying leg; and a leg only
    earns weight once the transport's keepalives are ANSWERED by the home end -
    which cannot happen until something knows where to send them. A process that
    starts with an empty cache and no uplink can never break that circle.

    That is not theoretical. Measured on suzu 2026-08-16 with a phone as the only
    uplink: the router booted, the phone announced itself twelve times over four
    minutes, the transport link came up - and `logread | grep "home endpoint"`
    was EMPTY for the whole boot, because that line only prints on a successful
    resolve. Every leg sat at weight 0 for three and a half minutes and the
    router never reached the internet. Every previous success had a warm cache
    from before the restart; a cold boot is the one case that never does.

    A STALE ADDRESS IS WORTH DIALING. The endpoint is dynamic DNS, so this file
    can go wrong - and being wrong costs nothing, because a leg that dials a
    dead address stays down, which is exactly where it already was. Being right,
    which is the overwhelmingly common case, is the difference between a bond
    that forms unattended and one that needs a human. `_resolve_home_ip` already
    argued this policy for its in-memory cache; this only makes it survive the
    power cycle, which is the case where it matters.

    PLAIN TEXT, NOT JSON. One address, read by a human over ssh with cat on a
    device that may be in a car. There is nothing to schema.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / "home-ip"

    def load(self) -> str | None:
        """The persisted address, or None if there isn't a usable one.

        SYNTAX IS CHECKED HERE because this value is handed to the datapath as a
        send target. A truncated write, a hand-edit, or a hostname someone put
        here by mistake must read as "no address" rather than propagate. Whether
        the address is *plausible* (see BondAgent._persist_home_ip on private
        addresses) is policy and lives with the rest of the home-endpoint policy.
        """
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("home-ip unreadable (%s); cold start will need DNS", exc)
            return None
        addr = raw.strip()
        try:
            socket.inet_aton(addr)
        except OSError:
            if addr:
                log.warning("home-ip holds %r, which is not an address - ignoring", addr)
            return None
        # inet_aton accepts "10", "1.2.3" and other short forms as addresses.
        # The datapath needs a dotted quad, so require one explicitly.
        if addr.count(".") != 3:
            log.warning("home-ip holds %r, which is not a dotted quad - ignoring", addr)
            return None
        return addr

    def save(self, addr: str) -> bool:
        """Persist the address. Returns True only if the file actually changed.

        NO-OPS WHEN UNCHANGED, and that guarantee lives here rather than in the
        caller so no caller can lose it. This is flash on a router that boots
        from it, and the address changes at the pace of a dynamic-DNS update -
        rewriting it on a timer would be wear for nothing.
        """
        if self.load() == addr:
            return False
        _atomic_write(self.path, addr + "\n")
        return True
