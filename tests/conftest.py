from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from openrois_core import Engine, EventEmitter

from openrois_adapter_reachy_mini import SimulatedBackend, build_engine

PROFILE: dict[str, Any] = {
    "engine": {"id": "reachy_mini_1", "platform": "reachy-mini"},
    "robot": {"simulate": True, "time_scale": 0.0},
    "components": {
        "PersonDetection": {"poll_interval": 0.01},
        "FaceDetection": {"poll_interval": 0.01},
    },
}


class Sink:
    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def __call__(self, envelope: Any) -> None:
        self.envelopes.append(envelope)

    def events(self, event_type: str) -> list[Any]:
        return [e for e in self.envelopes if e.event_type == event_type]

    def completions(self) -> dict[str, str]:
        return {
            e.payload[0].value: str(e.completed_status.value)
            for e in self.envelopes if e.event_type == "completed"
        }


@pytest.fixture
def backend() -> SimulatedBackend:
    return SimulatedBackend(time_scale=0.0)


@pytest.fixture
async def engine(backend: SimulatedBackend) -> AsyncIterator[Engine]:
    engine = build_engine(PROFILE, backend)
    engine.component_registry.set_emitter(EventEmitter(asyncio.get_running_loop()))
    yield engine
    for handler in engine.component_registry._handlers.values():
        close = getattr(handler, "close", None)
        if close:
            close()


@pytest.fixture
def sink() -> Sink:
    return Sink()
