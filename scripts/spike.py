"""Answer the open SDK questions of the step 6 design on a Reachy Mini.

Step 6 rebuilds this adapter on openrois-engine. Before any component is written, this
spike checks how the Reachy Mini SDK behaves in the installed version: connections, SDK
clients, move cancellation, faces, the emotions library, and speech. Run it once against
the simulator and once against the Wireless, and send back the two JSON reports.

    # The simulator, with reachy-mini-daemon --sim running on this laptop
    python scripts/spike.py --target sim

    # The Wireless on the network, with the simulator still running on this laptop
    python scripts/spike.py --target wireless --host reachy-mini.local --with-sim

Each run starts with a preflight: the SDK version, the GStreamer pieces this laptop
needs, and the daemon with its media. It stops at a missing piece and says how to add
it. Pass --preflight to run the preflight alone, or --skip-preflight to go on anyway.

The robot moves its head and antennas and plays sounds. Some checks ask you to stand in
front of the camera or to say what you heard. Pass --yes to run without questions, and
--checks to run some of the checks only. The no_media check runs last, because it makes
the daemon release its camera and audio for a moment.

It needs reachy-mini, at the version of the daemon, and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import math
import re
import statistics
import tempfile
import threading
import time
import traceback
import urllib.request
import wave
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from reachy_mini import ReachyMini
from reachy_mini.motion.move import Move
from reachy_mini.utils import create_head_pose

CHECKS = ("versions", "connect", "clients", "cancel", "faces", "emotions", "speech", "no_media")
MOTION_CHECKS = frozenset({"cancel", "emotions", "speech"})
MEDIA_CHECKS = frozenset({"clients", "faces", "speech", "no_media"})
EMOTIONS = "pollen-robotics/reachy-mini-emotions-library"
SPEECH_SAMPLE = "count.wav"

Facts = dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def package_version(name: str) -> str | None:
    """The installed version of a package, or None."""
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def importable(module: str, name: str) -> bool:
    """Whether the installed SDK has this name."""
    try:
        return hasattr(importlib.import_module(module), name)
    except ImportError:
        return False


def angles(pose: NDArray[np.float64]) -> dict[str, float]:
    """Roll, pitch and yaw in degrees, and the height in millimetres, of a 4x4 head pose."""
    r = np.asarray(pose, dtype=np.float64)
    return {
        "roll": math.degrees(math.atan2(r[2, 1], r[2, 2])),
        "pitch": math.degrees(math.asin(max(-1.0, min(1.0, -r[2, 0])))),
        "yaw": math.degrees(math.atan2(r[1, 0], r[0, 0])),
        "z_mm": float(r[2, 3]) * 1000.0,
    }


def connection(target: str, host: str) -> dict[str, Any]:
    """The ReachyMini arguments that reach the daemon of a target."""
    if target == "sim":
        return {"connection_mode": "localhost_only"}
    return {"connection_mode": "network", "host": host}


def open_client(conn: dict[str, Any], media_backend: str = "default") -> tuple[ReachyMini, float]:
    """Connect a new SDK client, and the seconds it took."""
    started = time.monotonic()
    mini = ReachyMini(media_backend=media_backend, **conn)
    return mini, round(time.monotonic() - started, 2)


def close(mini: ReachyMini) -> None:
    """Disconnect a client as its context manager does."""
    mini.__exit__(None, None, None)


def head_angles(mini: ReachyMini) -> dict[str, float]:
    """The present head pose of a client's robot, rounded."""
    return {k: round(v, 1) for k, v in angles(mini.get_current_head_pose()).items()}


