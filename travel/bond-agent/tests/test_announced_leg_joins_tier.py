"""An announcing phone must JOIN the bond, not take it over.

WHAT HAPPENED. On 2026-08-08 the bond carried on ONE leg for about an hour -
the phone's cellular - while the router's ethernet and its repeated wifi sat
idle. Nothing alerted, because from the datapath's point of view it was
healthy: one leg, carrying, no loss.

    ethernet      tier=2  eth0     degraded  in_bond=False
    hotspot       tier=3  apclix0  degraded  in_bond=False
    iphone-8fe5   tier=1  br-lan   up        in_bond=True

`packet_mode_legs` admits only the minimum tier present. The operator had
demoted two legs to tiers 2 and 3, which is a reasonable thing to do; the phone
then announced at tier 1, which is the DEFAULT, and evicted both.

The trap needs two innocuous things: an operator demoting a leg once, and a
phone arriving later. Neither looks wrong on its own, and `DynamicLeg.tier`
defaulting to 1 is invisible in an announce that never mentions tier.

THE RULE THIS PINS. A leg that does not ask for a tier joins the tier that is
already carrying. It does not get to define it. An explicit tier is still
honoured, because that is the operator or the app saying something deliberate -
the bug was never that tiers exist, it was that silence meant "highest
priority" instead of "whatever everyone else is on".
"""
from __future__ import annotations

from zippie import agent as agent_mod
from zippie.dynamic import DynamicLeg, DynamicLegs
from zippie.models import PathConfig, PathMatch, PathRuntime, PathState


def _leg(name, tier, *, interface="eth0", state=PathState.UP):
    cfg = PathConfig(name=name, match=PathMatch(type="interface", interface=interface),
                     tier=tier)
    p = PathRuntime(name=name, config=cfg)
    p.interface = interface
    p.state = state
    return p


class _NoOverrides:
    """A LegStore with nothing in it.

    reconcile_dynamic_legs consults legs.json so an operator's label is not
    clobbered by the one a phone announces (#80). Stubbed rather than defended
    against with getattr in the agent: a missing store in production would mean
    overrides silently stop applying, and a test double is the honest way to say
    "this agent has no overrides" without teaching the real code to shrug.
    """

    def load(self):
        return {}


def _agent(paths):
    a = object.__new__(agent_mod.BondAgent)
    a.paths = list(paths)
    a._dynamic_paths = {}
    a.dynamic = DynamicLegs()
    a._transport_ids = {}
    a._leg_store = _NoOverrides()
    return a


def _announce(agent, name="iphone-abcd", tier=None):
    agent.dynamic.announce(name=name, host="10.20.0.151", port=51999,
                           label="iPhone", tier=tier)
    agent_mod.BondAgent.reconcile_dynamic_legs(agent)
    return next(p for p in agent.paths if p.name == name)


# ------------------------------------------------------- the reported bug
def test_a_phone_joins_the_tier_that_is_already_carrying():
    """THE REGRESSION. Two demoted legs and a silent phone: the phone must land
    on tier 2 with them, not on tier 1 above them."""
    agent = _agent([_leg("ethernet", 2), _leg("hotspot", 3, interface="apclix0")])
    phone = _announce(agent)
    assert phone.config.tier == 2, "the phone claimed a tier above the carrying legs"


def test_the_existing_legs_are_not_evicted():
    """The observable consequence, asserted through the real gate rather than
    by reading the tier back."""
    from zippie import policy
    agent = _agent([_leg("ethernet", 2), _leg("hotspot", 3, interface="apclix0")])
    phone = _announce(agent)
    # A freshly announced leg is DOWN until it has been probed. Put it in the
    # state where eviction would actually happen, which is once it is up.
    phone.interface = "br-lan"
    phone.state = PathState.UP
    admitted = {p.name for p in policy.packet_mode_legs(agent.paths)}
    assert "ethernet" in admitted, "the router's own uplink was evicted by a phone"
    assert "iphone-abcd" in admitted, "the phone did not join"


def test_a_phone_joins_tier_1_when_that_is_what_is_carrying():
    """The ordinary case must be unchanged - this is not a demotion of phones."""
    agent = _agent([_leg("ethernet", 1), _leg("hotspot", 1, interface="apclix0")])
    assert _announce(agent).config.tier == 1


# ------------------------------------------------- explicit intent is honoured
def test_an_explicitly_announced_tier_is_respected():
    """Silence means "join". Saying a number means the caller meant it."""
    agent = _agent([_leg("ethernet", 2)])
    assert _announce(agent, tier=5).config.tier == 5


def test_an_explicit_tier_1_is_still_allowed_to_take_over():
    """Deliberately asking to be primary is a legitimate thing to ask for, and
    must stay distinguishable from not asking at all."""
    agent = _agent([_leg("ethernet", 2)])
    assert _announce(agent, tier=1).config.tier == 1


# ---------------------------------------------------------------- edge cases
def test_the_first_leg_of_all_defaults_to_tier_1():
    """With nothing to join, there is no active tier to copy."""
    agent = _agent([])
    assert _announce(agent).config.tier == 1


def test_legs_that_are_down_do_not_set_the_tier_to_join():
    """A dead tier-1 leg must not drag a phone up to a tier nothing carries -
    that would strand the phone alongside a corpse while tier 2 works."""
    agent = _agent([
        _leg("dead", 1, interface=None, state=PathState.DOWN),
        _leg("hotspot", 2, interface="apclix0"),
    ])
    assert _announce(agent).config.tier == 2


def test_legs_with_no_interface_are_ignored_when_choosing():
    """A configured leg whose hardware is absent is not carrying anything, so
    it cannot define the tier to join."""
    agent = _agent([
        _leg("ghost", 1, interface=None),
        _leg("ethernet", 3, interface="eth0"),
    ])
    assert _announce(agent).config.tier == 3


def test_two_phones_land_on_the_same_tier():
    agent = _agent([_leg("ethernet", 2)])
    a = _announce(agent, name="iphone-aaaa")
    b = _announce(agent, name="pixel-bbbb")
    assert a.config.tier == b.config.tier == 2


# ------------------------------------------------------- the store's contract
def test_dynamic_leg_tier_is_optional_at_the_store_level():
    """`None` has to survive as far as reconcile, or the default is re-applied
    before anything can resolve it - which is the bug in miniature."""
    legs = DynamicLegs()
    leg = legs.announce(name="iphone-abcd", host="10.20.0.151", port=51999, tier=None)
    assert leg.tier is None


def test_an_out_of_range_explicit_tier_is_still_rejected():
    """Relaxing the default must not relax validation - this arrives over the
    network."""
    legs = DynamicLegs()
    for bad in (0, 100, -1):
        try:
            legs.announce(name="iphone-abcd", host="10.20.0.151", port=51999, tier=bad)
        except ValueError:
            continue
        raise AssertionError(f"tier {bad} was accepted")
