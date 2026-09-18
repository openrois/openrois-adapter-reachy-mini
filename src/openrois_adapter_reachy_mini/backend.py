"""What the components need from a Reachy Mini, and two ways to provide it.

A backend owns the robot: its head pose, body yaw, antennas, voice, ears, and
camera. The components translate RoIS messages into these calls and never
import the robot SDK themselves, so the same components run against the
simulator in tests and against the hardware in the lab.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeadPose:
    """The head target space: millimeters and degrees, in the robot's own frame."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "x": self.x, "y": self.y, "z": self.z,
            "roll": self.roll, "pitch": self.pitch, "yaw": self.yaw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HeadPose:
        return cls(**{k: float(data.get(k, 0.0)) for k in cls().to_dict()})


@dataclass(frozen=True)
class RobotState:
    head: HeadPose = HeadPose()
    body_yaw: float = 0.0
    antennas: tuple[float, float] = (0.0, 0.0)
    moving: bool = False


class Backend(Protocol):
    """The robot as the components see it."""

    @property
    def name(self) -> str: ...

    def state(self) -> RobotState: ...

    async def move_to(
        self,
        head: HeadPose | None = None,
        body_yaw: float | None = None,
        antennas: tuple[float, float] | None = None,
        duration: float = 1.0,
    ) -> None:
        """Move smoothly to the targets given and return when the motion ends."""
        ...

    def stop_motion(self) -> None:
        """Interrupt the motion in progress, keeping the current pose."""
        ...

    async def say(self, text: str, volume: int = 100) -> None:
        """Speak the text and return when the speech ends."""
        ...

    async def listen(self, timeout: float) -> str | None:
        """Wait up to ``timeout`` seconds for an utterance; None when nothing was heard."""
        ...

    async def people_in_view(self) -> int:
        """How many people the camera currently sees."""
        ...

    async def faces_in_view(self) -> int:
        """How many faces the camera currently sees."""
        ...

    def camera_url(self) -> str:
        """Where a consumer attaches to the camera stream (a WHEP or RTSP URL)."""
        ...

    def battery_level(self) -> float | None:
        """Battery percentage, or None when the robot is mains powered."""
        ...


class SimulatedBackend:
    """A Reachy Mini in software.

    Motion is kinematic: a move takes ``duration`` seconds scaled by
    ``time_scale`` (use 0 in tests) and the pose is interpolated on the way.
    Speech takes time proportional to its length. Utterances and people are
    scripted through :meth:`hear` and :meth:`show`, so a test decides what the
    robot perceives and when.
    """

    name = "reachy-mini-sim"

    def __init__(self, time_scale: float = 1.0, seconds_per_character: float = 0.05) -> None:
        self.time_scale = time_scale
        self.seconds_per_character = seconds_per_character
        self._state = RobotState()
        self._stop = False
        self._heard: deque[str] = deque()
        self._utterance = asyncio.Event()
        self._people = 0
        self._faces = 0
        self.spoken: list[str] = []
        self.motions: list[RobotState] = []

    # Scripting ---------------------------------------------------------------

    def hear(self, text: str) -> None:
        """Queue an utterance for the next :meth:`listen`."""
        self._heard.append(text)
        self._utterance.set()

    def show(self, people: int, faces: int | None = None) -> None:
        """Put ``people`` (and ``faces``, default the same number) in front of the camera."""
        self._people = people
        self._faces = people if faces is None else faces

    # Backend -----------------------------------------------------------------

    def state(self) -> RobotState:
        return self._state

    async def move_to(
        self,
        head: HeadPose | None = None,
        body_yaw: float | None = None,
        antennas: tuple[float, float] | None = None,
        duration: float = 1.0,
    ) -> None:
        start = self._state
        target = RobotState(
            head=head if head is not None else start.head,
            body_yaw=body_yaw if body_yaw is not None else start.body_yaw,
            antennas=antennas if antennas is not None else start.antennas,
        )
        self._stop = False
        self._state = replace(start, moving=True)
        steps = max(1, int(duration * 10))
        for i in range(1, steps + 1):
            await asyncio.sleep(duration * self.time_scale / steps)
            if self._stop:
                self._state = replace(self._state, moving=False)
                return
            t = i / steps
            self._state = RobotState(
                head=HeadPose(**{
                    k: (1 - t) * getattr(start.head, k) + t * getattr(target.head, k)
                    for k in start.head.to_dict()
                }),
                body_yaw=(1 - t) * start.body_yaw + t * target.body_yaw,
                antennas=(
                    (1 - t) * start.antennas[0] + t * target.antennas[0],
                    (1 - t) * start.antennas[1] + t * target.antennas[1],
                ),
                moving=i < steps,
            )
        self.motions.append(self._state)

    def stop_motion(self) -> None:
        self._stop = True

    async def say(self, text: str, volume: int = 100) -> None:
        logger.info("[%s] says (volume %d): %s", self.name, volume, text)
        self.spoken.append(text)
        await asyncio.sleep(max(0.05, len(text) * self.seconds_per_character) * self.time_scale)

    async def listen(self, timeout: float) -> str | None:
        if not self._heard:
            self._utterance.clear()
            try:
                await asyncio.wait_for(self._utterance.wait(), timeout=timeout)
            except TimeoutError:
                return None
        return self._heard.popleft() if self._heard else None

    async def people_in_view(self) -> int:
        return self._people

    async def faces_in_view(self) -> int:
        return self._faces

    def camera_url(self) -> str:
        return "http://127.0.0.1:8080/whep/reachy-mini-sim"

    def battery_level(self) -> float | None:
        return None