def frame_info(mini: ReachyMini, timeout: float = 5.0) -> Facts:
    """Wait for a camera frame and describe it."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        frame = mini.media.get_frame()
        if frame is not None:
            return {"shape": list(frame.shape), "wait_s": round(time.monotonic() - started, 2)}
        time.sleep(0.1)
    return {"shape": None, "wait_s": timeout}


def daemon_status(mini: ReachyMini) -> Any:
    """The latest daemon status a client received."""
    return mini.client.get_status(wait=True, timeout=5.0)


def neutral(mini: ReachyMini) -> None:
    """Enable the motors and bring the head and antennas to rest."""
    with contextlib.suppress(Exception):
        mini.enable_motors()
    mini.goto_target(create_head_pose(), antennas=[0.0, 0.0], duration=1.0)


def wav_duration(path: Path) -> float | None:
    """The length of a PCM WAV file in seconds, or None when it cannot be read."""
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / float(audio.getframerate())
    except (OSError, wave.Error, EOFError):
        return None


def babble_wav(path: Path, seconds: float = 3.0, rate: int = 16000) -> Path:
    """Write a voice-like WAV: a buzz shaped into syllables, with pauses."""
    t = np.arange(int(seconds * rate)) / rate
    carrier = sum(
        np.sin(2 * np.pi * f * t) / (i + 1) for i, f in enumerate((180.0, 360.0, 540.0, 720.0))
    )
    syllables = 0.5 * (1 - np.cos(2 * np.pi * 4.0 * t))
    pauses = (np.sin(2 * np.pi * 0.7 * t) > -0.3).astype(np.float64)
    signal = carrier * syllables * pauses
    signal = 0.4 * signal / np.max(np.abs(signal))
    pcm = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm.tobytes())
    return path


def speech_sample() -> tuple[str, float]:
    """The SDK's bundled voice sample and its length."""
    module = importlib.import_module("reachy_mini")
    path = Path(module.__file__ or "").parent / "assets" / SPEECH_SAMPLE
    if path.is_file():
        return str(path), wav_duration(path) or 3.0
    return SPEECH_SAMPLE, 3.0


class PoseSampler:
    """Read the head pose in a thread, to see how the head moved during a check."""

    def __init__(self, mini: ReachyMini, hz: float = 30.0) -> None:
        self._mini = mini
        self._period = 1.0 / hz
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = time.monotonic()
        self.samples: list[tuple[float, dict[str, float]]] = []
        self.errors = 0

    def __enter__(self) -> PoseSampler:
        self._started = time.monotonic()
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def now(self) -> float:
        """Seconds since sampling started."""
        return time.monotonic() - self._started

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append((self.now(), angles(self._mini.get_current_head_pose())))
            except Exception:
                self.errors += 1
            self._stop.wait(self._period)

    def spread(self, since: float = 0.0, until: float = math.inf) -> dict[str, float]:
        """How far each angle moved between two times, in degrees and millimetres."""
        window = [a for t, a in self.samples if since <= t <= until]
        if not window:
            return {}
        return {
            k: round(max(a[k] for a in window) - min(a[k] for a in window), 2) for k in window[0]
        }

    def at(self, moment: float) -> dict[str, float]:
        """The sample closest to a time, rounded."""
        if not self.samples:
            return {}
        _, closest = min(self.samples, key=lambda s: abs(s[0] - moment))
        return {k: round(v, 1) for k, v in closest.items()}


class Sweep(Move):  # type: ignore[misc]
    """Turn the head left and right, a slow move that is easy to see stop."""

    def __init__(self, amplitude_deg: float = 20.0, duration: float = 4.0) -> None:
        self._amplitude = amplitude_deg
        self._duration = duration

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(
        self, t: float
    ) -> tuple[NDArray[np.float64] | None, NDArray[np.float64] | None, float | None]:
        yaw = self._amplitude * math.sin(2 * math.pi * t / self._duration)
        return create_head_pose(yaw=yaw), np.array([0.0, 0.0]), None


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

SDK_FLOOR = (1, 9)
DAEMON_PORT = 8000

GIR = "sudo apt install gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0"
PLUGINS_BASE = "sudo apt install gstreamer1.0-plugins-base"
PLUGINS_GOOD = "sudo apt install gstreamer1.0-plugins-good"
PLUGINS_BAD = "sudo apt install gstreamer1.0-plugins-bad, from GStreamer 1.24"
NICE = "sudo apt install gstreamer1.0-nice"
WEBRTC = (
    "build the WebRTC plugin of gst-plugins-rs 0.14.5, then link libgstrswebrtc.so "
    "into ~/.local/share/gstreamer-1.0/plugins"
)
BOTH = frozenset({"sim", "wireless"})

