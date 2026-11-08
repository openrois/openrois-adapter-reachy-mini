"""SpeechSynthesis: speaking with the head moving, cutting off, and the speech engines."""

from __future__ import annotations

import array
import asyncio
import json
import socket
import stat
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest

from openrois.components.reachy_mini import (
    PiperSynthesizer,
    ReachyMiniConnection,
    ReachyMiniSpeechSynthesis,
    Synthesizer,
    ToneSynthesizer,
)
from openrois.components.reachy_mini.daemon_media import DaemonMedia, DaemonMediaError
from openrois.components.reachy_mini.synthesizers import scale_wav
from tests._fakes import FakeDaemonHttp, FakeSdk
from tests.conftest import Host, Robot

FAST_TONE = ToneSynthesizer(seconds_per_character=0.01, min_seconds=0.05)


async def speech(host: Host, synthesizer: Synthesizer = FAST_TONE, *, wobble: bool = True) -> Robot:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    component = ReachyMiniSpeechSynthesis(connection, synthesizer, wobble=wobble, tail=0.0)
    return await host({"speech_synthesis": component})


def test_serves_the_common_commands_and_both_queries() -> None:
    profile = ReachyMiniSpeechSynthesis.rois_profile()
    assert [c.name for c in profile.command_profiles] == ["start", "stop", "suspend", "resume"]
    assert [q.name for q in profile.query_profiles] == [
        "component_status",
        "synthesizable_languages",
        "available_voices",
    ]
    assert profile.function == "actuation"


async def test_start_speaks_with_the_head_moving(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host)
    assert await robot.set("speech_synthesis", speech_text="Hello, I am Reachy Mini.") == "OK"
    assert await robot.run("speech_synthesis", "start") == "OK"
    [(name, seconds)] = sdk.robot().played
    assert name == "openrois-reachy-speech_synthesis-0.wav"
    assert seconds == pytest.approx(0.24, abs=0.01)
    assert sdk.robot().wobble_log == [True, False]
    assert sdk.robot().log == ["play"]


async def test_without_wobble_the_head_stays_still(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host, wobble=False)
    await robot.set("speech_synthesis", speech_text="Hello")
    assert await robot.run("speech_synthesis", "start") == "OK"
    assert sdk.robot().wobble_log == []


async def test_empty_text_fails_the_start(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host)
    assert await robot.run("speech_synthesis", "start") == "ERROR"
    assert sdk.robot().played == []


async def test_ssml_is_spoken_as_its_plain_text(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host)
    ssml = "<speak>Hello <break time='1s'/> world</speak>"
    await robot.set("speech_synthesis", ssml_text=ssml)
    assert await robot.run("speech_synthesis", "start") == "OK"
    [(_, seconds)] = sdk.robot().played
    assert seconds == pytest.approx(0.11, abs=0.01)  # "Hello world"


async def test_stop_cuts_the_voice_off(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host, ToneSynthesizer(seconds_per_character=0.5))
    await robot.set("speech_synthesis", speech_text="A long sentence to cut off.")
    start = await robot.execute("speech_synthesis", "start")
    async with asyncio.timeout(5.0):
        while not sdk.robot().played:  # The tone takes a moment to write.
            await asyncio.sleep(0.01)
    assert await robot.run("speech_synthesis", "stop") == "OK"
    assert await robot.completed(start) == "ABORT"
    assert sdk.robot().log == ["play", "stop_playing"]
    assert sdk.robot().wobble_log == [True, False]
    assert not sdk.robot().wobbling


async def test_without_the_daemon_the_start_fails_and_the_component_reports_error(
    host: Host, sdk: FakeSdk
) -> None:
    robot = await speech(host)
    await robot.set("speech_synthesis", speech_text="Hello")
    sdk.stop_daemon()
    assert await robot.run("speech_synthesis", "start") == "ERROR"
    assert await robot.status("speech_synthesis") == "ERROR"
    # Nothing plays, so the engine refuses the stop as any command while ERROR holds.
    assert await robot.run("speech_synthesis", "stop") == "ERROR"
    assert sdk.robot().played == []


