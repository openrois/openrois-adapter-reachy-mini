"""The connection of the Reachy Mini components to the daemon of one robot.

The components of one robot share one client of the Reachy Mini SDK. Every SDK client
opens its own media session, a WebRTC stream when the robot is on the network, and a
client without media makes the daemon release its camera and audio, which stops the
video of every other client. So an adapter creates one ``ReachyMiniConnection`` per
robot, a ``Resource``, and gives it to each component, which records it with
``Component.uses``. The engine opens it before the first of them connects and closes it
after the last one disconnects.

The connection outlives the daemon. A watchdog checks the daemon every ``retry``
seconds. While the daemon is not running yet, stops, restarts or drops off the network,
the connection raises ERROR, which every component of the robot reports: clients get
``rois.system.notify_error``, component_status says ERROR, and the engine refuses the
components' commands and queries. The watchdog connects again, and clears the error
once the daemon answers. So the adapter and the daemon start in any order, and a robot
that comes back is driven again without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import Any, Literal

from openrois.components.core import Resource
from openrois.interfaces.common import ComponentStatus

logger = logging.getLogger(__name__)

ConnectionMode = Literal["auto", "localhost_only", "network"]


class ReachyMiniConnection(Resource):
    """One SDK client of one Reachy Mini daemon, shared by the components of the robot.

    Args:
        host: The host of the daemon on the network, for example the Reachy Mini
            Wireless at ``reachy-mini.local``.
        connection_mode: ``network`` for the daemon at ``host``, ``localhost_only`` for
            a daemon on this machine, such as the simulator, or ``auto`` to try this
            machine first and ``host`` after. With ``auto``, a simulator running on this
            machine wins over the robot at ``host``.
        port: The port of the daemon.
        timeout: How long to wait for the daemon, in seconds.
        retry: How often the watchdog checks the daemon, and tries to reach it again
            while it does not answer, in seconds.
    """

    def __init__(
        self,
        *,
        host: str = "reachy-mini.local",
        connection_mode: ConnectionMode = "network",
        port: int = 8000,
        timeout: float = 5.0,
        retry: float = 3.0,
    ) -> None:
        self.host = host
        self.connection_mode: ConnectionMode = connection_mode
        self.port = port
        self.timeout = timeout
        self.retry = retry
        self._mini: Any = None
        self._open = False
        self._lost = False
        self._wake: asyncio.Event | None = None
        self._watchdog: asyncio.Task[None] | None = None

    @property
    def is_open(self) -> bool:
        """Whether the engine opened the connection and has not closed it."""
        return self._open

    @property
    def current(self) -> Any:
        """The SDK client as it is, without connecting again, or None."""
        return self._mini

    async def open(self) -> None:
        """Connect to the daemon, and start the watchdog.

        Connecting runs in a worker thread, since it blocks for up to a few seconds. A
        daemon that does not answer raises ERROR, and the watchdog reaches it later.

        Raises:
            ImportError: The Reachy Mini SDK is not installed.
        """
        self._open = True
        self._wake = asyncio.Event()
        try:
            await self._connect()
        except ConnectionError:
            pass
        except BaseException:
            self._open = False
            raise
        self._watchdog = asyncio.create_task(self._watch(), name="reachy-mini-watchdog")

    async def close(self) -> None:
        """Stop the watchdog and disconnect the SDK client."""
        self._open = False
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None
        mini, self._mini = self._mini, None
        if mini is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(mini.__exit__, None, None, None)
            logger.info("Disconnected from the Reachy Mini daemon at %s", _address(mini))

    async def client(self) -> Any:
        """The SDK client, a ``reachy_mini.ReachyMini``, connected to a running daemon.

        A client whose daemon stopped sending the robot's state is dropped at once, and
        the watchdog connects again.

        Raises:
            RuntimeError: The engine has not opened the connection.
            ConnectionError: The daemon does not answer.
        """
        if not self._open:
            raise RuntimeError("The Reachy Mini connection is not open")
        mini = self._mini
        if mini is not None and await asyncio.to_thread(_alive, mini):
            return mini
        if mini is not None:
            self._drop(mini)
        raise ConnectionError(f"The Reachy Mini daemon at {self._where()} does not answer")

    async def _watch(self) -> None:
        """Check the daemon every ``retry`` seconds, and reach it again when it is gone."""
        assert self._wake is not None
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.retry)
            self._wake.clear()
            mini = self._mini
            if mini is not None:
                if await asyncio.to_thread(_alive, mini):
                    continue
                self._drop(mini)
            with contextlib.suppress(ConnectionError):
                await self._connect()

    def _drop(self, mini: Any) -> None:
        """Forget a client whose daemon went away, raise ERROR and wake the watchdog."""
        if self._mini is not mini:
            return
        self._mini = None
        logger.warning("Lost the Reachy Mini daemon at %s. Connecting again", _address(mini))
        self._lose(f"Lost the Reachy Mini daemon at {_address(mini)}")
        _dispose(mini)
        if self._wake is not None:
            self._wake.set()

    async def _connect(self) -> Any:
        try:
            mini = await asyncio.to_thread(self._create)
        except (ConnectionError, TimeoutError) as exc:
            if not self._lost:
                logger.warning(
                    "The Reachy Mini daemon at %s does not answer, trying again every %g s: %s",
                    self._where(),
                    self.retry,
                    exc,
                )
            self._lose(f"The Reachy Mini daemon at {self._where()} does not answer")
            raise ConnectionError(
                f"The Reachy Mini daemon at {self._where()} does not answer"
            ) from exc
        if not self._open:
            # The engine closed the connection while it connected.
            _dispose(mini)
            raise ConnectionError("The Reachy Mini connection is closed")
        self._mini = mini
        logger.info("Connected to the Reachy Mini daemon at %s", _address(mini))
        if self._lost:
            self._lost = False
            self.clear_status()
        return mini

    def _lose(self, message: str) -> None:
        """Raise ERROR once for each time the daemon goes away."""
        if not self._lost:
            self._lost = True
            self.raise_status(ComponentStatus.ERROR, message)

    def _create(self) -> Any:
        from reachy_mini import ReachyMini

        # The default media backend: local media for a daemon on this machine, and
        # WebRTC for a robot on the network. Never no_media, which takes the daemon's
        # camera and audio away from the other clients.
        return ReachyMini(
            host=self.host,
            port=self.port,
            connection_mode=self.connection_mode,
            timeout=self.timeout,
            media_backend="default",
        )

    def _where(self) -> str:
        if self.connection_mode == "localhost_only":
            return f"localhost:{self.port}"
        if self.connection_mode == "auto":
            return f"localhost:{self.port} or {self.host}:{self.port}"
        return f"{self.host}:{self.port}"


def _alive(mini: Any) -> bool:
    """Whether the daemon still sends the robot's state, which it does many times a second."""
    return bool(mini.client.is_connected())


def _address(mini: Any) -> str:
    """Where the SDK client connected, which ``auto`` picks."""
    return f"{mini.client.host}:{mini.client.port}"


def _dispose(mini: Any) -> None:
    """Disconnect a client whose daemon went away, without waiting for it."""

    def close() -> None:
        with contextlib.suppress(Exception):
            mini.__exit__(None, None, None)

    threading.Thread(target=close, name="reachy-mini-dispose", daemon=True).start()
