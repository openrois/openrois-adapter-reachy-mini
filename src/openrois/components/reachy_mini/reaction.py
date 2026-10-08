"""Reaction for the Reachy Mini.

``reaction_ref`` names an expression, ``nod`` by default. ``start`` plays it with the
head and the antennas, from wherever the head is, and ends when the expression ends.
``stop``, or a new ``start``, ends it where it is, with ABORT. ``available_reactions``
lists the built-in expressions, and the moves of a recorded library, such as Pollen's
emotions, when the component loads one. A recorded move plays without its sound, so it
never talks over speech.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from openrois.components.core import (
    CommandFailed,
    Component,
    component,
    invoke,
    on_set_parameter,
    query,
)
from openrois.interfaces.components import REACTION_PROFILE
from openrois.interfaces.service import CompletedStatus

from openrois.components.reachy_mini.connection import ReachyMiniConnection
from openrois.components.reachy_mini.moves import EXPRESSIONS, BlendedMove, Keyframe, KeyframeMove

logger = logging.getLogger(__name__)

#: Pollen's library of recorded emotions, on the Hugging Face Hub.
EMOTIONS_LIBRARY = "pollen-robotics/reachy-mini-emotions-library"

#: The reaction a start plays before any client sets ``reaction_ref``.
DEFAULT_REACTION = "nod"

#: Reaction with ``nod`` as the default of reaction_ref. Reaction.xml gives none, and the
#: default lets a client start a reaction before setting any parameter.
REACTION_WITH_DEFAULT = REACTION_PROFILE.model_copy(
    update={
        "parameter_profiles": [
            p.model_copy(update={"default_value": DEFAULT_REACTION})
            if p.name == "reaction_ref"
            else p
            for p in REACTION_PROFILE.parameter_profiles
        ]
    }
)


@component(REACTION_WITH_DEFAULT)
class ReachyMiniReaction(Component):
    """Reaction of one Reachy Mini: expressions of the head and the antennas.

    Args:
        connection: The connection to the robot, shared with its other components.
        library: A Hugging Face dataset of recorded moves, such as ``EMOTIONS_LIBRARY``,
            whose moves become reactions too. Without the Hub or a cached copy, the
            component offers the built-in expressions only.
        blend: How long a reaction takes to ease from the current pose into its first
            pose, in seconds.
    """

    def __init__(
        self,
        connection: ReachyMiniConnection,
        *,
        library: str | None = None,
        blend: float = 0.3,
    ) -> None:
        self._connection = connection
        self._library = library
        self._blend = blend
        self._recorded: Any = None

    async def connect(self) -> None:
        await self._connection.open()
        if self._library is not None:
            try:
                self._recorded = await asyncio.to_thread(_load_library, self._library)
            except Exception:
                logger.exception(
                    "Could not load %s, so only the built-in expressions are offered",
                    self._library,
                )

    async def disconnect(self) -> None:
        await self._connection.close()

    def reactions(self) -> list[str]:
        """Every reaction this component plays: the expressions, then the library."""
        names = list(EXPRESSIONS)
        if self._recorded is not None:
            names.extend(sorted(self._recorded.list_moves()))
        return names

    @query("available_reactions")
    async def available_reactions(self) -> dict[str, object]:
        return {"available_reactions": self.reactions()}

    @on_set_parameter
    async def check(self, values: Mapping[str, Any]) -> None:
        name = values.get("reaction_ref")
        if name is not None and name not in self.reactions():
            raise ValueError(f"No reaction is named {name!r}. See available_reactions")

    @invoke("start")
    async def start(self) -> None:
        name = self.parameters.get("reaction_ref", DEFAULT_REACTION)
        if name not in self.reactions():
            raise CommandFailed(CompletedStatus.ERROR, f"No reaction is named {name!r}")
        mini = self._connection.mini
        logger.info("Playing the reaction %s", name)
        # A stop, or a new start, cancels the playback here: the head stays where it is,
        # and the start ends with ABORT. The engine serves stop for a component that
        # handles start, so no stop handler is needed.
        await mini.async_play_move(self._move(name, mini), sound=False)

    def _move(self, name: str, mini: Any) -> BlendedMove:
        from reachy_mini.utils import create_head_pose
        from reachy_mini.utils.interpolation import linear_pose_interpolation

        def head_pose(frame: Keyframe) -> Any:
            return create_head_pose(
                z=frame.z, roll=frame.roll, pitch=frame.pitch, yaw=frame.yaw, mm=True
            )

        if name in EXPRESSIONS:
            move: Any = KeyframeMove(EXPRESSIONS[name], head_pose)
        else:
            move = self._recorded.get(name)
        head_joints, antennas = mini.get_current_joint_positions()
        return BlendedMove(
            move,
            head=mini.get_current_head_pose(),
            antennas=(float(antennas[0]), float(antennas[1])),
            body_yaw=float(head_joints[0]),
            blend=self._blend,
            interpolate=linear_pose_interpolation,
        )


def _load_library(name: str) -> Any:
    """The recorded moves of a library, from the cache or the Hugging Face Hub."""
    from reachy_mini.motion.recorded_move import RecordedMoves

    return RecordedMoves(name)