# The GStreamer elements the SDK and the simulator's daemon use on this laptop, the
# targets that need each one here, and the Ubuntu package that has it.
ELEMENTS: tuple[tuple[str, frozenset[str], str], ...] = (
    ("webrtcsink", frozenset({"sim"}), WEBRTC),
    ("webrtcsrc", frozenset({"wireless"}), WEBRTC),
    ("webrtcbin", BOTH, PLUGINS_BAD),
    ("nicesrc", BOTH, NICE),
    ("unixfdsink", frozenset({"sim"}), PLUGINS_BAD),
    ("unixfdsrc", frozenset({"sim"}), PLUGINS_BAD),
    ("playbin", BOTH, PLUGINS_BASE),
    ("appsrc", BOTH, PLUGINS_BASE),
    ("appsink", BOTH, PLUGINS_BASE),
    ("audioconvert", BOTH, PLUGINS_BASE),
    ("audioresample", BOTH, PLUGINS_BASE),
    ("opusenc", BOTH, PLUGINS_BASE),
    ("opusdec", BOTH, PLUGINS_BASE),
    ("jpegdec", BOTH, PLUGINS_GOOD),
    ("udpsrc", frozenset({"sim"}), PLUGINS_GOOD),
)
AUDIO_SINKS = ("pulsesink", "pipewiresink", "alsasink")


@dataclass
class Item:
    """One line of the preflight: what was checked, and how to fix it when it failed."""

    name: str
    ok: bool
    detail: str
    required: bool = True


def version_tuple(text: str | None) -> tuple[int, int]:
    """The major and minor version of a version string, or (0, 0)."""
    match = re.match(r"(\d+)\.(\d+)", text or "")
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def gstreamer() -> tuple[Item, Any]:
    """Load the GStreamer bindings the SDK loads, and the Gst module when they load."""
    name = "GStreamer bindings Gst, GstApp, GstPbutils"
    try:
        import gi

        for namespace in ("Gst", "GstApp", "GstPbutils"):
            gi.require_version(namespace, "1.0")
        from gi.repository import Gst

        Gst.init(None)
    except (ImportError, ValueError) as exc:
        return Item(name, False, f"{exc}. Fix: {GIR}"), None
    return Item(name, True, "loaded"), Gst


def daemon_items(host: str, sdk: str | None, needs_media: bool) -> list[Item]:
    """Whether the daemon on a host answers, at which version, and with its media."""
    url = f"http://{host}:{DAEMON_PORT}/api/daemon/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            status: dict[str, Any] = json.load(response)
    except (OSError, ValueError) as exc:
        start = "reachy-mini-daemon --sim" if host == "localhost" else "the robot"
        return [Item(f"daemon at {host}", False, f"{exc}. Fix: start {start}, or check the host")]

    daemon = status.get("version")
    if status.get("simulation_enabled"):
        kind = "simulation"
    elif status.get("mockup_sim_enabled"):
        kind = "mock-up simulation"
    else:
        kind = "robot"
    items = [Item(f"daemon at {host}", True, f"{status.get('robot_name')}, {kind}, {daemon}")]
    same = daemon is None or sdk is None or daemon == sdk
    detail = f"SDK {sdk}, daemon {daemon}"
    if not same:
        detail += f". Fix: install reachy-mini=={daemon}"
    items.append(Item(f"SDK at the version of the daemon at {host}", same, detail, required=False))
    name = f"daemon media at {host}"
    if status.get("no_media"):
        items.append(
            Item(name, False, "off. Fix: restart the daemon without --no-media", needs_media)
        )
    elif status.get("media_released"):
        detail = "released by an app. Fix: close that app, or restart the daemon"
        items.append(Item(name, False, detail, needs_media))
    elif "camera_specs_name" in status and not status["camera_specs_name"]:
        detail = (
            "the media server did not start. Fix: see 'Failed to initialize media server' "
            "in the daemon log"
        )
        items.append(Item(name, False, detail, needs_media))
    else:
        items.append(Item(name, True, f"on, camera {status.get('camera_specs_name')}", needs_media))
    return items


