"""The OpenRoIS adapter process for the Pollen Robotics Reachy Mini.

The adapter (planned) builds an ``Engine`` with the components of
``openrois.components.reachy_mini`` for one robot, the real Reachy Mini or its simulator,
and serves it to an OpenRoIS gateway with ``WsClient``. Two adapters with different
engine ids put both robots behind one gateway.
"""

__all__: list[str] = []
