"""The RoIS components of the Reachy Mini platform.

Each class is a basic RoIS HRI Component (RoIS 2.0, section 8.4) expressed
with the OpenRoIS adapter SDK decorators. They speak to the robot only
through the :class:`~openrois_adapter_reachy_mini.backend.Backend` they are
given, so the simulator and the hardware share every line here.

The head and body pose is the target space of Navigation: a Reachy Mini does
not drive, it looks. A target position is a JSON object such as
``{"head": {"pitch": -10, "yaw": 30}, "body_yaw": 15, "antennas": [20, 20]}``
with millimeters and degrees.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from openrois.interfaces.bus import InvokeResponse
from openrois.interfaces.hri import Result, ReturnCode
from openrois_components_core import component, invoke, query, results, subscribe

from openrois_adapter_reachy_mini.backend import Backend, HeadPose

logger = logging.getLogger(__name__)

# Named expressions: a sequence of (head, body_yaw, antennas, seconds).
Step = tuple[HeadPose | None, float | None, tuple[float, float] | None, float]
REACTIONS: dict[str, list[Step]] = {
    "neutral": [(HeadPose(), 0.0, (0.0, 0.0), 0.8)],
    "nod": [(HeadPose(pitch=-15), None, None, 0.3), (HeadPose(pitch=10), None, None, 0.3),
            (HeadPose(), None, None, 0.3)],
    "shake": [(HeadPose(yaw=-25), None, None, 0.3), (HeadPose(yaw=25), None, None, 0.4),
              (HeadPose(), None, None, 0.3)],
    "curious": [(HeadPose(roll=15, z=5), None, (30.0, -30.0), 0.5),
                (HeadPose(), None, (0.0, 0.0), 0.6)],
    "happy": [(HeadPose(z=8, pitch=-5), None, (45.0, 45.0), 0.3),
              (HeadPose(z=0), None, (-20.0, -20.0), 0.3),
              (HeadPose(z=8, pitch=-5), None, (45.0, 45.0), 0.3),
              (HeadPose(), None, (0.0, 0.0), 0.4)],
    "sad": [(HeadPose(pitch=20, z=-8), None, (-60.0, -60.0), 1.0),
            (HeadPose(), None, (0.0, 0.0), 1.0)],
    "wave_antennas": [(None, None, (60.0, -60.0), 0.2), (None, None, (-60.0, 60.0), 0.2),
                      (None, None, (60.0, -60.0), 0.2), (None, None, (0.0, 0.0), 0.3)],
}


def _param(parameters: list[Any], name: str, default: str = "") -> str:
    """The value of a named parameter, from the dict shape the engine passes."""
    for p in parameters or []:
        pname = p.get("name") if isinstance(p, dict) else getattr(p, "name", None)
        if pname == name:
            value = p.get("value", default) if isinstance(p, dict) else getattr(p, "value", default)
            return str(value)
    return default


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _Counter:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n}"


# --- SystemInformation ---------------------------------------------------------

@component("SystemInformation")
class SystemInformation:
    """Where the robot is looking, and whether it is operable."""

    def __init__(self, config: dict[str, Any], backend: Backend) -> None:
        self._backend = backend
        self._id = str(config.get("engine", {}).get("id", "reachy_mini"))

    @query("robot_position")
    async def robot_position(self) -> list[Result]:
        # A Reachy Mini sits still: its "position" is its body yaw at the origin.
        state = self._backend.state()
        return results.robot_position(
            x=0.0, y=0.0, theta=state.body_yaw, timestamp=_now(), robot_ref=[self._id],
        )

    @query("engine_status")
    async def engine_status(self) -> list[Result]:
        return results.engine_status(status="READY", operable_time=["", ""])

    @query("component_status")
    async def status(self) -> list[Result]:
        return results.status("READY")


# --- Navigation ----------------------------------------------------------------

@component(
    "Navigation",
    function="actuation",
    parameters=[
        {"name": "target_positions", "data_type_ref": "string[]", "default_value": "[]"},
        {"name": "time_limit", "data_type_ref": "int", "default_value": "5"},
        {"name": "routing_policy", "data_type_ref": "string", "default_value": "direct"},
    ],
)
class Navigation:
    """Moves the head and body to target poses, one after the other."""

    def __init__(self, config: dict[str, Any], backend: Backend) -> None:
        self._backend = backend
        self._speed = float(config.get("speed", 1.0))
        self._targets: list[dict[str, Any]] = []
        self._time_limit = 5
        self._policy = "direct"
        self._task: asyncio.Task[None] | None = None
        self._paused = asyncio.Event()
        self._paused.set()
        self._ids = _Counter("nav")

    @query("component_status")
    async def status(self) -> list[Result]:
        busy = self._task is not None and not self._task.done()
        return results.status("BUSY" if busy else "READY")

    @query("get_parameter")
    async def get_parameter(self) -> list[Result]:
        return [
            Result(name="target_positions", data_type_ref="string[]",
                   value=json.dumps(self._targets)),
            Result(name="time_limit", data_type_ref="int", value=str(self._time_limit)),
            Result(name="routing_policy", data_type_ref="string", value=self._policy),
        ]

    @invoke("set_parameter")
    async def set_parameter(self, parameters: list[Any]) -> InvokeResponse:
        raw = _param(parameters, "target_positions", json.dumps(self._targets))
        try:
            targets = json.loads(raw) if raw else []
            if isinstance(targets, dict):
                targets = [targets]
            targets = [json.loads(t) if isinstance(t, str) else t for t in targets]
            if not all(isinstance(t, dict) for t in targets):
                raise ValueError("each target must be an object")
        except ValueError as exc:
            logger.warning("Bad target_positions %r: %s", raw, exc)
            return InvokeResponse(return_code=ReturnCode.BAD_PARAMETER, command_id="")
        self._targets = targets
        self._time_limit = int(_param(parameters, "time_limit", str(self._time_limit)) or 5)
        self._policy = _param(parameters, "routing_policy", self._policy)
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("start")
    async def start(self, parameters: list[Any]) -> InvokeResponse:
        return await self.execute(parameters)

    @invoke("execute")
    async def execute(self, parameters: list[Any]) -> InvokeResponse:
        if self._task is not None and not self._task.done():
            return InvokeResponse(return_code=ReturnCode.ERROR, command_id="")
        if parameters:
            answer = await self.set_parameter(parameters)
            if answer.return_code != ReturnCode.OK:
                return answer
        command_id = self._ids.next()
        self._task = asyncio.get_running_loop().create_task(self._run(command_id))
        return InvokeResponse(return_code=ReturnCode.OK, command_id=command_id)

    async def _run(self, command_id: str) -> None:
        targets = list(self._targets)
        try:
            for index, target in enumerate(targets):
                await self._paused.wait()
                head = HeadPose.from_dict(target["head"]) if "head" in target else None
                antennas = target.get("antennas")
                await self._backend.move_to(
                    head=head,
                    body_yaw=float(target["body_yaw"]) if "body_yaw" in target else None,
                    antennas=(float(antennas[0]), float(antennas[1])) if antennas else None,
                    duration=max(0.1, float(target.get("duration", 1.0)) / self._speed),
                )
                await self.parent.emit_async(  # type: ignore[attr-defined]
                    "Navigation", "reached_target",
                    results.reached_target(
                        target=json.dumps(target), is_final_target=index == len(targets) - 1,
                    ),
                )
            await self.parent.complete_async(command_id, "OK")  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            await self.parent.complete_async(command_id, "ABORT")  # type: ignore[attr-defined]
            raise

    @invoke("stop")
    async def stop(self, parameters: list[Any]) -> InvokeResponse:
        if self._task is not None and not self._task.done():
            self._backend.stop_motion()
            self._task.cancel()
        self._paused.set()
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("suspend")
    async def suspend(self, parameters: list[Any]) -> InvokeResponse:
        self._paused.clear()
        self._backend.stop_motion()
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("resume")
    async def resume(self, parameters: list[Any]) -> InvokeResponse:
        self._paused.set()
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @subscribe("reached_target")
    async def on_reached(self) -> None:
        pass


# --- Reaction ------------------------------------------------------------------

@component(
    "Reaction",
    function="actuation",
    parameters=[{"name": "reaction_ref", "data_type_ref": "string", "default_value": "neutral"}],
)
class Reaction:
    """Performs a named expression with the head and the antennas."""

    def __init__(self, config: dict[str, Any], backend: Backend) -> None:
        self._backend = backend
        self._reaction = "neutral"
        self._task: asyncio.Task[None] | None = None
        self._ids = _Counter("react")

    @query("component_status")
    async def status(self) -> list[Result]:
        busy = self._task is not None and not self._task.done()
        return results.status("BUSY" if busy else "READY")

    @query("get_parameter")
    async def get_parameter(self) -> list[Result]:
        return [Result(name="reaction_ref", data_type_ref="string", value=self._reaction)]

    @invoke("set_parameter")
    async def set_parameter(self, parameters: list[Any]) -> InvokeResponse:
        reaction = _param(parameters, "reaction_ref", self._reaction)
        if reaction not in REACTIONS:
            return InvokeResponse(return_code=ReturnCode.BAD_PARAMETER, command_id="")
        self._reaction = reaction
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("start")
    async def start(self, parameters: list[Any]) -> InvokeResponse:
        return await self.execute(parameters)

    @invoke("execute")
    async def execute(self, parameters: list[Any]) -> InvokeResponse:
        if self._task is not None and not self._task.done():
            return InvokeResponse(return_code=ReturnCode.ERROR, command_id="")
        if parameters:
            answer = await self.set_parameter(parameters)
            if answer.return_code != ReturnCode.OK:
                return answer
        command_id = self._ids.next()
        self._task = asyncio.get_running_loop().create_task(self._perform(command_id))
        return InvokeResponse(return_code=ReturnCode.OK, command_id=command_id)

    async def _perform(self, command_id: str) -> None:
        logger.info("Reaction: %s", self._reaction)
        try:
            for head, body_yaw, antennas, seconds in REACTIONS[self._reaction]:
                await self._backend.move_to(head, body_yaw, antennas, duration=seconds)
            await self.parent.complete_async(command_id, "OK")  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            await self.parent.complete_async(command_id, "ABORT")  # type: ignore[attr-defined]
            raise

    @invoke("stop")
    async def stop(self, parameters: list[Any]) -> InvokeResponse:
        if self._task is not None and not self._task.done():
            self._backend.stop_motion()
            self._task.cancel()
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("suspend")
    async def suspend(self, parameters: list[Any]) -> InvokeResponse:
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("resume")
    async def resume(self, parameters: list[Any]) -> InvokeResponse:
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")


# --- SpeechSynthesis -----------------------------------------------------------

@component(
    "SpeechSynthesis",
    function="actuation",
    parameters=[
        {"name": "speech_text", "data_type_ref": "string", "default_value": ""},
        {"name": "SSML_text", "data_type_ref": "string", "default_value": ""},
        {"name": "volume", "data_type_ref": "int", "default_value": "100"},
        {"name": "language", "data_type_ref": "string", "default_value": "en"},
        {"name": "character", "data_type_ref": "string", "default_value": "default"},
    ],
)
class SpeechSynthesis:
    """Speaks through the robot's speaker. The command completes when the speech ends."""

    def __init__(self, config: dict[str, Any], backend: Backend) -> None:
        self._backend = backend
        self._params = {
            "speech_text": "", "SSML_text": "", "volume": "100",
            "language": str(config.get("language", "en")), "character": "default",
        }
        self._task: asyncio.Task[None] | None = None
        self._ids = _Counter("say")

    @query("component_status")
    async def status(self) -> list[Result]:
        busy = self._task is not None and not self._task.done()
        return results.status("BUSY" if busy else "READY")

    @query("get_parameter")
    async def get_parameter(self) -> list[Result]:
        types = {"volume": "int"}
        return [
            Result(name=k, data_type_ref=types.get(k, "string"), value=v)
            for k, v in self._params.items()
        ]

    @invoke("set_parameter")
    async def set_parameter(self, parameters: list[Any]) -> InvokeResponse:
        for name in self._params:
            self._params[name] = _param(parameters, name, self._params[name])
        try:
            volume = int(self._params["volume"] or 100)
        except ValueError:
            return InvokeResponse(return_code=ReturnCode.BAD_PARAMETER, command_id="")
        if not 0 <= volume <= 100:
            return InvokeResponse(return_code=ReturnCode.BAD_PARAMETER, command_id="")
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("start")
    async def start(self, parameters: list[Any]) -> InvokeResponse:
        return await self.execute(parameters)

    @invoke("execute")
    async def execute(self, parameters: list[Any]) -> InvokeResponse:
        if self._task is not None and not self._task.done():
            return InvokeResponse(return_code=ReturnCode.ERROR, command_id="")
        if parameters:
            answer = await self.set_parameter(parameters)
            if answer.return_code != ReturnCode.OK:
                return answer
        text = self._params["speech_text"] or self._params["SSML_text"]
        command_id = self._ids.next()
        self._task = asyncio.get_running_loop().create_task(self._speak(command_id, text))
        return InvokeResponse(return_code=ReturnCode.OK, command_id=command_id)

    async def _speak(self, command_id: str, text: str) -> None:
        try:
            await self._backend.say(text, volume=int(self._params["volume"] or 100))
            await self.parent.complete_async(command_id, "OK")  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            await self.parent.complete_async(command_id, "ABORT")  # type: ignore[attr-defined]
            raise

    @invoke("stop")
    async def stop(self, parameters: list[Any]) -> InvokeResponse:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("suspend")
    async def suspend(self, parameters: list[Any]) -> InvokeResponse:
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("resume")
    async def resume(self, parameters: list[Any]) -> InvokeResponse:
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")