def preflight(args: argparse.Namespace, checks: list[str]) -> tuple[bool, Facts]:
    """Check this laptop and the daemons before the checks run, and print what to fix."""
    targets = {args.target} | ({"sim"} if args.with_sim else set())
    needs_media = bool(MEDIA_CHECKS & set(checks))
    sdk = package_version("reachy_mini")
    floor = ".".join(map(str, SDK_FLOOR))
    items = [
        Item(
            "Reachy Mini SDK",
            version_tuple(sdk) >= SDK_FLOOR,
            f"{sdk}, needs {floor} or later",
        )
    ]

    bindings, gst = gstreamer()
    bindings.required = needs_media
    items.append(bindings)
    if gst is not None:
        items.append(
            Item(
                "GStreamer 1.22 or later",
                tuple(gst.version()[:2]) >= (1, 22),
                gst.version_string(),
                needs_media,
            )
        )
        for element, wanted, fix in ELEMENTS:
            if wanted & targets:
                found = gst.ElementFactory.find(element) is not None
                detail = "found" if found else f"missing. Fix: {fix}"
                items.append(Item(f"element {element}", found, detail, needs_media))
        if "sim" in targets:
            sinks = [s for s in AUDIO_SINKS if gst.ElementFactory.find(s) is not None]
            detail = ", ".join(sinks) or "none. Fix: sudo apt install gstreamer1.0-pulseaudio"
            items.append(Item("an audio sink", bool(sinks), detail, needs_media))

    hosts = ["localhost" if args.target == "sim" else args.host]
    if args.with_sim and args.target != "sim":
        hosts.append("localhost")
    for host in hosts:
        items.extend(daemon_items(host, sdk, needs_media))

    media = "with media" if needs_media else "without media"
    print(f"Preflight for the {args.target} target, {media}", flush=True)
    for item in items:
        label = "ok" if item.ok else ("FAIL" if item.required else "warn")
        print(f"  {label:5} {item.name}: {item.detail}", flush=True)
    ok = all(item.ok for item in items if item.required)
    return ok, {"ok": ok, "targets": sorted(targets), "items": [asdict(i) for i in items]}


# ---------------------------------------------------------------------------
# Context and prompts
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """What every check works with."""

    args: argparse.Namespace
    conn: dict[str, Any]
    mini: ReachyMini
    connect_s: float

    @property
    def interactive(self) -> bool:
        return not self.args.yes

    @property
    def is_sim(self) -> bool:
        return bool(self.args.target == "sim")

    def say(self, text: str) -> None:
        print(f"    {text}", flush=True)

    def ask(self, question: str) -> str | None:
        """Ask the person at the robot, and record their answer as typed."""
        if not self.interactive:
            return None
        answer = input(f"    ? {question} [y/n, or a note] ").strip()
        return answer or None

    async def countdown(self, text: str, seconds: int = 3) -> None:
        self.say(text)
        if self.interactive:
            for left in range(seconds, 0, -1):
                print(f"      {left}...", flush=True)
                await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


async def check_versions(ctx: Context) -> Facts:
    """Q6 and Q8: the SDK and daemon versions, and what this SDK version has."""
    status = daemon_status(ctx.mini)
    fields = (
        "version",
        "robot_name",
        "wireless_version",
        "simulation_enabled",
        "mockup_sim_enabled",
        "no_media",
        "media_released",
        "camera_specs_name",
    )
    facts: Facts = {"sdk_version": package_version("reachy_mini")}
    facts.update({f"daemon_{f}": getattr(status, f, None) for f in fields})
    facts["media_backend"] = str(getattr(ctx.mini.media, "backend", None))
    facts["has"] = {
        "ReachyMini.enable_wobbling": hasattr(ctx.mini, "enable_wobbling"),
        "ReachyMini.cancel_move": hasattr(ctx.mini, "cancel_move"),
        "ReachyMini.start_head_tracking": hasattr(ctx.mini, "start_head_tracking"),
        "protocol.StopMoveCmd": importable("reachy_mini.io.protocol", "StopMoveCmd"),
        "vision.FaceDetector": importable("reachy_mini.vision.face_detector", "FaceDetector"),
        "motion.RecordedMoves": importable("reachy_mini.motion.recorded_move", "RecordedMoves"),
    }
    return facts


