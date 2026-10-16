"""The OpenRoIS adapter process for the Pollen Robotics Reachy Mini.

The adapter builds an ``Engine`` with the components of
``openrois.components.reachy_mini`` for one robot, the real Reachy Mini or its simulator,
and serves it to an OpenRoIS gateway with ``WsClient``. Two adapters with different
engine ids put both robots behind one gateway. ``ReachyMiniConfig`` holds the settings,
which ``adapter.yaml`` gives.
"""

from openrois_adapter_reachy_mini.cli import ReachyMiniConfig, build_engine, load_config, main

__all__ = ["ReachyMiniConfig", "build_engine", "load_config", "main"]