# --- SpeechRecognition ---------------------------------------------------------

@component(
    "SpeechRecognition",
    function="sensing",
    parameters=[
        {"name": "languages", "data_type_ref": "string[]", "default_value": '["en"]'},
        {"name": "grammar", "data_type_ref": "string", "default_value": ""},
        {"name": "rule", "data_type_ref": "string", "default_value": ""},
    ],
)
class SpeechRecognition:
    """Listens while someone subscribes, and reports each utterance as speech_recognized."""

    def __init__(self, config: dict[str, Any], backend: Backend) -> None:
        self._backend = backend
        self._listening: asyncio.Task[None] | None = None
        self._subscribers = 0

    @query("component_status")
    async def status(self) -> list[Result]:
        return results.status("BUSY" if self._subscribers else "READY")

    @query("get_parameter")
    async def get_parameter(self) -> list[Result]:
        return [
            Result(name="languages", data_type_ref="string[]", value='["en"]'),
            Result(name="grammar", data_type_ref="string", value=""),
            Result(name="rule", data_type_ref="string", value=""),
        ]

    @invoke("set_parameter")
    async def set_parameter(self, parameters: list[Any]) -> InvokeResponse:
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @subscribe("speech_recognized")
    async def on_speech(self) -> None:
        self._subscribers += 1
        if self._listening is None or self._listening.done():
            self._listening = asyncio.get_running_loop().create_task(self._listen())

    async def _listen(self) -> None:
        while True:
            heard = await self._backend.listen(timeout=1.0)
            if heard is None:
                continue
            logger.info("Heard: %s", heard)
            await self.parent.emit_async(  # type: ignore[attr-defined]
                "SpeechRecognition", "speech_recognized",
                [
                    Result(name="timestamp", data_type_ref="DateTime", value=_now()),
                    Result(name="recognized_text", data_type_ref="string[]",
                           value=json.dumps([heard])),
                ],
            )

    def close(self) -> None:
        if self._listening is not None:
            self._listening.cancel()


