"""The OpenRoIS adapter process for the Pollen Robotics Reachy Mini.

The adapter builds an ``Engine`` with the components of
``openrois.components.reachy_mini`` for one robot, the real Reachy Mini or its simulator,
and serves it to an OpenRoIS gateway with ``WsClient``. Two adapters with different
engine ids put both robots behind one gateway. ``openrois-adapter-reachy-mini --help``
lists the settings.
"""

from openrois_adapter_reachy_mini.cli import Settings, build_engine, main

__all__ = ["Settings", "build_engine", "main"]
