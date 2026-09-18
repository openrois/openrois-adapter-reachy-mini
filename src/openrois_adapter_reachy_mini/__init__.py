"""OpenRoIS reference platform on the Pollen Robotics Reachy Mini.

An adapter built on the Python adapter SDK, a simulated backend so everything
runs without the robot, and a one-command bring-up of gateway plus adapter.

Public API:
    build_engine: An Engine with the platform's components registered.
    SimulatedBackend: The robot model used without hardware.
    ReachyMiniBackend: The backend that drives the robot through its SDK.
"""

from __future__ import annotations

from openrois_adapter_reachy_mini.adapter import backend_from_profile, build_engine
from openrois_adapter_reachy_mini.backend import (
    Backend,
    HeadPose,
    ReachyMiniBackend,
    SimulatedBackend,
)

__all__ = [
    "Backend",
    "HeadPose",
    "ReachyMiniBackend",
    "SimulatedBackend",
    "backend_from_profile",
    "build_engine",
]
