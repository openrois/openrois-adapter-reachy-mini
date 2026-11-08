"""The media part of a Reachy Mini daemon's HTTP API, for a robot on the network.

The SDK plays a sound on a robot on the network by uploading the file to the daemon and
asking the daemon to play it, but it only logs a refusal, so a voice that never played
would look like one that did. ``DaemonMedia`` makes the same calls and raises when the
daemon refuses, so that SpeechSynthesis can tell its client.

The calls block, so the components run them in a worker thread. They go straight to the
robot, never through an HTTP proxy of the environment, since the robot is on the local
network.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

#: The uploads and the play requests of the SDK wait this long, in seconds.
UPLOAD_TIMEOUT = 30.0
REQUEST_TIMEOUT = 10.0


class DaemonMediaError(RuntimeError):
    """The daemon refused a media request, or could not be reached."""


class DaemonMedia:
    """The media endpoints of one daemon.

    Args:
        base_url: The daemon's HTTP API, for example ``http://192.168.1.42:8000``.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def check_speaker(self) -> None:
        """Raise unless the daemon's media run, so that a sound would play.

        A daemon started without media, or whose media another client released, answers
        a play request with success all the same.

        Raises:
            DaemonMediaError: The media do not run, or the daemon cannot be reached.
        """
        status = self._request("GET", "/api/media/status", timeout=REQUEST_TIMEOUT)
        if status.get("no_media"):
            raise DaemonMediaError("The robot's daemon runs without media, so nothing plays")
        if status.get("released"):
            raise DaemonMediaError(
                "Another application released the robot's media, so nothing plays"
            )
        if status.get("available") is False:
            raise DaemonMediaError("The robot's media server does not run, so nothing plays")

    def upload(self, path: Path) -> str:
        """Upload a sound file, and return its path on the daemon.

        A connection that fails, or a name that does not resolve, is tried once more, as
        with a robot whose Wi-Fi drops out for a moment.

        Raises:
            DaemonMediaError: The daemon refused the file, or cannot be reached.
        """
        boundary = uuid.uuid4().hex
        body = b"".join(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
                b"Content-Type: audio/wav\r\n\r\n",
                path.read_bytes(),
                f"\r\n--{boundary}--\r\n".encode(),
            )
        )
        result = self._request(
            "POST",
            "/api/media/sounds/upload",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=UPLOAD_TIMEOUT,
        )
        remote = result.get("path")
        if not isinstance(remote, str):
            raise DaemonMediaError(f"The robot's daemon answered the upload with {result}")
        return remote

    def play(self, remote: str) -> None:
        """Play a sound the daemon has, such as an uploaded one.

        Raises:
            DaemonMediaError: The daemon refused to play it, or cannot be reached.
        """
        self._request(
            "POST",
            "/api/media/play_sound",
            body=json.dumps({"file": remote}).encode(),
            content_type="application/json",
            timeout=REQUEST_TIMEOUT,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        for attempt in (1, 2):
            request = urllib.request.Request(url, data=body, method=method)
            if content_type is not None:
                request.add_header("Content-Type", content_type)
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    payload = response.read()
            except urllib.error.HTTPError as exc:
                raise DaemonMediaError(
                    f"The robot's daemon answered {method} {path} with {exc.code}: {_detail(exc)}"
                ) from exc
            except (urllib.error.URLError, OSError) as exc:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                if attempt == 1 and _transient(reason):
                    continue
                raise DaemonMediaError(
                    f"Cannot reach the robot's daemon at {url}: {reason}"
                ) from exc
            try:
                result = json.loads(payload) if payload else {}
            except ValueError:
                result = {}
            return result if isinstance(result, dict) else {}
        raise AssertionError("unreachable")  # pragma: no cover


def _transient(reason: object) -> bool:
    """Whether a failure is one a second try may get past: no connection, or no name."""
    return isinstance(reason, ConnectionError | socket.gaierror)


def _detail(error: urllib.error.HTTPError) -> str:
    """The ``detail`` of a FastAPI error answer, or its text."""
    try:
        text = error.read().decode(errors="replace")
    except OSError:
        return error.reason or ""
    try:
        detail = json.loads(text).get("detail")
    except (ValueError, AttributeError):
        return text.strip()
    return str(detail) if detail is not None else text.strip()
