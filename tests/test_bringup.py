"""The one-command bring-up runs the demo end to end through a real gateway."""

from __future__ import annotations

from openrois_adapter_reachy_mini.bringup import run
from tests.conftest import PROFILE


async def test_demo_through_the_gateway() -> None:
    outcome = await run("127.0.0.1", 0, PROFILE, simulate=True, run_demo=True)
    assert outcome["url"].startswith("ws://127.0.0.1:")
    assert sorted(outcome["components"]) == sorted(
        f"reachy_mini_1/{c}" for c in (
            "SystemInformation", "Navigation", "Reaction", "SpeechSynthesis",
            "SpeechRecognition", "PersonDetection", "FaceDetection", "VideoStreaming",
        )
    )
    assert (outcome["say"], outcome["nod"], outcome["look"]) == ("OK", "OK", "OK")
    assert outcome["position"] == ["0.0", "0.0", "0.0"]
