"""The fake SDK, and an engine that hosts Reachy Mini components with a session of it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from openrois.components.core import Component
from openrois.engine import Engine, Session
from openrois.interfaces.catalog import METHODS_BY_NAME
from pydantic import BaseModel

from tests._fakes import FakeSdk


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> FakeSdk:
    """The fake Reachy Mini SDK, installed as the modules the components import."""
    fake = FakeSdk()
    fake.install(monkeypatch)
    return fake


@dataclass
class Robot:
    """A started engine with Reachy Mini components, and a trusted session of it."""

    engine: Engine
    refs: dict[str, str]
    notifications: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    session: Session | None = None
    _ids: int = 0

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        assert self.session is not None
        model = METHODS_BY_NAME[method].params.model_validate(params)
        result = await self.engine.handle(self.session, method, model)
        dumped: dict[str, Any] = result.model_dump(mode="json")
        return dumped

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.notifications if name == method]

    async def completed(self, command_id: str, timeout: float = 5.0) -> str:
        """Wait for the completion of a command and return its status."""
        async with asyncio.timeout(timeout):
            while True:
                for params in self.sent("rois.command.completed"):
                    if params["command_id"] == command_id:
                        status: str = params["status"]
                        return status
                await asyncio.sleep(0.005)

    async def execute(self, name: str, command_type: str) -> str:
        """Run a command on a component, and return its command id at once."""
        self._ids += 1
        command_id = f"c{self._ids}"
        unit = {
            "component_ref": self.refs[name],
            "command_type": command_type,
            "command_id": command_id,
        }
        result = await self.call("rois.command.execute", command_unit_list=[unit])
        assert result["return_code"] == "OK", result
        return command_id

    async def run(self, name: str, command_type: str) -> str:
        """Run a command on a component until it ends, and return its status."""
        return await self.completed(await self.execute(name, command_type))

    async def set(self, name: str, **values: str) -> str:
        """Set parameters of a component, and return the status of the change."""
        parameters = [{"name": k, "data_type_ref": "", "value": v} for k, v in values.items()]
        result = await self.call(
            "rois.command.set_parameter", component_ref=self.refs[name], parameters=parameters
        )
        assert result["return_code"] == "OK", result
        return await self.completed(result["command_id"])

    async def query(self, name: str, query_type: str) -> dict[str, str]:
        """The results of a query, by name."""
        condition = f"component_ref = '{self.refs[name]}'"
        result = await self.call("rois.query.query", query_type=query_type, condition=condition)
        assert result["return_code"] == "OK", result
        return {r["name"]: r["value"] for r in result["results"]}

    async def subscribe(self, name: str, event_type: str) -> None:
        condition = f"component_ref = '{self.refs[name]}'"
        result = await self.call("rois.event.subscribe", event_type=event_type, condition=condition)
        assert result["return_code"] == "OK", result

    def events(self) -> list[dict[str, str]]:
        """The results of every event sent so far, by name."""
        return [
            {r["name"]: r["value"] for r in params["results"]}
            for params in self.sent("rois.event.notify_event")
        ]


type Host = Callable[..., Awaitable[Robot]]


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    """Host components in a started engine. The engines stop after the test."""
    engines: list[Engine] = []

    async def start(components: Mapping[str, Component], engine_id: str = "reachy") -> Robot:
        engine = Engine(engine_id)
        refs = {name: engine.add_component(name, c) for name, c in components.items()}
        await engine.start()
        engines.append(engine)
        robot = Robot(engine, refs)

        def notify(method: str, params: BaseModel) -> None:
            robot.notifications.append((method, params.model_dump(mode="json")))

        robot.session = engine.open_session(notify, trusted=True)
        return robot

    yield start
    for engine in engines:
        await engine.stop()
