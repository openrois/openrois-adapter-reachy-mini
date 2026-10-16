"""Reaction: expressions and recorded moves, played from the current pose, and stopped."""

from __future__ import annotations

import asyncio
import json

import pytest

from openrois.components.reachy_mini import (
    EMOTIONS_LIBRARY,
    EXPRESSIONS,
    ReachyMiniConnection,
    ReachyMiniReaction,
)
from tests._fakes import FakeSdk, pitch_of
from tests.conftest import Host, Robot


async def reaction(host: Host, library: str | None = None) -> Robot:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    return await host({"reaction": ReachyMiniReaction(connection, library=library)})


def test_serves_the_common_commands_and_available_reactions() -> None:
    profile = ReachyMiniReaction.rois_profile()
    assert [c.name for c in profile.command_profiles] == ["start", "stop", "suspend", "resume"]
    assert [q.name for q in profile.query_profiles] == ["component_status", "available_reactions"]
    assert profile.function == "actuation"
    [reaction_ref] = profile.parameter_profiles
    assert reaction_ref.default_value == "nod"


async def test_start_plays_the_default_reaction_without_sound(host: Host, sdk: FakeSdk) -> None:
    robot = await reaction(host)
    assert await robot.run("reaction", "start") == "OK"
    [played] = sdk.robot().moves
    assert played.finished and not played.sound
    assert played.move.duration > EXPRESSIONS["nod"][-1].at  # The blend comes first.
    # The nod looks down, and the head comes back to rest.
    assert max(pitch_of(p) for p in sdk.robot().head_targets) > 10.0
    assert abs(pitch_of(sdk.robot().head)) < 0.5


async def test_a_reaction_is_chosen_with_reaction_ref(host: Host, sdk: FakeSdk) -> None:
    robot = await reaction(host)
    assert await robot.set("reaction", reaction_ref="sad") == "OK"
    assert await robot.run("reaction", "start") == "OK"
    assert max(pitch_of(p) for p in sdk.robot().head_targets) > 20.0


async def test_an_unknown_reaction_is_refused(host: Host, sdk: FakeSdk) -> None:
    robot = await reaction(host)
    assert await robot.set("reaction", reaction_ref="cartwheel") == "ERROR"
    assert await robot.run("reaction", "start") == "OK"  # Still the default.


async def test_stop_ends_the_reaction_where_it_is(host: Host, sdk: FakeSdk) -> None:
    sdk.speed = 1.0
    robot = await reaction(host)
    await robot.set("reaction", reaction_ref="sad")
    start = await robot.execute("reaction", "start")
    await asyncio.sleep(0.2)
    assert await robot.run("reaction", "stop") == "OK"
    assert await robot.completed(start) == "ABORT"
    [played] = sdk.robot().moves
    assert played.cancelled and not played.finished
    targets = len(sdk.robot().head_targets)
    await asyncio.sleep(0.1)
    assert len(sdk.robot().head_targets) == targets


async def test_a_new_start_replaces_the_running_one(host: Host, sdk: FakeSdk) -> None:
    sdk.speed = 1.0
    robot = await reaction(host)
    first = await robot.execute("reaction", "start")
    await asyncio.sleep(0.1)
    sdk.speed = 50.0
    second = await robot.execute("reaction", "start")
    assert await robot.completed(first) == "ABORT"
    assert await robot.completed(second) == "OK"
    assert [m.finished for m in sdk.robot().moves] == [False, True]


async def test_available_reactions_lists_the_expressions(host: Host, sdk: FakeSdk) -> None:
    robot = await reaction(host)
    values = await robot.query("reaction", "available_reactions")
    assert json.loads(values["available_reactions"]) == list(EXPRESSIONS)


async def test_the_emotions_library_adds_its_moves_played_muted(host: Host, sdk: FakeSdk) -> None:
    robot = await reaction(host, library=EMOTIONS_LIBRARY)
    assert sdk.libraries == [EMOTIONS_LIBRARY]
    values = await robot.query("reaction", "available_reactions")
    assert json.loads(values["available_reactions"]) == [*EXPRESSIONS, "cheerful1", "sad2"]
    assert await robot.set("reaction", reaction_ref="cheerful1") == "OK"
    assert await robot.run("reaction", "start") == "OK"
    [played] = sdk.robot().moves
    assert played.move.sound_path is not None and not played.sound
    assert sdk.robot().body_yaw > 0.0  # A recorded move turns the body too.


async def test_without_the_library_the_expressions_remain(host: Host, sdk: FakeSdk) -> None:
    sdk.library_fails = True
    robot = await reaction(host, library=EMOTIONS_LIBRARY)
    values = await robot.query("reaction", "available_reactions")
    assert json.loads(values["available_reactions"]) == list(EXPRESSIONS)
    assert await robot.run("reaction", "start") == "OK"


async def test_time_scale_plays_the_reactions_faster(host: Host, sdk: FakeSdk) -> None:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    component = ReachyMiniReaction(connection, blend=0.4, time_scale=0.5)
    robot = await host({"reaction": component})
    assert await robot.run("reaction", "start") == "OK"
    [played] = sdk.robot().moves
    assert played.move.duration == pytest.approx(0.5 * (0.4 + EXPRESSIONS["nod"][-1].at))
    assert max(pitch_of(p) for p in sdk.robot().head_targets) > 10.0
