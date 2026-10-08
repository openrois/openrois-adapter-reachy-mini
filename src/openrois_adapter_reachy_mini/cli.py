"""The ``openrois-adapter-reachy-mini`` command: one Reachy Mini behind an OpenRoIS gateway.

The adapter builds an ``Engine`` with the four components of one robot, shares one
connection to the robot's daemon between them, and serves the engine to a gateway with
``WsClient``, reconnecting whenever the connection drops. Two adapters with different
engine ids put two robots behind one gateway::

    # The simulator on this machine
    openrois-adapter-reachy-mini --engine-id reachy_sim --sim

    # The Reachy Mini Wireless on the network
    openrois-adapter-reachy-mini --engine-id reachy_real --host reachy-mini.local

The components are ``<engine id>/system_information``, ``<engine id>/reaction``,
``<engine id>/speech_synthesis`` and ``<engine id>/person_detection``. Without Piper
voices, the robot speaks with a voice-like tone.

Every flag falls back to an environment variable: ``OPENROIS_GATEWAY_URL``,
``OPENROIS_ENGINE_ID``, ``OPENROIS_REACHY_HOST``, ``OPENROIS_REACHY_CONNECTION_MODE``,
``OPENROIS_REACHY_EMOTIONS`` (``1`` to load the emotions), ``OPENROIS_PIPER`` and
``OPENROIS_PIPER_VOICES``.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from openrois.engine import Engine, WsClient

from openrois.components.reachy_mini import (
    EMOTIONS_LIBRARY,
    ConnectionMode,
    PiperSynthesizer,
    ReachyMiniConnection,
    ReachyMiniPersonDetection,
    ReachyMiniReaction,
    ReachyMiniSpeechSynthesis,
    ReachyMiniSystemInformation,
    Synthesizer,
    ToneSynthesizer,
)

logger = logging.getLogger("openrois_adapter_reachy_mini")

CONNECTION_MODES: tuple[ConnectionMode, ...] = ("auto", "localhost_only", "network")


@dataclass(frozen=True)
class Settings:
    """What the adapter serves, and where.

    Attributes:
        gateway_url: The gateway to serve the engine to.
        engine_id: The engine id, the first part of every ref, for example ``reachy_sim``.
        host: The host of the robot's daemon when it runs on another machine.
        connection_mode: ``localhost_only`` for the simulator on this machine,
            ``network`` for a robot on the network, ``auto`` to try this machine first.
        emotions: Whether Pollen's recorded emotions join the built-in reactions.
        piper: The command that runs Piper.
        piper_voices: The folder of the Piper voices. Without it the robot speaks with a
            voice-like tone.
    """

    gateway_url: str = "ws://127.0.0.1:8765"
    engine_id: str = "reachy_mini"
    host: str = "reachy-mini.local"
    connection_mode: ConnectionMode = "auto"
    emotions: bool = False
    piper: str = "piper"
    piper_voices: Path | None = None

    def synthesizer(self) -> Synthesizer:
        """The speech engine: Piper with the voices of ``piper_voices``, or the tone."""
        if self.piper_voices is None:
            return ToneSynthesizer()
        return PiperSynthesizer(self.piper_voices, command=shlex.split(self.piper))


def build_engine(settings: Settings) -> Engine:
    """The engine of one robot, with its four components on one shared connection."""
    connection = ReachyMiniConnection(host=settings.host, connection_mode=settings.connection_mode)
    library = EMOTIONS_LIBRARY if settings.emotions else None
    engine = Engine(settings.engine_id)
    engine.add_component("system_information", ReachyMiniSystemInformation(connection))
    engine.add_component("reaction", ReachyMiniReaction(connection, library=library))
    engine.add_component(
        "speech_synthesis", ReachyMiniSpeechSynthesis(connection, settings.synthesizer())
    )
    engine.add_component("person_detection", ReachyMiniPersonDetection(connection))
    return engine


def parse_args(argv: Sequence[str] | None = None) -> tuple[Settings, str]:
    """The settings and the log level, from the flags and the environment."""
    env = os.environ
    defaults = Settings()
    parser = argparse.ArgumentParser(
        prog="openrois-adapter-reachy-mini",
        description="Serve one Reachy Mini, real or simulated, to an OpenRoIS gateway.",
    )
    parser.add_argument(
        "--gateway-url",
        default=env.get("OPENROIS_GATEWAY_URL", defaults.gateway_url),
        help="the gateway to connect to (env OPENROIS_GATEWAY_URL)",
    )
    parser.add_argument(
        "--engine-id",
        default=env.get("OPENROIS_ENGINE_ID", defaults.engine_id),
        help="the engine id, the first part of every ref (env OPENROIS_ENGINE_ID)",
    )
    parser.add_argument(
        "--host",
        default=env.get("OPENROIS_REACHY_HOST", defaults.host),
        help="the host of a robot on the network (env OPENROIS_REACHY_HOST)",
    )
    parser.add_argument(
        "--connection-mode",
        choices=CONNECTION_MODES,
        default=env.get("OPENROIS_REACHY_CONNECTION_MODE", defaults.connection_mode),
        help="how to reach the daemon (env OPENROIS_REACHY_CONNECTION_MODE)",
    )
    parser.add_argument(
        "--sim",
        dest="connection_mode",
        action="store_const",
        const="localhost_only",
        help="the simulator on this machine, the same as --connection-mode localhost_only",
    )
    parser.add_argument(
        "--emotions",
        action="store_true",
        default=env.get("OPENROIS_REACHY_EMOTIONS", "") in ("1", "true", "yes"),
        help="add Pollen's recorded emotions to the reactions (env OPENROIS_REACHY_EMOTIONS)",
    )
    parser.add_argument(
        "--piper",
        default=env.get("OPENROIS_PIPER", defaults.piper),
        help="the command that runs Piper (env OPENROIS_PIPER)",
    )
    parser.add_argument(
        "--piper-voices",
        type=Path,
        default=env.get("OPENROIS_PIPER_VOICES") or None,
        help="the folder of the Piper voices, for real speech (env OPENROIS_PIPER_VOICES)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="how much to log",
    )
    args = parser.parse_args(argv)
    if args.connection_mode not in CONNECTION_MODES:
        parser.error(f"the connection mode is one of {', '.join(CONNECTION_MODES)}")
    voices = Path(args.piper_voices).expanduser() if args.piper_voices else None
    settings = Settings(
        gateway_url=args.gateway_url,
        engine_id=args.engine_id,
        host=args.host,
        connection_mode=args.connection_mode,
        emotions=args.emotions,
        piper=args.piper,
        piper_voices=voices,
    )
    try:
        settings.synthesizer()
    except ValueError as exc:
        parser.error(str(exc))
    return settings, args.log_level


def main(argv: Sequence[str] | None = None) -> None:
    """Parse the flags and serve the gateway until Ctrl+C."""
    settings, level = parse_args(argv)
    logging.basicConfig(
        level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if settings.piper_voices is None:
        logger.info("No Piper voices, so the robot speaks with a tone. See --piper-voices")
    WsClient(build_engine(settings), settings.gateway_url).run()
