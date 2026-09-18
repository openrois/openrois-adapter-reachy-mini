"""The Reachy Mini adapter: an Engine with the platform's components, behind a WsClient.

Usage:
    openrois-adapter-reachy-mini --config openrois-profile.yaml            # the robot
    openrois-adapter-reachy-mini --config openrois-profile.yaml --simulate # no robot needed
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from typing import Any

from openrois_components_core import meta_from_decorators
from openrois_core import Engine, WsClient, component_config, read_profile

from openrois_adapter_reachy_mini.backend import Backend, ReachyMiniBackend, SimulatedBackend
from openrois_adapter_reachy_mini.components import COMPONENT_CLASSES

logger = logging.getLogger("openrois.reachy_mini")


def backend_from_profile(profile: dict[str, Any], simulate: bool = False) -> Backend:
    """The backend the profile asks for.

    ``robot.simulate: true`` in the profile, or ``simulate=True``, picks the simulator.
    """
    robot = dict(profile.get("robot", {}))
    if simulate or robot.get("simulate", False):
        return SimulatedBackend(time_scale=float(robot.get("time_scale", 1.0)))
    options = {k: v for k, v in robot.items() if k not in ("simulate", "time_scale")}
    return ReachyMiniBackend(**options)


def build_engine(profile: dict[str, Any], backend: Backend) -> Engine:
    """An engine with every platform component registered from the profile."""
    engine = Engine(
        engine_id=profile["engine"]["id"],
        platform=profile["engine"].get("platform", "reachy-mini"),
    )
    for cls in COMPONENT_CLASSES:
        meta = meta_from_decorators(cls)
        handler = cls(component_config(profile, meta.ref), backend)
        engine.register_component(meta.ref, handler, meta)
    return engine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openrois-adapter-reachy-mini", description="OpenRoIS adapter for the Reachy Mini",
    )
    parser.add_argument("--config", default="openrois-profile.yaml", help="adapter profile YAML")
    parser.add_argument("--simulate", action="store_true", help="run without the robot")
    parser.add_argument("--gateway-url", help="override engine.gateway_url of the profile")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    profile = read_profile(args.config)
    backend = backend_from_profile(profile, simulate=args.simulate)
    logger.info("Backend: %s", backend.name)
    engine = build_engine(profile, backend)
    url = args.gateway_url or profile["engine"]["gateway_url"]
    WsClient(engine, url, token=profile["engine"].get("token")).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
