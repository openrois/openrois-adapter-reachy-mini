"""PersonDetection: the number of faces in view, reported once it holds, from start to stop."""

from __future__ import annotations

import asyncio

from openrois.components.reachy_mini import ReachyMiniConnection, ReachyMiniPersonDetection
from openrois.components.reachy_mini.person_detection import Debounce
from tests._fakes import FakeSdk
from tests.conftest import Host, Robot


async def detection(
    host: Host, sdk: FakeSdk, faces: int | None, *, retry: float = 3.0, start: bool = True
) -> Robot:
    sdk.robot().show(faces)
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=retry)
    component = ReachyMiniPersonDetection(connection, interval=0.01, hold=0.05, threshold=0.7)
    robot = await host({"person_detection": component})
    await robot.subscribe("person_detection", "person_detected")
    if start:
        assert await robot.run("person_detection", "start") == "OK"
    return robot


async def wait_for_events(robot: Robot, count: int) -> list[dict[str, str]]:
    async with asyncio.timeout(2.0):
        while len(robot.events()) < count:
            await asyncio.sleep(0.01)
    return robot.events()


def test_serves_the_four_common_commands_component_status_and_person_detected() -> None:
    profile = ReachyMiniPersonDetection.rois_profile()
    assert [c.name for c in profile.command_profiles] == ["start", "stop", "suspend", "resume"]
    assert [q.name for q in profile.query_profiles] == ["component_status"]
    assert [e.name for e in profile.event_profiles] == ["person_detected"]
    assert profile.function == "sensing"


def test_debounce_reports_a_value_once_it_holds() -> None:
    debounce = Debounce(0.5)
    assert debounce.update(0, 0.0) is None
    assert debounce.update(0, 0.5) == 0
    assert debounce.update(1, 0.6) is None
    assert debounce.update(0, 0.7) is None  # The face flickered away again.
    assert debounce.update(1, 0.8) is None
    assert debounce.update(1, 1.3) == 1
    assert debounce.update(1, 2.0) is None  # Reported once.


async def test_reports_the_faces_in_view_and_their_changes(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=1)
    [first] = await wait_for_events(robot, 1)
    assert first["number"] == "1"
    assert first["timestamp"]
    sdk.robot().show(2)
    events = await wait_for_events(robot, 2)
    assert events[1]["number"] == "2"
    sdk.robot().show(0)
    events = await wait_for_events(robot, 3)
    assert events[2]["number"] == "0"


async def test_frames_are_scaled_to_320_pixels(host: Host, sdk: FakeSdk) -> None:
    await detection(host, sdk, faces=1)
    async with asyncio.timeout(2.0):
        while not sdk.detected_shapes:
            await asyncio.sleep(0.01)
    assert sdk.detected_shapes[0] == (180, 320, 3)
    assert sdk.detector_thresholds == [0.7]


async def test_without_frames_nothing_is_reported(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=None)
    await asyncio.sleep(0.2)
    assert robot.events() == []
    status = await robot.query("person_detection", "component_status")
    assert status["status"] == "READY"


async def test_the_count_resumes_when_the_daemon_comes_back(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=1, retry=0.01)
    await wait_for_events(robot, 1)
    sdk.stop_daemon()
    await asyncio.sleep(0.1)
    sdk.robot().show(2)
    sdk.start_daemon()
    events = await wait_for_events(robot, 2)
    assert events[1]["number"] == "2"
    assert len(sdk.open_clients()) == 1


async def test_the_detector_stops_with_the_engine(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=1)
    await wait_for_events(robot, 1)
    await robot.engine.stop()
    shapes = len(sdk.detected_shapes)
    await asyncio.sleep(0.1)
    assert len(sdk.detected_shapes) == shapes
    assert sdk.open_clients() == []


async def test_nothing_is_detected_before_start(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=1, start=False)
    await asyncio.sleep(0.2)
    assert robot.events() == []
    assert sdk.detected_shapes == []
    assert await robot.run("person_detection", "start") == "OK"
    [event] = await wait_for_events(robot, 1)
    assert event["number"] == "1"


async def test_a_start_while_running_reports_the_current_count_once(
    host: Host, sdk: FakeSdk
) -> None:
    robot = await detection(host, sdk, faces=1)
    await wait_for_events(robot, 1)
    sdk.robot().show(2)
    events = await wait_for_events(robot, 2)
    assert await robot.run("person_detection", "start") == "OK"
    events = await wait_for_events(robot, 3)
    assert [e["number"] for e in events] == ["1", "2", "2"]
    await asyncio.sleep(0.2)
    assert len(robot.events()) == 3


async def test_a_start_before_the_first_count_reports_it_once(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=None)
    assert await robot.run("person_detection", "start") == "OK"
    sdk.robot().show(1)
    [event] = await wait_for_events(robot, 1)
    assert event["number"] == "1"
    await asyncio.sleep(0.2)
    assert len(robot.events()) == 1


async def test_stop_ends_detection_and_a_new_start_reports_again(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=1)
    await wait_for_events(robot, 1)
    assert await robot.run("person_detection", "stop") == "OK"
    shapes = len(sdk.detected_shapes)
    await asyncio.sleep(0.1)
    assert len(sdk.detected_shapes) == shapes
    # The count starts from scratch, so the same count is reported again.
    assert await robot.run("person_detection", "start") == "OK"
    events = await wait_for_events(robot, 2)
    assert events[1]["number"] == "1"


async def test_suspend_holds_the_events_and_resume_reports_a_change(
    host: Host, sdk: FakeSdk
) -> None:
    robot = await detection(host, sdk, faces=1)
    await wait_for_events(robot, 1)
    assert await robot.run("person_detection", "suspend") == "OK"
    sdk.robot().show(3)
    await asyncio.sleep(0.2)
    assert len(robot.events()) == 1
    assert await robot.run("person_detection", "resume") == "OK"
    events = await wait_for_events(robot, 2)
    assert events[1]["number"] == "3"


async def test_resume_without_a_change_reports_nothing(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=1)
    await wait_for_events(robot, 1)
    assert await robot.run("person_detection", "suspend") == "OK"
    await asyncio.sleep(0.1)
    assert await robot.run("person_detection", "resume") == "OK"
    await asyncio.sleep(0.2)
    assert len(robot.events()) == 1


async def test_suspend_and_resume_before_start_do_nothing(host: Host, sdk: FakeSdk) -> None:
    robot = await detection(host, sdk, faces=1, start=False)
    assert await robot.run("person_detection", "suspend") == "OK"
    assert await robot.run("person_detection", "resume") == "OK"
    await asyncio.sleep(0.1)
    assert robot.events() == []
    assert sdk.detected_shapes == []
