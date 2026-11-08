"""The adapter's packages import next to the OpenRoIS packages they build on."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import openrois.components.core

import openrois.components.reachy_mini
import openrois_adapter_reachy_mini


def test_components_share_the_openrois_namespace() -> None:
    """openrois.components.reachy_mini sits next to openrois.components.core."""
    assert openrois.components.reachy_mini.__name__ == "openrois.components.reachy_mini"
    assert openrois.components.core.Component is not None


def test_adapter_package_imports() -> None:
    assert openrois_adapter_reachy_mini.__name__ == "openrois_adapter_reachy_mini"


def test_components_import_without_the_sdk() -> None:
    """Importing the components and the adapter never imports the Reachy Mini SDK."""
    code = (
        "import sys, openrois.components.reachy_mini, openrois_adapter_reachy_mini\n"
        "print('reachy_mini' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_openrois_packages_have_the_engine_and_the_four_profiles() -> None:
    """The OpenRoIS releases the adapter requires have the engine and the four profiles."""
    from openrois.engine import Engine
    from openrois.interfaces.components import (
        PERSON_DETECTION_PROFILE,
        REACTION_PROFILE,
        SPEECH_SYNTHESIS_PROFILE,
        SYSTEM_INFORMATION_PROFILE,
    )

    codes = [
        p.identifier.code
        for p in (
            SYSTEM_INFORMATION_PROFILE,
            REACTION_PROFILE,
            SPEECH_SYNTHESIS_PROFILE,
            PERSON_DETECTION_PROFILE,
        )
    ]
    assert codes == ["SystemInformation", "Reaction", "SpeechSynthesis", "PersonDetection"]
    assert Engine is not None


def test_the_readme_carries_the_shipped_adapter_files() -> None:
    """The wheel has no adapter.yaml, so the README gives both files to copy."""
    root = Path(__file__).parent.parent
    readme = (root / "README.md").read_text()
    for name in ("adapter.yaml", "adapter.sim.yaml"):
        content = (root / name).read_text().rstrip("\n")
        assert f"```yaml\n{content}\n```" in readme, name
