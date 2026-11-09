"""SpeechSynthesis for the Reachy Mini.

``start`` speaks ``speech_text`` in ``language`` with the voice ``character``, at
``volume``, and ends when the voice ends. ``ssml_text`` is spoken as its plain text when
``speech_text`` is empty. While the robot speaks, the head moves with the voice: the SDK
turns the audio into small head offsets that the daemon adds to whatever the head does,
a Reaction included. ``stop``, or a new ``start``, cuts the voice off, with ABORT, also
when it comes while the sound is still on its way to the robot.

``volume`` is the share of the voice's full level, from 0 to 100, and 100 by default:
the voice plays as the speech engine wrote it, which Piper normalizes to full scale, so
a higher level would only distort it. ``character`` is the first of ``available_voices``
until a client picks another, and the first voice stands for the first voice of
``language``. ``default`` names the same voice.

The speech engine writes a WAV file. For the simulator on this machine the SDK plays it.
For a robot on the network the component uploads it to the daemon and asks the daemon
to play it, and a start whose sound the daemon refuses, or whose daemon has no media
running, ends with ERROR rather than OK. A sound that fails on the robot once it plays
cannot be seen from here. ``synthesizable_languages`` and ``available_voices`` list what
the speech engine offers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openrois.components.core import (
    CommandFailed,
    Component,
    command,
    component,
    query,
)
from openrois.interfaces.components import SPEECH_SYNTHESIS_PROFILE
from openrois.interfaces.profiles import HRIComponentProfile
from openrois.interfaces.service import CompletedStatus

from openrois.components.reachy_mini.connection import ReachyMiniConnection
from openrois.components.reachy_mini.daemon_media import DaemonMedia, DaemonMediaError
from openrois.components.reachy_mini.synthesizers import (
    DEFAULT_CHARACTER,
    Synthesizer,
    ToneSynthesizer,
    scale_wav,
)

logger = logging.getLogger(__name__)

# The volume parameter at which the voice plays as the speech engine wrote it.
_FULL_VOLUME = 100

# The sentences in turn use this many file names, so a sentence never overwrites the
# file of the one before it, here or on the daemon, and the daemon keeps a few files.
_SLOTS = 3

# How long a cut-off start waits for a sound still on its way to the robot, in seconds,
# to stop it once it plays.
_LATE_PLAY = 15.0


def _with_default(profile: HRIComponentProfile, name: str, value: str) -> HRIComponentProfile:
    """A profile whose parameter ``name`` defaults to ``value``."""
    return profile.model_copy(
        update={
            "parameter_profiles": [
                p.model_copy(update={"default_value": value}) if p.name == name else p
                for p in profile.parameter_profiles
            ]
        }
    )


#: SpeechSynthesis with 100 as the default of volume: the voice at its own level.
SPEECH_SYNTHESIS_AT_FULL_VOLUME = _with_default(
    SPEECH_SYNTHESIS_PROFILE, "volume", str(_FULL_VOLUME)
)


class _Utterance:
    """One sentence on its way to the speaker, which a stop may cut off."""

    def __init__(self) -> None:
        #: Set by a stop: the sound must not start to play any more.
        self.cancelled = threading.Event()
        #: Set once the sound plays.
        self.playing = threading.Event()
        #: Whether a stop cut the sound off after it started to play.
        self.cut = False


@component(SPEECH_SYNTHESIS_AT_FULL_VOLUME)
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
        self.uses(connection.media)
        self._synthesizer: Synthesizer = synthesizer or ToneSynthesizer()
        self._wobble = wobble
        self._tail = tail
        self._folder: Path | None = None
        self._slot = 0
        self._utterance: _Utterance | None = None
        # The default voice is one that available_voices lists: the first one.
        voices = self._synthesizer.voices()
        self._default_character = voices[0] if voices else DEFAULT_CHARACTER
        profile = _with_default(type(self).rois_profile(), "character", self._default_character)
        # The engine reads the profile from the component it hosts, so this instance
        # serves its own default voice.
        vars(self)["rois_profile"] = lambda: profile

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

    @command("set_parameter")
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

    @command("start")
    async def start(self) -> None:
        text = self._text()
        if not text:
            raise CommandFailed(CompletedStatus.ERROR, "speech_text is empty")
        parameters = self.parameters
        language = str(parameters.get("language", "en"))
        character = str(parameters.get("character", self._default_character))
        if character == self._default_character:
            # The first voice stands for the first voice of the language.
            character = DEFAULT_CHARACTER
        volume = int(parameters.get("volume", _FULL_VOLUME))

        folder = self._workspace()
        slot, self._slot = self._slot, (self._slot + 1) % _SLOTS
        spoken = folder / f"spoken-{slot}.wav"
        # The file name the daemon keeps, one of a few per component.
        played = folder / f"openrois-{self.ref.replace('/', '-')}-{slot}.wav"
        try:
            await self._synthesizer.synthesize(text, language, character, spoken)
        except ValueError as exc:
            raise CommandFailed(CompletedStatus.ERROR, str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            # For example a piper command that is not installed any more.
            raise CommandFailed(CompletedStatus.ERROR, f"The speech engine failed: {exc}") from exc
        gain = min(volume, _FULL_VOLUME) / _FULL_VOLUME
        seconds = await asyncio.to_thread(scale_wav, spoken, played, gain)

        try:
            await self._speak(played, seconds, text)
        except ConnectionError as exc:
            raise CommandFailed(CompletedStatus.ERROR, f"No Reachy Mini daemon: {exc}") from exc
        except DaemonMediaError as exc:
            raise CommandFailed(CompletedStatus.ERROR, str(exc)) from exc

    async def _speak(self, played: Path, seconds: float, text: str) -> None:
        mini = await self._connection.client()
        media = mini.media
        if getattr(media, "audio", None) is None:
            raise CommandFailed(
                CompletedStatus.ERROR, "The robot's speaker does not work: the SDK has no audio"
            )
        # For a robot on the network, the daemon plays the sound, and says if it cannot.
        daemon = DaemonMedia(_daemon_url(mini)) if _over_webrtc(media) else None
        utterance = self._utterance = _Utterance()
        logger.info("Speaking %.1f s: %s", seconds, text)
        if self._wobble:
            await asyncio.to_thread(mini.enable_wobbling)
        # The upload and the play request cannot be cancelled once they run, so the
        # playback runs on its own and a cut-off start waits for it.
        playback = asyncio.ensure_future(asyncio.to_thread(_play, media, daemon, played, utterance))
        try:
            await asyncio.shield(playback)
            # A stop, or a new start, cancels the wait here, after the stop handler cut
            # the voice off, and the start ends with ABORT.
            await asyncio.sleep(seconds + self._tail)
        except asyncio.CancelledError:
            utterance.cancelled.set()
            if not utterance.cut:
                # The stop came before the sound played, so it could not cut it off.
                # A sound already on its way plays, and is stopped here once it does.
                # A new start right after the stop cancels this wait again: the late
                # stop still runs to its end, before the new start plays its sound.
                late = asyncio.ensure_future(_stop_late(media, playback, utterance))
                while not late.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(late)
            raise
        finally:
            if self._utterance is utterance:
                self._utterance = None
            if self._wobble:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(mini.disable_wobbling)

    @command("stop")
    async def stop(self) -> None:
        utterance = self._utterance
        if utterance is not None:
            utterance.cancelled.set()
        # Without a daemon nothing plays, and the engine aborts the start all the same.
        mini = self._connection.current
        if mini is None:
            return
        # Read before the stop request: a sound that plays by then is cut off by it.
        playing = utterance is not None and utterance.playing.is_set()
        try:
            await asyncio.to_thread(mini.media.stop_playing)
        except Exception as exc:
            logger.warning("Could not cut the voice off: %s", exc)
            return
        if utterance is not None and playing:
            utterance.cut = True

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


def _play(media: Any, daemon: DaemonMedia | None, played: Path, utterance: _Utterance) -> None:
    """Play a sound on the robot's speaker, in a worker thread, unless a stop came first."""
    if daemon is None:
        if utterance.cancelled.is_set():
            return
        media.play_sound(str(played))
    else:
        daemon.check_speaker()
        remote = daemon.upload(played)
        if utterance.cancelled.is_set():
            return
        daemon.play(remote)
    utterance.playing.set()


async def _stop_late(media: Any, playback: asyncio.Future[None], utterance: _Utterance) -> None:
    """Wait for a sound still on its way to the robot, and stop it once it plays."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(playback), _LATE_PLAY)
    if utterance.playing.is_set():
        with contextlib.suppress(Exception):
            await asyncio.to_thread(media.stop_playing)


def _over_webrtc(media: Any) -> bool:
    """Whether the SDK streams the media of a robot on the network."""
    return getattr(getattr(media, "backend", None), "value", None) == "webrtc"


def _daemon_url(mini: Any) -> str:
    """The HTTP API of the daemon the SDK client connected to."""
    return f"http://{mini.client.host}:{mini.client.port}"
