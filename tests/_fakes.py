"""A stand-in for the Reachy Mini SDK, installed as the modules the components import.

``FakeSdk`` plays the daemons of any number of robots, keyed by host, ``localhost`` for
the simulator. A ``FakeRobot`` keeps what the components did to it: the moves played and
whether with their sound, the head pose and the antennas, the sounds played, the stops,
the wobbling, and the camera frames it serves. A frame whose first pixel is ``n`` shows
``n`` faces to the fake face detector.
"""

from __future__ import annotations

import asyncio
import math
import sys
import types
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

SDK_MODULES = (
    "reachy_mini",
    "reachy_mini.utils",
    "reachy_mini.utils.interpolation",
    "reachy_mini.motion",
    "reachy_mini.motion.recorded_move",
    "reachy_mini.vision",
    "reachy_mini.vision.face_detector",
)


def create_head_pose(
    x: float = 0,
    y: float = 0,
    z: float = 0,
    roll: float = 0,
    pitch: float = 0,
    yaw: float = 0,
    mm: bool = False,
    degrees: bool = True,
) -> np.ndarray:
    """The SDK's head pose: a 4x4 matrix from a position and roll, pitch and yaw."""
    if degrees:
        roll, pitch, yaw = (math.radians(a) for a in (roll, pitch, yaw))
    scale = 0.001 if mm else 1.0
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    pose = np.eye(4)
    pose[:3, :3] = [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]
    pose[:3, 3] = [x * scale, y * scale, z * scale]
    return pose


def pitch_of(pose: np.ndarray) -> float:
    """The pitch of a head pose, in degrees. Positive looks down."""
    return math.degrees(math.asin(max(-1.0, min(1.0, -float(pose[2, 0])))))


def linear_pose_interpolation(start: np.ndarray, target: np.ndarray, t: float) -> np.ndarray:
    a, b = np.asarray(start, dtype=float), np.asarray(target, dtype=float)
    blended: np.ndarray = a + (b - a) * t
    return blended


@dataclass
class PlayedMove:
    move: Any
    sound: bool
    cancelled: bool = False
    finished: bool = False


@dataclass
class FakeRobot:
    """The daemon of one robot, as the components see it through the SDK."""

    host: str
    head: np.ndarray = field(default_factory=lambda: np.eye(4))
    antennas: list[float] = field(default_factory=lambda: [-0.1745, 0.1745])
    body_yaw: float = 0.0
    moves: list[PlayedMove] = field(default_factory=list)
    head_targets: list[np.ndarray] = field(default_factory=list)
    played: list[tuple[str, float]] = field(default_factory=list)
    stops: int = 0
    wobbling: bool = False
    wobble_log: list[bool] = field(default_factory=list)
    frames: list[np.ndarray | None] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def show(self, faces: int | None, width: int = 1280, height: int = 720) -> None:
        """Serve a frame with ``faces`` faces from now on, or no frame with None."""
        if faces is None:
            self.frames = [None]
        else:
            self.frames = [np.full((height, width, 3), faces, dtype=np.uint8)]

    def next_frame(self) -> np.ndarray | None:
        if not self.frames:
            return None
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]


class FakeMedia:
    def __init__(self, robot: FakeRobot, backend: str) -> None:
        self.robot = robot
        self.backend = backend

    def get_frame(self) -> np.ndarray | None:
        return self.robot.next_frame()

    def play_sound(self, sound_file: str) -> None:
        with wave.open(sound_file, "rb") as audio:
            seconds = audio.getnframes() / float(audio.getframerate())
        self.robot.played.append((Path(sound_file).name, seconds))
        self.robot.log.append("play")

    def stop_playing(self) -> None:
        self.robot.stops += 1
        self.robot.log.append("stop_playing")


@dataclass
class FakeRecordedMove:
    name: str
    duration: float = 1.0

    @property
    def sound_path(self) -> Path:
        return Path(f"/sounds/{self.name}.wav")

    def evaluate(self, t: float) -> tuple[np.ndarray, tuple[float, float], float]:
        return create_head_pose(pitch=-5 * t), (0.3, -0.3), 0.2 * t