async def check_connect(ctx: Context) -> Facts:
    """Q1: reaching the daemon of this target, and the simulator next to the Wireless."""
    facts: Facts = {
        "connection": ctx.conn,
        "connect_s": ctx.connect_s,
        "connection_mode": str(getattr(ctx.mini, "connection_mode", None)),
        "daemon_host": getattr(ctx.mini.client, "host", None),
        "head": head_angles(ctx.mini),
    }
    if ctx.args.with_sim and not ctx.is_sim:
        sim, seconds = open_client(connection("sim", ""))
        try:
            facts["sim_alongside"] = {
                "connect_s": seconds,
                "daemon_host": getattr(sim.client, "host", None),
                "robot_name": getattr(daemon_status(sim), "robot_name", None),
                "head": head_angles(sim),
                "wireless_head_still_readable": bool(head_angles(ctx.mini)),
            }
        finally:
            close(sim)
    return facts


async def check_clients(ctx: Context) -> Facts:
    """Q2: one SDK client per component, four clients on one daemon, each with media."""
    facts: Facts = {"main_frame": frame_info(ctx.mini)}
    with contextlib.ExitStack() as stack:
        extra: list[tuple[ReachyMini, float]] = []
        for _ in range(3):
            mini, seconds = open_client(ctx.conn)
            stack.callback(close, mini)
            extra.append((mini, seconds))
        facts["extra_connect_s"] = [s for _, s in extra]
        facts["extra_media_backend"] = [str(getattr(m.media, "backend", None)) for m, _ in extra]
        facts["extra_heads"] = [head_angles(m) for m, _ in extra]
        facts["extra_frames"] = [frame_info(m) for m, _ in extra]
        facts["main_frame_with_four_clients"] = frame_info(ctx.mini)
    facts["main_frame_after"] = frame_info(ctx.mini)
    facts["main_head_after"] = head_angles(ctx.mini)
    return facts


async def check_cancel(ctx: Context) -> Facts:
    """Q3: ending a client-side move, cancel_move, and stopping a goto in the daemon."""
    mini = ctx.mini
    facts: Facts = {}
    neutral(mini)

    # A client-side move ends when its asyncio task is cancelled.
    with PoseSampler(mini) as poses:
        task = asyncio.create_task(mini.async_play_move(Sweep(20.0, 4.0), sound=False))
        await asyncio.sleep(1.5)
        stopped_at = poses.now()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.0)
    facts["task_cancel"] = {
        "yaw_range_before": poses.spread(0.0, stopped_at).get("yaw"),
        "yaw_range_after": poses.spread(stopped_at + 0.3).get("yaw"),
        "head_at_cancel": poses.at(stopped_at),
        "pose_read_errors": poses.errors,
    }

    # cancel_move ends the same loop, and calls media.stop_playing.
    neutral(mini)
    with PoseSampler(mini) as poses:
        task = asyncio.create_task(mini.async_play_move(Sweep(20.0, 4.0), sound=False))
        await asyncio.sleep(1.5)
        stopped_at = poses.now()
        mini.cancel_move()
        await asyncio.wait_for(task, timeout=3.0)
        ended_after = poses.now() - stopped_at
        await asyncio.sleep(1.0)
    facts["cancel_move"] = {
        "move_ended_after_s": round(ended_after, 2),
        "yaw_range_after": poses.spread(stopped_at + 0.3).get("yaw"),
    }

    # goto_target blocks until the daemon ends the move, and StopMoveCmd ends it early.
    try:
        from reachy_mini.io.protocol import StopMoveCmd
    except ImportError:
        facts["stop_move"] = {"skipped": "this SDK has no StopMoveCmd"}
    else:
        neutral(mini)
        with PoseSampler(mini) as poses:
            goto = asyncio.create_task(
                asyncio.to_thread(mini.goto_target, create_head_pose(yaw=-25.0), duration=3.0)
            )
            await asyncio.sleep(1.0)
            stopped_at = poses.now()
            mini.client.send_command(StopMoveCmd())
            error = None
            try:
                await asyncio.wait_for(goto, timeout=6.0)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            returned_after = poses.now() - stopped_at
            await asyncio.sleep(1.0)
        facts["stop_move"] = {
            "goto_returned_after_s": round(returned_after, 2),
            "goto_error": error,
            "head_at_stop": poses.at(stopped_at),
            "head_at_end": poses.at(poses.now()),
            "yaw_range_after": poses.spread(stopped_at + 0.3).get("yaw"),
            "note": "The goto aimed at yaw -25. A head near the yaw at stop means it stopped.",
        }
    neutral(mini)
    return facts


