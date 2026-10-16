"""SpeechSynthesis for the Reachy Mini.

``start`` speaks ``speech_text`` in ``language`` with the voice ``character``, at
``volume``, and ends when the voice ends. ``ssml_text`` is spoken as its plain text when
``speech_text`` is empty. While the robot speaks, the head moves with the voice: the SDK
turns the audio into small head offsets that the daemon adds to whatever the head does,
a Reaction included. ``stop``, or a new ``start``, cuts the voice off, with ABORT.

The speech engine writes a WAV file, which the SDK plays on the robot's speaker: for a
robot on the network it uploads the file to the daemon. ``synthesizable_languages`` and
``available_voices`` list what the speech engine offers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openrois.components.core import (
    CommandFailed,
    Component,
    component,
    invoke,
    on_set_parameter,
    query,
)
from openrois.interfaces.components import SPEECH_SYNTHESIS_PROFILE
from openrois.interfaces.service import CompletedStatus

from openrois.components.reachy_mini.connection import ReachyMiniConnection
from openrois.components.reachy_mini.synthesizers import (
    DEFAULT_CHARACTER,
    Synthesizer,
    ToneSynthesizer,
    scale_wav,
)

logger = logging.getLogger(__name__)

# The volume parameter at which the voice plays as the speech engine wrote it.
_UNITY_VOLUME = 50


@component(SPEECH_SYNTHESIS_PROFILE)
class ReachyMiniSpeechSynthesis(Component):
    """SpeechSynthesis of one Reachy Mini, with the head moving to the voice.

    Args:
        connection: The connection to the robot, shared with its other components.
        synthesizer: The speech engine. Without one, the robot plays a voice-like tone.
        wobble: Whether the head moves with the voice.
        tail: How long a start waits after the end of the audio, in seconds, for the
            speaker to finish.
    """

    def __init__(
        self,
        connection: ReachyMiniConnection,
        synthesizer: Synthesizer | None = None,
        *,
        wobble: bool = True,
        tail: float = 0.3,
    ) -> None:
        self._connection = self.uses(connection)
        self._synthesizer: Synthesizer = synthesizer or ToneSynthesizer()
        self._wobble = wobble
        self._tail = tail
        self._folder: Path | None = None

    async def connect(self) -> None:
        self._folder = Path(tempfile.mkdtemp(prefix="openrois-speech-"))

    async def disconnect(self) -> None:
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)
            self._folder = None

    @query("synthesizable_languages")
    async def synthesizable_languages(self) -> dict[str, object]:
        return {"languages": self._synthesizer.languages()}

    @query("available_voices")
    async def available_voices(self) -> dict[str, object]:
        return {"characters": self._synthesizer.voices()}

    @on_set_parameter
    async def check(self, values: Mapping[str, Any]) -> None:
        volume = values.get("volume")
        if volume is not None and not 0 <= volume <= 100:
            raise ValueError(f"volume is from 0 to 100, not {volume}")
        languages = self._synthesizer.languages()
        language = values.get("language")
        if language is not None and languages and language not in languages:
            raise ValueError(f"No voice speaks {language!r}. See synthesizable_languages")
        character = values.get("character")
        voices = [DEFAULT_CHARACTER, *self._synthesizer.voices()]
        if character is not None and character not in voices:
            raise ValueError(f"No voice is named {character!r}. See available_voices")

    @invoke("start")
    async def start(self) -> None:
        text = self._text()
        if not text:
            raise CommandFailed(CompletedStatus.ERROR, "speech_text is empty")
        parameters = self.parameters
        language = str(parameters.get("language", "en"))
        character = str(parameters.get("character", DEFAULT_CHARACTER))
        volume = int(parameters.get("volume", _UNITY_VOLUME))

        folder = self._workspace()
        spoken = folder / "spoken.wav"
        # The file name the daemon keeps: one per component, replaced by each sentence.
        played = folder / f"openrois-{self.ref.replace('/', '-')}.wav"
        try:
            await self._synthesizer.synthesize(text, language, character, spoken)
        except ValueError as exc:
            raise CommandFailed(CompletedStatus.ERROR, str(exc)) from exc
        seconds = await asyncio.to_thread(scale_wav, spoken, played, volume / _UNITY_VOLUME)

        try:
            await self._speak(played, seconds, text)
        except ConnectionError as exc:
            raise CommandFailed(CompletedStatus.ERROR, f"No Reachy Mini daemon: {exc}") from exc

    async def _speak(self, played: Path, seconds: float, text: str) -> None:
        mini = await self._connection.client()
        logger.info("Speaking %.1f s: %s", seconds, text)
        if self._wobble:
            await asyncio.to_thread(mini.enable_wobbling)
        try:
            await asyncio.to_thread(mini.media.play_sound, str(played))
            # A stop, or a new start, cancels the wait here, after the stop handler cut
            # the voice off, and the start ends with ABORT.
            await asyncio.sleep(seconds + self._tail)
        finally:
            if self._wobble:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(mini.disable_wobbling)

    @invoke("stop")
    async def stop(self) -> None:
        # Without a daemon nothing plays, and the engine aborts the start all the same.
        mini = self._connection.current
        if mini is None:
            return
        try:
            await asyncio.to_thread(mini.media.stop_playing)
        except Exception as exc:
            logger.warning("Could not cut the voice off: %s", exc)

    def _text(self) -> str:
        """The text to speak: speech_text, or the plain text of ssml_text."""
        text = str(self.parameters.get("speech_text", "")).strip()
        if text:
            return text
        ssml = str(self.parameters.get("ssml_text", ""))
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", ssml)).strip()

    def _workspace(self) -> Path:
        if self._folder is None:
            raise CommandFailed(CompletedStatus.ERROR, "The component is not connected")
        return self._folder
