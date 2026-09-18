"""Each component against the simulated robot, through the engine's dispatch."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from openrois.interfaces.common import COMPONENT_STATUS_MAP, ComponentStatus
from openrois_core import Engine

from openrois_adapter_reachy_mini import HeadPose, SimulatedBackend
from tests.conftest import Sink

E = "reachy_mini_1"


async def settle(sink: Sink, command_id: str, timeout: float = 2.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if command_id in sink.completions():
            return sink.completions()[command_id]
        await asyncio.sleep(0.01)
    raise AssertionError(f"{command_id} never completed")


async def execute(engine: Engine, sink: Sink, component: str, **params: str) -> dict[str, Any]:
    ref = f"{E}/{component}"
    await engine.dispatch("rois.command.bind", {"component_ref": ref}, sink, "app")
    return await engine.dispatch(
        "rois.command.execute",
        {
            "component_ref": ref, "command_type": "execute",
            "parameters": [
                {"name": k, "data_type_ref": "string", "value": v} for k, v in params.items()
            ],
        },
        sink, "app",
    )


async def test_navigation_moves_the_head_and_reports_each_target(
    engine: Engine, backend: SimulatedBackend, sink: Sink,
) -> None:
    ref = f"{E}/Navigation"
    await engine.dispatch(
        "rois.event.subscribe",
        {"component_ref": ref, "event_type": "reached_target"}, sink, "app",
    )
    targets = [{"head": {"yaw": 30, "pitch": -10}, "duration": 0.2},
               {"head": {"yaw": 0}, "body_yaw": 20, "antennas": [10, -10], "duration": 0.2}]
    executed = await execute(engine, sink, "Navigation", target_positions=json.dumps(targets))
    assert executed["return_code"] == "OK"
    assert await settle(sink, executed["command_id"]) == "OK"
    reached = sink.events("reached_target")
    assert [json.loads(e.payload[0].value) for e in reached] == targets
    assert [e.payload[1].value for e in reached] == ["false", "true"]
    state = backend.state()
    assert state.head == HeadPose(yaw=0.0)
    assert (state.body_yaw, state.antennas) == (20.0, (10.0, -10.0))


async def test_navigation_rejects_a_malformed_target(engine: Engine, sink: Sink) -> None:
    executed = await execute(engine, sink, "Navigation", target_positions="not json")
    assert executed["return_code"] == "BAD_PARAMETER"


async def test_navigation_stop_aborts_the_command(
    engine: Engine, backend: SimulatedBackend, sink: Sink,
) -> None:
    backend.time_scale = 1.0  # a long move, so stop lands while it runs
    ref = f"{E}/Navigation"
    far = json.dumps([{"head": {"yaw": 40}, "duration": 5}])
    executed = await execute(engine, sink, "Navigation", target_positions=far)
    await asyncio.sleep(0.05)
    status = await engine.dispatch(
        "rois.query.query", {"component_ref": ref, "query_type": "component_status"}, sink, "app",
    )
    assert status["results"][0]["value"] == str(COMPONENT_STATUS_MAP[ComponentStatus("BUSY")])
    stopped = await engine.dispatch(
        "rois.command.execute", {"component_ref": ref, "command_type": "stop"}, sink, "app",
    )
    assert stopped["return_code"] == "OK"
    assert await settle(sink, executed["command_id"]) == "ABORT"
    assert abs(backend.state().head.yaw) < 40


async def test_reaction_performs_a_named_expression(
    engine: Engine, backend: SimulatedBackend, sink: Sink,
) -> None:
    executed = await execute(engine, sink, "Reaction", reaction_ref="nod")
    assert await settle(sink, executed["command_id"]) == "OK"
    assert len(backend.motions) == 3
    assert backend.state().head == HeadPose()
    unknown = await execute(engine, sink, "Reaction", reaction_ref="backflip")
    assert unknown["return_code"] == "BAD_PARAMETER"


async def test_speech_synthesis_speaks_and_completes(
    engine: Engine, backend: SimulatedBackend, sink: Sink,
) -> None:
    executed = await execute(engine, sink, "SpeechSynthesis", speech_text="Hello", volume="50")
    assert await settle(sink, executed["command_id"]) == "OK"
    assert backend.spoken == ["Hello"]
    loud = await execute(engine, sink, "SpeechSynthesis", speech_text="Hi", volume="500")
    assert loud["return_code"] == "BAD_PARAMETER"


async def test_speech_recognition_reports_what_the_robot_hears(
    engine: Engine, backend: SimulatedBackend, sink: Sink,
) -> None:
    ref = f"{E}/SpeechRecognition"
    sub = await engine.dispatch(
        "rois.event.subscribe",
        {"component_ref": ref, "event_type": "speech_recognized"}, sink, "app",
    )
    assert sub["return_code"] == "OK"
    backend.hear("turn left")
    for _ in range(50):
        if sink.events("speech_recognized"):
            break
        await asyncio.sleep(0.01)
    event = sink.events("speech_recognized")[0]
    assert {r.name: r.value for r in event.payload}["recognized_text"] == '["turn left"]'


async def test_person_and_face_detection_report_changes(
    engine: Engine, backend: SimulatedBackend, sink: Sink,
) -> None:
    watchers = (("PersonDetection", "person_detected"), ("FaceDetection", "face_detected"))
    for component, event in watchers:
        await engine.dispatch(
            "rois.event.subscribe", {"component_ref": f"{E}/{component}", "event_type": event},
            sink, "app",
        )

    async def seen(how_many: int) -> None:
        for _ in range(200):
            if all(len(sink.events(e)) >= how_many for _, e in watchers):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("detection events never arrived")

    await seen(1)  # the count at subscription time
    backend.show(people=2)
    await seen(2)  # the change
    assert counts(sink, "person_detected", "number_of_people")[:2] == ["0", "2"]
    assert counts(sink, "face_detected", "number_of_faces")[:2] == ["0", "2"]


def counts(sink: Sink, event: str, name: str) -> list[str]:
    return [r.value for e in sink.events(event) for r in e.payload if r.name == name]


async def test_video_stream_control_plane(engine: Engine, sink: Sink) -> None:
    connected = await engine.dispatch(
        "rois.stream.connect_stream", {"component_ref": f"{E}/VideoStreaming"}, sink, "app",
    )
    assert connected["return_code"] == "OK"
    stream_id = connected["stream_id"]
    urls = [r["value"] for r in connected["results"] if r["name"] == "media_url"]
    assert urls and urls[0].startswith("http")

    async def op(name: str) -> str:
        answer = await engine.dispatch(f"rois.stream.{name}", {"stream_id": stream_id})
        return str(answer.get("return_code", answer.get("status")))

    status = await engine.dispatch("rois.stream.query_stream_status", {"stream_id": stream_id})
    assert status["status"] == "STREAMING_RUNNING"
    assert await op("suspend_stream") == "OK"
    assert sink.events("notify_stream_status")[-1].stream_status == "STREAMING_SUSPENDED"
    assert await op("disconnect_stream") == "OK"


async def test_system_information_reports_the_body_yaw(
    engine: Engine, backend: SimulatedBackend, sink: Sink,
) -> None:
    await backend.move_to(body_yaw=12.5, duration=0.1)
    position = await engine.dispatch(
        "rois.query.query",
        {"component_ref": f"{E}/SystemInformation", "query_type": "robot_position"},
        sink, "app",
    )
    values = {r["name"]: r["value"] for r in position["results"]}
    assert json.loads(values["position_data"]) == ["0.0", "0.0", "12.5"]
