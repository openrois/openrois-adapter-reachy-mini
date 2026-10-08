"""SpeechSynthesis: speaking with the head moving, cutting off, and the speech engines."""

from __future__ import annotations

import array
import asyncio
import json
import stat
import sys
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
from openrois.components.reachy_mini.synthesizers import scale_wav
from tests._fakes import FakeSdk
from tests.conftest import Host, Robot

FAST_TONE = ToneSynthesizer(seconds_per_character=0.01, min_seconds=0.05)


async def speech(host: Host, synthesizer: Synthesizer = FAST_TONE, *, wobble: bool = True) -> Robot:
    connection = ReachyMiniConnection(connection_mode="localhost_only")
    component = ReachyMiniSpeechSynthesis(connection, synthesizer, wobble=wobble, tail=0.0)
    return await host({"speech_synthesis": component})


def test_serves_start_stop_and_both_queries() -> None:
    profile = ReachyMiniSpeechSynthesis.rois_profile()
    assert [c.name for c in profile.command_profiles] == ["start", "stop"]
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
    assert name == "openrois-reachy-speech_synthesis.wav"
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
    await asyncio.sleep(0.3)
    assert await robot.run("speech_synthesis", "stop") == "OK"
    assert await robot.completed(start) == "ABORT"
    assert sdk.robot().log == ["play", "stop_playing"]
    assert sdk.robot().wobble_log == [True, False]
    assert not sdk.robot().wobbling


async def test_the_parameters_are_checked(host: Host, sdk: FakeSdk) -> None:
    robot = await speech(host)
    assert await robot.set("speech_synthesis", volume="150") == "ERROR"
    assert await robot.set("speech_synthesis", character="robot") == "ERROR"
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
    assert await robot.set("speech_synthesis", language="ja") == "ERROR"
    await robot.set("speech_synthesis", speech_text="Hello")
    assert await robot.run("speech_synthesis", "start") == "OK"
    [(_, seconds)] = sdk.robot().played
    assert seconds == pytest.approx(0.1)
    # A failing Piper fails the start.
    await robot.set("speech_synthesis", language="es")
    assert await robot.run("speech_synthesis", "start") == "ERROR"
