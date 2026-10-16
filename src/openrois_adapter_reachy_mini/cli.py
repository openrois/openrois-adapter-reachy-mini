"""The ``openrois-adapter-reachy-mini`` command: one Reachy Mini behind an OpenRoIS gateway.

The adapter builds an ``Engine`` with the four components of one robot, shares one
connection to the robot's daemon between them, and serves the engine to a gateway with
``WsClient``, reconnecting whenever the connection drops. Two adapters with different
engine ids put two robots behind one gateway::

    # The simulator on this machine
    openrois-adapter-reachy-mini --config adapter.sim.yaml

    # The Reachy Mini Wireless on the network
    openrois-adapter-reachy-mini --config adapter.yaml --robot-address reachy-mini.local

The components are ``<engine id>/system_information``, ``<engine id>/reaction``,
``<engine id>/speech_synthesis`` and ``<engine id>/person_detection``. Without Piper
voices, the robot speaks with a voice-like tone. The adapter reaches the daemon at
``robot_address`` on the network, or the daemon on this machine with ``simulator:
true``. While the daemon does not answer, the four components report ERROR, and the
adapter connects again once the daemon is back.

The settings come from the engine's adapter loader: the defaults of
``ReachyMiniConfig``, then ``adapter.yaml`` (named by ``--config`` or
``OPENROIS_ADAPTER_CONFIG``), then the ``OPENROIS_ADAPTER_*`` environment variables,
then the flags. The environment and the flags set only the gateway URL, the engine id,
the robot's address and the log level. Everything else comes from the file.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from openrois.engine import Engine, WsClient
from openrois.engine.config import AdapterConfig, ConfigError, load_adapter_config
from pydantic import BaseModel, ConfigDict, Field, field_validator

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

DESCRIPTION = "Serve one Reachy Mini, real or simulated, to an OpenRoIS gateway."

#: Seconds a speech waits after the end of its audio, for the speaker to finish.
SPEECH_TAIL = 0.3


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectionSettings(_Section):
    """How the adapter reaches the robot's daemon."""

    port: int = Field(default=8000, ge=1, le=65535, description="The port of the daemon.")
    timeout: float = Field(default=5.0, gt=0, description="Seconds to wait for the daemon.")
    retry: float = Field(default=3.0, gt=0, description="Seconds between two checks of the daemon.")


class ReactionSettings(_Section):
    """The Reaction component."""

    emotions: bool = Field(
        default=False,
        description="Add Pollen's recorded emotions to the reactions, from the Hugging Face Hub.",
    )
    blend: float = Field(
        default=0.3, ge=0, description="Seconds a reaction eases from the current pose."
    )


class SpeechSettings(_Section):
    """The SpeechSynthesis component."""

    piper: str = Field(default="piper", description="The command that runs Piper.")
    piper_voices: Path | None = Field(
        default=None,
        description="The folder of the Piper voices. Without it the robot speaks with a tone.",
    )
    wobble: bool = Field(default=True, description="Whether the head moves with the voice.")

    @field_validator("piper_voices")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None


class PersonDetectionSettings(_Section):
    """The PersonDetection component."""

    interval: float = Field(default=0.2, gt=0, description="Seconds between two frames.")
    hold: float = Field(default=0.5, ge=0, description="Seconds a new count holds first.")
    threshold: float = Field(
        default=0.6, gt=0, le=1, description="The detection score from which a face counts."
    )


class ReachyMiniConfig(AdapterConfig):
    """The settings of one Reachy Mini adapter, one section per component.

    ``robot_address`` is the host of the robot's daemon on the network, for example the
    Reachy Mini Wireless at ``reachy-mini.local``. ``simulator: true`` reaches the daemon
    on this machine instead, such as Pollen's simulator. ``time_scale`` multiplies every
    duration the adapter times itself: the reactions and their blends, the checks of the
    daemon, the frames of PersonDetection and the hold of a new count, and the wait after
    a speech. What the robot or the simulator takes for a motion stays as it is.
    """

    engine_id: str = "reachy_mini"
    robot_address: str | None = "reachy-mini.local"
    simulator: bool = Field(
        default=False, description="Reach the daemon on this machine, such as the simulator."
    )
    time_scale: float = Field(
        default=1.0, gt=0, description="Factor on every duration the adapter times itself."
    )
    connection: ConnectionSettings = ConnectionSettings()
    reaction: ReactionSettings = ReactionSettings()
    speech_synthesis: SpeechSettings = SpeechSettings()
    person_detection: PersonDetectionSettings = PersonDetectionSettings()

    def connection_mode(self) -> ConnectionMode:
        """``localhost_only`` for the simulator, ``network`` for the robot at its address."""
        return "localhost_only" if self.simulator else "network"

    def synthesizer(self) -> Synthesizer:
        """The speech engine: Piper with the voices of ``piper_voices``, or the tone.

        Raises:
            ValueError: The folder holds no Piper voice.
        """
        speech = self.speech_synthesis
        if speech.piper_voices is None:
            return ToneSynthesizer()
        return PiperSynthesizer(speech.piper_voices, command=shlex.split(speech.piper))


def build_engine(config: ReachyMiniConfig) -> Engine:
    """The engine of one robot, with its four components on one shared connection."""
    connection = ReachyMiniConnection(
        host=config.robot_address or "reachy-mini.local",
        connection_mode=config.connection_mode(),
        port=config.connection.port,
        timeout=config.connection.timeout,
        retry=config.connection.retry * config.time_scale,
    )
    reaction = config.reaction
    detection = config.person_detection
    engine = Engine(config.engine_id)
    engine.add_component("system_information", ReachyMiniSystemInformation(connection))
    engine.add_component(
        "reaction",
        ReachyMiniReaction(
            connection,
            library=EMOTIONS_LIBRARY if reaction.emotions else None,
            blend=reaction.blend,
            time_scale=config.time_scale,
        ),
    )
    engine.add_component(
        "speech_synthesis",
        ReachyMiniSpeechSynthesis(
            connection,
            config.synthesizer(),
            wobble=config.speech_synthesis.wobble,
            tail=SPEECH_TAIL * config.time_scale,
        ),
    )
    engine.add_component(
        "person_detection",
        ReachyMiniPersonDetection(
            connection,
            interval=detection.interval * config.time_scale,
            hold=detection.hold * config.time_scale,
            threshold=detection.threshold,
        ),
    )
    return engine


def load_config(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> ReachyMiniConfig:
    """The settings, from the defaults, adapter.yaml, the environment and the flags.

    Raises:
        ConfigError: The file cannot be read, a setting is invalid, or the Piper voices
            folder holds no voice.
        SystemExit: For an unknown flag or ``--help``.
    """
    config = load_adapter_config(
        ReachyMiniConfig,
        argv,
        os.environ if environ is None else environ,
        description=DESCRIPTION,
    )
    try:
        config.synthesizer()
    except ValueError as exc:
        raise ConfigError(
            f"Invalid configuration:\n  speech_synthesis.piper_voices: {exc}"
        ) from exc
    return config


def main(argv: Sequence[str] | None = None) -> None:
    """Load the settings and serve the gateway until Ctrl+C."""
    try:
        config = load_config(argv)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if config.speech_synthesis.piper_voices is None:
        logger.info("No Piper voices, so the robot speaks with a tone. See speech_synthesis")
    WsClient(build_engine(config), config.gateway_url).run()
