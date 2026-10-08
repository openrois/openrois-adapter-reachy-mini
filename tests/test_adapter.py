"""The adapter command, and two robots behind one gateway, driven by a client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from openrois.engine import Engine, WsClient, WsServer
from websockets.asyncio.client import ClientConnection, connect

from openrois.components.reachy_mini import (
    PiperSynthesizer,
    ReachyMiniPersonDetection,
    ReachyMiniReaction,
    ReachyMiniSpeechSynthesis,
    ReachyMiniSystemInformation,
    ToneSynthesizer,
)
from openrois_adapter_reachy_mini import Settings, build_engine
from openrois_adapter_reachy_mini.cli import parse_args
from tests._fakes import FakeSdk

COMPONENTS = ["person_detection", "reaction", "speech_synthesis", "system_information"]


def test_the_flags_fall_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROIS_ENGINE_ID", "reachy_real")
    monkeypatch.setenv("OPENROIS_REACHY_HOST", "192.168.1.20")
    monkeypatch.setenv("OPENROIS_REACHY_EMOTIONS", "1")
    settings, level = parse_args(["--connection-mode", "network"])
    assert settings == Settings(
        engine_id="reachy_real",
        host="192.168.1.20",
        connection_mode="network",
        emotions=True,
    )
    assert level == "info"
    assert isinstance(settings.synthesizer(), ToneSynthesizer)


def test_a_host_is_reached_over_the_network() -> None:
    settings, _ = parse_args(["--host", "192.168.1.20"])
    assert settings.connection_mode == "network"


def test_sim_is_the_simulator_on_this_machine() -> None:
    settings, _ = parse_args(["--sim", "--engine-id", "reachy_sim"])
    assert settings.connection_mode == "localhost_only"
    assert settings.engine_id == "reachy_sim"


def test_piper_voices_choose_piper(tmp_path: Path) -> None:
    (tmp_path / "en_US-lessac-medium.onnx").write_bytes(b"")
    settings, _ = parse_args(["--piper-voices", str(tmp_path), "--piper", "/opt/piper/bin/piper"])
    assert isinstance(settings.synthesizer(), PiperSynthesizer)


def test_an_empty_voices_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--piper-voices", str(tmp_path)])


def test_build_engine_hosts_the_four_components_on_one_connection() -> None:
    engine = build_engine(Settings(engine_id="reachy_sim", connection_mode="localhost_only"))
    components = engine.local_components()
    assert [type(c) for c in components] == [
        ReachyMiniSystemInformation,
        ReachyMiniReaction,
        ReachyMiniSpeechSynthesis,
        ReachyMiniPersonDetection,
    ]
    connections = {id(c._connection) for c in components}  # type: ignore[attr-defined]
    assert len(connections) == 1


class Client:
    """A RoIS client over JSON-RPC, as the HRI client talks to a gateway."""

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection
        self._ids = 0
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._ids += 1
        request = {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params}
        await self._connection.send(json.dumps(request))
        while True:
            message: dict[str, Any] = json.loads(await self._connection.recv())
            if message.get("id") == self._ids:
                assert "result" in message, message
                result: dict[str, Any] = message["result"]
                return result
            self.notifications.append((message["method"], message["params"]))

    async def wait(
        self, method: str, match: dict[str, Any], timeout: float = 5.0
    ) -> dict[str, Any]:
        """Wait for a notification whose params contain ``match``."""
        async with asyncio.timeout(timeout):
            while True:
                for name, params in self.notifications:
                    if name == method and match.items() <= params.items():
                        return params
                message = json.loads(await self._connection.recv())
                if "method" in message:
                    self.notifications.append((message["method"], message["params"]))


@pytest.fixture
async def gateway(sdk: FakeSdk) -> AsyncIterator[str]:
    """A gateway with the simulator and the Wireless behind it, as two adapters."""
    engine = Engine("lab")
    server = WsServer(engine, child_timeout=1.0)
    await server.start(port=0)
    url = f"ws://127.0.0.1:{server.port}"
    robots = [
        Settings(gateway_url=url, engine_id="reachy_sim", connection_mode="localhost_only"),
        Settings(gateway_url=url, engine_id="reachy_real", host="reachy-mini.local"),
    ]
    adapters = [asyncio.create_task(WsClient(build_engine(s), url).run_async()) for s in robots]
    try:
        async with asyncio.timeout(5.0):
            while sorted(engine.child_engine_ids()) != ["reachy_real", "reachy_sim"]:
                await asyncio.sleep(0.01)
        yield url
    finally:
        for adapter in adapters:
            adapter.cancel()
        await asyncio.gather(*adapters, return_exceptions=True)
        await server.stop()


async def test_a_client_sees_both_robots_with_their_four_components(gateway: str) -> None:
    async with connect(gateway) as connection:
        profile = (await Client(connection).call("rois.system.get_profile"))["profile"]
    robots = {p["identifier"]["code"]: p for p in profile["sub_profiles"]}
    assert sorted(robots) == ["reachy_real", "reachy_sim"]
    assert sorted(robots["reachy_sim"]["component_ids"]) == [
        f"reachy_sim/{name}" for name in COMPONENTS
    ]
    assert sorted(robots["reachy_real"]["component_ids"]) == [
        f"reachy_real/{name}" for name in COMPONENTS
    ]


async def test_the_simulator_speaks_while_it_reacts(gateway: str, sdk: FakeSdk) -> None:
    async with connect(gateway) as connection:
        client = Client(connection)
        for name in ("reaction", "speech_synthesis"):
            bound = await client.call("rois.command.bind", component_ref=f"reachy_sim/{name}")
            assert bound["return_code"] == "OK"
        result = await client.call(
            "rois.command.set_parameter",
            component_ref="reachy_sim/speech_synthesis",
            parameters=[{"name": "speech_text", "data_type_ref": "", "value": "Hi!"}],
        )
        await client.wait("rois.command.completed", {"command_id": result["command_id"]})
        group = {
            "command_list": [
                {
                    "component_ref": "reachy_sim/speech_synthesis",
                    "command_type": "start",
                    "command_id": "speak",
                },
                {
                    "component_ref": "reachy_sim/reaction",
                    "command_type": "start",
                    "command_id": "react",
                },
            ]
        }
        executed = await client.call("rois.command.execute", command_unit_list=[group])
        assert executed["return_code"] == "OK"
        for command_id in ("speak", "react"):
            done = await client.wait("rois.command.completed", {"command_id": command_id})
            assert done["status"] == "OK"
    sim = sdk.robot("localhost")
    assert len(sim.played) == 1 and sim.moves[0].finished
    assert sdk.robot("reachy-mini.local").moves == []


async def test_the_wireless_moves_while_the_simulator_runs(gateway: str, sdk: FakeSdk) -> None:
    async with connect(gateway) as connection:
        client = Client(connection)
        bound = await client.call("rois.command.bind", component_ref="reachy_real/reaction")
        assert bound["return_code"] == "OK"
        unit = {
            "component_ref": "reachy_real/reaction",
            "command_type": "start",
            "command_id": "react",
        }
        executed = await client.call("rois.command.execute", command_unit_list=[unit])
        assert executed["return_code"] == "OK"
        done = await client.wait("rois.command.completed", {"command_id": "react"})
        assert done["status"] == "OK"
    assert sdk.robot("reachy-mini.local").moves[0].finished
    assert sdk.robot("localhost").moves == []


async def test_the_wireless_reports_a_person(gateway: str, sdk: FakeSdk) -> None:
    async with connect(gateway) as connection:
        client = Client(connection)
        subscribed = await client.call(
            "rois.event.subscribe",
            event_type="person_detected",
            condition="component_ref = 'reachy_real/person_detection'",
        )
        assert subscribed["return_code"] == "OK"
        sdk.robot("reachy-mini.local").show(1)
        event = await client.wait("rois.event.notify_event", {"event_type": "person_detected"})
    values = {r["name"]: r["value"] for r in event["results"]}
    assert values["number"] == "1"
