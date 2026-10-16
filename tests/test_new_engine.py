"""The components on the merged engine: the contract, the shared connection and its watchdog.

Each test is named after an item of the Stream A1 brief of the OpenRoIS First Release.
"""

from __future__ import annotations

import asyncio

from openrois.components.core import Component, Resource
from openrois.interfaces.common import ComponentStatus
from openrois.interfaces.components import (
    PERSON_DETECTION_PROFILE,
    REACTION_PROFILE,
    SPEECH_SYNTHESIS_PROFILE,
    SYSTEM_INFORMATION_PROFILE,
)
from openrois.interfaces.profiles import HRIComponentProfile

from openrois.components.reachy_mini import (
    ReachyMiniConnection,
    ReachyMiniPersonDetection,
    ReachyMiniReaction,
    ReachyMiniSpeechSynthesis,
    ReachyMiniSystemInformation,
    ToneSynthesizer,
)
from tests._fakes import FakeSdk, pitch_of
from tests.conftest import Host, Robot

FAST_TONE = ToneSynthesizer(seconds_per_character=0.01, min_seconds=0.05)
ACTING = ("reaction", "speech_synthesis", "person_detection")


def four(connection: ReachyMiniConnection) -> dict[str, object]:
    return {
        "system_information": ReachyMiniSystemInformation(connection),
        "reaction": ReachyMiniReaction(connection),
        "speech_synthesis": ReachyMiniSpeechSynthesis(connection, FAST_TONE, tail=0.0),
        "person_detection": ReachyMiniPersonDetection(connection, interval=0.01, hold=0.02),
    }


async def until(check: object, timeout: float = 2.0) -> None:
    """Wait until ``check()`` is true."""
    assert callable(check)
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.005)


# -- Item 1: onto dev's engine -----------------------------------------------------------