# --- PersonDetection and FaceDetection -----------------------------------------

class _Watcher:
    """Polls the camera while subscribed and emits an event when the count changes."""

    ref = ""
    event = ""
    count_name = ""

    def __init__(self, config: dict[str, Any], backend: Backend) -> None:
        self._backend = backend
        self._interval = float(config.get("poll_interval", 0.5))
        self._task: asyncio.Task[None] | None = None
        self._last = -1

    async def _count(self) -> int:
        raise NotImplementedError

    @query("component_status")
    async def status(self) -> list[Result]:
        return results.status("READY")

    async def _watch(self) -> None:
        while True:
            count = await self._count()
            if count != self._last:
                self._last = count
                await self.parent.emit_async(  # type: ignore[attr-defined]
                    self.ref, self.event,
                    [
                        Result(name="timestamp", data_type_ref="DateTime", value=_now()),
                        Result(name=self.count_name, data_type_ref="int", value=str(count)),
                    ],
                )
            await asyncio.sleep(self._interval)

    def _start(self) -> None:
        if self._task is None or self._task.done():
            self._last = -1
            self._task = asyncio.get_running_loop().create_task(self._watch())

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()


@component("PersonDetection", function="sensing")
class PersonDetection(_Watcher):
    """Reports how many people are in view, whenever the number changes."""

    ref = "PersonDetection"
    event = "person_detected"
    count_name = "number_of_people"

    async def _count(self) -> int:
        return await self._backend.people_in_view()

    @subscribe("person_detected")
    async def on_person(self) -> None:
        self._start()


