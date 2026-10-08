"""The connection of the Reachy Mini components to the daemon of one robot.

The components of one robot share one client of the Reachy Mini SDK. Every SDK client
opens its own media session, a WebRTC stream when the robot is on the network, and a
client without media makes the daemon release its camera and audio, which stops the
video of every other client. So an adapter creates one ``ReachyMiniConnection`` per
robot and gives it to each component. Each component opens it in ``connect()`` and
closes it in ``disconnect()``, and the components share the SDK client in between.

The connection outlives the daemon. When the daemon is not running yet, stops, restarts
or drops off the network, ``client()`` connects again, at most once every ``retry``
seconds, and until then it raises ``ConnectionError``. So the adapter and the daemon
start in any order, and a robot that comes back is driven again without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import Any, Literal

logger = logging.getLogger(__name__)

ConnectionMode = Literal["auto", "localhost_only", "network"]


class ReachyMiniConnection:
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
        retry: How long to wait between two attempts to reach the daemon, in seconds.
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
        self._users = 0
        self._retry_at = 0.0
        self._reported = False
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        """Whether a component holds the connection open."""
        return self._users > 0

    @property
    def current(self) -> Any:
        """The SDK client as it is, without connecting again, or None."""
        return self._mini

    async def open(self) -> None:
        """Open the connection for one more user.

        The first user connects to the daemon, in a worker thread, since connecting
        blocks for up to a few seconds. A daemon that does not answer is reached later,
        by ``client()``.

        Raises:
            ImportError: The Reachy Mini SDK is not installed.
        """
        async with self._lock:
            self._users += 1
            if self._users > 1:
                return
            try:
                await self._connect()
            except ConnectionError:
                pass
            except BaseException:
                self._users -= 1
                raise

    async def close(self) -> None:
        """Close the connection for one user. The last one disconnects the SDK client."""
        async with self._lock:
            if self._users == 0:
                return
            self._users -= 1
            if self._users == 0 and self._mini is not None:
                mini, self._mini = self._mini, None
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(mini.__exit__, None, None, None)
                logger.info("Disconnected from the Reachy Mini daemon at %s", _address(mini))

    async def client(self) -> Any:
        """The SDK client, a ``reachy_mini.ReachyMini``, connected to a running daemon.

        A client whose daemon stopped sending the robot's state is dropped, and a new
        one connects, unless the last attempt failed less than ``retry`` seconds ago.

        Raises:
            RuntimeError: No component holds the connection open.
            ConnectionError: The daemon does not answer.
        """
        async with self._lock:
            if self._users == 0:
                raise RuntimeError("The Reachy Mini connection is not open")
            mini = self._mini
            if mini is not None:
                if await asyncio.to_thread(_alive, mini):
                    return mini
                logger.warning(
                    "Lost the Reachy Mini daemon at %s. Connecting again", _address(mini)
                )
                self._mini = None
                self._reported = False
                _dispose(mini)
            if asyncio.get_running_loop().time() < self._retry_at:
                raise ConnectionError(f"The Reachy Mini daemon at {self._where()} does not answer")
            return await self._connect()

    async def _connect(self) -> Any:
        loop = asyncio.get_running_loop()
        try:
            mini = await asyncio.to_thread(self._create)
        except (ConnectionError, TimeoutError) as exc:
            self._retry_at = loop.time() + self.retry
            if not self._reported:
                logger.warning(
                    "The Reachy Mini daemon at %s does not answer, trying again every %g s: %s",
                    self._where(),
                    self.retry,
                    exc,
                )
                self._reported = True
            raise ConnectionError(
                f"The Reachy Mini daemon at {self._where()} does not answer"
            ) from exc
        self._mini = mini
        self._reported = False
        logger.info("Connected to the Reachy Mini daemon at %s", _address(mini))
        return mini

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