async def test_the_parameters_are_checked(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host)
    assert await robot.set("speech_synthesis", volume="150") == "BAD_PARAMETER"
    assert await robot.set("speech_synthesis", character="robot") == "BAD_PARAMETER"
    assert await robot.set("speech_synthesis", character="tone", volume="80") == "OK"


async def test_the_tone_lists_its_voice_and_no_language(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host)
    languages = await robot.query("speech_synthesis", "synthesizable_languages")
    voices = await robot.query("speech_synthesis", "available_voices")
    assert json.loads(languages["languages"]) == []
    assert json.loads(voices["characters"]) == ["tone"]


def test_volume_scales_the_samples(tmp_path: Path) -> None:
    source = tmp_path / "in.wav"
    samples = array.array("h", [1000, -1000, 30000, -30000])
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(4)
        audio.writeframes(samples.tobytes())
    assert scale_wav(source, tmp_path / "out.wav", 2.0) == pytest.approx(1.0)
    with wave.open(str(tmp_path / "out.wav"), "rb") as audio:
        out = array.array("h", audio.readframes(4))
    assert list(out) == [2000, -2000, 32767, -32768]


@pytest.fixture
def piper(tmp_path: Path) -> tuple[Path, Path]:
    """Two Piper voices, and a stand-in piper that writes a tone where Piper writes speech."""
    voices = tmp_path / "voices"
    voices.mkdir()
    for name in ("en_US-lessac-medium", "es_ES-davefx-medium"):
        (voices / f"{name}.onnx").write_bytes(b"")
        (voices / f"{name}.onnx.json").write_text("{}")
    command = tmp_path / "piper"
    command.write_text(
        f"#!{sys.executable}\n"
        "import sys, wave\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('-f') + 1]\n"
        "if args[args.index('-m') + 1].startswith('es'):\n"
        "    sys.exit('no Spanish today')\n"
        "with wave.open(out, 'wb') as audio:\n"
        "    audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(22050)\n"
        "    audio.writeframes(b'\\x00\\x00' * 2205)\n"
    )
    command.chmod(command.stat().st_mode | stat.S_IEXEC)
    return voices, command


def test_piper_voices_and_languages(piper: tuple[Path, Path]) -> None:
    voices, command = piper
    synthesizer = PiperSynthesizer(voices, command=[str(command)])
    assert synthesizer.voices() == ["en_US-lessac-medium", "es_ES-davefx-medium"]
    assert synthesizer.languages() == ["en", "es"]
    assert synthesizer.voice("en", "default") == "en_US-lessac-medium"
    assert synthesizer.voice("en", "es_ES-davefx-medium") == "es_ES-davefx-medium"
    with pytest.raises(ValueError):
        synthesizer.voice("ja", "default")
    with pytest.raises(ValueError):
        PiperSynthesizer(voices / "empty")


async def test_piper_speaks_through_the_component(
    host: Host, sdk: FakeSdk, piper: tuple[Path, Path]
) -> None:
    voices, command = piper
    robot = await speech(host, PiperSynthesizer(voices, command=[str(command)]))
    assert await robot.set("speech_synthesis", language="ja") == "BAD_PARAMETER"
    await robot.set("speech_synthesis", speech_text="Hello")
    assert await robot.run("speech_synthesis", "start") == "OK"
    [(_, seconds)] = sdk.robot().played
    assert seconds == pytest.approx(0.1)
    # A failing Piper fails the start.
    await robot.set("speech_synthesis", language="es")
    assert await robot.run("speech_synthesis", "start") == "ERROR"


