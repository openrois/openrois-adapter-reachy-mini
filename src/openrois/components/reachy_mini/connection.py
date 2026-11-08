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
that comes back is driven again without a restart. Whatever the SDK raises while it
connects, the watchdog keeps trying: only a missing SDK stops the connection for good.

A daemon that closes the connection is lost at once. A daemon whose state stream only
pauses, as over Wi-Fi now and then, is lost once the pause lasts ``lost_after`` seconds,
counted from the first check that found the stream silent. Until then nothing
reconnects and no ERROR is raised, but a command that runs in the pause can still fail.

The camera and the speaker come with the SDK client too, but they can fail while the
daemon answers, for example while the daemon's media server restarts or another client
has released it. The client then connects without them, so the robot still moves, and
the connection raises ERROR on ``media``, a second resource that only the components
that need the camera or the speaker use. The watchdog tries the media again, and clears
the error once they work. A client replaced so is disposed of only once the last
command that holds it, through ``hold``, has ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import threading
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

from openrois.components.core import Resource
from openrois.interfaces.common import ComponentStatus

logger = logging.getLogger(__name__)

ConnectionMode = Literal["auto", "localhost_only", "network"]

# How long a client that passed the liveness check is used without checking it again,
# in seconds. PersonDetection asks for the client five times a second.
_FRESH = 0.5

# How often the watchdog checks a daemon whose state stream paused, at most, in seconds.
_PAUSED_CHECK = 1.0


