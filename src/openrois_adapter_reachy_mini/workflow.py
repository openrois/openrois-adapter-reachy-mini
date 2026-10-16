"""The full RoIS workflow against one Reachy Mini behind a gateway, step by step.

A service application's whole life with the robot, run as a client of an OpenRoIS
gateway: connect, get_profile, search, bind, set_parameter, execute with two branches
that run at the same time, subscribe and notify_event, notify_error and
get_error_detail while the robot's daemon is stopped, then release and disconnect. Each
step checks what the robot and the gateway answer, and the run stops at the first step
that fails::

    python -m openrois_adapter_reachy_mini.workflow --engine-id reachy_sim

The workflow stops the daemon and starts it again through the daemon's own HTTP API, so
it runs the same against the simulator and the Reachy Mini Wireless. It needs the robot's
camera to see the room: the count of persons it waits for may be 0. A simulator that
runs headless renders no camera image, so run it with a window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from openrois.interfaces.components import (
    PERSON_DETECTION_URN,
    REACTION_URN,
    SPEECH_SYNTHESIS_URN,
)
from websockets.asyncio.client import ClientConnection, connect

logger = logging.getLogger(__name__)

#: Stops or starts the robot's daemon.
type DaemonSwitch = Callable[[], Awaitable[None]]


class WorkflowFailed(Exception):  # noqa: N818, the name says what happened to the run.
    """Raised by a step whose answer is not the one the workflow expects."""


@dataclass
class Step:
    """One step of the workflow and how long it took."""

    name: str
    seconds: float
    detail: str = ""


@dataclass
class Client:
    """A RoIS service application over JSON-RPC, as the HRI client talks to a gateway."""

    connection: ClientConnection
    notifications: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _ids: int = 0
    _replies: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    _arrived: asyncio.Event = field(default_factory=asyncio.Event)
    _reader: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read(), name="workflow-reader")

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        """Send a request and return its result."""
        self._ids += 1
        reply: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._replies[self._ids] = reply
        request = {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params}
        await self.connection.send(json.dumps(request))
        message = await asyncio.wait_for(reply, 30.0)
        if "result" not in message:
            raise WorkflowFailed(f"{method} failed: {message.get('error')}")
        result: dict[str, Any] = message["result"]
        return result

    async def ok(self, method: str, **params: Any) -> dict[str, Any]:
        """Send a request whose return code must be OK."""
        result = await self.call(method, **params)
        if result.get("return_code") != "OK":
            raise WorkflowFailed(f"{method} answered {result.get('return_code')}: {result}")
        return result

    async def wait(
        self, method: str, check: Callable[[dict[str, Any]], bool], timeout: float
    ) -> dict[str, Any]:
        """Wait for a notification that passes ``check``, sent since the workflow began."""
        async with asyncio.timeout(timeout):
            while True:
                for name, params in self.notifications:
                    if name == method and check(params):
                        return params
                self._arrived.clear()
                await self._arrived.wait()

    async def _read(self) -> None:
        async for raw in self.connection:
            message: dict[str, Any] = json.loads(raw)
            if "method" in message:
                self.notifications.append((message["method"], message.get("params", {})))
                self._arrived.set()
            elif (reply := self._replies.pop(message.get("id", -1), None)) is not None:
                reply.set_result(message)


def daemon_api(base_url: str) -> tuple[DaemonSwitch, DaemonSwitch]:
    """Stop and start a Reachy Mini daemon through its HTTP API, at ``base_url``.

    The robot stays where it is: it neither goes to sleep on the stop nor wakes up on
    the start.
    """

    def post(path: str) -> None:
        request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", method="POST")
        with urllib.request.urlopen(request, timeout=10.0) as response:
            response.read()

    async def stop() -> None:
        await asyncio.to_thread(post, "/api/daemon/stop?goto_sleep=false")

    async def start() -> None:
        await asyncio.to_thread(post, "/api/daemon/start?wake_up=false")

    return stop, start


async def run_workflow(
    gateway_url: str,
    engine_id: str,
    *,
    stop_daemon: DaemonSwitch,
    start_daemon: DaemonSwitch,
    timeout: float = 30.0,
    on_step: Callable[[Step], None] | None = None,
) -> list[Step]:
    """Run the workflow against the robot ``engine_id`` behind a gateway.

    Args:
        gateway_url: The gateway's client endpoint, for example ``ws://127.0.0.1:8765``.
        engine_id: The engine id of the robot's adapter.
        stop_daemon: Stops the robot's daemon.
        start_daemon: Starts the robot's daemon again.
        timeout: How long a step waits for a notification, in seconds.
        on_step: Called after each step that passed.

    Returns:
        The steps, in order, each with how long it took.

    Raises:
        WorkflowFailed: A step did not get the answer it expects.
    """
    steps: list[Step] = []
    clock = time.monotonic()

    def passed(name: str, detail: str = "") -> None:
        nonlocal clock
        now = time.monotonic()
        step = Step(name, now - clock, detail)
        clock = now
        steps.append(step)
        if on_step is not None:
            on_step(step)

    async with connect(gateway_url) as connection:
        client = Client(connection)
        client.start()
        try:
            await _workflow(client, engine_id, stop_daemon, start_daemon, timeout, passed)
        finally:
            await client.close()
    return steps


async def _workflow(
    client: Client,
    engine_id: str,
    stop_daemon: DaemonSwitch,
    start_daemon: DaemonSwitch,
    timeout: float,
    passed: Callable[..., None],
) -> None:
    def ref(name: str) -> str:
        return f"{engine_id}/{name}"

    def completed(command_id: str) -> Callable[[dict[str, Any]], bool]:
        return lambda params: params.get("command_id") == command_id

    async def status(name: str) -> str:
        condition = f"component_ref = '{ref(name)}'"
        result = await client.ok(
            "rois.query.query", query_type="component_status", condition=condition
        )
        values = {r["name"]: r["value"] for r in result["results"]}
        return str(values["status"])

    await client.ok("rois.system.connect")
    passed("connect")

    profile = (await client.ok("rois.system.get_profile", condition=""))["profile"]
    robots = {p["identifier"]["code"]: p for p in profile.get("sub_profiles", [])}
    if engine_id not in robots:
        raise WorkflowFailed(f"The gateway serves no engine {engine_id!r}: {sorted(robots)}")
    components = sorted(robots[engine_id]["component_ids"])
    passed("get_profile", ", ".join(components))

    found: dict[str, str] = {}
    for name, urn in (
        ("reaction", REACTION_URN),
        ("speech_synthesis", SPEECH_SYNTHESIS_URN),
        ("person_detection", PERSON_DETECTION_URN),
    ):
        condition = f"component_type = '{urn}' AND component_ref LIKE '{engine_id}/%'"
        refs = (await client.ok("rois.command.search", condition=condition))["component_ref_list"]
        if refs != [ref(name)]:
            raise WorkflowFailed(f"search for {urn} found {refs}, not {ref(name)}")
        found[name] = refs[0]
    passed("search", ", ".join(found.values()))

    for name in found:
        await client.ok("rois.command.bind", component_ref=ref(name))
    passed("bind", ", ".join(found.values()))

    text = "Hello, I am Reachy Mini."
    parameters = [{"name": "speech_text", "data_type_ref": "string", "value": text}]
    command_id = (
        await client.ok(
            "rois.command.set_parameter",
            component_ref=ref("speech_synthesis"),
            parameters=parameters,
        )
    )["command_id"]
    done = await client.wait("rois.command.completed", completed(command_id), timeout)
    if done["status"] != "OK":
        raise WorkflowFailed(f"set_parameter completed with {done['status']}")
    passed("set_parameter", "speech_text")

    # Two branches at the same time: the robot speaks while it chooses and plays a reaction.
    branches = {
        "branch_list": [
            {"command_list": [{"component_ref": ref("speech_synthesis"), "command_type": "start"}]},
            {
                "command_list": [
                    {
                        "component_ref": ref("reaction"),
                        "command_type": "set_parameter",
                        "arguments": [
                            {"name": "reaction_ref", "data_type_ref": "", "value": "happy"}
                        ],
                    },
                    {"component_ref": ref("reaction"), "command_type": "start"},
                ]
            },
        ]
    }
    executed = await client.ok("rois.command.execute", command_unit_list=[branches])
    ids = [
        unit["command_id"]
        for item in executed["command_unit_list"]
        for branch in item["branch_list"]
        for unit in branch["command_list"]
    ]
    # The gateway assigns the ids, so they start with its own engine id.
    if len(ids) != 3 or len(set(ids)) != 3 or not all(ids):
        raise WorkflowFailed(f"execute assigned the command ids {ids}")
    for command_id in ids:
        done = await client.wait("rois.command.completed", completed(command_id), timeout)
        if done["status"] != "OK":
            raise WorkflowFailed(f"{command_id} completed with {done['status']}")
    passed("execute with a branch", ", ".join(ids))

    condition = f"component_ref = '{ref('person_detection')}'"
    subscription = await client.ok(
        "rois.event.subscribe", event_type="person_detected", condition=condition
    )
    # Every start reports the count, also of a count an earlier application left running.
    start = await client.ok(
        "rois.command.execute",
        command_unit_list=[{"component_ref": ref("person_detection"), "command_type": "start"}],
    )
    [unit] = start["command_unit_list"]
    await client.wait("rois.command.completed", completed(unit["command_id"]), timeout)
    event = await client.wait(
        "rois.event.notify_event",
        lambda params: params.get("subscribe_id") == subscription["subscribe_id"],
        timeout,
    )
    values = {r["name"]: r["value"] for r in event["results"]}
    passed("subscribe and notify_event", f"{values.get('number')} person(s) in view")

    await stop_daemon()
    try:
        error = await client.wait(
            "rois.system.notify_error",
            lambda params: params.get("error_type") == "COMPONENT_NOT_RESPONDING",
            timeout,
        )
        detail = await client.ok(
            "rois.system.get_error_detail", error_id=error["error_id"], condition=""
        )
        details = {r["name"]: r["value"] for r in detail["results"]}
        for name in found:
            if (seen := await status(name)) != "ERROR":
                raise WorkflowFailed(f"{ref(name)} reports {seen} while the daemon is stopped")
        passed("notify_error and get_error_detail", str(details.get("message", "")))
    finally:
        await start_daemon()
    async with asyncio.timeout(timeout):
        for name in found:
            while await status(name) != "READY":
                await asyncio.sleep(0.2)
    passed("the daemon is back", "every component READY")

    for name in found:
        await client.ok("rois.command.release", component_ref=ref(name))
    passed("release")
    await client.ok("rois.system.disconnect")
    passed("disconnect")


def main(argv: Sequence[str] | None = None) -> None:
    """Run the workflow and print each step, or the step that failed."""
    parser = argparse.ArgumentParser(
        prog="python -m openrois_adapter_reachy_mini.workflow",
        description="Run the full RoIS workflow against one Reachy Mini behind a gateway.",
    )
    parser.add_argument("--gateway-url", default="ws://127.0.0.1:8765", help="the gateway")
    parser.add_argument("--engine-id", default="reachy_mini", help="the robot's engine id")
    parser.add_argument(
        "--daemon",
        default="http://localhost:8000",
        help="the HTTP API of the robot's daemon, for example http://reachy-mini.local:8000",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds a step waits")
    args = parser.parse_args(argv)
    stop, start = daemon_api(args.daemon)

    def show(step: Step) -> None:
        print(f"PASS  {step.name:<36} {step.seconds:6.2f} s  {step.detail}", flush=True)

    try:
        asyncio.run(
            run_workflow(
                args.gateway_url,
                args.engine_id,
                stop_daemon=stop,
                start_daemon=start,
                timeout=args.timeout,
                on_step=show,
            )
        )
    except (WorkflowFailed, TimeoutError, OSError) as exc:
        print(f"FAIL  {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
    print("The workflow passed.")


if __name__ == "__main__":
    main()