async def test_set_parameter_applies_before_its_reply(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host({"reaction": ReachyMiniReaction(connection)})
    parameters = [{"name": "reaction_ref", "data_type_ref": "", "value": "sad"}]
    result = await robot.call(
        "rois.command.set_parameter", component_ref=robot.refs["reaction"], parameters=parameters
    )
    assert result["return_code"] == "OK"
    # No wait for the completed notification: the reply means the value is applied.
    assert await robot.run("reaction", "start") == "OK"
    assert max(pitch_of(p) for p in sdk.robot().head_targets) > 20.0
    assert await robot.completed(result["command_id"]) == "OK"


async def test_set_parameter_inside_execute_applies_before_the_next_command(
    host: Host, sdk: FakeSdk
) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host({"reaction": ReachyMiniReaction(connection)})
    ref = robot.refs["reaction"]
    sequence = [
        {
            "component_ref": ref,
            "command_type": "set_parameter",
            "arguments": [{"name": "reaction_ref", "data_type_ref": "", "value": "sad"}],
        },
        {"component_ref": ref, "command_type": "start"},
    ]
    result = await robot.call("rois.command.execute", command_unit_list=sequence)
    assert result["return_code"] == "OK", result
    for unit in result["command_unit_list"]:
        assert await robot.completed(unit["command_id"]) == "OK"
    assert max(pitch_of(p) for p in sdk.robot().head_targets) > 20.0


async def test_command_ids_come_from_the_engine(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host({"reaction": ReachyMiniReaction(connection)})
    unit = {"component_ref": robot.refs["reaction"], "command_type": "start", "command_id": "mine"}
    result = await robot.call("rois.command.execute", command_unit_list=[unit])
    assert result["return_code"] == "OK"
    [assigned] = result["command_unit_list"]
    assert assigned["command_id"] != "mine"
    assert assigned["command_id"].startswith("reachy/")
    assert await robot.completed(assigned["command_id"]) == "OK"


def test_profiles_are_the_generated_standard_constants() -> None:
    declared: dict[type[Component], HRIComponentProfile] = {
        ReachyMiniSystemInformation: SYSTEM_INFORMATION_PROFILE,
        ReachyMiniReaction: REACTION_PROFILE,
        ReachyMiniSpeechSynthesis: SPEECH_SYNTHESIS_PROFILE,
        ReachyMiniPersonDetection: PERSON_DETECTION_PROFILE,
    }
    for cls, constant in declared.items():
        served = cls.rois_profile()
        assert served.identifier == constant.identifier, cls
        assert served.function == constant.function, cls
        assert served.sub_component_profiles == constant.sub_component_profiles, cls
        for kind in ("command_profiles", "query_profiles", "event_profiles"):
            standard = {m.name: m for m in getattr(constant, kind)}
            for message in getattr(served, kind):
                assert message == standard[message.name], (cls, message.name)
        assert [p.name for p in served.parameter_profiles] == [
            p.name for p in constant.parameter_profiles
        ], cls


def test_the_reaction_profile_only_adds_the_default_of_reaction_ref() -> None:
    served = ReachyMiniReaction.rois_profile()
    [standard] = REACTION_PROFILE.parameter_profiles
    [reaction_ref] = served.parameter_profiles
    assert reaction_ref.default_value == "nod"
    assert reaction_ref.model_copy(update={"default_value": standard.default_value}) == standard


async def test_component_status_comes_from_the_engine_and_the_raised_status(
    host: Host, sdk: FakeSdk
) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host(four(connection))
    for name in ACTING:
        assert await robot.status(name) == "READY"
    # No component answers component_status itself, and SystemInformation has none.
    assert "component_status" not in {
        q.name for q in ReachyMiniSystemInformation.rois_profile().query_profiles
    }
    robot.engine.raise_status(ComponentStatus.WARNING, "battery low")
    status = await robot.query("system_information", "engine_status")
    assert status["status"] == "WARNING"
    robot.engine.clear_status()
    status = await robot.query("system_information", "engine_status")
    assert status["status"] == "READY"


# -- Item 2: the connection is a Resource, with a watchdog -------------------------------


async def test_the_connection_is_a_resource_the_engine_opens_once(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    assert isinstance(connection, Resource)
    components = four(connection)
    for component in components.values():
        assert list(component.rois_resources()) == [connection]  # type: ignore[attr-defined]
    robot = await host(components)
    [client] = sdk.clients
    assert connection.is_open
    await robot.engine.stop()
    assert client.closed
    assert not connection.is_open


async def wait_for_errors(robot: Robot, count: int) -> list[dict[str, str]]:
    await until(lambda: len(robot.sent("rois.system.notify_error")) >= count)
    return robot.sent("rois.system.notify_error")


async def test_the_watchdog_raises_error_on_the_four_components_while_the_daemon_is_gone(
    host: Host, sdk: FakeSdk
) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.02)
    robot = await host(four(connection))
    sdk.stop_daemon()
    errors = await wait_for_errors(robot, 4)
    assert {e["error_type"] for e in errors} == {"COMPONENT_NOT_RESPONDING"}
    for name in ACTING:
        assert await robot.status(name) == "ERROR"
    # A component in ERROR refuses its commands and queries.
    result = await robot.call(
        "rois.query.query",
        query_type="robot_position",
        condition=f"component_ref = '{robot.refs['system_information']}'",
    )
    assert result["return_code"] == "ERROR"
    assert await robot.run("reaction", "start") == "ERROR"
    detail = await robot.call(
        "rois.system.get_error_detail", error_id=errors[0]["error_id"], condition=""
    )
    assert detail["return_code"] == "OK"
    values = {r["name"]: r["value"] for r in detail["results"]}
    assert values["status"] == "ERROR"
    assert "localhost:8000" in values["message"]

    sdk.start_daemon()
    await until(lambda: connection.current is not None and not sdk.down)
    for name in ACTING:
        async with asyncio.timeout(2.0):
            while await robot.status(name) != "READY":
                await asyncio.sleep(0.01)
    assert await robot.run("reaction", "start") == "OK"
    assert len(sdk.open_clients()) == 1


async def test_the_watchdog_raises_once_per_loss(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=0.01)
    robot = await host({"reaction": ReachyMiniReaction(connection)})
    sdk.stop_daemon()
    await wait_for_errors(robot, 1)
    await asyncio.sleep(0.1)
    assert len(robot.sent("rois.system.notify_error")) == 1


async def test_the_daemon_is_tried_once_every_retry(host: Host, sdk: FakeSdk) -> None:
    sdk.stop_daemon()
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=60.0)
    robot = await host({"reaction": ReachyMiniReaction(connection)})
    sdk.start_daemon()
    # The attempt of the open failed less than a minute ago, so the robot stays in ERROR.
    await asyncio.sleep(0.1)
    assert await robot.status("reaction") == "ERROR"
    assert sdk.clients == []


async def test_a_lost_daemon_seen_by_a_command_wakes_the_watchdog(host: Host, sdk: FakeSdk) -> None:
    # The watchdog checks every minute, but a command that finds the daemon gone does
    # not wait for it: the components go to ERROR at once.
    connection = ReachyMiniConnection(connection_mode="localhost_only", retry=60.0)
    robot = await host({"reaction": ReachyMiniReaction(connection)})
    sdk.stop_daemon()
    assert await robot.run("reaction", "start") == "ERROR"
    await wait_for_errors(robot, 1)
    assert await robot.status("reaction") == "ERROR"


# -- Item 3: the components follow the Closing CP1 to CP3 table --------------------------


def test_system_information_serves_robot_position_and_engine_status_only() -> None:
    profile = ReachyMiniSystemInformation.rois_profile()
    assert profile.command_profiles == []
    assert [q.name for q in profile.query_profiles] == ["robot_position", "engine_status"]
    assert profile.event_profiles == []
    assert profile.sub_component_profiles == []


async def test_a_missing_command_answers_unsupported(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host(
        {
            "reaction": ReachyMiniReaction(connection),
            "speech_synthesis": ReachyMiniSpeechSynthesis(connection, FAST_TONE),
        }
    )
    for name in ("reaction", "speech_synthesis"):
        unit = {"component_ref": robot.refs[name], "command_type": "fly"}
        result = await robot.call("rois.command.execute", command_unit_list=[unit])
        assert result["return_code"] == "UNSUPPORTED", name


async def test_the_four_common_commands_are_served_by_the_engine_default(
    host: Host, sdk: FakeSdk
) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host(
        {
            "reaction": ReachyMiniReaction(connection),
            "speech_synthesis": ReachyMiniSpeechSynthesis(connection, FAST_TONE),
        }
    )
    for name in ("reaction", "speech_synthesis"):
        for command in ("suspend", "resume", "stop"):
            assert await robot.run(name, command) == "OK", (name, command)
    # A suspended Reaction holds its start until the resume.
    assert await robot.run("reaction", "start") == "OK"
    assert await robot.run("reaction", "suspend") == "OK"
    start = await robot.execute("reaction", "start")
    await asyncio.sleep(0.1)
    assert len(sdk.robot().moves) == 1
    assert await robot.run("reaction", "resume") == "OK"
    assert await robot.completed(start) == "OK"
    assert len(sdk.robot().moves) == 2


async def test_stop_cancels_the_running_start_which_ends_with_abort(
    host: Host, sdk: FakeSdk
) -> None:
    sdk.speed = 1.0
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    robot = await host({"reaction": ReachyMiniReaction(connection)})
    await robot.set("reaction", reaction_ref="sad")
    start = await robot.execute("reaction", "start")
    await until(lambda: bool(sdk.robot().moves))
    assert await robot.run("reaction", "stop") == "OK"
    assert await robot.completed(start) == "ABORT"
    assert sdk.robot().moves[0].cancelled
