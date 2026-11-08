"""The shared connection, SystemInformation, and the moves the Reaction plays."""

from __future__ import annotations

import asyncio
import logging
import math
import sys

import pytest

from openrois.components.reachy_mini import (
    EXPRESSIONS,
    Keyframe,
    ReachyMiniConnection,
    ReachyMiniPersonDetection,
    ReachyMiniReaction,
    ReachyMiniSpeechSynthesis,
    ReachyMiniSystemInformation,
)
from openrois.components.reachy_mini.moves import REST_ANTENNAS, BlendedMove, KeyframeMove
from tests._fakes import FakeSdk, create_head_pose, linear_pose_interpolation, pitch_of
from tests.conftest import Host, Robot


def four(connection: ReachyMiniConnection) -> dict[str, object]:
    return {
        "system_information": ReachyMiniSystemInformation(connection),
        "reaction": ReachyMiniReaction(connection, time_scale=0.3),
        "speech_synthesis": ReachyMiniSpeechSynthesis(connection),
        "person_detection": ReachyMiniPersonDetection(connection),
    }


async def test_the_four_components_share_one_sdk_client(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host(four(connection))
    [client] = sdk.clients
    assert client.options["media_backend"] == "default"
    assert client.options["connection_mode"] == "localhost_only"
    assert connection.is_open
    await robot.engine.stop()
    assert client.closed
    assert not connection.is_open


async def test_two_robots_get_one_client_each(host: Host, sdk: FakeSdk) -> None:
    sim = ReachyMiniConnection(connection_mode="localhost_only")
    real = ReachyMiniConnection(host="reachy-mini.local", connection_mode="network")
    await host(four(sim), engine_id="reachy_sim")
    await host(four(real), engine_id="reachy_real")
    assert sorted(c.robot.host for c in sdk.open_clients()) == ["localhost", "reachy-mini.local"]


async def test_auto_tries_this_machine_first_as_the_sdk_does(host: Host, sdk: FakeSdk) -> None:
    # So a robot at a host is reached over the network, or the simulator wins over it.
    auto = ReachyMiniConnection(host="reachy-mini.local", connection_mode="auto")
    await host({"reaction": ReachyMiniReaction(auto)}, engine_id="first")
    sdk.stop_daemon("localhost")
    fallback = ReachyMiniConnection(host="reachy-mini.local", connection_mode="auto")
    await host({"reaction": ReachyMiniReaction(fallback)}, engine_id="second")
    assert [c.robot.host for c in sdk.clients] == ["localhost", "reachy-mini.local"]


async def ready(robot: Robot, name: str) -> None:
    """Wait until the watchdog reached the daemon and cleared the error of a component."""
    async with asyncio.timeout(2.0):
        while await robot.status(name) != "READY":
            await asyncio.sleep(0.01)


async def test_a_daemon_that_comes_back_is_reached_again(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01)
    robot = await host(four(connection))
    assert await robot.run("reaction", "start") == "OK"
    sdk.stop_daemon()
    assert await robot.run("reaction", "start") == "ERROR"
    sdk.start_daemon()
    await ready(robot, "reaction")
    assert await robot.run("reaction", "start") == "OK"
    # The client of the stopped daemon is disconnected in the background.
    async with asyncio.timeout(1.0):
        while not sdk.clients[0].closed:
            await asyncio.sleep(0.01)
    assert len(sdk.open_clients()) == 1


async def test_the_adapter_starts_before_the_daemon(host: Host, sdk: FakeSdk) -> None:
    sdk.stop_daemon()
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01)
    robot = await host(four(connection))
    assert connection.is_open
    assert await robot.status("reaction") == "ERROR"
    assert await robot.run("reaction", "start") == "ERROR"
    sdk.start_daemon()
    await ready(robot, "reaction")
    assert await robot.run("reaction", "start") == "OK"


async def test_without_the_sdk_the_components_are_in_error(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "reachy_mini", None)
    connection = ReachyMiniConnection()
    robot = await host({"system_information": ReachyMiniSystemInformation(connection)})
    result = await robot.call("rois.query.query", query_type="robot_position", condition="")
    assert result["return_code"] == "ERROR"
    assert not connection.is_open


