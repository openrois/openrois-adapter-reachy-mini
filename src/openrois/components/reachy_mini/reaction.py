"""Reaction for the Reachy Mini.

``reaction_ref`` names an expression, ``nod`` by default. ``start`` plays it with the
head and the antennas, from wherever the head is, and ends when the expression ends.
``stop``, or a new ``start``, ends it where it is, with ABORT. ``available_reactions``
lists the built-in expressions, and the moves of a recorded library, such as Pollen's
emotions, when the component loads one. A recorded move plays without its sound, so it
never talks over speech.

The move plays in a worker thread, which sends the targets of the head, the antennas
and the body 100 times a second. The SDK sends them on a blocking socket, so over a
Wi-Fi that stalls a send can block for as long as the stall lasts: the thread waits it
out, and the adapter goes on serving the other components and the stop meanwhile.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
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
from openrois.components.reachy_mini.moves import (
    EXPRESSIONS,
    BlendedMove,
    Keyframe,
    KeyframeMove,
    ScaledMove,
)

logger = logging.getLogger(__name__)

#: Pollen's library of recorded emotions, on the Hugging Face Hub.
EMOTIONS_LIBRARY = "pollen-robotics/reachy-mini-emotions-library"

#: The reaction a start plays before any client sets ``reaction_ref``.
DEFAULT_REACTION = "nod"

# How often a playing move sends its targets, in hertz, as the SDK plays moves.
_PLAY_FREQUENCY = 100.0

# How long a cut-off reaction waits for its thread to send its last target, in seconds,
# so that two playbacks never drive the head at once.
_JOIN = 0.5

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
        time_scale: The factor on the duration of every reaction and its blend: below 1
            plays them faster, for tests, and above 1 slower.
    """

    def __init__(
        self,
        connection: ReachyMiniConnection,
        *,
        library: str | None = None,
        blend: float = 0.3,
        time_scale: float = 1.0,
    ) -> None:
        if time_scale <= 0:
            raise ValueError(f"time_scale is above 0, not {time_scale}")
        self._connection = self.uses(connection)
        self._library = library
        self._blend = blend
        self.time_scale = time_scale
        self._recorded: Any = None

    async def connect(self) -> None:
        if self._library is not None:
            try:
                self._recorded = await asyncio.to_thread(_load_library, self._library)
            except Exception:
                logger.exception(
                    "Could not load %s, so only the built-in expressions are offered",
                    self._library,
                )

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
        try:
            mini = await self._connection.client()
            logger.info("Playing the reaction %s", name)
            move = self._move(name, mini)
            stop = threading.Event()
            playback = asyncio.ensure_future(asyncio.to_thread(_play, mini, move, stop))
            try:
                await asyncio.shield(playback)
            except asyncio.CancelledError:
                # A stop, or a new start, ends the playback here: the head stays where
                # it is, and the start ends with ABORT. The engine serves stop for a
                # component that handles start, so no stop handler is needed.
                stop.set()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(playback), _JOIN)
                raise
        except ConnectionError as exc:
            raise CommandFailed(CompletedStatus.ERROR, f"No Reachy Mini daemon: {exc}") from exc

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
        if self.time_scale != 1.0:
            move = ScaledMove(move, self.time_scale)
        head_joints, antennas = mini.get_current_joint_positions()
        return BlendedMove(
            move,
            head=mini.get_current_head_pose(),
            antennas=(float(antennas[0]), float(antennas[1])),
            body_yaw=float(head_joints[0]),
            blend=self._blend * self.time_scale,
            interpolate=linear_pose_interpolation,
        )


def _play(mini: Any, move: BlendedMove, stop: threading.Event) -> None:
    """Send the targets of a move until it ends or ``stop`` is set, in a worker thread.

    It plays as the SDK's ``async_play_move`` does, without the move's sound, and sends
    the head, the antennas and the body in one message for each tick.
    """
    period = 1.0 / _PLAY_FREQUENCY
    last = max(0.0, move.duration - 1e-2)
    started = time.monotonic()
    while not stop.is_set():
        elapsed = time.monotonic() - started
        # The last tick sends the end of the move, so the head always arrives there.
        head, antennas, body_yaw = move.evaluate(min(elapsed, last))
        if head is not None or antennas is not None or body_yaw is not None:
            mini.set_target(
                head=head,
                antennas=list(antennas) if antennas is not None else None,
                body_yaw=body_yaw,
            )
        if elapsed >= last:
            return
        stop.wait(max(0.0, period - (time.monotonic() - started - elapsed)))


def _load_library(name: str) -> Any:
    """The recorded moves of a library, from the cache or the Hugging Face Hub."""
    from reachy_mini.motion.recorded_move import RecordedMoves

    return RecordedMoves(name)