class ReachyMiniMedia(Resource):
    """The camera and the speaker of one robot, which only some components use.

    ``ReachyMiniConnection`` raises ERROR on it while its SDK client has no media, so
    PersonDetection and SpeechSynthesis report ERROR while Reaction and
    SystemInformation stay READY. It opens and closes nothing itself.
    """


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
        lost_after: How long the daemon's state stream may pause, its connection still
            open, before the connection counts the daemon as lost and connects again, in
            seconds. The pause counts from the first check that finds the stream silent.

    Attributes:
        media: The camera and the speaker of the robot, a resource for the components
            that need them, next to the connection itself.
    """

    def __init__(
        self,
        *,
        host: str = "reachy-mini.local",
        connection_mode: ConnectionMode = "network",
        port: int = 8000,
        timeout: float = 5.0,
        retry: float = 3.0,
        lost_after: float = 3.0,
    ) -> None:
        self.host = host
        self.connection_mode: ConnectionMode = connection_mode
        self.port = port
        self.timeout = timeout
        self.retry = retry
        self.lost_after = lost_after
        self.media = ReachyMiniMedia()
        self._mini: Any = None
        self._open = False
        self._lost = False
        self._last_ok = 0.0
        # When the first check that found the state stream silent began, during a pause.
        self._paused_since: float | None = None
        # How many running commands hold each client, by id, and the clients replaced
        # while held, which are disposed of once the last hold ends.
        self._holds: dict[int, int] = {}
        self._retired: dict[int, Any] = {}
        self._media_error: str | None = None
        self._watch_failing = False
        self._wake: asyncio.Event | None = None
        self._watchdog: asyncio.Task[None] | None = None
        if connection_mode != "localhost_only" and not _trusted_host(host):
            logger.warning(
                "Reachy Mini daemons 1.12 and later refuse clients that reach them by a name "
                "other than an IP address or a .local name, such as %r. The daemon then looks "
                "as if it does not answer. Use the robot's IP address",
                host,
            )

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
        daemon that does not answer, or fails in any other way while the SDK connects,
        raises ERROR, and the watchdog reaches it later.

        Raises:
            ImportError: The Reachy Mini SDK is not installed.
        """
        self._open = True
        self._wake = asyncio.Event()
        try:
            await self._connect()
        except ConnectionError:
            pass
        except ImportError:
            self._open = False
            raise
        self._watchdog = asyncio.create_task(self._watch(), name="reachy-mini-watchdog")

    async def close(self) -> None:
        """Stop the watchdog and disconnect the SDK client."""
        self._open = False
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("The Reachy Mini watchdog failed")
            finally:
                self._watchdog = None
        retired = list(self._retired.values())
        self._retired.clear()
        for old in retired:
            _dispose(old)
        mini, self._mini = self._mini, None
        if mini is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(mini.__exit__, None, None, None)
            logger.info("Disconnected from the Reachy Mini daemon at %s", _address(mini))

    async def client(self) -> Any:
        """The SDK client, a ``reachy_mini.ReachyMini``, connected to a running daemon.

        A client whose daemon closed the connection is dropped at once, and the
        watchdog connects again. A client whose daemon has sent no state for a moment
        fails this call only: the watchdog decides when the daemon is lost.

        Raises:
            RuntimeError: The engine has not opened the connection.
            ConnectionError: The daemon does not answer.
        """
        if not self._open:
            raise RuntimeError("The Reachy Mini connection is not open")
        mini = self._mini
        if mini is not None:
            if _closed(mini):
                self._drop(mini)
            elif time.monotonic() - self._last_ok < _FRESH:
                return mini
            elif await asyncio.to_thread(_alive, mini):
                self._last_ok = time.monotonic()
                if self._mini is mini:
                    self._paused_since = None
                return mini
            else:
                raise ConnectionError(
                    f"The Reachy Mini daemon at {_address(mini)} sends no state at the moment"
                )
        raise ConnectionError(f"The Reachy Mini daemon at {self._where()} does not answer")

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[Any]:
        """The SDK client, as ``client`` gives it, kept connected while the block runs.

        A command that sends through the client for a while, such as a reaction, holds
        it: when the watchdog replaces the client by one whose media work, the old one
        stays connected until the last block that holds it ends. A client whose daemon
        went away is disposed of at once all the same.

        Raises:
            RuntimeError: The engine has not opened the connection.
            ConnectionError: The daemon does not answer.
        """
        mini = await self.client()
        key = id(mini)
        self._holds[key] = self._holds.get(key, 0) + 1
        try:
            yield mini
        finally:
            left = self._holds[key] - 1
            if left:
                self._holds[key] = left
            else:
                del self._holds[key]
                retired = self._retired.pop(key, None)
                if retired is not None:
                    _dispose(retired)

    async def _watch(self) -> None:
        """Check the daemon every ``retry`` seconds, and reach it again when it is gone.

        Nothing ends the loop but the cancel of ``close``: a check that fails in an
        unexpected way is logged, once for each run of failures, and the next check
        runs as usual.
        """
        assert self._wake is not None
        while True:
            paused = self._paused_since is not None
            interval = min(self.retry, _PAUSED_CHECK) if paused else self.retry
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), interval)
            self._wake.clear()
            try:
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._watch_failing:
                    logger.exception("The Reachy Mini watchdog could not check the daemon")
                self._watch_failing = True
            else:
                self._watch_failing = False

    async def _check(self) -> None:
        """One check of the watchdog: the daemon, then its media, or a new connection."""
        mini = self._mini
        if mini is not None:
            if not _closed(mini):
                started = time.monotonic()
                if await asyncio.to_thread(_alive, mini):
                    self._last_ok = time.monotonic()
                    self._paused_since = None
                    if self._media_error is not None:
                        await self._retry_media(mini)
                    return
                # The SDK waits a moment for the state before it says the stream is
                # silent, and the last good check can be ``retry`` seconds old, so the
                # pause counts from this check rather than from the last good one.
                if self._paused_since is None:
                    self._paused_since = started
                if time.monotonic() - self._paused_since < self.lost_after:
                    return
            self._drop(mini)
        with contextlib.suppress(ConnectionError):
            await self._connect()

    async def _retry_media(self, mini: Any) -> None:
        """Try a new SDK client for a robot whose media failed, and keep it if they work.

        The client in use keeps moving the robot meanwhile, and is disposed of only once
        the new one has its camera and speaker, and no running command holds it.
        """
        try:
            fresh = await asyncio.to_thread(self._create)
        except Exception as exc:
            logger.debug("A new client for the Reachy Mini media failed: %r", exc)
            return
        if _media_error(fresh) is not None or not self._open or self._mini is not mini:
            _dispose(fresh)
            return
        self._mini = fresh
        self._last_ok = time.monotonic()
        self._paused_since = None
        if id(mini) in self._holds:
            self._retired[id(mini)] = mini
        else:
            _dispose(mini)
        logger.info("The camera and the speaker of the Reachy Mini at %s work", _address(fresh))
        self._set_media(None)

    def _set_media(self, error: str | None) -> None:
        """Raise ERROR on ``media`` while the client has no media, and clear it after."""
        if error == self._media_error:
            return
        self._media_error = error
        if error is None:
            self.media.clear_status()
            return
        logger.warning(
            "The Reachy Mini at %s moves, but its camera and speaker do not work, "
            "trying again every %g s: %s",
            self._where(),
            self.retry,
            error,
        )
        self.media.raise_status(
            ComponentStatus.ERROR, f"The camera and the speaker of the robot do not work: {error}"
        )

    def _drop(self, mini: Any) -> None:
        """Forget a client whose daemon went away, raise ERROR and wake the watchdog."""
        if self._mini is not mini:
            return
        self._mini = None
        self._paused_since = None
        logger.warning("Lost the Reachy Mini daemon at %s. Connecting again", _address(mini))
        self._lose(f"Lost the Reachy Mini daemon at {_address(mini)}")
        _dispose(mini)
        if self._wake is not None:
            self._wake.set()

    async def _connect(self) -> Any:
        """Create the SDK client, or raise ERROR and ConnectionError.

        Raises:
            ImportError: The Reachy Mini SDK is not installed, which waiting cannot fix.
            ConnectionError: The daemon does not answer, or the SDK failed while it
                connected.
        """
        try:
            mini = await asyncio.to_thread(self._create)
        except ImportError:
            raise
        except (ConnectionError, TimeoutError) as exc:
            if not self._lost:
                logger.warning(
                    "The Reachy Mini daemon at %s does not answer, trying again every %g s: %r",
                    self._where(),
                    self.retry,
                    exc,
                )
            self._lose(f"The Reachy Mini daemon at {self._where()} does not answer")
            raise ConnectionError(
                f"The Reachy Mini daemon at {self._where()} does not answer"
            ) from exc
        except Exception as exc:
            # For example a daemon that has just started, whose media are not ready yet.
            reason = f"{type(exc).__name__}: {exc}"
            if not self._lost:
                logger.warning(
                    "The Reachy Mini daemon at %s is not ready, trying again every %g s: %s",
                    self._where(),
                    self.retry,
                    reason,
                )
            self._lose(f"The Reachy Mini daemon at {self._where()} is not ready: {reason}")
            raise ConnectionError(
                f"The Reachy Mini daemon at {self._where()} is not ready: {reason}"
            ) from exc
        if not self._open:
            # The engine closed the connection while it connected.
            _dispose(mini)
            raise ConnectionError("The Reachy Mini connection is closed")
        self._mini = mini
        self._last_ok = time.monotonic()
        self._paused_since = None
        logger.info("Connected to the Reachy Mini daemon at %s", _address(mini))
        self._set_media(_media_error(mini))
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

        class Mini(ReachyMini):  # type: ignore[misc]
            """The SDK client, which connects without media when they fail."""

            #: Why the media failed, or None while they work.
            openrois_media_error: str | None = None

            def _configure_mediamanager(self, media_backend: str, log_level: str) -> Any:
                try:
                    manager = super()._configure_mediamanager(media_backend, log_level)
                except Exception as exc:
                    # For example the WebRTC signalling of the daemon refuses the
                    # connection, or its media server has not registered yet.
                    self.openrois_media_error = f"{type(exc).__name__}: {exc}"
                    return _no_media(log_level)
                refusal = _signalling_refusal(self, manager)
                if refusal is not None:
                    with contextlib.suppress(Exception):
                        manager.close()
                    self.openrois_media_error = refusal
                    return _no_media(log_level)
                return manager

        # The default media backend: local media for a daemon on this machine, and
        # WebRTC for a robot on the network. Never no_media, which takes the daemon's
        # camera and audio away from the other clients. A media manager made with
        # NO_MEDIA directly, as above, takes nothing away.
        return Mini(
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


def _closed(mini: Any) -> bool:
    """Whether the daemon closed the connection, which ends the SDK's receiving thread."""
    thread = getattr(mini.client, "_recv_thread", None)
    return thread is not None and not thread.is_alive()


def _media_error(mini: Any) -> str | None:
    """Why the media of an SDK client failed, or None while they work."""
    error = getattr(mini, "openrois_media_error", None)
    return error if isinstance(error, str) else None


def _no_media(log_level: str) -> Any:
    """A media manager without camera and speaker, which leaves the daemon's media alone."""
    from reachy_mini.media.media_manager import MediaBackend, MediaManager

    return MediaManager(backend=MediaBackend.NO_MEDIA, log_level=log_level)


def _signalling_refusal(mini: Any, manager: Any) -> str | None:
    """Why the WebRTC media of a robot on the network would come from the wrong place.

    The SDK fetches the media from the address the daemon reports for its Wi-Fi, and
    from this machine when the daemon reports none, which happens when the daemon
    started before the Wi-Fi had an address. With a simulator on this machine, the robot
    would then stream the simulator's camera and play its voice on the simulator.
    """
    if getattr(mini, "connection_mode", None) != "network":
        return None
    if getattr(getattr(manager, "backend", None), "value", None) != "webrtc":
        return None
    # The SDK read the status just before, so this returns at once.
    wlan_ip = getattr(mini.client.get_status(wait=False), "wlan_ip", None)
    if not wlan_ip:
        return (
            "the robot's daemon reports no Wi-Fi address, so the SDK would take the camera "
            "and the speaker of this machine instead; restart the robot's daemon once its "
            "Wi-Fi is up"
        )
    host = str(mini.client.host)
    if _is_ip(host) and host != wlan_ip:
        logger.warning(
            "The Reachy Mini daemon at %s reports the Wi-Fi address %s, so its camera and "
            "speaker may not work; restart the robot's daemon after a change of network",
            host,
            wlan_ip,
        )
    return None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _trusted_host(host: str) -> bool:
    """Whether a Reachy Mini daemon 1.12 or later accepts a client that names it so."""
    name = host.lower().rstrip(".")
    return _is_ip(name) or name == "localhost" or name.endswith(".local")


def _address(mini: Any) -> str:
    """Where the SDK client connected, which ``auto`` picks."""
    return f"{mini.client.host}:{mini.client.port}"


def _dispose(mini: Any) -> None:
    """Disconnect a client whose daemon went away, without waiting for it."""

    def close() -> None:
        with contextlib.suppress(Exception):
            mini.__exit__(None, None, None)

    threading.Thread(target=close, name="reachy-mini-dispose", daemon=True).start()