async def test_robot_position_is_the_body_yaw(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host({"system_information": ReachyMiniSystemInformation(connection)})
    sdk.robot().body_yaw = 0.5
    values = await robot.query("system_information", "robot_position")
    assert values["position_data"] == '["0.0000,0.0000,0.5000"]'
    assert values["robot_ref"] == '["reachy"]'
    status = await robot.query("system_information", "engine_status")
    assert status["status"] == "READY"


def test_every_expression_starts_at_zero_and_ends_at_rest() -> None:
    for name, frames in EXPRESSIONS.items():
        assert frames[0].at == 0.0, name
        last = frames[-1]
        assert (last.roll, last.pitch, last.yaw, last.z) == (0, 0, 0, 0), name
        assert last.antennas == REST_ANTENNAS, name
        assert [f.at for f in frames] == sorted(f.at for f in frames), name


def test_a_keyframe_move_eases_between_its_keyframes() -> None:
    frames = (Keyframe(0.0), Keyframe(1.0, pitch=20, antennas=(-30.0, 30.0)), Keyframe(2.0))

    def head(frame: Keyframe) -> object:
        return create_head_pose(pitch=frame.pitch, mm=True)

    move = KeyframeMove(frames, head)
    assert move.duration == 2.0
    assert move.frame(0.5).pitch == pytest.approx(10.0)
    assert move.frame(0.25).pitch < 5.0  # Slow at the start of a segment.
    assert move.frame(1.0).pitch == pytest.approx(20.0)
    pose, antennas, body_yaw = move.evaluate(1.0)
    assert pitch_of(pose) == pytest.approx(20.0)
    assert antennas == pytest.approx((math.radians(-30), math.radians(30)))
    assert body_yaw is None
    with pytest.raises(ValueError):
        KeyframeMove((Keyframe(0.5),), head)


def test_a_blended_move_starts_from_the_current_pose() -> None:
    inner = KeyframeMove(
        (Keyframe(0.0, pitch=10), Keyframe(1.0, pitch=10)),
        lambda f: create_head_pose(pitch=f.pitch),
    )
    blended = BlendedMove(
        inner,
        head=create_head_pose(pitch=-20),
        antennas=(0.5, -0.5),
        body_yaw=0.3,
        blend=0.4,
        interpolate=linear_pose_interpolation,
    )
    assert blended.duration == pytest.approx(1.4)
    head, antennas, body_yaw = blended.evaluate(0.0)
    assert pitch_of(head) == pytest.approx(-20.0, abs=0.01)
    assert antennas == pytest.approx((0.5, -0.5))
    assert body_yaw is None  # The expression leaves the body where it is.
    head, _, _ = blended.evaluate(0.4)
    assert pitch_of(head) == pytest.approx(10.0, abs=0.01)


@pytest.mark.parametrize(
    "error",
    [
        KeyError("Producer reachymini not found."),
        RuntimeError("Failed to create webrtcsrc element"),
        OSError("[Errno 113] No route to host"),
    ],
    ids=["producer-not-found", "gstreamer", "no-route"],
)
async def test_any_error_while_connecting_again_is_retried(
    host: Host, sdk: FakeSdk, error: Exception
) -> None:
    # As a daemon that restarts: its state stream answers before its media are ready.
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01)
    robot = await host(four(connection))
    sdk.stop_daemon()
    assert await robot.run("reaction", "start") == "ERROR"
    sdk.connect_failures.append(error)
    sdk.start_daemon()
    await ready(robot, "reaction")
    assert not sdk.connect_failures
    assert await robot.run("reaction", "start") == "OK"
    await robot.engine.stop()  # The watchdog ends without an error.
    assert connection.current is None


async def test_an_error_on_the_first_connect_is_retried(host: Host, sdk: FakeSdk) -> None:
    sdk.connect_failures.append(KeyError("Producer reachymini not found."))
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01)
    robot = await host(four(connection))
    assert connection.is_open
    await ready(robot, "reaction")
    await ready(robot, "speech_synthesis")
    assert await robot.run("reaction", "start") == "OK"


async def test_the_watchdog_survives_a_failing_check(
    host: Host, sdk: FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01)
    robot = await host(four(connection))
    checks = 0

    def broken(mini: object) -> bool:
        nonlocal checks
        checks += 1
        raise RuntimeError("The SDK broke")

    monkeypatch.setattr("openrois.components.reachy_mini.connection._alive", broken)
    await asyncio.sleep(0.1)
    assert checks > 1  # Each failed check is followed by the next one.
    monkeypatch.undo()
    sdk.install(monkeypatch)
    sdk.stop_daemon()
    async with asyncio.timeout(2.0):
        while await robot.status("reaction") != "ERROR":
            await asyncio.sleep(0.01)
    sdk.start_daemon()
    await ready(robot, "reaction")


