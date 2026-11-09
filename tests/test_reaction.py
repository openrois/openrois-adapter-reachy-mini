"""Reaction: expressions and recorded moves, played from the current pose, and stopped."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from openrois.components.reachy_mini import (
    EMOTIONS_LIBRARY,
    EXPRESSIONS,
    ReachyMiniConnection,
    ReachyMiniReaction,
)
from tests._fakes import FakeSdk, pitch_of
from tests.conftest import Host, Robot


async def reaction(host: Host, library: str | None = None, *, time_scale: float = 0.5) -> Robot:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    component = ReachyMiniReaction(connection, library=library, time_scale=time_scale)
    return await host({"reaction": component})


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
    assert sdk.robot().played == []
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
    assert await robot.set("reaction", reaction_ref="cartwheel") == "BAD_PARAMETER"
    assert await robot.run("reaction", "start") == "OK"  # Still the default.


async def test_stop_ends_the_reaction_where_it_is(host: Host, sdk: FakeSdk) -> None:
    robot = await reaction(host, time_scale=1.0)
    await robot.set("reaction", reaction_ref="sad")
    start = await robot.execute("reaction", "start")
    await asyncio.sleep(0.2)
    assert await robot.run("reaction", "stop") == "OK"
    assert await robot.completed(start) == "ABORT"
    targets = len(sdk.robot().head_targets)
    assert targets > 0
    await asyncio.sleep(0.1)
    assert len(sdk.robot().head_targets) == targets


async def test_a_new_start_replaces_the_running_one(host: Host, sdk: FakeSdk) -> None:
    robot = await reaction(host, time_scale=1.0)
    first = await robot.execute("reaction", "start")
    await asyncio.sleep(0.1)
    second = await robot.execute("reaction", "start")
    assert await robot.completed(first) == "ABORT"
    assert await robot.completed(second) == "OK"
    assert abs(pitch_of(sdk.robot().head)) < 0.5


async def test_a_send_that_blocks_leaves_the_adapter_responsive(host: Host, sdk: FakeSdk) -> None:
    # As over a Wi-Fi that stalls: the SDK's send blocks until the link comes back.
    gate = threading.Event()
    sdk.robot().send_gate = gate
    robot = await reaction(host, time_scale=1.0)
    try:
        start = await robot.execute("reaction", "start")
        await asyncio.sleep(0.1)
        began = time.monotonic()
        assert await robot.status("reaction") == "READY"
        assert await robot.run("reaction", "stop") == "OK"
        assert await robot.completed(start) == "ABORT"
        assert time.monotonic() - began < 2.0
    finally:
        gate.set()


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
    assert sdk.robot().played == []  # The move has a sound, which never plays.
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
    move = component._move("nod", connection.current)
    assert move.duration == pytest.approx(0.5 * (0.4 + EXPRESSIONS["nod"][-1].at))
    began = time.monotonic()
    assert await robot.run("reaction", "start") == "OK"
    assert move.duration - 0.05 <= time.monotonic() - began < move.duration + 1.0
    assert max(pitch_of(p) for p in sdk.robot().head_targets) > 10.0
