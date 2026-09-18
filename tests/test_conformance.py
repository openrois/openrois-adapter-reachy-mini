"""The platform passes the OpenRoIS conformance suite in simulation."""

from __future__ import annotations

from openrois_components_core.conformance import assert_conformant
from openrois_core import Engine


async def test_simulated_platform_conforms(engine: Engine) -> None:
    await assert_conformant(engine)