@component("FaceDetection", function="sensing")
class FaceDetection(_Watcher):
    """Reports how many faces are in view, whenever the number changes."""

    ref = "FaceDetection"
    event = "face_detected"
    count_name = "number_of_faces"

    async def _count(self) -> int:
        return await self._backend.faces_in_view()

    @subscribe("face_detected")
    async def on_face(self) -> None:
        self._start()


# --- VideoStreaming ------------------------------------------------------------

@component(
    "VideoStreaming",
    function="function",
    parameters=[
        {"name": "encoding_parameters", "data_type_ref": "string", "default_value": ""},
        {"name": "transport_parameters", "data_type_ref": "string", "default_value": "whep"},
    ],
)
class VideoStreaming:
    """Controls the camera stream. The media travels from the camera URL, not through RoIS."""

    def __init__(self, config: dict[str, Any], backend: Backend) -> None:
        self._backend = backend
        self._url = str(config.get("camera_url", "")) or backend.camera_url()
        self._params = {"encoding_parameters": "", "transport_parameters": "whep"}
        self._streams: dict[str, str] = {}
        self._ids = _Counter("video")

    @query("component_status")
    async def status(self) -> list[Result]:
        return results.status("READY")

    @query("get_parameter")
    async def get_parameter(self) -> list[Result]:
        return [Result(name=k, data_type_ref="string", value=v) for k, v in self._params.items()]

    @invoke("set_parameter")
    async def set_parameter(self, parameters: list[Any]) -> InvokeResponse:
        for name in self._params:
            self._params[name] = _param(parameters, name, self._params[name])
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("connect_stream")
    async def connect_stream(self, parameters: list[Any]) -> InvokeResponse:
        stream_id = self._ids.next()
        self._streams[stream_id] = "STREAMING_RUNNING"
        await self._emit_status(stream_id)
        return InvokeResponse(
            return_code=ReturnCode.OK, command_id=stream_id,
            results=[
                Result(name="stream_id", data_type_ref="string", value=stream_id),
                Result(name="media_url", data_type_ref="string", value=self._url),
            ],
        )

    async def _emit_status(self, stream_id: str) -> None:
        status = self._streams.get(stream_id, "STREAMING_NOT_CONNECTED")
        await self.parent.emit_async(  # type: ignore[attr-defined]
            "VideoStreaming", "notify_stream_status",
            [
                Result(name="stream_id", data_type_ref="string", value=stream_id),
                Result(name="timestamp", data_type_ref="DateTime", value=_now()),
                Result(name="status", data_type_ref="Stream_Status", value=status),
            ],
        )

    async def _transition(self, parameters: list[Any], status: str) -> InvokeResponse:
        stream_id = _param(parameters, "stream_id")
        if stream_id not in self._streams:
            return InvokeResponse(return_code=ReturnCode.BAD_PARAMETER, command_id="")
        if status == "STREAMING_NOT_CONNECTED":
            self._streams.pop(stream_id)
        else:
            self._streams[stream_id] = status
        await self._emit_status(stream_id)
        return InvokeResponse(return_code=ReturnCode.OK, command_id="")

    @invoke("disconnect_stream")
    async def disconnect_stream(self, parameters: list[Any]) -> InvokeResponse:
        return await self._transition(parameters, "STREAMING_NOT_CONNECTED")

    @invoke("suspend_stream")
    async def suspend_stream(self, parameters: list[Any]) -> InvokeResponse:
        return await self._transition(parameters, "STREAMING_SUSPENDED")

    @invoke("resume_stream")
    async def resume_stream(self, parameters: list[Any]) -> InvokeResponse:
        return await self._transition(parameters, "STREAMING_RESUMED")

    @query("get_stream_status")
    async def get_stream_status(self, stream_id: str) -> list[Result]:
        status = self._streams.get(stream_id, "STREAMING_NOT_CONNECTED")
        return [Result(name="status", data_type_ref="Stream_Status", value=status)]

    @subscribe("notify_stream_status")
    async def on_status(self) -> None:
        pass


COMPONENT_CLASSES: list[type] = [
    SystemInformation,
    Navigation,
    Reaction,
    SpeechSynthesis,
    SpeechRecognition,
    PersonDetection,
    FaceDetection,
    VideoStreaming,
]