async def check_faces(ctx: Context) -> Facts:
    """Q4: counting faces without moving the head, in the daemon or from camera frames."""
    mini = ctx.mini
    facts: Facts = {}
    detector: Any = None
    try:
        from reachy_mini.vision.face_detector import FaceDetector

        started = time.monotonic()
        detector = FaceDetector()
        facts["detector_load_s"] = round(time.monotonic() - started, 2)
    except Exception as exc:
        facts["detector_error"] = f"{type(exc).__name__}: {exc}"

    tracking = hasattr(mini, "start_head_tracking")
    if tracking:
        mini.start_head_tracking(weight=ctx.args.tracking_weight)
    facts["tracking_weight"] = ctx.args.tracking_weight if tracking else None

    phases = (
        ("in_view", "Stand in front of the robot, about one metre away, and face it."),
        ("out_of_view", "Step out of the camera's view."),
    )
    seconds = ctx.args.face_seconds / 2
    for name, instruction in phases:
        if ctx.is_sim:
            ctx.say(f"{name}: the simulator renders no faces, so this checks the pipeline only.")
        else:
            await ctx.countdown(instruction)
        counts: list[int] = []
        detect_ms: list[float] = []
        tracker: list[bool] = []
        missing = 0
        with PoseSampler(mini) as poses:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                frame = mini.media.get_frame()
                if frame is None:
                    missing += 1
                elif detector is not None:
                    started = time.monotonic()
                    counts.append(len(detector.detect(frame)))
                    detect_ms.append((time.monotonic() - started) * 1000)
                if tracking:
                    face = mini.get_tracked_face(wait=False)
                    tracker.append(bool(getattr(face, "detected", False)))
                await asyncio.sleep(0.1)
        facts[name] = {
            "frames": len(counts),
            "frames_missing": missing,
            "faces_max": max(counts, default=None),
            "faces_mean": round(statistics.fmean(counts), 2) if counts else None,
            "detect_ms_median": round(statistics.median(detect_ms), 1) if detect_ms else None,
            "tracker_detected_ratio": (round(sum(tracker) / len(tracker), 2) if tracker else None),
            "head_spread": poses.spread(),
        }

    if tracking:
        mini.start_head_tracking(weight=0.0)
        await asyncio.sleep(1.5)
        face = mini.get_tracked_face(wait=False)
        facts["tracker_detected_at_weight_0"] = bool(getattr(face, "detected", False))
        mini.stop_head_tracking()
    return facts


async def check_emotions(ctx: Context) -> Facts:
    """Q5: loading the emotions library, and playing a move without its sound."""
    from reachy_mini.motion.recorded_move import RecordedMoves

    facts: Facts = {}
    started = time.monotonic()
    library = RecordedMoves(EMOTIONS)
    facts["load_s"] = round(time.monotonic() - started, 2)
    names = sorted(library.list_moves())
    facts["moves"] = len(names)
    facts["sample"] = names[:15]
    wanted = ("cheerful", "happy", "yes", "welcoming")
    picked = next((n for w in wanted for n in names if w in n), names[0] if names else None)
    if picked is None:
        return facts
    move = library.get(picked)
    sound = getattr(move, "sound_path", None)
    facts["picked"] = {
        "name": picked,
        "duration_s": round(float(move.duration), 2),
        "has_sound": sound is not None,
    }
    neutral(ctx.mini)
    with PoseSampler(ctx.mini) as poses:
        started = time.monotonic()
        await ctx.mini.async_play_move(move, initial_goto_duration=1.0, sound=False)
        facts["played_s"] = round(time.monotonic() - started, 2)
    facts["head_spread"] = poses.spread()
    facts["silent"] = ctx.ask(f"Was {picked} played without a sound?")
    neutral(ctx.mini)
    return facts