def write_wav(path: Path, samples: list[int], rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(array.array("h", samples).tobytes())


def peak(path: Path) -> int:
    with wave.open(str(path), "rb") as audio:
        samples = array.array("h", audio.readframes(audio.getnframes()))
    return max(abs(s) for s in samples)


class FullScale:
    """A speech engine that writes full-scale audio, as Piper normalizes its output."""

    def languages(self) -> list[str]:
        return []

    def voices(self) -> list[str]:
        return ["loud"]

    async def synthesize(self, text: str, language: str, character: str, path: Path) -> None:
        write_wav(path, [32767, -32768, 16000, -16000] * 400)


async def test_the_voice_plays_at_its_own_level_by_default_and_never_clips(
    host: Host, sdk: FakeSdk
) -> None:
    robot = await speech(host, FullScale())
    folder = robot.engine.local_components()[0]._folder  # type: ignore[attr-defined]
    await robot.set("speech_synthesis", speech_text="Hello")
    assert await robot.run("speech_synthesis", "start") == "OK"
    assert peak(folder / "openrois-reachy-speech_synthesis-0.wav") == 32768
    await robot.set("speech_synthesis", volume="100")
    assert await robot.run("speech_synthesis", "start") == "OK"
    assert peak(folder / "openrois-reachy-speech_synthesis-1.wav") == 32768
    await robot.set("speech_synthesis", volume="50")
    assert await robot.run("speech_synthesis", "start") == "OK"
    assert peak(folder / "openrois-reachy-speech_synthesis-2.wav") == 16384


async def test_the_default_voice_is_one_of_the_available_voices(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host)
    result = await robot.call(
        "rois.command.get_parameter", component_ref=robot.refs["speech_synthesis"]
    )
    values = {p["name"]: p["value"] for p in result["parameters"]}
    voices = json.loads((await robot.query("speech_synthesis", "available_voices"))["characters"])
    assert values["character"] == voices[0] == "tone"
    assert values["volume"] == "100"


async def test_the_first_piper_voice_is_the_default_and_follows_the_language(
    host: Host, sdk: FakeSdk, piper: tuple[Path, Path], tmp_path: Path
) -> None:
    voices, command = piper
    synthesizer = PiperSynthesizer(voices, command=[str(command)])
    robot = await speech(host, synthesizer)
    result = await robot.call(
        "rois.command.get_parameter", component_ref=robot.refs["speech_synthesis"]
    )
    values = {p["name"]: p["value"] for p in result["parameters"]}
    assert values["character"] == "en_US-lessac-medium"
    await robot.set("speech_synthesis", speech_text="Hola")
    # The default voice gives way to the first voice of the language, which fails here.
    await robot.set("speech_synthesis", language="es")
    assert await robot.run("speech_synthesis", "start") == "ERROR"
    await robot.set("speech_synthesis", language="en")
    assert await robot.run("speech_synthesis", "start") == "OK"
    # A component without its own profile default keeps the class profile as it is.
    assert ReachyMiniSpeechSynthesis.rois_profile().parameter_profiles[4].default_value == (
        "default"
    )


async def test_a_speech_engine_that_cannot_run_fails_the_start(
    host: Host, sdk: FakeSdk, piper: tuple[Path, Path], tmp_path: Path
) -> None:
    voices, _ = piper
    robot = await speech(host, PiperSynthesizer(voices, command=[str(tmp_path / "gone")]))
    await robot.set("speech_synthesis", speech_text="Hello")
    assert await robot.run("speech_synthesis", "start") == "ERROR"
    assert sdk.robot().played == []


async def test_a_stop_while_the_sound_is_on_its_way_stops_it_once_it_plays(
    host: Host, sdk: FakeSdk
) -> None:
    # The upload to a robot on the network takes a moment, and the stop comes first.
    sdk.robot().play_delay = 0.3
    robot = await speech(host, ToneSynthesizer(seconds_per_character=0.05))
    await robot.set("speech_synthesis", speech_text="A long sentence to cut off.")
    start = await robot.execute("speech_synthesis", "start")
    await asyncio.to_thread(sdk.robot().sending.wait, 5.0)
    assert await robot.run("speech_synthesis", "stop") == "OK"
    assert await robot.completed(start) == "ABORT"
    log = sdk.robot().log
    assert log.count("play") == 1
    assert log[-1] == "stop_playing" and log.index("play") < len(log) - 1
    assert not sdk.robot().wobbling


async def test_a_new_start_while_the_sound_is_on_its_way_uses_its_own_file(
    host: Host, sdk: FakeSdk
) -> None:
    sdk.robot().play_delay = 0.3
    robot = await speech(host)
    await robot.set("speech_synthesis", speech_text="The first sentence, a long one.")
    first = await robot.execute("speech_synthesis", "start")
    await asyncio.to_thread(sdk.robot().sending.wait, 5.0)
    await robot.set("speech_synthesis", speech_text="Then this.")
    second = await robot.execute("speech_synthesis", "start")
    assert await robot.completed(first) == "ABORT"
    assert await robot.completed(second, timeout=10.0) == "OK"
    names = [name for name, _ in sdk.robot().played]
    assert names == [
        "openrois-reachy-speech_synthesis-0.wav",
        "openrois-reachy-speech_synthesis-1.wav",
    ]
    # The first sentence was stopped before the second one played.
    log = sdk.robot().log
    assert log.index("stop_playing", log.index("play")) < len(log) - 1
    assert log[-1] == "play"


async def test_a_new_start_right_after_a_stop_stops_the_late_sound_first(
    host: Host, sdk: FakeSdk
) -> None:
    # The stop comes while the sound is on its way, and a new start at once, which
    # cancels the start that waits to stop the sound once it plays.
    sdk.robot().play_delay = 0.5
    robot = await speech(host, ToneSynthesizer(seconds_per_character=0.05))
    await robot.set("speech_synthesis", speech_text="A long sentence to cut off.")
    first = await robot.execute("speech_synthesis", "start")
    await asyncio.to_thread(sdk.robot().sending.wait, 5.0)
    assert await robot.run("speech_synthesis", "stop") == "OK"
    await robot.set("speech_synthesis", speech_text="", ssml_text="")
    second = await robot.execute("speech_synthesis", "start")
    assert await robot.completed(first) == "ABORT"
    assert await robot.completed(second) == "ERROR"
    log = sdk.robot().log
    assert log.count("play") == 1
    assert "stop_playing" in log[log.index("play") :]
    assert not sdk.robot().wobbling


async def network_speech(host: Host, daemon: FakeDaemonHttp) -> Robot:
    """SpeechSynthesis of a robot on the network, whose daemon plays the sounds."""
    connection = ReachyMiniConnection(host="127.0.0.1", port=daemon.port, connection_mode="network")
    component = ReachyMiniSpeechSynthesis(connection, FAST_TONE, tail=0.0)
    robot = await host({"speech_synthesis": component})
    await robot.set("speech_synthesis", speech_text="Hello")
    return robot


async def test_a_robot_on_the_network_plays_the_sound_through_its_daemon(
    host: Host, sdk: FakeSdk, daemon_http: FakeDaemonHttp
) -> None:
    robot = await network_speech(host, daemon_http)
    assert await robot.run("speech_synthesis", "start") == "OK"
    assert daemon_http.uploads == ["openrois-reachy-speech_synthesis-0.wav"]
    assert daemon_http.played == ["/tmp/reachy_mini_sounds/openrois-reachy-speech_synthesis-0.wav"]
    assert sdk.robot("127.0.0.1").wobble_log == [True, False]


@pytest.mark.parametrize(
    ("media", "play_status", "reason"),
    [
        ({"available": True, "released": False, "no_media": True}, 200, "without media"),
        ({"available": False, "released": True, "no_media": False}, 200, "released"),
        ({"available": True, "released": False, "no_media": False}, 503, "Backend not running"),
    ],
    ids=["no-media", "released", "refused"],
)
async def test_a_sound_the_daemon_cannot_play_fails_the_start(
    host: Host,
    sdk: FakeSdk,
    daemon_http: FakeDaemonHttp,
    caplog: pytest.LogCaptureFixture,
    media: dict[str, bool],
    play_status: int,
    reason: str,
) -> None:
    daemon_http.media = media
    daemon_http.play_status = play_status
    robot = await network_speech(host, daemon_http)
    assert await robot.run("speech_synthesis", "start") == "ERROR"
    assert daemon_http.played == []
    assert reason in caplog.text


async def test_a_stop_during_the_upload_plays_nothing(
    host: Host, sdk: FakeSdk, daemon_http: FakeDaemonHttp
) -> None:
    daemon_http.upload_delay = 0.3
    robot = await network_speech(host, daemon_http)
    start = await robot.execute("speech_synthesis", "start")
    await asyncio.sleep(0.15)
    assert await robot.run("speech_synthesis", "stop") == "OK"
    assert await robot.completed(start) == "ABORT"
    assert daemon_http.uploads == ["openrois-reachy-speech_synthesis-0.wav"]
    assert daemon_http.played == []


async def test_without_audio_the_start_fails(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host)
    connection = robot.engine.local_components()[0]._connection  # type: ignore[attr-defined]
    connection.current.media.audio = None  # As a daemon that runs without media.
    await robot.set("speech_synthesis", speech_text="Hello")
    assert await robot.run("speech_synthesis", "start") == "ERROR"
    assert sdk.robot().played == []


def daemon_media_failing(
    daemon: FakeDaemonHttp, monkeypatch: pytest.MonkeyPatch, *errors: BaseException
) -> tuple[DaemonMedia, list[str]]:
    """A DaemonMedia whose next requests fail with ``errors`` before they reach the daemon."""
    media = DaemonMedia(f"http://127.0.0.1:{daemon.port}")
    opened: list[str] = []
    real = media._opener.open
    pending = list(errors)

    def open_(request: urllib.request.Request, timeout: float) -> object:
        opened.append(request.full_url)
        if pending:
            raise pending.pop(0)
        return real(request, timeout=timeout)

    monkeypatch.setattr(media._opener, "open", open_)
    return media, opened


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
        urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
        ConnectionResetError(104, "Connection reset by peer"),
    ],
    ids=["refused", "name", "reset"],
)
def test_an_upload_is_tried_again_once_after_a_connection_error(
    daemon_http: FakeDaemonHttp,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: BaseException,
) -> None:
    write_wav(tmp_path / "hello.wav", [0] * 160)
    media, opened = daemon_media_failing(daemon_http, monkeypatch, error)
    assert media.upload(tmp_path / "hello.wav") == "/tmp/reachy_mini_sounds/hello.wav"
    assert len(opened) == 2
    assert daemon_http.uploads == ["hello.wav"]
    # Twice in a row is too many.
    media, opened = daemon_media_failing(daemon_http, monkeypatch, error, error)
    with pytest.raises(DaemonMediaError, match="Cannot reach"):
        media.upload(tmp_path / "hello.wav")
    assert len(opened) == 2


def test_other_upload_errors_are_not_tried_again(
    daemon_http: FakeDaemonHttp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_wav(tmp_path / "hello.wav", [0] * 160)
    media, opened = daemon_media_failing(
        daemon_http, monkeypatch, urllib.error.URLError(TimeoutError("timed out"))
    )
    with pytest.raises(DaemonMediaError):
        media.upload(tmp_path / "hello.wav")
    assert len(opened) == 1
    daemon_http.play_status = 500
    media = DaemonMedia(f"http://127.0.0.1:{daemon_http.port}")
    with pytest.raises(DaemonMediaError, match="500: Backend not running"):
        media.play("/tmp/reachy_mini_sounds/hello.wav")
