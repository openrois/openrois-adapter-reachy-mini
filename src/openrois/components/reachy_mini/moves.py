"""The moves the Reaction component plays: built-in expressions and a blend into any move.

A move has the shape ``ReachyMini.async_play_move`` plays: a ``duration`` in seconds,
``evaluate(t)`` that returns the head pose, the antennas in radians and the body yaw at
time ``t`` (None leaves one where it is), and a ``sound_path``.

The built-in expressions are keyframes of the head and the antennas. ``BlendedMove``
starts any move from wherever the head is, so a reaction never jumps, without the
blocking goto of the SDK.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# The antennas at rest, in degrees (right, left): the SDK's start position, a little
# apart so they do not shake when upright.
REST_ANTENNAS = (-10.0, 10.0)


class Move(Protocol):
    """What ``ReachyMini.async_play_move`` reads from a move."""

    @property
    def duration(self) -> float: ...

    @property
    def sound_path(self) -> Path | None: ...

    def evaluate(self, t: float) -> tuple[Any, Any, float | None]: ...


@dataclass(frozen=True)
class Keyframe:
    """The pose of the head and the antennas at one time of an expression.

    Attributes:
        at: The time of the keyframe, in seconds from the start of the expression.
        roll: The head roll, in degrees.
        pitch: The head pitch, in degrees. Positive looks down.
        yaw: The head yaw, in degrees.
        z: The head height, in millimetres from rest.
        antennas: The antennas (right, left), in degrees. Rest is (-10, 10), and a wider
            spread folds them back.
    """

    at: float
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    z: float = 0.0
    antennas: tuple[float, float] = REST_ANTENNAS


def _rest(at: float) -> Keyframe:
    return Keyframe(at)


#: The built-in expressions, each a sequence of keyframes that ends at rest.
EXPRESSIONS: Mapping[str, tuple[Keyframe, ...]] = {
    "neutral": (_rest(0.0), _rest(0.6)),
    "nod": (
        _rest(0.0),
        Keyframe(0.3, pitch=14),
        Keyframe(0.6, pitch=-4),
        Keyframe(0.9, pitch=14),
        _rest(1.3),
    ),
    "shake": (
        _rest(0.0),
        Keyframe(0.3, yaw=22),
        Keyframe(0.7, yaw=-22),
        Keyframe(1.1, yaw=22),
        Keyframe(1.5, yaw=-22),
        _rest(1.9),
    ),
    "happy": (
        _rest(0.0),
        Keyframe(0.3, pitch=-12, z=8, antennas=(-40.0, 40.0)),
        Keyframe(0.55, pitch=-12, roll=8, z=8, antennas=(15.0, -15.0)),
        Keyframe(0.8, pitch=-12, roll=-8, z=8, antennas=(-40.0, 40.0)),
        Keyframe(1.05, pitch=-12, roll=8, z=8, antennas=(15.0, -15.0)),
        Keyframe(1.3, pitch=-10, z=6, antennas=(-40.0, 40.0)),
        _rest(1.8),
    ),
    "sad": (
        _rest(0.0),
        Keyframe(1.0, pitch=22, z=-8, antennas=(-75.0, 75.0)),
        Keyframe(2.2, pitch=24, roll=-6, z=-8, antennas=(-80.0, 80.0)),
        _rest(3.4),
    ),
    "curious": (
        _rest(0.0),
        Keyframe(0.5, roll=18, yaw=12, pitch=-6, antennas=(-10.0, 45.0)),
        Keyframe(1.5, roll=18, yaw=12, pitch=-6, antennas=(-35.0, 45.0)),
        _rest(2.2),
    ),
}


def _ease(s: float) -> float:
    """Smooth step from 0 to 1: no jerk at either end."""
    s = min(max(s, 0.0), 1.0)
    return s * s * (3.0 - 2.0 * s)


def _mix(a: float, b: float, s: float) -> float:
    return a + (b - a) * s


def _between(a: Keyframe, b: Keyframe, s: float) -> Keyframe:
    return Keyframe(
        at=_mix(a.at, b.at, s),
        roll=_mix(a.roll, b.roll, s),
        pitch=_mix(a.pitch, b.pitch, s),
        yaw=_mix(a.yaw, b.yaw, s),
        z=_mix(a.z, b.z, s),
        antennas=(_mix(a.antennas[0], b.antennas[0], s), _mix(a.antennas[1], b.antennas[1], s)),
    )


class KeyframeMove:
    """An expression played from its keyframes, easing between each pair.

    Args:
        frames: The keyframes, the first one at time 0, in time order.
        head_pose: Builds the head pose of a keyframe, ``reachy_mini.utils``'s
            ``create_head_pose`` in degrees and millimetres.
    """

    sound_path: Path | None = None

    def __init__(self, frames: tuple[Keyframe, ...], head_pose: Callable[[Keyframe], Any]) -> None:
        if not frames or frames[0].at != 0.0:
            raise ValueError("An expression starts with a keyframe at time 0")
        self._frames = frames
        self._head_pose = head_pose

    @property
    def duration(self) -> float:
        return self._frames[-1].at

    def frame(self, t: float) -> Keyframe:
        """The pose at time ``t``, between the two keyframes around it."""
        frames = self._frames
        for a, b in zip(frames, frames[1:], strict=False):
            if t <= b.at:
                span = b.at - a.at
                return _between(a, b, _ease((t - a.at) / span) if span > 0 else 1.0)
        return frames[-1]

    def evaluate(self, t: float) -> tuple[Any, tuple[float, float], None]:
        frame = self.frame(t)
        right, left = frame.antennas
        return self._head_pose(frame), (math.radians(right), math.radians(left)), None


class ScaledMove:
    """A move played ``scale`` times as long: faster below 1, slower above.

    Args:
        move: The move to play.
        scale: The factor on its duration, above 0.
    """

    def __init__(self, move: Move, scale: float) -> None:
        if scale <= 0:
            raise ValueError("The time scale of a move is above 0")
        self._move = move
        self._scale = scale

    @property
    def duration(self) -> float:
        return self._move.duration * self._scale

    @property
    def sound_path(self) -> Path | None:
        return self._move.sound_path

    def evaluate(self, t: float) -> tuple[Any, Any, float | None]:
        return self._move.evaluate(t / self._scale)


class BlendedMove:
    """A move that starts from where the robot is, then plays another move.

    During ``blend`` seconds the head, the antennas and the body yaw ease from their
    current position into the first pose of the move, and then the move plays.

    Args:
        move: The move to play.
        head: The current head pose, a 4x4 matrix.
        antennas: The current antennas (right, left), in radians.
        body_yaw: The current body yaw, in radians.
        blend: How long the blend lasts, in seconds.
        interpolate: Interpolates two head poses, ``reachy_mini.utils.interpolation``'s
            ``linear_pose_interpolation``.
    """

    def __init__(
        self,
        move: Move,
        *,
        head: Any,
        antennas: tuple[float, float],
        body_yaw: float,
        blend: float,
        interpolate: Callable[[Any, Any, float], Any],
    ) -> None:
        self._move = move
        self._head = head
        self._antennas = antennas
        self._body_yaw = body_yaw
        self._blend = blend
        self._interpolate = interpolate

    @property
    def duration(self) -> float:
        return self._blend + self._move.duration

    @property
    def sound_path(self) -> Path | None:
        return self._move.sound_path

    def evaluate(self, t: float) -> tuple[Any, Any, float | None]:
        if t >= self._blend:
            return self._move.evaluate(min(t - self._blend, self._move.duration))
        s = _ease(t / self._blend)
        head, antennas, body_yaw = self._move.evaluate(0.0)
        if head is not None:
            head = self._interpolate(self._head, head, s)
        if antennas is not None:
            right, left = (float(a) for a in antennas)
            antennas = (_mix(self._antennas[0], right, s), _mix(self._antennas[1], left, s))
        if body_yaw is not None:
            body_yaw = _mix(self._body_yaw, float(body_yaw), s)
        return head, antennas, body_yaw
