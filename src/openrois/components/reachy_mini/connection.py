"""The connection of the Reachy Mini components to the daemon of one robot.

The components of one robot share one client of the Reachy Mini SDK. Every SDK client
opens its own media session, a WebRTC stream when the robot is on the network, and a
client without media makes the daemon release its camera and audio, which stops the
video of every other client. So an adapter creates one ``ReachyMiniConnection`` per
robot and gives it to each component. Each component opens it in ``connect()`` and
closes it in ``disconnect()``, and the SDK client lives from the first open to the last
close.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

ConnectionMode = Literal["auto", "localhost_only", "network"]


class ReachyMiniConnection:
    """One SDK client of one Reachy Mini daemon, shared by the components of the robot.

    Args:
        host: The host of the daemon when it runs on another machine, for example the
            Reachy Mini Wireless at ``reachy-mini.local``.
        connection_mode: ``localhost_only`` for a daemon on this machine, such as the
            simulator, ``network`` for a robot on the network, or ``auto`` to try this
            machine first.
        port: The port of the daemon.
        timeout: How long to wait for the daemon, in seconds.
    """

    def __init__(
        self,
        *,
        host: str = "reachy-mini.local",
        connection_mode: ConnectionMode = "auto",
        port: int = 8000,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.connection_mode: ConnectionMode = connection_mode
        self.port = port
        self.timeout = timeout
        self._mini: Any = None
        self._users = 0
        self._lock = asyncio.Lock()

    @property
    def mini(self) -> Any:
        """The SDK client, a ``reachy_mini.ReachyMini``, while a component holds it open.

        Raises:
            RuntimeError: No component has opened the connection.
        """
        if self._mini is None:
            raise RuntimeError("The Reachy Mini connection is not open")
        return self._mini

    @property
    def is_open(self) -> bool:
        return self._mini is not None

    async def open(self) -> Any:
        """Open the connection for one more user, and return the SDK client.

        The first user creates the client, with its media, in a worker thread, since
        connecting blocks for up to a few seconds.
        """
        async with self._lock:
            if self._mini is None:
                self._mini = await asyncio.to_thread(self._create)
                logger.info("Connected to the Reachy Mini daemon (%s)", self._where())
            self._users += 1
            return self._mini

    async def close(self) -> None:
        """Close the connection for one user. The last one disconnects the SDK client."""
        async with self._lock:
            if self._users == 0:
                return
            self._users -= 1
            if self._users == 0 and self._mini is not None:
                mini, self._mini = self._mini, None
                await asyncio.to_thread(mini.__exit__, None, None, None)
                logger.info("Disconnected from the Reachy Mini daemon (%s)", self._where())

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
        return f"{self.host}:{self.port}, {self.connection_mode}"
