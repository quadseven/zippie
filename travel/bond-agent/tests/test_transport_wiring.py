"""Wiring the per-packet transport into the agent.

The risk here is not the transport itself but the seam: an agent that starts a
forwarder it cannot stop, or that rebuilds every socket each second, or that
quietly turns the new datapath on for an existing config.
"""

from __future__ import annotations

from zippie.models import Datapath, PolicyConfig


class TestDefaultIsUnchanged:
    def test_existing_configs_keep_the_kernel_datapath(self):
        """PACKET moves every byte through new code. Turning it on must be a
        deliberate act, never a side effect of upgrading."""
        assert PolicyConfig().datapath is Datapath.ROUTE

    def test_the_packet_datapath_is_opt_in_by_name(self):
        assert PolicyConfig(datapath=Datapath.PACKET).datapath is Datapath.PACKET
        assert Datapath("packet") is Datapath.PACKET
        assert Datapath("route") is Datapath.ROUTE


class TestTransportLifecycle:
    def _agent(self, datapath):
        from zippie.agent import BondAgent
        from zippie.models import AgentConfig, HomeConfig

        cfg = AgentConfig(
            home=HomeConfig(endpoint="home.example", server_public_key="k" * 44),
            policy=PolicyConfig(datapath=datapath, transport_port=51899),
            paths=[],
        )
        return BondAgent(cfg)

    def test_route_mode_starts_no_transport(self):
        a = self._agent(Datapath.ROUTE)
        a.start_transport()
        assert a._transport is None, "route mode must not open the forwarder"

    def test_sync_is_a_no_op_without_a_transport(self):
        """sync_transport runs every control loop; in route mode it must cost
        nothing and never raise."""
        a = self._agent(Datapath.ROUTE)
        a.sync_transport()          # must not raise
        a.stop_transport()          # must not raise

    def test_stop_is_idempotent(self):
        a = self._agent(Datapath.ROUTE)
        a.stop_transport()
        a.stop_transport()