@dataclass
class FakeSdk:
    """The SDK of every robot of a test, and what the components asked of it.

    Attributes:
        speed: How much faster than real time a move plays.
        library_moves: The moves of every recorded library.
        library_fails: Whether loading a library fails, as without the Hub.
    """

    speed: float = 20.0
    robots: dict[str, FakeRobot] = field(default_factory=dict)
    clients: list[Any] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)
    library_moves: tuple[str, ...] = ("cheerful1", "sad2")
    library_fails: bool = False
    detector_thresholds: list[float] = field(default_factory=list)
    detected_shapes: list[tuple[int, ...]] = field(default_factory=list)

    def robot(self, host: str = "localhost") -> FakeRobot:
        if host not in self.robots:
            self.robots[host] = FakeRobot(host)
        return self.robots[host]

    def open_clients(self) -> list[Any]:
        return [c for c in self.clients if not c.closed]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sdk = self

        class ReachyMini:
            def __init__(
                self,
                robot_name: str = "reachy_mini",
                host: str = "reachy-mini.local",
                port: int = 8000,
                connection_mode: str = "auto",
                timeout: float = 5.0,
                media_backend: str = "default",
                **options: Any,
            ) -> None:
                where = "localhost" if connection_mode == "localhost_only" else host
                self.options = {
                    "host": host,
                    "port": port,
                    "connection_mode": connection_mode,
                    "timeout": timeout,
                    "media_backend": media_backend,
                    **options,
                }
                self.robot = sdk.robot(where)
                self.media = FakeMedia(self.robot, media_backend)
                self.closed = False
                sdk.clients.append(self)

            def __exit__(self, *exc: object) -> None:
                self.closed = True

            def get_current_head_pose(self) -> np.ndarray:
                return self.robot.head.copy()

            def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
                return [self.robot.body_yaw, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], list(
                    self.robot.antennas
                )

            def set_target_head_pose(self, pose: np.ndarray) -> None:
                self.robot.head = np.array(pose)
                self.robot.head_targets.append(self.robot.head)

            def set_target_antenna_joint_positions(self, antennas: list[float]) -> None:
                self.robot.antennas = list(antennas)

            def set_target_body_yaw(self, body_yaw: float) -> None:
                self.robot.body_yaw = body_yaw

            def enable_wobbling(self) -> None:
                self.robot.wobbling = True
                self.robot.wobble_log.append(True)

            def disable_wobbling(self) -> None:
                self.robot.wobbling = False
                self.robot.wobble_log.append(False)

            async def async_play_move(
                self,
                move: Any,
                play_frequency: float = 100.0,
                initial_goto_duration: float = 0.0,
                sound: bool = True,
            ) -> None:
                played = PlayedMove(move, sound)
                self.robot.moves.append(played)
                steps = max(4, int(move.duration * 20))
                try:
                    for i in range(steps + 1):
                        t = min(i * move.duration / steps, move.duration - 1e-2)
                        head, antennas, body_yaw = move.evaluate(max(t, 0.0))
                        if head is not None:
                            self.set_target_head_pose(head)
                        if antennas is not None:
                            self.set_target_antenna_joint_positions(list(antennas))
                        if body_yaw is not None:
                            self.set_target_body_yaw(body_yaw)
                        await asyncio.sleep(move.duration / sdk.speed / steps)
                except asyncio.CancelledError:
                    played.cancelled = True
                    raise
                played.finished = True

        class RecordedMoves:
            def __init__(self, hf_dataset_name: str) -> None:
                if sdk.library_fails:
                    raise OSError("The Hugging Face Hub is unreachable")
                sdk.libraries.append(hf_dataset_name)

            def list_moves(self) -> list[str]:
                return list(sdk.library_moves)

            def get(self, move_name: str) -> FakeRecordedMove:
                return FakeRecordedMove(move_name)

        class FaceDetector:
            def __init__(self, score_threshold: float = 0.6, nms_threshold: float = 0.3) -> None:
                sdk.detector_thresholds.append(score_threshold)

            def detect(self, frame: np.ndarray) -> list[object]:
                sdk.detected_shapes.append(tuple(frame.shape))
                return [object()] * int(frame[0, 0, 0])

        modules = {name: types.ModuleType(name) for name in SDK_MODULES}
        modules["reachy_mini"].ReachyMini = ReachyMini  # type: ignore[attr-defined]
        modules["reachy_mini.utils"].create_head_pose = create_head_pose  # type: ignore[attr-defined]
        interpolation = modules["reachy_mini.utils.interpolation"]
        interpolation.linear_pose_interpolation = linear_pose_interpolation  # type: ignore[attr-defined]
        modules["reachy_mini.motion.recorded_move"].RecordedMoves = RecordedMoves  # type: ignore[attr-defined]
        modules["reachy_mini.vision.face_detector"].FaceDetector = FaceDetector  # type: ignore[attr-defined]
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)
