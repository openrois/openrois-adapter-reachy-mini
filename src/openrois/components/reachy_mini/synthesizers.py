"""Speech engines for the SpeechSynthesis component: text in, a WAV file out.

``PiperSynthesizer`` runs Piper, an offline neural speech engine, as its own program.
Piper is GPL-3.0-or-later, so the adapter calls the installed ``piper`` command and never
imports it. ``ToneSynthesizer`` writes a voice-like tone instead of speech, for tests and
for a robot without a speech engine.
"""

from __future__ import annotations

import array
import asyncio
import math
import sys
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

DEFAULT_CHARACTER = "default"


class Synthesizer(Protocol):
    """A speech engine for the SpeechSynthesis component."""

    def languages(self) -> list[str]:
        """The languages it speaks, as codes such as ``en``. Empty when it speaks none."""
        ...

    def voices(self) -> list[str]:
        """The voice characters it offers."""
        ...

    async def synthesize(self, text: str, language: str, character: str, path: Path) -> None:
        """Write ``text`` spoken in ``language`` with the voice ``character`` to a WAV file.

        ``character`` is one of ``voices()``, or ``default`` for the first voice of the
        language.

        Raises:
            ValueError: No voice speaks the language, or no voice has that name.
            RuntimeError: The engine failed.
        """
        ...


class ToneSynthesizer:
    """A voice-like tone instead of speech: a buzz shaped into syllables.

    It speaks no language, and its tone lasts as long as the text would take to say.

    Args:
        seconds_per_character: How long the tone lasts for each character of the text.
        min_seconds: The shortest tone.
        max_seconds: The longest tone.
        rate: The sample rate of the WAV file, in hertz.
    """

    def __init__(
        self,
        *,
        seconds_per_character: float = 0.06,
        min_seconds: float = 0.6,
        max_seconds: float = 10.0,
        rate: int = 16000,
    ) -> None:
        self._seconds_per_character = seconds_per_character
        self._min_seconds = min_seconds
        self._max_seconds = max_seconds
        self._rate = rate

    def languages(self) -> list[str]:
        return []

    def voices(self) -> list[str]:
        return ["tone"]

    async def synthesize(self, text: str, language: str, character: str, path: Path) -> None:
        seconds = min(
            max(len(text) * self._seconds_per_character, self._min_seconds), self._max_seconds
        )
        await asyncio.to_thread(_write_tone, path, seconds, self._rate)


class PiperSynthesizer:
    """Speech from Piper, run as its own program for each sentence.

    A voice is a Piper model in ``voices_dir``, for example
    ``en_US-lessac-medium.onnx`` with its ``.onnx.json``, which
    ``python -m piper.download_voices en_US-lessac-medium --data-dir DIR`` downloads. The
    language of a voice is the start of its name: ``en`` for ``en_US-lessac-medium``.

    Args:
        voices_dir: The folder of the Piper voices.
        command: The command that runs Piper, for example ``("piper",)`` or the full path
            of the ``piper`` of its own virtual environment.
    """

    def __init__(self, voices_dir: Path, *, command: Sequence[str] = ("piper",)) -> None:
        self._voices_dir = Path(voices_dir)
        self._command = tuple(command)
        if not self.voices():
            raise ValueError(f"No Piper voice (*.onnx) in {self._voices_dir}")

    def voices(self) -> list[str]:
        return sorted(p.name.removesuffix(".onnx") for p in self._voices_dir.glob("*.onnx"))

    def languages(self) -> list[str]:
        return sorted({_language(v) for v in self.voices()})

    def voice(self, language: str, character: str) -> str:
        """The voice to speak with: the one named, or the first of the language."""
        voices = self.voices()
        if character != DEFAULT_CHARACTER:
            if character not in voices:
                raise ValueError(f"No Piper voice is named {character!r}")
            return character
        for voice in voices:
            if _language(voice) == language:
                return voice
        raise ValueError(f"No Piper voice speaks {language!r}")

    async def synthesize(self, text: str, language: str, character: str, path: Path) -> None:
        voice = self.voice(language, character)
        process = await asyncio.create_subprocess_exec(
            *self._command,
            "-m",
            voice,
            "--data-dir",
            str(self._voices_dir),
            "-f",
            str(path),
            "--",
            text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, errors = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            message = errors.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
            raise RuntimeError(f"Piper failed with code {process.returncode}: {message[0]}")


def _language(voice: str) -> str:
    """The language of a Piper voice: ``en`` for ``en_US-lessac-medium``."""
    return voice.split("-", 1)[0].split("_", 1)[0]


def _write_tone(path: Path, seconds: float, rate: int) -> None:
    """Write a buzz shaped into syllables, with pauses, as 16-bit mono PCM."""
    samples = array.array("h")
    for n in range(int(seconds * rate)):
        t = n / rate
        carrier = sum(
            math.sin(2 * math.pi * f * t) / (i + 1) for i, f in enumerate((180.0, 360.0, 540.0))
        )
        syllables = 0.5 * (1 - math.cos(2 * math.pi * 4.0 * t))
        pause = 1.0 if math.sin(2 * math.pi * 0.7 * t) > -0.3 else 0.0
        samples.append(int(7000 * carrier * syllables * pause))
    if sys.byteorder == "big":
        samples.byteswap()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples.tobytes())


def scale_wav(source: Path, target: Path, gain: float) -> float:
    """Copy a WAV file with its 16-bit samples multiplied by ``gain``, and return its length.

    A file of another sample width is copied as it is.
    """
    with wave.open(str(source), "rb") as audio:
        params = audio.getparams()
        frames = audio.readframes(params.nframes)
    if params.sampwidth == 2 and gain != 1.0:
        samples = array.array("h")
        samples.frombytes(frames)
        if sys.byteorder == "big":
            samples.byteswap()
        for i, sample in enumerate(samples):
            samples[i] = max(-32768, min(32767, int(sample * gain)))
        if sys.byteorder == "big":
            samples.byteswap()
        frames = samples.tobytes()
    with wave.open(str(target), "wb") as audio:
        audio.setparams(params)
        audio.writeframes(frames)
    return params.nframes / float(params.framerate)