class ReachyMiniBackend:
    """The backend that drives a Reachy Mini through the ``reachy_mini`` SDK.

    The SDK is imported when the backend is created, so the package installs
    and the simulator runs without it (``pip install
    openrois-adapter-reachy-mini[robot]`` adds it).

    Status: written against the public SDK's ``ReachyMini`` client
    (``goto_target``, ``get_current_head_pose``) and not yet verified on
    hardware, since the maintainers have no unit at hand. Speech, hearing, and
    detection depend on what the SDK exposes on the installed version and fall
    back to logging a warning.
    """

    name = "reachy-mini"

    def __init__(self, camera_url: str = "", **sdk_options: Any) -> None:
        try:
            from reachy_mini import ReachyMini
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "The reachy_mini SDK is not installed: "
                "pip install 'openrois-adapter-reachy-mini[robot]', or run with --simulate",
            ) from exc
        self._mini = ReachyMini(**sdk_options)
        self._camera_url = camera_url
        self._state = RobotState()

    def state(self) -> RobotState:
        try:
            pose = self._mini.get_current_head_pose()
            self._state = replace(self._state, head=_head_from_matrix(pose))
        except Exception as exc:  # pragma: no cover - hardware only
            logger.debug("Head pose unavailable: %s", exc)
        return self._state

    async def move_to(
        self,
        head: HeadPose | None = None,
        body_yaw: float | None = None,
        antennas: tuple[float, float] | None = None,
        duration: float = 1.0,
    ) -> None:
        from reachy_mini.utils import create_head_pose

        target = head if head is not None else self._state.head
        kwargs: dict[str, Any] = {
            "head": create_head_pose(
                x=target.x, y=target.y, z=target.z,
                roll=target.roll, pitch=target.pitch, yaw=target.yaw,
                degrees=True, mm=True,
            ),
            "duration": duration,
        }
        if body_yaw is not None:
            kwargs["body_yaw"] = math.radians(body_yaw)
        if antennas is not None:
            kwargs["antennas"] = [math.radians(a) for a in antennas]
        self._state = replace(self._state, moving=True)
        await asyncio.to_thread(self._mini.goto_target, **kwargs)
        self._state = RobotState(
            head=target,
            body_yaw=body_yaw if body_yaw is not None else self._state.body_yaw,
            antennas=antennas if antennas is not None else self._state.antennas,
        )

    def stop_motion(self) -> None:
        # goto_target blocks for its duration; the SDK has no cancel, so the
        # next target simply overrides. The state stops reporting motion.
        self._state = replace(self._state, moving=False)

    async def say(self, text: str, volume: int = 100) -> None:
        media = getattr(self._mini, "media", None)
        speak = getattr(media, "say", None) or getattr(media, "speak", None)
        if speak is None:
            logger.warning("This SDK version exposes no speech output; would say: %s", text)
            return
        await asyncio.to_thread(speak, text)

    async def listen(self, timeout: float) -> str | None:
        media = getattr(self._mini, "media", None)
        listen = getattr(media, "listen", None)
        if listen is None:
            logger.warning("This SDK version exposes no speech input")
            await asyncio.sleep(timeout)
            return None
        heard = await asyncio.to_thread(listen, timeout)
        return str(heard) if heard else None

    async def people_in_view(self) -> int:
        return await self.faces_in_view()

    async def faces_in_view(self) -> int:
        media = getattr(self._mini, "media", None)
        detect = getattr(media, "detect_faces", None)
        if detect is None:
            return 0
        faces = await asyncio.to_thread(detect)
        return len(faces or [])

    def camera_url(self) -> str:
        return self._camera_url

    def battery_level(self) -> float | None:
        return None


def _head_from_matrix(matrix: Any) -> HeadPose:
    """Translate a 4x4 pose matrix (meters, radians) into a HeadPose (mm, degrees)."""
    m = [[float(matrix[i][j]) for j in range(4)] for i in range(4)]
    sy = math.hypot(m[0][0], m[1][0])
    if sy > 1e-6:
        roll = math.atan2(m[2][1], m[2][2])
        pitch = math.atan2(-m[2][0], sy)
        yaw = math.atan2(m[1][0], m[0][0])
    else:
        roll = math.atan2(-m[1][2], m[1][1])
        pitch = math.atan2(-m[2][0], sy)
        yaw = 0.0
    return HeadPose(
        x=m[0][3] * 1000, y=m[1][3] * 1000, z=m[2][3] * 1000,
        roll=math.degrees(roll), pitch=math.degrees(pitch), yaw=math.degrees(yaw),
    )