async def check_speech(ctx: Context) -> Facts:
    """Q7: an uploaded WAV, the bundled voice, wobbling, stop_playing, and a move at once."""
    mini = ctx.mini
    media = mini.media
    facts: Facts = {"media_backend": str(getattr(media, "backend", None))}
    neutral(mini)
    where = "this laptop" if ctx.is_sim else "the robot's speaker"

    async def play(sound: str, seconds: float, wobble: bool) -> dict[str, float]:
        if wobble:
            mini.enable_wobbling()
        try:
            with PoseSampler(mini) as poses:
                await asyncio.sleep(0.3)
                media.play_sound(sound)
                await asyncio.sleep(seconds + 0.5)
        finally:
            if wobble:
                mini.disable_wobbling()
        await asyncio.sleep(0.5)
        return poses.spread()

    voice, voice_s = speech_sample()
    facts["voice_sample"] = {"file": voice, "duration_s": round(voice_s, 2)}

    with tempfile.TemporaryDirectory() as folder:
        babble = babble_wav(Path(folder) / "openrois-spike-babble.wav")
        babble_s = wav_duration(babble) or 3.0
        ctx.say(f"Playing a buzzing babble, uploaded from this laptop, on {where}.")
        facts["uploaded_wav"] = {
            "head_spread_wobbling": await play(str(babble), babble_s, wobble=True),
            "heard": ctx.ask(f"Did you hear a buzzing babble from {where}?"),
        }

    ctx.say("Playing the bundled voice sample without wobbling, then with it.")
    still = await play(voice, voice_s, wobble=False)
    moving = await play(voice, voice_s, wobble=True)
    facts["voice"] = {
        "head_spread_still": still,
        "head_spread_wobbling": moving,
        "heard": ctx.ask(f"Did you hear a voice twice from {where}?"),
        "wobbled": ctx.ask("Did the head move with the voice the second time only?"),
    }
    if moving.get("pitch", 0.0) < 0.3 and moving.get("roll", 0.0) < 0.3:
        ctx.say("No wobble measured. Trying again after media.start_playing().")
        media.start_playing()
        facts["voice"]["head_spread_wobbling_after_start_playing"] = await play(
            voice, voice_s, wobble=True
        )

    ctx.say("Playing the voice and stopping it after about a second.")
    media.play_sound(voice)
    await asyncio.sleep(1.0)
    started = time.monotonic()
    media.stop_playing()
    facts["stop_playing"] = {
        "call_s": round(time.monotonic() - started, 2),
        "cut_short": ctx.ask("Did the voice stop after about a second?"),
    }
    await asyncio.sleep(max(0.0, voice_s - 1.0))

    ctx.say("Turning the head while the voice plays, with wobbling on.")
    neutral(mini)
    mini.enable_wobbling()
    try:
        with PoseSampler(mini) as poses:
            move = asyncio.create_task(
                mini.async_play_move(Sweep(15.0, voice_s + 0.5), sound=False)
            )
            await asyncio.sleep(0.2)
            media.play_sound(voice)
            await move
    finally:
        mini.disable_wobbling()
    facts["voice_with_move"] = {
        "head_spread": poses.spread(),
        "pose_read_errors": poses.errors,
        "both": ctx.ask("Did the head turn and wobble while the voice played?"),
    }
    neutral(mini)
    return facts


async def check_no_media(ctx: Context) -> Facts:
    """Q2: what a client created with media_backend="no_media" does to the daemon."""
    mini = ctx.mini
    voice, voice_s = speech_sample()
    facts: Facts = {
        "media_released_before": getattr(daemon_status(mini), "media_released", None),
        "main_frame_before": frame_info(mini),
    }
    other, seconds = open_client(ctx.conn, media_backend="no_media")
    try:
        await asyncio.sleep(1.0)
        facts["no_media_connect_s"] = seconds
        facts["media_released_while_open"] = getattr(daemon_status(mini), "media_released", None)
        facts["main_frame_while_open"] = frame_info(mini, timeout=3.0)
        ctx.say("Playing the voice from the main client while the no_media client is open.")
        try:
            mini.media.play_sound(voice)
            await asyncio.sleep(voice_s + 0.5)
            facts["main_play_error"] = None
        except Exception as exc:
            facts["main_play_error"] = f"{type(exc).__name__}: {exc}"
        facts["main_heard_while_open"] = ctx.ask("Did you hear the voice?")
    finally:
        close(other)
    await asyncio.sleep(2.0)
    facts["media_released_after"] = getattr(daemon_status(mini), "media_released", None)
    facts["main_frame_after"] = frame_info(mini)
    return facts


