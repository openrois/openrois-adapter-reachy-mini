"""A stand-in for the Reachy Mini SDK, installed as the modules the components import.

``FakeSdk`` plays the daemons of any number of robots, keyed by host, ``localhost`` for
the simulator. A ``FakeRobot`` keeps what the components did to it: the head pose and the
antennas, every head target, the sounds played, the stops, the wobbling, and the camera
frames it serves. A frame whose first pixel is ``n`` shows
``n`` faces to the fake face detector.

Every daemon runs until ``stop_daemon``. A client connects as the SDK does, ``auto``
included, which tries this machine first, and a client whose daemon stopped fails its
commands with the SDK's ``ConnectionError``. ``stall`` pauses a daemon's state stream
with its connection open, as over a Wi-Fi that drops out for a moment.

A client sets up its media in ``_configure_mediamanager``, as the SDK does: local media
for the simulator on this machine, and WebRTC for a robot on the network, whose sounds
``FakeDaemonHttp`` plays through the daemon's HTTP API.
"""

from __future__ import annotations

import enum
import json
import math
import sys
import threading
import time
import types
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
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
    "reachy_mini.media",
    "reachy_mini.media.media_manager",
)


class MediaBackend(enum.Enum):
    """The SDK's media backends."""

    NO_MEDIA = "no_media"
    LOCAL = "local"
    WEBRTC = "webrtc"


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
class FakeRobot:
    """The daemon of one robot, as the components see it through the SDK."""

    host: str
    head: np.ndarray = field(default_factory=lambda: np.eye(4))
    antennas: list[float] = field(default_factory=lambda: [-0.1745, 0.1745])
    body_yaw: float = 0.0
    head_targets: list[np.ndarray] = field(default_factory=list)
    played: list[tuple[str, float]] = field(default_factory=list)
    stops: int = 0
    wobbling: bool = False
    wobble_log: list[bool] = field(default_factory=list)
    frames: list[np.ndarray | None] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    #: The Wi-Fi address the daemon reports, which the SDK fetches WebRTC media from.
    wlan_ip: str | None = None
    #: How long playing a sound takes before it plays, as an upload over Wi-Fi does.
    play_delay: float = 0.0
    #: Set once a sound is on its way, before the play delay.
    sending: threading.Event = field(default_factory=threading.Event)
    #: While set, a target waits until the event is set, as a send over a stalled Wi-Fi.
    send_gate: threading.Event | None = None

    def show(
        self, faces: int | None, width: int = 1280, height: int = 720, *, cut: int = 0
    ) -> None:
        """Serve a frame with ``faces`` faces from now on, or no frame with None.

        ``cut`` adds faces that the right edge of the frame cuts off.
        """
        if faces is None:
            self.frames = [None]
        else:
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            frame[..., 0] = faces
            frame[..., 1] = cut
            self.frames = [frame]

    def next_frame(self) -> np.ndarray | None:
        if not self.frames:
            return None
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]


LOST = "Lost connection with the server."


class _ReceivingThread:
    """The SDK's thread that receives the state, which ends when the daemon closes."""

    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def is_alive(self) -> bool:
        return not self._client.lost


@dataclass
class FakeClient:
    """The SDK's connection to one daemon.

    ``lost`` is a daemon that stopped and closed the connection, and ``stalled`` one
    that sends nothing for a while, its connection still open.
    """

    host: str
    port: int
    robot: FakeRobot | None = None
    lost: bool = False
    stalled: bool = False

    def __post_init__(self) -> None:
        self._recv_thread = _ReceivingThread(self)

    def is_connected(self) -> bool:
        return not self.lost and not self.stalled

    def check(self) -> None:
        if self.lost or self.stalled:
            raise ConnectionError(LOST)

    def get_status(self, wait: bool = True, timeout: float = 5.0) -> SimpleNamespace:
        wlan_ip = self.robot.wlan_ip if self.robot is not None else None
        return SimpleNamespace(wlan_ip=wlan_ip, no_media=False)


class FakeMedia:
    """The media of one client: the camera and the speaker of its robot."""

    def __init__(self, robot: FakeRobot, client: FakeClient, backend: MediaBackend) -> None:
        self.robot = robot
        self.client = client
        self.backend = backend
        self.audio: object | None = self
        self.closed = False

    def get_frame(self) -> np.ndarray | None:
        if self.client.lost:
            return None
        return self.robot.next_frame()

    def play_sound(self, sound_file: str) -> None:
        self.client.check()
        with wave.open(sound_file, "rb") as audio:
            seconds = audio.getnframes() / float(audio.getframerate())
        self.robot.sending.set()
        time.sleep(self.robot.play_delay)
        self.robot.played.append((Path(sound_file).name, seconds))
        self.robot.log.append("play")

    def stop_playing(self) -> None:
        self.robot.stops += 1
        self.robot.log.append("stop_playing")

    def close(self) -> None:
        self.closed = True


