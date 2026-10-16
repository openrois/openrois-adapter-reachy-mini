"""OpenRoIS components for the Pollen Robotics Reachy Mini.

Canonical RoIS components that drive a Reachy Mini, the real robot or its simulator,
through the Reachy Mini SDK. Each one is a ``Component`` of ``openrois-components-core``
that declares the profile constant of its type, so an ``Engine`` serves it as that type:

- ``ReachyMiniSystemInformation``: the status of the robot and the yaw of its body.
- ``ReachyMiniReaction``: expressions of the head and the antennas, and the moves of a
  recorded library such as Pollen's emotions.
- ``ReachyMiniSpeechSynthesis``: a text spoken on the speaker, with the head moving to
  the voice.
- ``ReachyMiniPersonDetection``: the number of faces the camera sees.

The components of one robot share one ``ReachyMiniConnection``::

    connection = ReachyMiniConnection(connection_mode="localhost_only")
    engine = Engine("reachy_sim")
    engine.add_component("reaction", ReachyMiniReaction(connection))
    engine.add_component("speech_synthesis", ReachyMiniSpeechSynthesis(connection))

The components import the SDK when they connect, so this package imports without it.
"""

from openrois.components.reachy_mini.connection import ConnectionMode, ReachyMiniConnection
from openrois.components.reachy_mini.moves import EXPRESSIONS, Keyframe
from openrois.components.reachy_mini.person_detection import ReachyMiniPersonDetection
from openrois.components.reachy_mini.reaction import (
    DEFAULT_REACTION,
    EMOTIONS_LIBRARY,
    ReachyMiniReaction,
)
from openrois.components.reachy_mini.speech_synthesis import ReachyMiniSpeechSynthesis
from openrois.components.reachy_mini.synthesizers import (
    PiperSynthesizer,
    Synthesizer,
    ToneSynthesizer,
)
from openrois.components.reachy_mini.system_information import ReachyMiniSystemInformation

__all__ = [
    "DEFAULT_REACTION",
    "EMOTIONS_LIBRARY",
    "EXPRESSIONS",
    "ConnectionMode",
    "Keyframe",
    "PiperSynthesizer",
    "ReachyMiniConnection",
    "ReachyMiniPersonDetection",
    "ReachyMiniReaction",
    "ReachyMiniSpeechSynthesis",
    "ReachyMiniSystemInformation",
    "Synthesizer",
    "ToneSynthesizer",
]