async def test_failed_media_leave_the_robot_moving(host: Host, sdk: FakeSdk) -> None:
    # The daemon answers, but its WebRTC signalling refuses: motion works, the camera
    # and the speaker do not, and the watchdog tries them again.
    sdk.media_failures.append(ConnectionRefusedError("[Errno 111] Connection refused"))
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.3)
    robot = await host(four(connection))
    assert await robot.status("speech_synthesis") == "ERROR"
    assert await robot.status("person_detection") == "ERROR"
    assert await robot.status("reaction") == "READY"
    assert await robot.run("reaction", "start") == "OK"
    # The next client has working media, and replaces the one without.
    await ready(robot, "speech_synthesis")
    await ready(robot, "person_detection")
    async with asyncio.timeout(1.0):
        while not sdk.clients[0].closed:
            await asyncio.sleep(0.01)
    assert connection.current is sdk.clients[1]
    assert await robot.set("speech_synthesis", speech_text="Hello") == "OK"
    assert await robot.run("speech_synthesis", "start") == "OK"


async def test_media_that_keep_failing_raise_error_on_the_media_components_only(
    host: Host, sdk: FakeSdk, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01)
    robot = await host(four(connection))
    sdk.stop_daemon()
    await asyncio.sleep(0.05)
    sdk.media_failures.extend(KeyError("Producer reachymini not found.") for _ in range(1000))
    sdk.start_daemon()
    async with asyncio.timeout(2.0):
        while await robot.status("reaction") != "READY":
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    assert await robot.status("speech_synthesis") == "ERROR"
    assert await robot.status("person_detection") == "ERROR"
    assert await robot.status("reaction") == "READY"
    assert "camera and speaker do not work" in caplog.text
    assert "KeyError" in caplog.text
    assert await robot.run("reaction", "start") == "OK"
    # The client in use stays, and every client tried meanwhile is let go.
    async with asyncio.timeout(1.0):
        while len(sdk.open_clients()) > 1:
            await asyncio.sleep(0.01)
    assert connection.current is sdk.clients[1]


async def test_a_robot_without_wifi_address_never_streams_this_machine(
    host: Host, sdk: FakeSdk, caplog: pytest.LogCaptureFixture
) -> None:
    # The SDK would fetch the media from this machine, the simulator's, without the
    # robot's Wi-Fi address.
    robot_media = sdk.robot("192.168.1.42")
    robot_media.wlan_ip = None
    connection = ReachyMiniConnection(host="192.168.1.42", connection_mode="network", retry=60.0)
    with caplog.at_level(logging.WARNING):
        robot = await host(four(connection))
    assert await robot.status("person_detection") == "ERROR"
    assert await robot.status("speech_synthesis") == "ERROR"
    assert await robot.status("reaction") == "READY"
    assert "no Wi-Fi address" in caplog.text
    assert connection.current.media.audio is None


async def test_a_short_pause_of_the_state_stream_changes_nothing(host: Host, sdk: FakeSdk) -> None:
    # Over Wi-Fi the state stream pauses now and then, its connection still open.
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01, lost_after=0.5)
    robot = await host(four(connection))
    sdk.stall()
    await asyncio.sleep(0.2)
    sdk.stall(stalled=False)
    await asyncio.sleep(0.1)
    assert robot.sent("rois.system.notify_error") == []
    assert len(sdk.clients) == 1
    assert await robot.run("reaction", "start") == "OK"


async def test_a_long_pause_of_the_state_stream_loses_the_daemon(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01, lost_after=0.2)
    robot = await host(four(connection))
    sdk.stall()
    async with asyncio.timeout(2.0):
        while not robot.sent("rois.system.notify_error"):
            await asyncio.sleep(0.01)
    await ready(robot, "reaction")
    assert len(sdk.clients) == 2
    assert sdk.clients[0].closed


@pytest.mark.parametrize(
    ("host_name", "warned"),
    [
        ("reachy-mini.local", False),
        ("192.168.1.42", False),
        ("fe80::1", False),
        ("reachy-mini", True),
        ("reachy-mini.lan", True),
    ],
)
def test_a_name_the_daemon_refuses_is_warned_about(
    host_name: str, warned: bool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        ReachyMiniConnection(host=host_name, connection_mode="network")
    assert ("IP address" in caplog.text) is warned