class MediaManager:
    """The SDK's media manager, as the adapter builds it: without camera and speaker."""

    def __init__(self, backend: MediaBackend = MediaBackend.LOCAL, log_level: str = "INFO") -> None:
        assert backend is MediaBackend.NO_MEDIA
        self.backend = backend
        self.audio: object | None = None
        self.camera: object | None = None

    def get_frame(self) -> None:
        return None

    def play_sound(self, sound_file: str) -> None:
        pass  # The SDK only logs that the audio is not initialized.

    def stop_playing(self) -> None:
        pass

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class Face:
    """A face the SDK's detector found: its bounding box x, y, width and height."""

    bbox: tuple[float, float, float, float]


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
        library_moves: The moves of every recorded library.
        library_fails: Whether loading a library fails, as without the Hub.
    """

    robots: dict[str, FakeRobot] = field(default_factory=dict)
    clients: list[Any] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)
    library_moves: tuple[str, ...] = ("cheerful1", "sad2")
    library_fails: bool = False
    detector_thresholds: list[float] = field(default_factory=list)
    detected_shapes: list[tuple[int, ...]] = field(default_factory=list)
    detector_failures: int = 0
    down: set[str] = field(default_factory=set)
    #: Raised, one each, by the next clients after their state stream connected.
    connect_failures: list[BaseException] = field(default_factory=list)
    #: Raised, one each, by the media setup of the next clients.
    media_failures: list[BaseException] = field(default_factory=list)

    def stop_daemon(self, host: str = "localhost") -> None:
        """Stop the daemon of a robot: its clients lose it, and new ones cannot connect."""
        self.down.add(host)
        for client in self.clients:
            if client.robot.host == host:
                client.client.lost = True

    def start_daemon(self, host: str = "localhost") -> None:
        self.down.discard(host)

    def stall(self, host: str = "localhost", stalled: bool = True) -> None:
        """Pause, or resume, the state stream of a daemon, its connections still open."""
        for client in self.clients:
            if client.robot.host == host:
                client.client.stalled = stalled

    def robot(self, host: str = "localhost") -> FakeRobot:
        if host not in self.robots:
            # A robot on the network reports its Wi-Fi address, and the simulator none.
            wlan_ip = None if host == "localhost" else "192.168.1.42"
            self.robots[host] = FakeRobot(host, wlan_ip=wlan_ip)
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
                # As the SDK does: auto tries this machine first, then the host.
                if connection_mode == "localhost_only" or (
                    connection_mode == "auto" and "localhost" not in sdk.down
                ):
                    where = "localhost"
                else:
                    where = host
                if where in sdk.down:
                    raise ConnectionError(f"Failed to connect to ws://{where}:{port}/ws/sdk")
                self.options = {
                    "host": host,
                    "port": port,
                    "connection_mode": connection_mode,
                    "timeout": timeout,
                    "media_backend": media_backend,
                    **options,
                }
                self.robot = sdk.robot(where)
                self.client = FakeClient(where, port, self.robot)
                self.connection_mode = "localhost_only" if where == "localhost" else "network"
                self.closed = False
                sdk.clients.append(self)
                if sdk.connect_failures:
                    self.closed = True  # The SDK's client is collected with the error.
                    raise sdk.connect_failures.pop(0)
                self.media_manager = self._configure_mediamanager(media_backend, "INFO")

            def _configure_mediamanager(self, media_backend: str, log_level: str) -> Any:
                if sdk.media_failures:
                    raise sdk.media_failures.pop(0)
                local = self.connection_mode == "localhost_only"
                backend = MediaBackend.LOCAL if local else MediaBackend.WEBRTC
                return FakeMedia(self.robot, self.client, backend)

            @property
            def media(self) -> Any:
                return self.media_manager

            def __exit__(self, *exc: object) -> None:
                self.closed = True

            def get_current_head_pose(self) -> np.ndarray:
                return self.robot.head.copy()

            def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
                return [self.robot.body_yaw, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], list(
                    self.robot.antennas
                )

            def set_target_head_pose(self, pose: np.ndarray) -> None:
                self.client.check()
                self.robot.head = np.array(pose)
                self.robot.head_targets.append(self.robot.head)

            def set_target_antenna_joint_positions(self, antennas: list[float]) -> None:
                self.client.check()
                self.robot.antennas = list(antennas)

            def set_target_body_yaw(self, body_yaw: float) -> None:
                self.client.check()
                self.robot.body_yaw = body_yaw

            def set_target(
                self,
                head: np.ndarray | None = None,
                antennas: list[float] | None = None,
                body_yaw: float | None = None,
            ) -> None:
                if head is None and antennas is None and body_yaw is None:
                    raise ValueError("At least one of head, antennas or body_yaw must be provided.")
                if self.robot.send_gate is not None:
                    self.robot.send_gate.wait()
                self.client.check()
                if head is not None:
                    self.robot.head = np.array(head)
                    self.robot.head_targets.append(self.robot.head)
                if antennas is not None:
                    self.robot.antennas = list(antennas)
                if body_yaw is not None:
                    self.robot.body_yaw = body_yaw

            def enable_wobbling(self) -> None:
                self.client.check()
                self.robot.wobbling = True
                self.robot.wobble_log.append(True)

            def disable_wobbling(self) -> None:
                self.client.check()
                self.robot.wobbling = False
                self.robot.wobble_log.append(False)

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
                if sdk.detector_failures > 0:
                    sdk.detector_failures -= 1
                    raise OSError("The Hugging Face Hub is unreachable")
                sdk.detector_thresholds.append(score_threshold)

            def detect(self, frame: np.ndarray) -> list[Face]:
                """Faces in the middle of the frame, and a face cut by its right edge."""
                sdk.detected_shapes.append(tuple(frame.shape))
                height, width = frame.shape[:2]
                middle = Face((width / 4, height / 4, width / 8, height / 4))
                edge = Face((width - width / 16, height / 4, width / 8, height / 4))
                return [middle] * int(frame[0, 0, 0]) + [edge] * int(frame[0, 0, 1])

        modules = {name: types.ModuleType(name) for name in SDK_MODULES}
        modules["reachy_mini"].ReachyMini = ReachyMini  # type: ignore[attr-defined]
        modules["reachy_mini.utils"].create_head_pose = create_head_pose  # type: ignore[attr-defined]
        interpolation = modules["reachy_mini.utils.interpolation"]
        interpolation.linear_pose_interpolation = linear_pose_interpolation  # type: ignore[attr-defined]
        modules["reachy_mini.motion.recorded_move"].RecordedMoves = RecordedMoves  # type: ignore[attr-defined]
        modules["reachy_mini.vision.face_detector"].FaceDetector = FaceDetector  # type: ignore[attr-defined]
        media_manager = modules["reachy_mini.media.media_manager"]
        media_manager.MediaBackend = MediaBackend  # type: ignore[attr-defined]
        media_manager.MediaManager = MediaManager  # type: ignore[attr-defined]
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)


@dataclass
class FakeDaemonHttp:
    """The media part of a Reachy Mini daemon's HTTP API, on 127.0.0.1.

    Attributes:
        media: What ``GET /api/media/status`` answers.
        play_status: The HTTP status ``POST /api/media/play_sound`` answers.
        upload_delay: How long an upload takes, in seconds.
        uploads: The names of the files uploaded, in order.
        played: The daemon paths played, in order.
        log: ``upload``, ``play`` and ``stop`` as they happen.
    """

    media: dict[str, bool] = field(
        default_factory=lambda: {"available": True, "released": False, "no_media": False}
    )
    play_status: int = 200
    upload_delay: float = 0.0
    uploads: list[str] = field(default_factory=list)
    played: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    port: int = 0

    @contextmanager
    def serve(self) -> Iterator[FakeDaemonHttp]:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def answer(self, status: int, body: object) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/api/media/status":
                    self.answer(200, daemon.media)
                else:
                    self.answer(404, {"detail": "Not Found"})

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/api/media/sounds/upload":
                    time.sleep(daemon.upload_delay)
                    name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                    daemon.uploads.append(name)
                    daemon.log.append("upload")
                    self.answer(200, {"path": f"/tmp/reachy_mini_sounds/{name}"})
                elif self.path == "/api/media/play_sound":
                    if daemon.play_status != 200:
                        self.answer(daemon.play_status, {"detail": "Backend not running"})
                        return
                    daemon.played.append(json.loads(body)["file"])
                    daemon.log.append("play")
                    self.answer(200, {"status": "ok"})
                elif self.path == "/api/media/stop_sound":
                    daemon.log.append("stop")
                    self.answer(200, {"status": "ok"})
                else:
                    self.answer(404, {"detail": "Not Found"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            server.shutdown()
            server.server_close()
