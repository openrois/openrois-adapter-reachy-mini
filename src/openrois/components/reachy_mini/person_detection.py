"""PersonDetection for the Reachy Mini.

While connected, the component counts the faces the robot's camera sees, with the face
detector of the Reachy Mini SDK, and emits ``person_detected`` with the ``number`` of
persons when the count changes. A new count is reported once it holds for a moment, so
a face that flickers in and out of detection does not flood the clients. The head never
moves for it: the component reads camera frames, and leaves the daemon's head tracking
off.

The simulator renders no camera image while it runs headless, and then the component
reports nothing. Nor does it while the robot's daemon does not answer: the count resumes
once the connection reaches the daemon again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from openrois.components.core import Component, component, subscribe
from openrois.interfaces.components import PERSON_DETECTION_PROFILE

from openrois.components.reachy_mini.connection import ReachyMiniConnection

logger = logging.getLogger(__name__)


class Debounce:
    """Reports a new value once it holds for ``hold`` seconds.

    Args:
        hold: How long a value has to hold before it is reported, in seconds.
    """

    def __init__(self, hold: float) -> None:
        self._hold = hold
        self._reported: int | None = None
        self._candidate: int | None = None
        self._since = 0.0

    def update(self, value: int, now: float) -> int | None:
        """Take a new reading at time ``now``, and return a value to report, or None."""
        if value != self._candidate:
            self._candidate = value
            self._since = now
        if value != self._reported and now - self._since >= self._hold:
            self._reported = value
            return value
        return None


@component(PERSON_DETECTION_PROFILE)
class ReachyMiniPersonDetection(Component):
    """PersonDetection of one Reachy Mini: the number of faces the camera sees.

    Args:
        connection: The connection to the robot, shared with its other components.
        interval: How often a frame is checked, in seconds.
        hold: How long a new count has to hold before it is reported, in seconds.
        width: The width the frames are scaled down to before detection, in pixels. The
            daemon's own face tracker uses 320.
        threshold: The detection score from which a face counts, from 0 to 1.
    """

    def __init__(
        self,
        connection: ReachyMiniConnection,
        *,
        interval: float = 0.2,
        hold: float = 0.5,
        width: int = 320,
        threshold: float = 0.6,
    ) -> None:
        self._connection = connection
        self._interval = interval
        self._hold = hold
        self._width = width
        self._threshold = threshold
        self._detector: Any = None
        self._watcher: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        await self._connection.open()
        try:
            self._detector = await asyncio.to_thread(_face_detector, self._threshold)
        except BaseException:
            await self._connection.close()
            raise
        self._watcher = asyncio.create_task(self._watch(), name="person-detection")

    async def disconnect(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
            self._watcher = None
        await self._connection.close()

    @subscribe("person_detected")
    async def person_detected(self) -> None:
        pass  # The watcher emits the event when the count changes.

    async def _watch(self) -> None:
        loop = asyncio.get_running_loop()
        debounce = Debounce(self._hold)
        failing = False
        while True:
            started = loop.time()
            count = None
            try:
                mini = await self._connection.client()
                count = await asyncio.to_thread(self.count, mini)
            except ConnectionError:
                pass  # The connection reports the daemon that does not answer.
            except Exception:
                # Once per run of failures, so a broken camera does not flood the log.
                if not failing:
                    logger.exception("Could not count the faces in a camera frame")
                failing = True
            else:
                failing = False
            if count is not None:
                reported = debounce.update(count, loop.time())
                if reported is not None:
                    logger.info("%d person(s) in view", reported)
                    # The engine sends the event to every subscription, and drops it
                    # when there is none.
                    self.emit("person_detected", number=reported, timestamp=datetime.now(UTC))
            await asyncio.sleep(max(0.0, self._interval - (loop.time() - started)))

    def count(self, mini: Any) -> int | None:
        """The number of faces in the latest camera frame, or None without a frame."""
        frame = mini.media.get_frame()
        if frame is None:
            return None
        step = max(1, int(frame.shape[1]) // self._width)
        if step > 1:
            frame = frame[::step, ::step].copy()
        return len(self._detector.detect(frame))


def _face_detector(threshold: float) -> Any:
    """The YuNet face detector of the SDK. Its model comes from the Hugging Face Hub."""
    from reachy_mini.vision.face_detector import FaceDetector

    return FaceDetector(score_threshold=threshold)