CHECK_FUNCTIONS: dict[str, Callable[[Context], Awaitable[Facts]]] = {
    "versions": check_versions,
    "connect": check_connect,
    "clients": check_clients,
    "cancel": check_cancel,
    "faces": check_faces,
    "emotions": check_emotions,
    "speech": check_speech,
    "no_media": check_no_media,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_check(ctx: Context, name: str) -> Facts:
    started = time.monotonic()
    try:
        facts = await CHECK_FUNCTIONS[name](ctx)
        result: Facts = {"status": "ok", "facts": facts}
    except Exception as exc:
        result = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=6),
        }
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


async def run(args: argparse.Namespace, checks: list[str], ready: Facts | None) -> Facts:
    conn = connection(args.target, args.host)
    report: Facts = {
        "spike": "openrois-adapter-reachy-mini step 6 batch 1",
        "target": args.target,
        "connection": conn,
        "started": datetime.now(UTC).isoformat(),
        "preflight": ready,
        "checks": {},
    }
    print(f"Connecting to the {args.target} daemon...", flush=True)
    mini, seconds = open_client(conn)
    ctx = Context(args=args, conn=conn, mini=mini, connect_s=seconds)
    try:
        for name in checks:
            print(f"\n== {name}", flush=True)
            result = await run_check(ctx, name)
            report["checks"][name] = result
            print(f"   {result['status']} in {result['seconds']} s", flush=True)
            if result["status"] == "error":
                print(f"   {result['error']}", flush=True)
    finally:
        with contextlib.suppress(Exception):
            neutral(mini)
        close(mini)
        report["finished"] = datetime.now(UTC).isoformat()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=("sim", "wireless"), required=True)
    parser.add_argument("--host", default="reachy-mini.local", help="the Wireless host name or IP")
    parser.add_argument(
        "--with-sim",
        action="store_true",
        help="with --target wireless, also connect to the simulator on this laptop",
    )
    parser.add_argument(
        "--checks",
        default=",".join(CHECKS),
        help=f"comma-separated checks, from {', '.join(CHECKS)}",
    )
    parser.add_argument("--face-seconds", type=float, default=12.0)
    parser.add_argument(
        "--tracking-weight",
        type=float,
        default=0.001,
        help="the head tracking weight during the faces check, small so the head barely moves",
    )
    parser.add_argument("--yes", action="store_true", help="ask nothing, and start at once")
    parser.add_argument("--report", type=Path, help="where to write the JSON report")
    parser.add_argument("--preflight", action="store_true", help="run the preflight alone")
    parser.add_argument(
        "--skip-preflight", action="store_true", help="run the checks when the preflight fails"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    wanted = [c.strip() for c in args.checks.split(",") if c.strip()]
    unknown = sorted(set(wanted) - set(CHECKS))
    if unknown:
        raise SystemExit(f"Unknown checks: {', '.join(unknown)}")
    checks = [c for c in CHECKS if c in wanted]
    ok, ready = preflight(args, checks)
    if args.preflight:
        raise SystemExit(0 if ok else 1)
    if not ok and not args.skip_preflight:
        raise SystemExit(
            "\nFix the FAIL lines above and run again. Without media, run the checks that "
            "need none: --checks versions,connect,cancel,emotions"
        )
    print()
    if MOTION_CHECKS & set(checks) and not args.yes:
        input(
            "The robot will move its head and antennas and play sounds. "
            "Clear the space around it, then press Enter (Ctrl+C to stop). "
        )
    report = asyncio.run(run(args, checks, ready))
    path = args.report or Path(f"spike-{args.target}.json")
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nReport written to {path}")


if __name__ == "__main__":
    main()
