"""One command for the whole stack: a gateway, the adapter, and optionally a demo.

    openrois-reachy-mini-up                # gateway + simulated robot on ws://127.0.0.1:8765
    openrois-reachy-mini-up --robot        # the real robot through its SDK
    openrois-reachy-mini-up --demo         # also run a short application, then exit

Point the web inspector (examples/hri-client in the openrois repository) or any
SDK at the gateway URL that is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import websockets
from openrois_core import Engine, EventEmitter, WsClient, WsServer, read_profile

from openrois_adapter_reachy_mini.adapter import backend_from_profile, build_engine

logger = logging.getLogger("openrois.reachy_mini.up")
DEFAULT_PROFILE = Path(__file__).resolve().parent / "openrois-profile.yaml"


class _App:
    """A minimal JSON-RPC application, enough for the demo."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._n = 0
        self.notifications: list[dict[str, Any]] = []

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._n += 1
        await self._ws.send(json.dumps(
            {"jsonrpc": "2.0", "id": self._n, "method": method, "params": params},
        ))
        while True:
            msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=30))
            if msg.get("id") == self._n:
                return dict(msg["result"])
            self.notifications.append(msg)

    async def completed(self, command_id: str) -> str:
        while True:
            for msg in list(self.notifications):
                if msg.get("method") == "rois.command.completed" \
                        and msg["params"].get("command_id") == command_id:
                    self.notifications.remove(msg)
                    return str(msg["params"]["status"])
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
            self.notifications.append(json.loads(raw))


async def demo(url: str) -> dict[str, Any]:
    """Discover, greet, nod, and look around. Returns what happened, for the caller to check."""
    outcome: dict[str, Any] = {}
    async with websockets.connect(url) as ws:
        app = _App(ws)
        await app.call("rois.system.connect")
        refs = (await app.call("rois.command.search"))["component_ref_list"]
        outcome["components"] = refs
        engine_id = refs[0].split("/", 1)[0]

        async def run(component: str, **params: Any) -> str:
            ref = f"{engine_id}/{component}"
            await app.call("rois.command.bind", component_ref=ref)
            parameters = [
                {"name": k, "data_type_ref": "string", "value": v} for k, v in params.items()
            ]
            executed = await app.call(
                "rois.command.execute", component_ref=ref, command_type="execute",
                parameters=parameters,
            )
            status = await app.completed(executed["command_id"])
            await app.call("rois.command.release", component_ref=ref)
            logger.info("%s %s: %s", component, params, status)
            return status

        outcome["say"] = await run("SpeechSynthesis", speech_text="Hello, I am Reachy Mini.")
        outcome["nod"] = await run("Reaction", reaction_ref="nod")
        outcome["look"] = await run(
            "Navigation",
            target_positions=json.dumps([{"head": {"yaw": 30, "pitch": -10}, "duration": 0.5},
                                         {"head": {"yaw": 0, "pitch": 0}, "duration": 0.5}]),
        )
        position = await app.call(
            "rois.query.query", component_ref=f"{engine_id}/SystemInformation",
            query_type="robot_position",
        )
        values = {r["name"]: r["value"] for r in position["results"]}
        outcome["position"] = json.loads(values["position_data"])
    return outcome


async def run(
    host: str, port: int, profile: dict[str, Any], simulate: bool, run_demo: bool,
    stop: asyncio.Event | None = None,
) -> dict[str, Any]:
    gateway = Engine(engine_id="gateway", platform="", enforce_bindings=True)
    server = WsServer(gateway)
    await server.start(host, port)
    port = server._server.sockets[0].getsockname()[1]
    url = f"ws://{host}:{port}"

    backend = backend_from_profile(profile, simulate=simulate)
    engine = build_engine(profile, backend)
    engine.component_registry.set_emitter(EventEmitter(asyncio.get_running_loop()))
    adapter = asyncio.create_task(WsClient(engine, url)._run_async())
    while not gateway.get_sub_engines():
        await asyncio.sleep(0.05)
    logger.info(
        "Gateway on %s, adapter %s (%s) connected", url, profile["engine"]["id"], backend.name,
    )

    outcome: dict[str, Any] = {"url": url}
    try:
        if run_demo:
            outcome.update(await demo(url))
        else:
            print(f"OpenRoIS gateway: {url}  (Ctrl-C to stop)", flush=True)
            await (stop or asyncio.Event()).wait()
    finally:
        adapter.cancel()
        try:
            await adapter
        except asyncio.CancelledError:
            pass
        await server.stop()
    return outcome


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openrois-reachy-mini-up",
        description="Gateway plus Reachy Mini adapter in one process",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", default=str(DEFAULT_PROFILE), help="adapter profile YAML")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--simulate", action="store_true", default=True, help="simulated robot (default)",
    )
    mode.add_argument("--robot", action="store_true", help="drive the real robot through its SDK")
    parser.add_argument("--demo", action="store_true", help="run a short application, then exit")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    profile = read_profile(args.config)
    try:
        outcome = asyncio.run(run(args.host, args.port, profile, not args.robot, args.demo))
    except KeyboardInterrupt:
        return 0
    if args.demo:
        print(json.dumps(outcome, indent=2))
        return 0 if all(outcome.get(k) == "OK" for k in ("say", "nod", "look")) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
