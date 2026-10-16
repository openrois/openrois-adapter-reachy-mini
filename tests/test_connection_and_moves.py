"""The shared connection, SystemInformation, and the moves the Reaction plays."""

from __future__ import annotations

import asyncio
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
        "reaction": ReachyMiniReaction(connection),
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
