"""OpenRoIS components for the Pollen Robotics Reachy Mini.

Canonical RoIS components that drive a Reachy Mini, the real robot or its simulator,
through the Reachy Mini SDK. Each one is a ``Component`` of ``openrois-components-core``
that declares the profile constant of its type, so an ``Engine`` serves it as that type:

- SystemInformation (planned): the status of the robot and its body yaw.
- Reaction (planned): expressions of the head, the antennas and the body.
- SpeechSynthesis (planned): a text spoken on the speaker, with the head moving to the
  voice.
- PersonDetection (planned): the number of faces the camera sees.

The components import the SDK when they connect, so this package imports without it.
They move to the openrois repository as ``openrois-components-reachy-mini`` once the
adapter has run the full workflow, under the same import path.
"""

__all__: list[str] = []
