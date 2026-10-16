"""The adapter command, and two robots behind one gateway, driven by a client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from openrois.engine import WsClient
from openrois.engine.config import ConfigError
from openrois.gateway.app import Gateway
from openrois.gateway.config import GatewayConfig
from websockets.asyncio.client import connect

from openrois.components.reachy_mini import (
    PiperSynthesizer,
    ReachyMiniConnection,
    ReachyMiniPersonDetection,
    ReachyMiniReaction,
    ReachyMiniSpeechSynthesis,
    ReachyMiniSystemInformation,
    ToneSynthesizer,
)
from openrois_adapter_reachy_mini import ReachyMiniConfig, build_engine
from openrois_adapter_reachy_mini.cli import load_config
from openrois_adapter_reachy_mini.workflow import Client, run_workflow
from tests._fakes import FakeSdk

COMPONENTS = ["person_detection", "reaction", "speech_synthesis", "system_information"]


def load(tmp_path: Path, yaml_text: str | None = None, *argv: str, **env: str) -> ReachyMiniConfig:
    args = list(argv)
    if yaml_text is not None:
        path = tmp_path / "adapter.yaml"
        path.write_text(yaml_text)
        args = ["--config", str(path), *args]
    return load_config(args, env)


def test_the_defaults_reach_the_wireless_on_the_network(tmp_path: Path) -> None:
    config = load(tmp_path)
    assert config.engine_id == "reachy_mini"
    assert config.robot_address == "reachy-mini.local"
    assert not config.simulator
    assert config.connection_mode() == "network"
    assert config.time_scale == 1.0
    assert isinstance(config.synthesizer(), ToneSynthesizer)


def test_adapter_yaml_sets_the_robot_and_its_components(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        "engine_id: reachy_sim\n"
        "simulator: true\n"
        "time_scale: 0.5\n"
        "connection:\n  retry: 1.5\n"
        "reaction:\n  emotions: true\n"
        "person_detection:\n  threshold: 0.8\n",
    )
    assert config.engine_id == "reachy_sim"
    assert config.connection_mode() == "localhost_only"
    assert config.connection.retry == 1.5
    assert config.reaction.emotions
    assert config.person_detection.threshold == 0.8
    assert config.time_scale == 0.5


def test_the_environment_and_the_flags_set_the_four_common_settings(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        "engine_id: from_file\nrobot_address: 10.0.0.1\n",
        "--engine-id",
        "reachy_real",
        OPENROIS_ADAPTER_ENGINE_ID="from_env",
        OPENROIS_ADAPTER_ROBOT_ADDRESS="192.168.1.20",
        OPENROIS_ADAPTER_GATEWAY_URL="ws://gateway:8765",
    )
    assert config.engine_id == "reachy_real"  # The flag wins over the environment.
    assert config.robot_address == "192.168.1.20"  # The environment wins over the file.
    assert config.gateway_url == "ws://gateway:8765"


def test_the_simulator_is_only_a_setting_of_the_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load(tmp_path, None, "--sim")


def test_an_unknown_setting_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="reaction.dance"):
        load(tmp_path, "reaction:\n  dance: true\n")
    with pytest.raises(ConfigError, match="time_scale"):
        load(tmp_path, "time_scale: 0\n")


def test_piper_voices_choose_piper(tmp_path: Path) -> None:
    (tmp_path / "en_US-lessac-medium.onnx").write_bytes(b"")
    config = load(
        tmp_path,
        f"speech_synthesis:\n  piper: /opt/piper/bin/piper\n  piper_voices: {tmp_path}\n",
    )
    assert isinstance(config.synthesizer(), PiperSynthesizer)


def test_an_empty_voices_folder_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "voices"
    empty.mkdir()
    with pytest.raises(ConfigError, match="Piper"):
        load(tmp_path, f"speech_synthesis:\n  piper_voices: {empty}\n")


def test_the_shipped_adapter_files_load() -> None:
    root = Path(__file__).parent.parent
    wireless = load_config(["--config", str(root / "adapter.yaml")], {})
    sim = load_config(["--config", str(root / "adapter.sim.yaml")], {})
    assert (wireless.engine_id, wireless.connection_mode()) == ("reachy_real", "network")
    assert (sim.engine_id, sim.connection_mode()) == ("reachy_sim", "localhost_only")


def test_time_scale_scales_every_duration_the_adapter_times() -> None:
    engine = build_engine(ReachyMiniConfig(engine_id="fast", time_scale=0.5))
    system, reaction, speech, detection = engine.local_components()
    [connection] = system.rois_resources()
    assert isinstance(connection, ReachyMiniConnection)
    assert connection.retry == pytest.approx(1.5)
    assert isinstance(speech, ReachyMiniSpeechSynthesis)
    assert speech._tail == pytest.approx(0.15)
    assert isinstance(detection, ReachyMiniPersonDetection)
    assert (detection._interval, detection._hold) == pytest.approx((0.1, 0.25))
    assert isinstance(reaction, ReachyMiniReaction)
    assert reaction.time_scale == 0.5


def test_build_engine_hosts_the_four_components_on_one_connection() -> None:
    engine = build_engine(ReachyMiniConfig(engine_id="reachy_sim", simulator=True))
    components = engine.local_components()
    assert [type(c) for c in components] == [
        ReachyMiniSystemInformation,
        ReachyMiniReaction,
        ReachyMiniSpeechSynthesis,
        ReachyMiniPersonDetection,
    ]
    resources = {id(r) for c in components for r in c.rois_resources()}
    assert len(resources) == 1


@pytest.fixture
async def gateway(sdk: FakeSdk) -> AsyncIterator[str]:
    """An openrois-gateway with the simulator and the Wireless behind it, as two adapters."""
    server = Gateway(GatewayConfig(engine_id="lab", port=0, child_timeout=1.0))
    await server.start()
    url = f"ws://127.0.0.1:{server.port}"
    robots = [
        ReachyMiniConfig(gateway_url=url, engine_id="reachy_sim", simulator=True),
        ReachyMiniConfig(gateway_url=url, engine_id="reachy_real"),
    ]
    adapters = [asyncio.create_task(WsClient(build_engine(s), url).run_async()) for s in robots]
    try:
        async with asyncio.timeout(5.0):
            while sorted(server.engine.child_engine_ids()) != ["reachy_real", "reachy_sim"]:
                await asyncio.sleep(0.01)
        yield url
    finally:
        for adapter in adapters:
            adapter.cancel()
        await asyncio.gather(*adapters, return_exceptions=True)
        await server.stop()


@pytest.fixture
async def client(gateway: str) -> AsyncIterator[Client]:
    """A service application connected to the gateway."""
    async with connect(gateway) as connection:
        app = Client(connection)
        app.start()
        await app.ok("rois.system.connect")
        try:
            yield app
        finally:
            await app.close()


def completed(command_id: str) -> Callable[[dict[str, Any]], bool]:
    return lambda params: params.get("command_id") == command_id


async def test_a_client_sees_both_robots_with_their_four_components(client: Client) -> None:
    profile = (await client.ok("rois.system.get_profile", condition=""))["profile"]
    robots = {p["identifier"]["code"]: p for p in profile["sub_profiles"]}
    assert sorted(robots) == ["reachy_real", "reachy_sim"]
    assert sorted(robots["reachy_sim"]["component_ids"]) == [
        f"reachy_sim/{name}" for name in COMPONENTS
    ]
    assert sorted(robots["reachy_real"]["component_ids"]) == [
        f"reachy_real/{name}" for name in COMPONENTS
    ]


async def test_the_simulator_speaks_while_it_reacts(client: Client, sdk: FakeSdk) -> None:
    for name in ("reaction", "speech_synthesis"):
        await client.ok("rois.command.bind", component_ref=f"reachy_sim/{name}")
    result = await client.ok(
        "rois.command.set_parameter",
        component_ref="reachy_sim/speech_synthesis",
        parameters=[{"name": "speech_text", "data_type_ref": "", "value": "Hi!"}],
    )
    await client.wait("rois.command.completed", completed(result["command_id"]), 5.0)
    group = {
        "branch_list": [
            {"command_list": [{"component_ref": f"reachy_sim/{name}", "command_type": "start"}]}
            for name in ("speech_synthesis", "reaction")
        ]
    }
    executed = await client.ok("rois.command.execute", command_unit_list=[group])
    [item] = executed["command_unit_list"]
    ids = [branch["command_list"][0]["command_id"] for branch in item["branch_list"]]
    for command_id in ids:
        done = await client.wait("rois.command.completed", completed(command_id), 5.0)
        assert done["status"] == "OK"
    sim = sdk.robot("localhost")
    assert len(sim.played) == 1 and sim.moves[0].finished
    assert sdk.robot("reachy-mini.local").moves == []


async def test_the_wireless_moves_while_the_simulator_runs(client: Client, sdk: FakeSdk) -> None:
    await client.ok("rois.command.bind", component_ref="reachy_real/reaction")
    unit = {"component_ref": "reachy_real/reaction", "command_type": "start"}
    executed = await client.ok("rois.command.execute", command_unit_list=[unit])
    [assigned] = executed["command_unit_list"]
    done = await client.wait("rois.command.completed", completed(assigned["command_id"]), 5.0)
    assert done["status"] == "OK"
    assert sdk.robot("reachy-mini.local").moves[0].finished
    assert sdk.robot("localhost").moves == []


async def test_the_wireless_reports_a_person(client: Client, sdk: FakeSdk) -> None:
    ref = "reachy_real/person_detection"
    await client.ok(
        "rois.event.subscribe", event_type="person_detected", condition=f"component_ref = '{ref}'"
    )
    await client.ok("rois.command.bind", component_ref=ref)
    sdk.robot("reachy-mini.local").show(1)
    await client.ok(
        "rois.command.execute", command_unit_list=[{"component_ref": ref, "command_type": "start"}]
    )
    event = await client.wait(
        "rois.event.notify_event", lambda p: p.get("event_type") == "person_detected", 5.0
    )
    values = {r["name"]: r["value"] for r in event["results"]}
    assert values["number"] == "1"


async def test_the_workflow_passes_in_simulation(gateway: str, sdk: FakeSdk) -> None:
    """Item 4: the end-to-end run, the same script the simulator and the Wireless run."""
    sdk.robot("localhost").show(0)

    async def stop() -> None:
        sdk.stop_daemon("localhost")

    async def start() -> None:
        sdk.start_daemon("localhost")

    steps = await run_workflow(
        gateway, "reachy_sim", stop_daemon=stop, start_daemon=start, timeout=10.0
    )
    assert [s.name for s in steps] == [
        "connect",
        "get_profile",
        "search",
        "bind",
        "set_parameter",
        "execute with a branch",
        "subscribe and notify_event",
        "notify_error and get_error_detail",
        "the daemon is back",
        "release",
        "disconnect",
    ]
    sim = sdk.robot("localhost")
    assert len(sim.played) == 1 and [m.finished for m in sim.moves] == [True]
    assert sdk.robot("reachy-mini.local").moves == []
